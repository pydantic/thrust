"""Runs an LLM-written flight script in a pydantic-monty sandbox.

The script is plain Python executed by monty. It sees the world through a handful of
predefined names (the dataclasses below plus `update`/`ai`) and steers the rocket by
awaiting `update(move)` once per physics tick; the move is sent to the game and the call
returns once the next state arrives. One `Flight` wraps one script run; `make_plan` asks
the agent for the next script (behind `GET /plan`) and `ScriptPilot` flies it for one
websocket connection.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import logfire
from logfire.propagate import attach_context, get_context
from pydantic import BaseModel, Field
from pydantic_ai import AgentRunResult, ModelMessage
from pydantic_monty import (
    AsyncMonty,
    MontyError,
    MontyRuntimeError,
    MontySyntaxError,
    MontyTypingError,
    ResourceLimits,
)

from thrust_server import models
from thrust_server.models import State
from thrust_server.naive_policy import Policy

logger = logging.getLogger(__name__)

# --- the API the sandboxed script sees --------------------------------------------------
# Field docstrings double as documentation: agent.py pastes the source of these classes into
# the prompt, so keep them self-explanatory.


@dataclass
class Move:
    """What the engine does during the next tick. Each input is on or off; there is no throttle."""

    thrust: bool = False
    """Fire the engine along the nose."""
    left: bool = False
    """Rotate anticlockwise (angle decreases)."""
    right: bool = False
    """Rotate clockwise (angle increases)."""


@dataclass
class Status:
    """The rocket after a move was applied for one tick. Returned by `update`."""

    status: str
    """`"flying"`, `"landed"`, `"crashed"`, `"timeout"` or `"aborted"`. Over once not flying."""
    tick: int
    time: float
    """Seconds since the start of the flight (`tick * physics.dt`). This is the score."""
    x: float
    """Centre of the rocket body, metres from the left edge of the world."""
    y: float
    """Centre of the rocket body, metres above the bottom of the world (y is up)."""
    vx: float
    vy: float
    angle: float
    """Radians, 0 = nose straight up, positive = clockwise (nose tilted to the right)."""
    angular_velocity: float
    """Radians per second, positive = clockwise."""
    wind_x: float
    """Wind velocity at the rocket, m/s. Positive blows to the right."""
    wind_y: float
    """Wind velocity at the rocket, m/s. Positive blows upwards."""


@dataclass
class Pad:
    """A flat stretch of terrain. The landing pad is the target; the launch pad is the start."""

    x1: float
    x2: float
    y: float


@dataclass
class World:
    width: float
    """Metres. The walls are hard: a corner past x = 0, x = width or y = height is a crash."""
    height: float
    gravity: float
    """m/s^2, always pulling down."""


@dataclass
class Physics:
    """Simulation constants. See the physics description for how each one is used."""

    dt: float
    """Seconds per tick (one `update` call)."""
    thrust_accel: float
    """Engine acceleration along the nose, m/s^2."""
    rotation_accel: float
    """Angular acceleration while a rotation input is held, rad/s^2."""
    angular_damping: float
    """Angular velocity decays at this rate, 1/s."""
    max_angular_velocity: float
    """Angular velocity is clamped to +/- this, rad/s."""
    wind_drag: float
    """Acceleration toward the wind velocity is wind_drag * (wind - velocity), 1/s."""
    rocket_height: float
    """Body length, metres. The nose tip is 0.6 * height above the centre, the base 0.4 below."""
    rocket_half_base: float
    """Half the width of the base, metres. The two base corners are at +/- this."""
    landing_max_angle: float
    """|angle| must be below this (radians) on contact with the pad."""
    landing_max_vy: float
    """|vy| must be below this (m/s) on contact with the pad."""
    landing_max_vx: float
    """|vx| must be below this (m/s) on contact with the pad."""
    max_flight_time: float
    """A flight still going after this many seconds ends with status "timeout": a failure."""


SANDBOX_TYPES: list[type] = [Move, Status, Pad, World, Physics]

# --- host side ---------------------------------------------------------------------------

FLYING = 'flying'

LIMITS: ResourceLimits = {'max_duration_secs': 120, 'max_memory': 256 * 1024 * 1024}
"""Compute limits per flight. Time spent waiting for the game inside `update` does not count."""

MOVE_TIMEOUT_S = 15.0
"""How long the script may take between two `update` calls before it is abandoned."""

OUTPUT_TAIL_LINES = 40
HISTORY_SIZE = 10

PREFLIGHT_TICKS = 3
"""Synthetic ticks a new script is run for before it gets the real flight."""
MAX_ATTEMPTS = 3
"""Scripts requested per flight before giving up and using the naive policy."""
PREFLIGHT_OUTCOME = (
    'Did not fly: the script failed the pre-flight check, which runs it for a few ticks'
    ' against the starting state before the real flight.'
)

AskAI = Callable[[str], Awaitable[str]]


class FlightOver(Exception):  # noqa: N818 - the message says what happened
    """Raised inside `update` once the flight is over so the script unwinds."""


def status_from_state(state: State) -> Status:
    rocket = state.rocket
    return Status(
        status=state.status,
        tick=state.tick,
        time=state.tick * state.physics.dt,
        x=rocket.x,
        y=rocket.y,
        vx=rocket.vx,
        vy=rocket.vy,
        angle=rocket.angle,
        angular_velocity=rocket.angularVelocity,
        wind_x=state.wind.x,
        wind_y=state.wind.y,
    )


def pad_from(pad: models.Pad) -> Pad:
    return Pad(x1=pad.x1, x2=pad.x2, y=pad.y)


def physics_from(physics: models.Physics) -> Physics:
    return Physics(**physics.model_dump())


def world_from(world: models.WorldInfo) -> World:
    return World(width=world.width, height=world.height, gravity=world.gravity)


def script_inputs(state: State) -> dict[str, object]:
    """The globals a script starts with, built from the first state of the flight."""
    return {
        'status': status_from_state(state),
        'pad': pad_from(state.pad),
        'launch_pad': pad_from(state.launch_pad),
        'terrain': [(x, y) for x, y in state.terrain],
        'world': world_from(state.world),
        'physics': physics_from(state.physics),
    }


class PilotScript(BaseModel, use_attribute_docstrings=True):
    """What the agent submits for one flight."""

    code: str
    """The complete Python source of the script, run unchanged."""
    strategy: str
    """Short summary of the strategy used by the auto-pilot."""


class RunReport(BaseModel):
    """What the agent is told about a previous flight."""

    code: str
    outcome: str
    """One paragraph: landed/crashed/abandoned, when and where."""
    error: str | None = None
    """Traceback from the sandbox, if the script raised."""
    output: str = ''
    """Tail of what the script printed."""

    def feedback(self) -> str:
        """The `start_flight` tool result for this flight, so the agent can improve."""
        parts = [self.outcome]
        if self.error:
            parts.append(f'It raised this error:\n```\n{self.error}\n```')
        if self.output:
            parts.append(f'Last lines it printed:\n```\n{self.output}\n```')
        parts.append(
            'Fix what went wrong and improve on it, or rewrite if the approach was flawed,'
            ' then submit the complete new script.'
        )
        return '\n\n'.join(parts)


class PilotMemory(BaseModel):
    """Shared across connections so each flight can learn from the previous ones.

    `messages` is the running conversation with the script-writing agent, in which every
    script is a `start_flight` tool call whose result is the flight report; it only ever
    holds completed script/report pairs. With a `path` the memory is written to disk as
    JSON after every report and loaded again on start, so a server restart carries on
    iterating on the same script. A flight cut short by a restart is simply not in it.
    """

    messages: list[ModelMessage] = []
    history: list[RunReport] = []
    last_state: State | None = None
    """The first state of the most recent flight, which pre-flight checks fly against."""
    path: Path | None = Field(default=None, exclude=True)

    @classmethod
    def load(cls, path: Path) -> PilotMemory:
        if not path.exists():
            return cls(path=path)
        memory = cls.model_validate_json(path.read_text())
        memory.path = path
        logger.info('loaded pilot memory from %s (%d flights)', path, len(memory.history))
        return memory

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.model_dump_json(indent=2))

    @property
    def last_run(self) -> RunReport | None:
        return self.history[-1] if self.history else None

    def record(self, report: RunReport, result: AgentRunResult[PilotScript]) -> None:
        """Store the report as the result of the `start_flight` call that wrote the script."""
        self.history.append(report)
        del self.history[:-HISTORY_SIZE]
        self.messages = result.all_messages(output_tool_return_content=report.feedback())
        self.save()


class Flight:
    """One script steering one flight.

    The game side calls `push_state` then `next_move`; the script side, inside the
    sandbox, calls `update` which hands a move to the game and waits for the next state.
    """

    def __init__(
        self,
        pool: AsyncMonty,
        result: AgentRunResult[PilotScript],
        first_state: State,
        ask_ai: AskAI,
    ) -> None:
        self._pool = pool
        self.result = result
        """The agent run that wrote the script; the flight report becomes its tool result."""
        self.code = result.output.code
        self._ask_ai = ask_ai
        self._states: asyncio.Queue[State | None] = asyncio.Queue()
        self._moves: asyncio.Queue[Move | None] = asyncio.Queue()
        self._output: list[str] = []
        self._pending_line = ''
        self._log_context: Mapping[str, str] | None = None
        """The `pilot script` span's context: monty calls the print callback outside it."""
        self._closed = False
        self._task: asyncio.Future[None] | None = None
        self.first_state = first_state
        self.last_state = first_state
        self.error: str | None = None
        self.ticks_controlled = 0

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    @property
    def done(self) -> bool:
        return self._task is not None and self._task.done()

    @property
    def output(self) -> str:
        return ''.join(self._output) + self._pending_line

    def _on_print(self, stream: str, text: str) -> None:
        """Collect the script's print() output and log each complete line."""
        self._pending_line += text
        *lines, self._pending_line = self._pending_line.split('\n')
        with attach_context(self._log_context or {}):
            for line in lines:
                self._output.append(line + '\n')
                logfire.info('script: {line}', line=line, stream=stream, tick=self.last_state.tick)

    async def _run(self) -> None:
        externals: dict[str, object] = {
            'update': self._update,
            'ai': self._ask_ai,
            **{cls.__name__: cls for cls in SANDBOX_TYPES},
        }
        # One span for the whole script run; its prints and outcome nest inside it.
        with logfire.span(
            'pilot script',
            code=self.code,
            lines=self.code.count('\n') + 1,
            strategy=self.result.output.strategy,
        ) as span:
            self._log_context = get_context()
            try:
                async with self._pool.checkout(
                    script_name='pilot.py', limits=LIMITS, dataclass_registry=SANDBOX_TYPES
                ) as session:
                    returned = await session.feed_run(
                        self.code,
                        inputs=script_inputs(self.first_state),
                        external_lookup=externals,
                        print_callback=self._on_print,
                    )
                span.set_attribute('returned', returned)
            except MontyError as exc:
                if not self._closed:
                    self.error = display_error(exc)
                    span.set_attribute('error', self.error)
                    span.record_exception(exc)
            finally:
                span.set_attribute('ticks_controlled', self.ticks_controlled)
                span.set_attribute('status', self.last_state.status)
                self._moves.put_nowait(None)

    async def _update(self, move: object) -> Status:
        if self._closed:
            raise FlightOver(FLIGHT_OVER_MESSAGE)
        if not isinstance(move, Move):
            msg = f'update() expects a Move, got {type(move).__name__}'
            raise TypeError(msg)
        await self._moves.put(move)
        state = await self._states.get()
        if state is None:
            raise FlightOver(FLIGHT_OVER_MESSAGE)
        self.ticks_controlled += 1
        return status_from_state(state)

    def push_state(self, state: State) -> None:
        self.last_state = state
        self._states.put_nowait(state)

    async def next_move(self) -> Move | None:
        """The script's next move, or `None` once the script has finished or given up."""
        if self.done:
            return None
        try:
            move = await asyncio.wait_for(self._moves.get(), MOVE_TIMEOUT_S)
        except TimeoutError:
            self.error = f'the script took more than {MOVE_TIMEOUT_S:.0f} s to call update()'
            logfire.error('pilot script abandoned', error=self.error)
            await self.close()
            return None
        if move is None:
            # Keep `done` true for later callers: the sentinel is only queued once.
            self._moves.put_nowait(None)
        return move

    async def close(self) -> None:
        """Stop the script: unblock `update` with an error, then cancel if it lingers."""
        if self._closed:
            return
        self._closed = True
        self._states.put_nowait(None)
        if self._task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), 2.0)
        except TimeoutError:
            self._task.cancel()
        except Exception:  # noqa: BLE001, S110 - _run already recorded and logged it
            pass

    def report(self) -> RunReport:
        return RunReport(
            code=self.code,
            outcome=describe_outcome(self),
            error=self.error,
            output=tail(self.output, OUTPUT_TAIL_LINES),
        )


