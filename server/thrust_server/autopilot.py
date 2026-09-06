"""Runs an LLM-written flight script in a pydantic-monty sandbox.

The script is plain Python executed by monty. It sees the world through a handful of
predefined names (the dataclasses below plus `update`/`ai`) and steers the rocket by
awaiting `update(move)` once per physics tick; the move is sent to the game and the call
returns once the next state arrives. `make_plan` asks the agent for the next script
(behind `GET /plan`) and `fly_script` flies it for one websocket connection.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import logfire
from logfire.propagate import attach_context, get_context
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import AgentRunResult, ModelMessage
from pydantic_monty import (
    AsyncMonty,
    ClassInstance,
    ClassType,
    MontyClassProxy,
    MontyError,
    MontyRuntimeError,
    MontySyntaxError,
    MontyTypingError,
    ResourceLimits,
)

from thrust_server import models
from thrust_server.models import State
from thrust_server.telemetry import Sample, diagnose, render

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

SANDBOX_PRELUDE = f'from dataclasses import dataclass\n\n{inspect.getsource(Move)}'
"""Run in the session before the script. `Move` is the one class the script builds itself,
so it is defined inside the sandbox: a host class handed in as a `ClassType` would let the
script construct it, but attributes it then set would stay on the sandbox's copy and never
reach `update`. The other classes only ever travel host to sandbox, as `ClassInstance`s."""

SANDBOX_CLASSES = {
    cls.__name__: ClassType(cls, init=True, instance_eager_attrs='all')
    for cls in SANDBOX_TYPES
    if cls is not Move
}
"""The remaining classes by name, so the script can use them in annotations and `type(status)`
is the same `Status` the name refers to."""

# --- host side ---------------------------------------------------------------------------

FLYING = 'flying'

LIMITS: ResourceLimits = {
    'max_duration_secs': 60,
    'max_memory': 256 * 1024 * 1024,
    'max_suspensions': 10_000,
}
"""Sandbox limits per flight. `max_duration_secs` is compute only: time spent waiting for
the game inside `update` or for the helper model inside `ai` does not count, so it also
catches a script that stalls. Every `update` and `ai` call is a suspension, and a full
90 s flight at 60 Hz is 5400 ticks, so the default of 1000 would end the script mid-air."""

OUTPUT_TAIL_LINES = 40
HISTORY_SIZE = 10

PREFLIGHT_TICKS = 3
"""Synthetic ticks a new script is run for before it gets the real flight."""
MAX_ATTEMPTS = 3
"""Scripts requested per plan before giving up."""
PREFLIGHT_OUTCOME = (
    'Did not fly: the script failed the pre-flight check, which runs it for a few ticks'
    ' against the starting state before the real flight.'
)
STRAY_CALLS_LIMIT = 3
"""`update` calls tolerated after the flight is over before the script is cancelled."""

AskAI = Callable[[str], Awaitable[str]]
NextState = Callable[[], Awaitable[State]]
"""The next state from the game; raising means the game has gone away."""
SendReply = Callable[[models.Move | models.Abort], Awaitable[None]]

FLIGHT_OVER_MESSAGE = 'the flight is over, stop calling update()'


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
        **SANDBOX_CLASSES,
        'status': ClassInstance(status_from_state(state), eager_attrs='all'),
        'pad': ClassInstance(pad_from(state.pad), eager_attrs='all'),
        'launch_pad': ClassInstance(pad_from(state.launch_pad), eager_attrs='all'),
        'terrain': [(x, y) for x, y in state.terrain],
        'world': ClassInstance(world_from(state.world), eager_attrs='all'),
        'physics': ClassInstance(physics_from(state.physics), eager_attrs='all'),
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
    diagnosis: str = ''
    """Why it ended that way: peak altitude, closest approach, what touched what."""
    telemetry: str = ''
    """Sampled table of position, velocity, wind and inputs over the flight."""

    def feedback(self) -> str:
        """The `start_flight` tool result for this flight, so the agent can improve."""
        parts = [self.outcome]
        if self.diagnosis:
            parts.append(self.diagnosis)
        if self.telemetry:
            parts.append(
                'Telemetry, every 0.5 s and every 0.1 s over the last 2 s:'
                f'\n```\n{self.telemetry}\n```'
            )
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


class PrintCollector:
    """Keeps what the script prints and logs each line inside the `pilot script` span.

    Monty calls the print callback outside the span's task context, hence the explicit
    context propagation.
    """

    def __init__(self) -> None:
        self.text = ''
        self._pending = ''
        self.context: Mapping[str, str] = {}

    def on_print(self, stream: str, chunk: str) -> None:
        self._pending += chunk
        *lines, self._pending = self._pending.split('\n')
        with attach_context(self.context):
            for line in lines:
                self.text += line + '\n'
                logfire.info('script: {line}', line=line, stream=stream)

    def tail(self) -> str:
        text = self.text + self._pending
        parts = text.splitlines()
        if len(parts) <= OUTPUT_TAIL_LINES:
            return text
        omitted = len(parts) - OUTPUT_TAIL_LINES
        return '\n'.join([f'... ({omitted} earlier lines omitted)', *parts[-OUTPUT_TAIL_LINES:]])


@dataclass
class FlightResult:
    """How a script run went."""

    last_state: State
    ticks: int
    """`update` calls that were answered with a state."""
    error: str | None
    """Traceback if the script raised (a `FlightOver` unwinding does not count)."""
    output: str
    exited_early: bool
    """The script returned while the rocket was still flying and the game still there."""
    samples: list[Sample]
    """Every state the game sent with the move it got back; the last one is unanswered."""

    def report(self, code: str) -> RunReport:
        return RunReport(
            code=code,
            outcome=self.describe(),
            error=self.error,
            output=self.output,
            diagnosis=diagnose(self.samples),
            telemetry=render(self.samples),
        )

    def describe(self) -> str:
        state = self.last_state
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
                f'Ran out of time: flights are cut off at {limit:.0f} s,'
                f' still at {where}; {target}.'
            )
        else:
            summary = f'The flight was cut short {when} while still flying at {where}; {target}.'
        if self.error is not None:
            summary += (
                f' The script stopped controlling the rocket after {self.ticks} ticks'
                ' because it raised an error (below).'
            )
        elif self.exited_early:
            summary += f' The script exited after {self.ticks} ticks.'
        return summary


def display_error(exc: MontyError) -> str:
    if isinstance(exc, (MontyRuntimeError, MontySyntaxError, MontyTypingError)):
        return exc.display()
    return str(exc)


class GameLink:
    """What the sandbox's `update` talks to: send a move, wait for the next state.

    `over` is set once the state is terminal or the game has gone (`next_state` raised);
    after that `update` raises so the script unwinds, and a script that keeps calling it
    anyway is cancelled.
    """

    def __init__(self, first_state: State, next_state: NextState, send_reply: SendReply) -> None:
        self.state = first_state
        self.ticks = 0
        self.over = False
        self.game_gone = False
        self.stray_calls = 0
        self.samples: list[Sample] = []
        self.run: asyncio.Future[object] | None = None
        self._next_state = next_state
        self._send_reply = send_reply

    async def update(self, move: object) -> ClassInstance:
        if self.over:
            self.stray_calls += 1
            if self.stray_calls > STRAY_CALLS_LIMIT and self.run is not None:
                self.run.cancel()
            raise FlightOver(FLIGHT_OVER_MESSAGE)
        reply = move_from(move)
        self.samples.append(Sample(self.state, reply))
        await self._send_reply(reply)
        try:
            self.state = await self._next_state()
        except Exception:  # noqa: BLE001 - whatever failed, the game is no longer there
            self.over = self.game_gone = True
            raise FlightOver(FLIGHT_OVER_MESSAGE) from None
        self.ticks += 1
        if self.state.status != FLYING:
            self.over = True
        return ClassInstance(status_from_state(self.state), eager_attrs='all')


def move_from(move: object) -> models.Move:
    """The move a script passed to `update`: an instance of the sandbox's own `Move`."""
    if not (isinstance(move, MontyClassProxy) and move.name == Move.__name__):
        name = move.name if isinstance(move, MontyClassProxy) else type(move).__name__
        msg = f'update() expects a Move, got {name}'
        raise TypeError(msg)
    try:
        return models.Move.model_validate(move.attributes)
    except ValidationError as exc:
        msg = f'update() got a bad Move: {exc}'
        raise TypeError(msg) from None


