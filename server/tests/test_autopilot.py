"""The sandboxed pilot: fake code writers drive the real monty runtime."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import Agent, ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_monty import AsyncMonty

from thrust_server import agent
from thrust_server.autopilot import AgentPilot, PilotMemory, RunReport
from thrust_server.main import app
from thrust_server.models import Move, Physics, State

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


def writer(code: str) -> Any:  # noqa: ANN401 - callable of a fixed shape, see WriteCode
    async def write_code(instructions: str, memory: PilotMemory) -> str:
        return code

    return write_code


async def fake_ai(query: str) -> str:
    return f'climb, then cross ({query[:20]})'


@pytest.fixture
async def pool() -> AsyncIterator[AsyncMonty]:
    async with AsyncMonty() as pool:
        yield pool


async def test_script_controls_each_tick(pool: AsyncMonty) -> None:
    memory = PilotMemory()
    pilot = AgentPilot(
        pool,
        memory,
        instructions='land',
        write_code=writer(THRUST_EVERY_OTHER_TICK),
        ask_ai=fake_ai,
    )
    moves = [await pilot.decide(make_state(tick)) for tick in range(4)]
    assert [m.thrust for m in moves] == [True, False, True, False]
    assert moves[0].right is True
    assert moves[1].right is False

    landed = await pilot.decide(make_state(4, 'landed'))
    assert landed == Move()  # the script sees the landing and exits
    await pilot.close()

    report = memory.last_run
    assert report is not None
    assert report.error is None
    assert report.outcome.startswith('Landed after 0.1 s (tick 4)')
    assert 'plan: climb, then cross' in report.output
    assert 'finished landed 0.07 s' in report.output


async def test_script_error_is_reported_and_rocket_idles(pool: AsyncMonty) -> None:
    memory = PilotMemory()
    pilot = AgentPilot(
        pool,
        memory,
        instructions='land',
        write_code=writer(
            's = status\n'
            "while s.status == 'flying':\n"
            '    s = await update(Move(thrust=True))\n'
            '    if s.tick >= 5:\n'
            '        boom = 1 / 0\n'
        ),
        ask_ai=fake_ai,
    )
    for tick in range(5):
        assert (await pilot.decide(make_state(tick))).thrust is True
    assert (await pilot.decide(make_state(5))) == Move()  # the script has died: idle
    await pilot.decide(make_state(6, 'crashed'))
    report = memory.last_run
    assert report is not None
    assert report.error is not None
    assert 'ZeroDivisionError' in report.error
    assert 'stopped controlling the rocket after 5 ticks' in report.outcome
    await pilot.close()


async def test_restart_starts_a_new_flight_with_the_report(pool: AsyncMonty) -> None:
    seen: list[RunReport | None] = []

    async def write_code(instructions: str, memory: PilotMemory) -> str:
        seen.append(memory.last_run)
        return "s = status\nwhile s.status == 'flying':\n    s = await update(Move(thrust=True))\n"

    pilot = AgentPilot(
        pool, PilotMemory(), instructions='land', write_code=write_code, ask_ai=fake_ai
    )
    await pilot.decide(make_state(0))
    await pilot.decide(make_state(1, 'crashed'))
    assert (await pilot.decide(make_state(2, 'crashed'))) == Move()  # after the flight: idle
    assert (await pilot.decide(make_state(0))).thrust is True  # tick reset: new flight
    await pilot.close()
    assert seen[0] is None
    assert seen[1] is not None
    assert seen[1].outcome.startswith('Crashed')


async def test_agent_failure_falls_back_to_naive_policy(pool: AsyncMonty) -> None:
    async def write_code(instructions: str, memory: PilotMemory) -> str:
        msg = 'no api key'
        raise RuntimeError(msg)

    pilot = AgentPilot(
        pool, PilotMemory(), instructions='land', write_code=write_code, ask_ai=fake_ai
    )
    move = await pilot.decide(make_state(0))
    assert move.thrust is True  # the naive policy launches straight up
    await pilot.close()


def test_extract_code() -> None:
    reply = 'Here you go:\n```python\nx = 1\n```\nand a longer one\n```py\nx = 1\ny = 2\n```\n'
    assert agent.extract_code(reply) == 'x = 1\ny = 2'
    assert agent.extract_code('print(1)\n') == 'print(1)'


def test_flight_prompt_includes_last_run() -> None:
    memory = PilotMemory()
    assert 'first flight' in agent.flight_prompt('land', memory)
    memory.record(RunReport(code='x = 1', outcome='Crashed after 3.0 s', error='Boom', output='hi'))
    memory.record(RunReport(code='y = 2', outcome='Landed after 9.0 s'))
    prompt = agent.flight_prompt('land', memory)
    assert 'Goal: land' in prompt
    assert '- flight 1: Crashed after 3.0 s' in prompt
    assert 'Previous flight (flight 2): Landed after 9.0 s' in prompt
    assert 'y = 2' in prompt
    assert 'Boom' not in prompt


def test_websocket_runs_agent_script(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts: list[str] = []

    def pilot_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(str(messages[-1]))
        return ModelResponse(parts=[TextPart(f'```python\n{THRUST_EVERY_OTHER_TICK}\n```')])

    def helper_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart('go straight up')])

    monkeypatch.setattr(agent, 'pilot_agent', Agent(FunctionModel(pilot_model)))
    monkeypatch.setattr(agent, 'helper_agent', Agent(FunctionModel(helper_model)))
    monkeypatch.setenv('THRUST_PILOT', 'agent')

    with TestClient(app) as client, client.websocket_connect('/ws') as ws:
        moves: list[Move] = []
        for tick in range(3):
            ws.send_text(make_state(tick).model_dump_json())
            moves.append(Move.model_validate_json(ws.receive_text()))
        ws.send_text(make_state(3, 'landed').model_dump_json())
        Move.model_validate_json(ws.receive_text())
    assert [m.thrust for m in moves] == [True, False, True]
    assert len(prompts) == 1
    assert 'first flight' in prompts[0]
    memory: PilotMemory = app.state.memory
    assert memory.last_run is not None
    assert 'plan: go straight up' in memory.last_run.output


async def test_preflight_failure_gets_a_second_script(pool: AsyncMonty) -> None:
    scripts = iter(
        [
            'print("%.1f" % 1.5)\ns = await update(Move())\n',  # monty has no % formatting
            "s = status\nwhile s.status == 'flying':\n    s = await update(Move(thrust=True))\n",
        ]
    )
    memory = PilotMemory()

    async def write_code(instructions: str, memory: PilotMemory) -> str:
        return next(scripts)

    pilot = AgentPilot(pool, memory, instructions='land', write_code=write_code, ask_ai=fake_ai)
    assert (await pilot.decide(make_state(0))).thrust is True
    await pilot.close()
    assert len(memory.history) == 2
    assert memory.history[0].outcome.startswith('Did not fly')
    assert memory.history[0].error is not None
    assert 'TypeError' in memory.history[0].error
    assert memory.history[1].outcome.startswith('The flight was cut short')


async def test_preflight_rejects_script_that_exits_early(pool: AsyncMonty) -> None:
    async def write_code(instructions: str, memory: PilotMemory) -> str:
        return 's = await update(Move(thrust=True))\n'

    memory = PilotMemory()
    pilot = AgentPilot(pool, memory, instructions='land', write_code=write_code, ask_ai=fake_ai)
    move = await pilot.decide(make_state(0))
    assert move.thrust is True  # naive fallback after three rejected scripts
    await pilot.close()
    assert len(memory.history) == 3
    assert all('finished after 1 update() calls' in (r.error or '') for r in memory.history)