FLIGHT_OVER_MESSAGE = 'the flight is over, stop calling update()'


def display_error(exc: MontyError) -> str:
    if isinstance(exc, (MontyRuntimeError, MontySyntaxError, MontyTypingError)):
        return exc.display()
    return str(exc)


def describe_outcome(flight: Flight) -> str:
    state = flight.last_state
    rocket = state.rocket
    where = (
        f'x={rocket.x:.1f} y={rocket.y:.1f} vx={rocket.vx:.2f} vy={rocket.vy:.2f} '
        f'angle={rocket.angle:.2f} rad'
    )
    when = f'after {state.tick * state.physics.dt:.1f} s (tick {state.tick})'
    pad = state.pad
    target = f'the landing pad spans x {pad.x1:.1f}..{pad.x2:.1f} at y {pad.y:.1f}'
    if state.status == 'landed':
        summary = f'Landed {when}.'
    elif state.status == 'crashed':
        summary = f'Crashed {when} at {where}; {target}.'
    elif state.status == 'timeout':
        limit = state.physics.max_flight_time
        summary = (
            f'Ran out of time: flights are cut off at {limit:.0f} s, still at {where}; {target}.'
        )
    else:
        summary = f'The flight was cut short {when} while still flying at {where}; {target}.'
    if flight.error is not None:
        summary += (
            f' The script stopped controlling the rocket after {flight.ticks_controlled} ticks'
            ' because it raised an error (below).'
        )
    elif flight.done and state.status == FLYING:
        summary += f' The script exited after {flight.ticks_controlled} ticks.'
    return summary


