import math

from thrust_server.models import Move, Pad, Physics, Rocket, State, Vec2, WorldInfo
from thrust_server.naive_policy import Policy, resting_y, terrain_height_at, terrain_max

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
PAD = Pad(x1=120, x2=132, y=10)
LAUNCH = Pad(x1=20, x2=30, y=20)
TERRAIN: list[tuple[float, float]] = [
    (0, 15),
    (20, 20),
    (30, 20),
    (60, 50),
    (90, 30),
    (120, 10),
    (132, 10),
    (160, 25),
]


def make_state(
    *,
    x: float,
    y: float,
    vx: float = 0,
    vy: float = 0,
    angle: float = 0,
    angular_velocity: float = 0,
    status: str = "flying",
) -> State:
    return State(
        tick=1,
        status=status,  # pyright: ignore[reportArgumentType]
        rocket=Rocket(x=x, y=y, vx=vx, vy=vy, angle=angle, angularVelocity=angular_velocity),
        wind=Vec2(x=0, y=0),
        pad=PAD,
        launchPad=LAUNCH,
        terrain=TERRAIN,
        world=WorldInfo(width=160, height=100, gravity=4),
        physics=PHYSICS,
    )


def test_terrain_lookup() -> None:
    s = make_state(x=0, y=0)
    assert terrain_height_at(s, 25) == 20
    assert terrain_height_at(s, 45) == 35
    assert terrain_height_at(s, -5) == 15
    assert terrain_max(s, 25, 126) == 50


def test_terminal_state_is_noop() -> None:
    assert Policy().decide(make_state(x=126, y=12, status="landed")) == Move()


def test_takeoff_climbs_straight_up() -> None:
    # Resting on the launch pad, far from the target: thrust, no tilt yet.
    move = Policy().decide(make_state(x=25, y=resting_y(LAUNCH, PHYSICS)))
    assert move.thrust
    assert not move.left
    assert not move.right


def test_transit_tilts_toward_pad_when_clear() -> None:
    # High above everything, stationary, pad is to the right: rotate right.
    move = Policy().decide(make_state(x=25, y=90))
    assert move.right
    assert not move.left


def test_descent_brakes_a_fast_fall() -> None:
    p = Policy()
    s = make_state(x=126, y=30, vy=-8)
    p.decide(s)
    assert p.phase == "descent"
    assert p.decide(s).thrust


def test_descent_lets_a_slow_fall_continue() -> None:
    p = Policy()
    s = make_state(x=126, y=13, vy=-1.0)
    p.decide(s)
    assert p.phase == "descent"
    assert not p.decide(s).thrust


def test_never_thrusts_upside_down() -> None:
    move = Policy().decide(make_state(x=80, y=90, angle=math.pi))
    assert not move.thrust
