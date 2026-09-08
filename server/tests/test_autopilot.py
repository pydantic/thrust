"""The sandboxed pilot: fake code writers drive the real monty runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import (
    Agent,
    AgentRunResult,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolOutput,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_monty import AsyncMonty
from starlette.websockets import WebSocketDisconnect

from thrust_server import agent
from thrust_server.autopilot import (
    FlightResult,
    PilotMemory,
    PilotScript,
    RunReport,
    fly_script,
    make_plan,
)
from thrust_server.main import app
from thrust_server.models import Abort, Move, Physics, State

pytestmark = pytest.mark.anyio

PHYSICS = Physics(
    dt=1 / 60,
    thrustAccel=9,
    rotationAccel=6,
    angularDamping=2.5,
    maxAngularVelocity=3,
    windDrag=0.15,
    rocketHeight=4,
    rocketHalfBase=1.25,
    landingMaxAngle=0.28,
    landingMaxVy=5,
    landingMaxVx=3.2,
    maxFlightTime=90,
)


def make_state(tick: int, status: str = 'flying', y: float = 21.6) -> State:
    return State.model_validate(
        {
            'tick': tick,
            'status': status,
            'rocket': {'x': 25, 'y': y, 'vx': 0, 'vy': 0, 'angle': 0, 'angularVelocity': 0},
            'wind': {'x': 2, 'y': 0},
            'pad': {'x1': 120, 'x2': 132, 'y': 10},
            'launchPad': {'x1': 20, 'x2': 30, 'y': 20},
            'terrain': [(0, 15), (20, 20), (30, 20), (60, 50), (120, 10), (132, 10), (160, 25)],
            'world': {'width': 160, 'height': 100, 'gravity': 4},
            'physics': PHYSICS.model_dump(by_alias=True),
        }
    )


THRUST_EVERY_OTHER_TICK = """
import math

def ground(x):
    for (x1, y1), (x2, y2) in zip(terrain, terrain[1:]):
        if x1 <= x <= x2:
            return y1 + (y2 - y1) * (x - x1) / (x2 - x1)
    return terrain[-1][1]

plan = await ai(f"pad at {pad.x1}-{pad.x2}, ground under rocket {ground(status.x):.1f}")
print("plan:", plan)
s = status
while s.status == "flying":
    s = await update(Move(thrust=s.tick % 2 == 0, right=s.tick == 0))
    if s.tick % 60 == 0:
        print(f"t={s.time:.2f} y={s.y:.1f}")