async def fly_script(  # noqa: PLR0913 - the flight's whole interface
    pool: AsyncMonty,
    plan: AgentRunResult[PilotScript],
    ask_ai: AskAI,
    *,
    first_state: State,
    next_state: NextState,
    send_reply: SendReply,
) -> FlightResult:
    """Fly one flight with the planned script.

    The script's `update(move)` sends the move and waits for the next state, so the
    script runs the flight from start to finish. The run ends when the state turns
    terminal, the script finishes or raises, or `next_state` raises (the game went away).
    A script that dies with the game still listening gets an `Abort` sent on its behalf.
    """
    code = plan.output.code
    link = GameLink(first_state, next_state, send_reply)
    prints = PrintCollector()
    externals: dict[str, object] = {'update': link.update, 'ai': ask_ai}
    error: str | None = None
    with logfire.span(
        'pilot script', code=code, lines=code.count('\n') + 1, strategy=plan.output.strategy
    ) as span:
        prints.context = get_context()
        try:
            async with pool.checkout(script_name='pilot.py', limits=LIMITS) as session:
                await session.feed_run(SANDBOX_PRELUDE)
                link.run = asyncio.ensure_future(
                    session.feed_run(
                        code,
                        inputs=script_inputs(first_state),
                        external_lookup=externals,
                        print_callback=prints.on_print,
                    )
                )
                span.set_attribute('returned', await link.run)
        except asyncio.CancelledError:
            if not (link.run is not None and link.run.cancelled()):
                raise  # our own cancellation, not the stray-call cut-off
        except MontyError as exc:
            if not link.game_gone:
                error = display_error(exc)
                span.set_attribute('error', error)
                span.record_exception(exc)
        span.set_attribute('ticks', link.ticks)
        span.set_attribute('status', link.state.status)

    # Every state the game sent gets exactly one reply. The script answers all but the
    # last one itself; the last is the terminal state it exited on, or the state it died on.
    exited_early = error is None and not link.over
    if not link.game_gone:
        if link.state.status != FLYING:
            await send_reply(models.Move())
        elif error is not None:
            await send_reply(models.Abort(reason=error.strip().splitlines()[-1]))
        elif exited_early:
            await send_reply(models.Abort(reason='the script exited while still flying'))
    return FlightResult(
        last_state=link.state,
        ticks=link.ticks,
        error=error,
        output=prints.tail(),
        exited_early=exited_early,
        samples=[*link.samples, Sample(link.state, None)],
    )


async def preflight(
    pool: AsyncMonty, plan: AgentRunResult[PilotScript], state: State, ask_ai: AskAI
) -> str | None:
    """Run the script for a few synthetic ticks; the error it raised, if any."""
    states = (
        state.model_copy(update={'tick': state.tick + i}) for i in range(1, PREFLIGHT_TICKS + 1)
    )

    async def next_state() -> State:
        try:
            return next(states)
        except StopIteration:
            raise FlightOver(FLIGHT_OVER_MESSAGE) from None

    async def send_reply(_: models.Move | models.Abort) -> None:
        return

    result = await fly_script(
        pool, plan, ask_ai, first_state=state, next_state=next_state, send_reply=send_reply
    )
    if result.error is not None:
        return result.error
    if result.ticks < PREFLIGHT_TICKS:
        return f'the script finished after {result.ticks} update() calls, long before landing'
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
