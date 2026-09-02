"""The flight log and the diagnosis of how a flight ended."""

from thrust_server.models import Move, State
from thrust_server.telemetry import Sample, diagnose, render, rocket_corners

from .test_autopilot import make_state


def crashed_at(x: float, y: float, vx: float = 0, vy: float = -1, angle: float = 0) -> State:
    state = make_state(100, 'crashed')
    state.rocket.x, state.rocket.y = x, y
    state.rocket.vx, state.rocket.vy, state.rocket.angle = vx, vy, angle
    return state


def flight(*states: State) -> list[Sample]:
    samples = [Sample(s, Move(thrust=True)) for s in states[:-1]]
    return [*samples, Sample(states[-1], None)]


def test_render_samples_every_half_second_and_densely_at_the_end() -> None:
    states = [make_state(tick) for tick in range(600)] + [make_state(600, 'landed')]
    table = render(flight(*states))
    lines = table.splitlines()
    assert lines[0].startswith('     t       x       y     vx     vy  angle')
    times = [float(line.split()[0]) for line in lines[1:]]
    assert times[:3] == [0.0, 0.5, 1.0]
    assert times[-3:] == [9.8, 9.9, 10.0]
    assert len(times) == 17 + 20  # 0..8.0 s every 0.5 s, then 8.1..10.0 s every 0.1 s
    assert lines[1].endswith('T..')  # thrust on, no rotation
    assert lines[-1].endswith(f'{0.0:5.1f}  ')  # the final state was never answered


def test_diagnose_landing_reports_touchdown_from_the_last_flying_state() -> None:
    before = make_state(99)
    before.rocket.vy = -3.2
    landed = make_state(100, 'landed')
    text = diagnose(flight(make_state(0), before, landed))
    assert 'Touchdown at vx=0.00 vy=-3.20 angle=0.00' in text
    assert 'Peak altitude' in text


def test_diagnose_wall_and_ceiling() -> None:
    assert 'Hit the right wall' in diagnose(flight(make_state(0), crashed_at(x=160.5, y=50)))
    assert 'Hit the left wall' in diagnose(flight(make_state(0), crashed_at(x=-0.2, y=50)))
    assert 'Hit the ceiling' in diagnose(flight(make_state(0), crashed_at(x=80, y=99)))


def test_diagnose_hard_landing_names_the_broken_limits() -> None:
    # Pad spans x 120..132 at y 10; base corners 1.25 m either side, 1.6 m below centre.
    text = diagnose(flight(make_state(0), crashed_at(x=126, y=11.6, vx=0.1, vy=-7.5)))
    assert text.endswith('Touched the pad too hard: |vy|=7.50 >= 5.0.')


def test_diagnose_terrain_contact_outside_the_pad() -> None:
    # Terrain rises from (60, 50) to (120, 10); at x=100 the ground is 23.3 m.
    text = diagnose(flight(make_state(0), crashed_at(x=100, y=24.5, vx=1.0, vy=-2.0)))
    assert 'Touched the terrain with the' in text
    assert '26.0 m left of the pad centre' in text
    assert 'slowly, so it would have been a landing on the pad' in text


def test_rocket_corners_match_the_game_geometry() -> None:
    state = make_state(0)  # x=25, y=21.6, upright, height 4, half base 1.25
    tip, right, left = rocket_corners(state)
    assert tip == (25.0, 24.0)
    assert right == (26.25, 20.0)
    assert left == (23.75, 20.0)