print("finished", s.status, f"{s.time:.2f} s")
"""


async def fake_ai(query: str) -> str:
    return f'climb, then cross ({query[:20]})'


STRATEGY = 'Go straight up, then drift over and land.'


def submit(code: str) -> ModelResponse:
    """A model reply that submits `code` through the output tool."""
    args = {'code': code, 'strategy': STRATEGY}
    return ModelResponse(parts=[ToolCallPart(tool_name='start_flight', args=args)])


def fake_pilot_agent(model: FunctionModel) -> Agent[None, PilotScript]:
    return Agent(
        model,
        output_type=ToolOutput(PilotScript, name='start_flight'),
        capabilities=[ProcessHistory(agent.trim_history)],
    )


def scripted_pilot(monkeypatch: pytest.MonkeyPatch, *scripts: str) -> list[list[ModelMessage]]:
    """Make the pilot agent submit `scripts` in turn (the last one repeats); returns what it saw."""
    seen: list[list[ModelMessage]] = []
    remaining = list(scripts)

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return submit(remaining.pop(0) if len(remaining) > 1 else remaining[0])

    monkeypatch.setattr(agent, 'pilot_agent', fake_pilot_agent(FunctionModel(model)))
    return seen


async def fly(
    pool: AsyncMonty, plan: AgentRunResult[PilotScript], states: list[State]
) -> tuple[FlightResult, list[Move | Abort]]:
    """Fly the plan through `states`; once they run out the game "hangs up"."""
    remaining = iter(states[1:])
    replies: list[Move | Abort] = []

    async def next_state() -> State:
        try:
            return next(remaining)
        except StopIteration:
            msg = 'client hung up'
            raise ConnectionError(msg) from None

    async def send_reply(reply: Move | Abort) -> None:
        replies.append(reply)

    result = await fly_script(
        pool, plan, fake_ai, first_state=states[0], next_state=next_state, send_reply=send_reply
    )
    return result, replies


@pytest.fixture
async def pool() -> AsyncIterator[AsyncMonty]:
    async with AsyncMonty() as pool:
        yield pool


async def plan(pool: AsyncMonty, memory: PilotMemory) -> AgentRunResult[PilotScript]:
    result = await make_plan(pool, memory, agent.write_pilot, fake_ai)
    assert result is not None
    return result


async def test_script_controls_each_tick(pool: AsyncMonty, monkeypatch: pytest.MonkeyPatch) -> None:
    scripted_pilot(monkeypatch, THRUST_EVERY_OTHER_TICK)
    memory = PilotMemory()
    states = [make_state(tick) for tick in range(4)] + [make_state(4, 'landed')]
    result, replies = await fly(pool, await plan(pool, memory), states)
    moves = [r for r in replies if isinstance(r, Move)]
    assert len(moves) == len(replies) == 5  # one move per state; the landing gets an idle one
    assert [m.thrust for m in moves] == [True, False, True, False, False]
    assert moves[0].right is True
    assert moves[1].right is False

    assert result.error is None
    assert result.ticks == 4
    assert not result.exited_early
    report = result.report('code')
    assert report.outcome == 'Landed after 0.1 s (tick 4).'
    assert 'plan: climb, then cross' in report.output
    assert 'finished landed 0.07 s' in report.output


async def test_script_error_aborts_the_flight(
    pool: AsyncMonty, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_pilot(
        monkeypatch,
        's = status\n'
        "while s.status == 'flying':\n"
        '    s = await update(Move(thrust=True))\n'
        '    if s.tick >= 5:\n'
        '        boom = 1 / 0\n',
    )
    memory = PilotMemory()
    states = [make_state(tick) for tick in range(8)]
    result, replies = await fly(pool, await plan(pool, memory), states)
    assert [type(r).__name__ for r in replies] == ['Move'] * 5 + ['Abort']
    abort = replies[-1]
    assert isinstance(abort, Abort)
    assert abort.reason == 'ZeroDivisionError: division by zero'
    assert result.error is not None
    assert 'ZeroDivisionError' in result.error
    outcome = result.report('code').outcome
    assert outcome.startswith('The flight was cut short after 0.1 s (tick 5)')
    assert 'stopped controlling the rocket after 5 ticks' in outcome


async def test_script_that_exits_early_aborts_the_flight(
    pool: AsyncMonty, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_pilot(monkeypatch, 's = await update(Move(thrust=True))\nprint("bye")\n')
    memory = PilotMemory()
    result, replies = await fly(pool, await plan(pool, memory), [make_state(0), make_state(1)])
    assert [type(r).__name__ for r in replies] == ['Move', 'Abort']
    assert isinstance(replies[1], Abort)
    assert replies[1].reason == 'the script exited while still flying'
    assert result.exited_early
    assert 'The script exited after 1 ticks.' in result.report('code').outcome


async def test_game_going_away_ends_the_script_quietly(
    pool: AsyncMonty, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_pilot(monkeypatch, THRUST_EVERY_OTHER_TICK)
    memory = PilotMemory()
    result, replies = await fly(pool, await plan(pool, memory), [make_state(0), make_state(1)])
    assert len(replies) == 2  # the reply to state 1 went out, then the client hung up
    assert result.error is None  # the FlightOver unwinding is not the script's fault
    assert result.ticks == 1
    assert result.report('code').outcome.startswith('The flight was cut short after 0.0 s (tick 1)')


async def test_move_edited_after_construction_is_sent_as_edited(
    pool: AsyncMonty, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_pilot(
        monkeypatch,
        'm = Move()\n'
        'm.thrust = True\n'
        's = await update(m)\n'
        'm.left = True\n'
        's = await update(m)\n'
        'assert type(s) is Status and s.tick == 2, s\n'
        'assert type(pad) is Pad and pad.x1 == 120\n',
    )
    memory = PilotMemory()
    _, replies = await fly(pool, await plan(pool, memory), [make_state(t) for t in range(3)])
    assert replies[:2] == [Move(thrust=True), Move(thrust=True, left=True)]


async def test_update_rejects_anything_but_a_move(
    pool: AsyncMonty, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_pilot(monkeypatch, 'class Foo:\n    pass\nawait update(Foo())\n')
    memory = PilotMemory()
    result, replies = await fly(pool, await plan(pool, memory), [make_state(0)])
    assert result.error is not None
    assert 'TypeError: update() expects a Move, got Foo' in result.error
    assert replies == [Abort(reason='TypeError: update() expects a Move, got Foo')]


async def test_script_ignoring_flight_over_is_cancelled(
    pool: AsyncMonty, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_pilot(
        monkeypatch,
        'while True:\n'
        '    try:\n'
        '        await update(Move(thrust=True))\n'
        '    except Exception:\n'
        '        pass\n',
    )
    memory = PilotMemory()
    result, replies = await fly(
        pool, await plan(pool, memory), [make_state(0), make_state(1, 'landed')]
    )
    assert replies == [Move(thrust=True), Move()]  # the stray update() calls sent nothing more
    assert result.error is None
    assert result.last_state.status == 'landed'


async def test_make_plan_retries_after_preflight_failure(
    pool: AsyncMonty, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = [
        'print(1 + "bad")\ns = await update(Move())\n',
        "s = status\nwhile s.status == 'flying':\n    s = await update(Move(thrust=True))\n",
    ]
    scripted_pilot(monkeypatch, *scripts)
    memory = PilotMemory(last_state=make_state(0))
    result = await plan(pool, memory)
    assert result.output.code == scripts[1]
    assert len(memory.history) == 1
    assert memory.history[0].outcome.startswith('Did not fly')
    assert memory.history[0].error is not None
    assert 'TypeError' in memory.history[0].error


async def test_make_plan_skips_preflight_without_a_state(
    pool: AsyncMonty, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = 'print("%.1f" % 1.5)\n'
    scripted_pilot(monkeypatch, script)
    memory = PilotMemory()
    result = await plan(pool, memory)  # nothing to check against yet
    assert result.output.code == script
    assert memory.history == []


async def test_make_plan_gives_up_after_three_rejected_scripts(
    pool: AsyncMonty, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_pilot(monkeypatch, 's = await update(Move(thrust=True))\n')
    memory = PilotMemory(last_state=make_state(0))
    assert await make_plan(pool, memory, agent.write_pilot, fake_ai) is None
    assert len(memory.history) == 3
    assert all('finished after 1 update() calls' in (r.error or '') for r in memory.history)


async def test_make_plan_propagates_agent_errors(pool: AsyncMonty) -> None:
    async def write_code(memory: PilotMemory) -> AgentRunResult[PilotScript]:
        msg = 'no api key'
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match='no api key'):
        await make_plan(pool, PilotMemory(), write_code, fake_ai)


async def test_reports_come_back_as_tool_results(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[ModelMessage]] = []

    def pilot_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return submit(f'x = {len(seen)}')

    monkeypatch.setattr(agent, 'pilot_agent', fake_pilot_agent(FunctionModel(pilot_model)))
    memory = PilotMemory()
    result = await agent.write_pilot(memory)
    assert result.output.code == 'x = 1'
    assert memory.messages == []  # nothing is committed until the flight reports back
    memory.record(
        RunReport(code='x = 1', outcome='Crashed after 3.0 s', error='Boom', output='hi'), result
    )
    assert (await agent.write_pilot(memory)).output.code == 'x = 2'

    first, second = seen
    assert len(first) == 1
    assert agent.FIRST_PROMPT in str(first[0])
    assert len(second) == 3
    assert second[0] == first[0]
    call = second[1].parts[0]
    assert isinstance(call, ToolCallPart)
    assert call.args_as_dict()['code'] == 'x = 1'  # the previous script is the agent's own call
    ret = second[2].parts[0]
    assert isinstance(ret, ToolReturnPart)
    assert ret.tool_call_id == call.tool_call_id
    assert 'Crashed after 3.0 s' in str(ret.content)
    assert 'Boom' in str(ret.content)


async def test_history_is_trimmed_before_each_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[ModelMessage]] = []

    def pilot_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return submit(f'x = {len(seen)}')

    monkeypatch.setattr(agent, 'pilot_agent', fake_pilot_agent(FunctionModel(pilot_model)))
    memory = PilotMemory()
    for i in range(agent.MAX_HISTORY_TURNS + 3):
        result = await agent.write_pilot(memory)
        memory.record(RunReport(code=f'x = {i + 1}', outcome=f'Crashed on flight {i + 1}'), result)
    last = seen[-1]
    assert len(last) == 1 + 2 * agent.MAX_HISTORY_TURNS
    assert isinstance(last[0], ModelRequest)
    assert isinstance(last[0].parts[0], UserPromptPart)  # the goal survives trimming
    assert isinstance(last[1], ModelResponse)  # then whole call/result pairs
    assert isinstance(last[-1].parts[0], ToolReturnPart)
    # Nine requests: the last one carries reports 1..8, of which only the last six survive.
    assert 'Crashed on flight 2' not in str(last)
    assert 'Crashed on flight 3' in str(last)
    assert 'Crashed on flight 8' in str(last)


def test_plan_then_websocket_flight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('THRUST_MEMORY_FILE', str(tmp_path / 'memory.json'))
    seen = scripted_pilot(monkeypatch, THRUST_EVERY_OTHER_TICK)

    def helper_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart('go straight up')])

    monkeypatch.setattr(agent, 'helper_agent', Agent(FunctionModel(helper_model)))

    with TestClient(app) as client:
        response = client.get('/plan')
        assert response.status_code == 200
        assert response.json() == {'strategy': STRATEGY}
        assert len(seen) == 1

        with client.websocket_connect('/ws') as ws:
            ws.send_text('{}')  # invalid states are ignored
            moves: list[Move] = []
            for tick in range(3):
                ws.send_text(make_state(tick).model_dump_json())
                moves.append(Move.model_validate_json(ws.receive_text()))
            ws.send_text(make_state(3, 'landed').model_dump_json())
            Move.model_validate_json(ws.receive_text())
        assert [m.thrust for m in moves] == [True, False, True]

        memory: PilotMemory = app.state.memory
        assert memory.last_run is not None
        assert 'plan: go straight up' in memory.last_run.output
        assert memory.last_state is not None  # the next plan is pre-checked against it
        assert app.state.plan is None  # consumed by the flight

        # The plan flew once; another connection needs a new one.
        with client.websocket_connect('/ws') as ws, pytest.raises(WebSocketDisconnect):
            ws.receive_text()
        assert len(seen) == 1

    saved = PilotMemory.model_validate_json((tmp_path / 'memory.json').read_text())
    assert saved.history == memory.history
    assert 'Landed after' in str(saved.messages[-1])


async def test_memory_persists_and_resumes_the_conversation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[ModelMessage]] = []

    def pilot_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return submit(f'x = {len(seen)}')

    monkeypatch.setattr(agent, 'pilot_agent', fake_pilot_agent(FunctionModel(pilot_model)))
    path = tmp_path / 'memory.json'
    memory = PilotMemory.load(path)
    result = await agent.write_pilot(memory)
    assert not path.exists()  # nothing to persist until the flight reports back
    memory.record(RunReport(code='x = 1', outcome='Crashed after 3.0 s'), result)

    text = path.read_text()
    assert text.startswith('{\n  "messages": [')  # indented JSON
    resumed = PilotMemory.load(path)  # a new process picks up where the last one left off
    assert resumed.history == memory.history
    assert (await agent.write_pilot(resumed)).output.code == 'x = 2'
    ret = seen[1][-1].parts[0]
    assert isinstance(ret, ToolReturnPart)
    assert 'Crashed after 3.0 s' in str(ret.content)  # the report was saved as the tool result
