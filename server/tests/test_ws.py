import pytest
from fastapi.testclient import TestClient

from thrust_server.main import app
from thrust_server.models import Move, Pad, Physics, Rocket, State, Vec2, WorldInfo

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

SAMPLE_STATE = State(
    tick=1,
    status='flying',
    rocket=Rocket(x=80, y=90, vx=0, vy=0, angle=0, angularVelocity=0),
    wind=Vec2(x=2, y=0),
    pad=Pad(x1=20, x2=32, y=15),
    launchPad=Pad(x1=100, x2=110, y=20),
    terrain=[(0, 10), (20, 15), (32, 15), (160, 20)],
    world=WorldInfo(width=160, height=100, gravity=4),
    physics=PHYSICS,
)


@pytest.fixture(autouse=True)
def naive_pilot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('THRUST_PILOT', 'naive')


def test_health() -> None:
    with TestClient(app) as client:
        assert client.get('/health').json() == {'ok': True}


def test_plan_in_naive_mode_needs_no_agent() -> None:
    with TestClient(app) as client:
        assert client.get('/plan').json() == {
            'strategy': 'The hand-written controller flies this one.'
        }


def test_ws_returns_move() -> None:
    with TestClient(app) as client, client.websocket_connect('/ws') as ws:
        ws.send_text(SAMPLE_STATE.model_dump_json())
        move = Move.model_validate_json(ws.receive_text())
    assert move.type == 'move'


def test_ws_ignores_invalid_message() -> None:
    with TestClient(app) as client, client.websocket_connect('/ws') as ws:
        ws.send_text('{}')
        ws.send_text(SAMPLE_STATE.model_dump_json())
        move = Move.model_validate_json(ws.receive_text())
    assert move.type == 'move'