def tail(text: str, lines: int) -> str:
    parts = text.splitlines()
    if len(parts) <= lines:
        return text
    return '\n'.join([f'... ({len(parts) - lines} earlier lines omitted)', *parts[-lines:]])


async def preflight(
    pool: AsyncMonty, result: AgentRunResult[PilotScript], state: State, ask_ai: AskAI
) -> str | None:
    """Run the script for a few synthetic ticks; the error it raised, if any."""
    flight = Flight(pool, result, state, ask_ai)
    flight.start()
    finished_early = False
    for i in range(PREFLIGHT_TICKS):
        if await flight.next_move() is None:
            finished_early = True
            break
        flight.push_state(state.model_copy(update={'tick': state.tick + i + 1}))
    await flight.close()
    if flight.error is not None:
        return flight.error
    if finished_early:
        return (
            f'the script finished after {flight.ticks_controlled} update() calls,'
            ' long before landing'
        )
    return None


WriteCode = Callable[[PilotMemory], Awaitable[AgentRunResult[PilotScript]]]


async def make_plan(
    pool: AsyncMonty, memory: PilotMemory, write_code: WriteCode, ask_ai: AskAI
) -> AgentRunResult[PilotScript] | None:
    """Ask the agent for a script that survives the pre-flight check; `None` if none did.

    The check flies the script against the last state the game sent, so on the very
    first plan of a fresh memory there is nothing to check against and the script goes
    straight through. Errors from the agent itself propagate.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = await write_code(memory)
        if memory.last_state is None:
            logger.info('no flight seen yet, skipping the pre-flight check')
            return result
        error = await preflight(pool, result, memory.last_state, ask_ai)
        if error is None:
            return result
        logger.warning('script failed pre-flight (attempt %d), asking again:\n%s', attempt, error)
        report = RunReport(code=result.output.code, outcome=PREFLIGHT_OUTCOME, error=error)
        memory.record(report, result)
    logger.error('no working pilot script after %d attempts', MAX_ATTEMPTS)
    return None


class Pilot(Protocol):
    """What the websocket handler needs from a controller."""

    async def decide(self, state: State) -> models.Move | models.Abort: ...

    async def close(self) -> None: ...


class NaivePilot:
    """The hand-written controller behind the `Pilot` interface."""

    def __init__(self) -> None:
        self._policy = Policy()

    async def decide(self, state: State) -> models.Move:
        return self._policy.decide(state)

    async def close(self) -> None:
        return


class ScriptPilot:
    """Flies one connection's single flight with the script from `GET /plan`.

    The first state starts the script; a terminal state (or the connection closing)
    ends it and records the report against the plan's `start_flight` call. If the script
    dies the flight is aborted on the spot rather than left to drift. States that arrive
    after that get idle moves.
    """

    def __init__(
        self,
        pool: AsyncMonty,
        memory: PilotMemory,
        plan: AgentRunResult[PilotScript],
        ask_ai: AskAI,
    ) -> None:
        self._pool = pool
        self._memory = memory
        self._plan = plan
        self._ask_ai = ask_ai
        self._flight: Flight | None = None
        self._finished = False

    async def decide(self, state: State) -> models.Move | models.Abort:
        if self._finished:
            return models.Move()
        flight = self._flight
        if flight is None:
            flight = self._flight = Flight(self._pool, self._plan, state, self._ask_ai)
            flight.start()
        else:
            flight.push_state(state)
        move = await flight.next_move()
        if state.status != FLYING:
            await self._finish()
        if move is None:
            if flight.error is not None and not self._finished:
                # The script raised: end the flight now, with the exception on show.
                await self._finish()
                return models.Abort(reason=flight.error.strip().splitlines()[-1])
            return models.Move()
        return models.Move(thrust=move.thrust, left=move.left, right=move.right)

    async def _finish(self) -> None:
        flight = self._flight
        if flight is None or self._finished:
            return
        self._finished = True
        await flight.close()
        report = flight.report()
        self._memory.record(report, flight.result)
        logfire.info(
            'flight over: {outcome}',
            outcome=report.outcome,
            status=flight.last_state.status,
            time=flight.last_state.tick * flight.last_state.physics.dt,
            ticks_controlled=flight.ticks_controlled,
            error=report.error,
            output=report.output,
        )

    async def close(self) -> None:
        await self._finish()
