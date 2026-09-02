from fastapi.testclient import TestClient

from thrust_server.main import app
from thrust_server.models import Move, Physics, State

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

SAMPLE_STATE = State(
    tick=1,
    status="flying",
    rocket={"x": 80, "y": 90, "vx": 0, "vy": 0, "angle": 0, "angularVelocity": 0},  # pyright: ignore[reportArgumentType]
    wind={"x": 2, "y": 0},  # pyright: ignore[reportArgumentType]
    pad={"x1": 20, "x2": 32, "y": 15},  # pyright: ignore[reportArgumentType]
    launchPad={"x1": 100, "x2": 110, "y": 20},  # pyright: ignore[reportArgumentType]
    terrain=[(0, 10), (20, 15), (32, 15), (160, 20)],
    world={"width": 160, "height": 100, "gravity": 4},  # pyright: ignore[reportArgumentType]
    physics=PHYSICS,
)


def test_health() -> None:
    client = TestClient(app)
    assert client.get("/health").json() == {"ok": True}


def test_ws_returns_move() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text(SAMPLE_STATE.model_dump_json())
        move = Move.model_validate_json(ws.receive_text())
    assert move.type == "move"


def test_ws_ignores_invalid_message() -> None:
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_text("{}")
        ws.send_text(SAMPLE_STATE.model_dump_json())
        move = Move.model_validate_json(ws.receive_text())
    assert move.type == "move"
