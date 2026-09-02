"""Runs an LLM-written flight script in a pydantic-monty sandbox.

The script is plain Python executed by monty. It sees the world through a handful of
predefined names (the dataclasses below plus `update`/`ai`) and steers the rocket by
awaiting `update(move)` once per physics tick; the move is sent to the game and the call
returns once the next state arrives. One `Flight` wraps one script run; `AgentPilot`
drives a websocket connection, starting a new flight (and asking the agent for a new
script) every time the game restarts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from pydantic_monty import (
    AsyncMonty,
    CollectString,
    MontyError,
    MontyRuntimeError,
    MontySyntaxError,
    MontyTypingError,
    ResourceLimits,
)

from thrust_server import models
from thrust_server.naive_policy import Policy

if TYPE_CHECKING:
    from thrust_server.models import State

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
    """`"flying"`, `"landed"` or `"crashed"`. The flight is over once it is not flying."""
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
    """Metres. Leaving the world sideways (x < 0 or x > width) is a crash."""
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


@dataclass
class RunReport:
    """What the agent is told about a previous flight."""

    code: str
    outcome: str
    """One paragraph: landed/crashed/abandoned, when and where."""
    error: str | None = None
    """Traceback from the sandbox, if the script raised."""
    output: str = ''
    """Tail of what the script printed."""


@dataclass
class PilotMemory:
    """Shared across connections so each flight can learn from the previous ones."""

    history: list[RunReport] = field(default_factory=list)

    @property
    def last_run(self) -> RunReport | None:
        return self.history[-1] if self.history else None

    def record(self, report: RunReport) -> None:
        self.history.append(report)
        del self.history[:-HISTORY_SIZE]


class Flight:
    """One script steering one flight.

    The game side calls `push_state` then `next_move`; the script side, inside the
    sandbox, calls `update` which hands a move to the game and waits for the next state.
    """

    def __init__(self, pool: AsyncMonty, code: str, first_state: State, ask_ai: AskAI) -> None:
        self._pool = pool
        self.code = code
        self._ask_ai = ask_ai
        self._states: asyncio.Queue[State | None] = asyncio.Queue()
        self._moves: asyncio.Queue[Move | None] = asyncio.Queue()
        self._output = CollectString()
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
        return self._output.output

    async def _run(self) -> None:
        externals: dict[str, object] = {
            'update': self._update,
            'ai': self._ask_ai,
            **{cls.__name__: cls for cls in SANDBOX_TYPES},
        }
        try:
            async with self._pool.checkout(
                script_name='pilot.py', limits=LIMITS, dataclass_registry=SANDBOX_TYPES
            ) as session:
                await session.feed_run(
                    self.code,
                    inputs=script_inputs(self.first_state),
                    external_lookup=externals,
                    print_callback=self._output,
                )
        except MontyError as exc:
            if not self._closed:
                self.error = display_error(exc)
                logger.error('pilot script failed:\n%s', self.error)  # noqa: TRY400 - the display is the traceback
        finally:
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
            logger.error('pilot script abandoned: %s', self.error)  # noqa: TRY400 - nothing to trace
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


async def preflight(pool: AsyncMonty, code: str, state: State, ask_ai: AskAI) -> str | None:
    """Run the script for a few synthetic ticks; the error it raised, if any."""
    flight = Flight(pool, code, state, ask_ai)
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


WriteCode = Callable[[str, PilotMemory], Awaitable[str]]


class Pilot(Protocol):
    """What the websocket handler needs from a controller."""

    async def decide(self, state: State) -> models.Move: ...

    async def close(self) -> None: ...


class NaivePilot:
    """The hand-written controller behind the `Pilot` interface."""

    def __init__(self) -> None:
        self._policy = Policy()

    async def decide(self, state: State) -> models.Move:
        return self._policy.decide(state)

    async def close(self) -> None:
        return


class AgentPilot:
    """One per connection. Asks the agent for a script at the start of every flight.

    If the agent cannot produce a script (no API key, network down) the naive policy flies
    that flight instead so the game stays playable.
    """

    def __init__(
        self,
        pool: AsyncMonty,
        memory: PilotMemory,
        *,
        instructions: str,
        write_code: WriteCode,
        ask_ai: AskAI,
    ) -> None:
        self._pool = pool
        self._memory = memory
        self._instructions = instructions
        self._write_code = write_code
        self._ask_ai = ask_ai
        self._flight: Flight | None = None
        self._fallback: NaivePilot | None = None
        self._last: State | None = None

    def _is_new_flight(self, state: State) -> bool:
        if state.status != FLYING:
            return False
        last = self._last
        return last is None or last.status != FLYING or state.tick < last.tick

    async def decide(self, state: State) -> models.Move:
        if self._is_new_flight(state):
            await self._finish_flight()
            await self._start_flight(state)
        self._last = state
        if self._fallback is not None:
            move = await self._fallback.decide(state)
            if state.status != FLYING:
                self._fallback = None
            return move
        flight = self._flight
        if flight is None:
            return models.Move()
        if state is not flight.first_state:
            flight.push_state(state)
        move = await flight.next_move()
        if state.status != FLYING:
            await self._finish_flight()
        if move is None:
            return models.Move()
        return models.Move(thrust=move.thrust, left=move.left, right=move.right)

    async def _start_flight(self, state: State) -> None:
        self._fallback = None
        code = await self._working_script(state)
        if code is None:
            self._fallback = NaivePilot()
            return
        logger.info('new pilot script (%d lines):\n%s', code.count('\n') + 1, code)
        self._flight = Flight(self._pool, code, state, self._ask_ai)
        self._flight.start()

    async def _working_script(self, state: State) -> str | None:
        """Ask the agent for a script that survives the pre-flight check, or `None`."""
        for _ in range(MAX_ATTEMPTS):
            try:
                code = await self._write_code(self._instructions, self._memory)
            except Exception:
                logger.exception('could not get a pilot script from the agent')
                return None
            error = await preflight(self._pool, code, state, self._ask_ai)
            if error is None:
                return code
            logger.warning('pilot script failed pre-flight, asking again:\n%s', error)
            self._memory.record(RunReport(code=code, outcome=PREFLIGHT_OUTCOME, error=error))
        logger.error(
            'no working pilot script after %d attempts, using the naive policy', MAX_ATTEMPTS
        )
        return None

    async def _finish_flight(self) -> None:
        flight = self._flight
        if flight is None:
            return
        self._flight = None
        await flight.close()
        report = flight.report()
        self._memory.record(report)
        logger.info('flight over: %s', report.outcome)

    async def close(self) -> None:
        await self._finish_flight()
