"""What the agent is told about how a flight went: a sampled log and a diagnosis.

`Sample`s are recorded once per tick by the flight (the state the game sent and the move
the script answered it with). `render` turns them into a compact table and `diagnose`
works out from the final states why the flight ended the way it did, using the same
rocket geometry and landing limits as the game.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from thrust_server.models import Move, State

SAMPLE_EVERY_S = 0.5
"""Spacing of the main telemetry rows."""
TAIL_S = 2.0
"""The last stretch of the flight is sampled more densely."""
TAIL_EVERY_S = 0.1


@dataclass
class Sample:
    """One tick: the state the game sent and what the script replied to it."""

    state: State
    move: Move | None
    """`None` for the final state, which the script never answered."""

    @property
    def t(self) -> float:
        return self.state.tick * self.state.physics.dt


def render(samples: list[Sample]) -> str:
    """A fixed-width table: every 0.5 s, then every 0.1 s for the last 2 s."""
    if not samples:
        return ''
    end = samples[-1].t
    rows: list[str] = []
    next_t = 0.0
    for sample in samples:
        t = sample.t
        spacing = TAIL_EVERY_S if t >= end - TAIL_S else SAMPLE_EVERY_S
        if t + 1e-9 < next_t and sample is not samples[-1]:
            continue
        next_t = t + spacing
        r = sample.state.rocket
        w = sample.state.wind
        move = sample.move
        inputs = (
            ''
            if move is None
            else ('T' if move.thrust else '.')
            + ('L' if move.left else '.')
            + ('R' if move.right else '.')
        )
        rows.append(
            f'{t:6.1f} {r.x:7.1f} {r.y:7.1f} {r.vx:6.2f} {r.vy:6.2f} {r.angle:6.2f}'
            f' {w.x:5.1f} {w.y:5.1f}  {inputs}'
        )
    header = (
        '     t       x       y     vx     vy  angle  wind_x wind_y'
        '  inputs(T=thrust,L=left,R=right)'
    )
    return '\n'.join([header, *rows])


def diagnose(samples: list[Sample]) -> str:
    """Why the flight ended as it did, from the final states."""
    if not samples:
        return ''
    last = samples[-1].state
    flying = [s.state for s in samples if s.state.status == 'flying']
    before = flying[-1] if flying else last
    """The last state before the outcome: the game snaps a landed rocket, so use this."""
    parts = [stats(samples)]
    if last.status == 'landed':
        r = before.rocket
        parts.append(
            f'Touchdown at vx={r.vx:.2f} vy={r.vy:.2f} angle={r.angle:.2f}'
            f' (limits {last.physics.landing_max_vx}, {last.physics.landing_max_vy},'
            f' {last.physics.landing_max_angle}).'
        )
    elif last.status == 'crashed':
        parts.append(crash_cause(last))
    elif last.status == 'timeout':
        r = last.rocket
        parts.append(f'Still flying at x={r.x:.1f} y={r.y:.1f} when the time ran out.')
    return ' '.join(parts)


def stats(samples: list[Sample]) -> str:
    states = [s.state for s in samples]
    pad = states[-1].pad
    pad_x = (pad.x1 + pad.x2) / 2
    top = max(states, key=lambda s: s.rocket.y)
    fastest = max(states, key=lambda s: math.hypot(s.rocket.vx, s.rocket.vy))
    closest = min(states, key=lambda s: abs(s.rocket.x - pad_x))
    dt = states[-1].physics.dt
    return (
        f'Peak altitude y={top.rocket.y:.1f} at t={top.tick * dt:.1f}; top speed'
        f' {math.hypot(fastest.rocket.vx, fastest.rocket.vy):.1f} m/s at t={fastest.tick * dt:.1f};'
        f' closest to the pad centre (x={pad_x:.1f}) at t={closest.tick * dt:.1f},'
        f' {abs(closest.rocket.x - pad_x):.1f} m away.'
    )


def crash_cause(state: State) -> str:
    r = state.rocket
    world = state.world
    corners = rocket_corners(state)
    if any(c[0] < 0 for c in corners):
        return f'Hit the left wall at y={r.y:.1f}.'
    if any(c[0] > world.width for c in corners):
        return f'Hit the right wall at y={r.y:.1f}.'
    if any(c[1] > world.height for c in corners):
        return f'Hit the ceiling at x={r.x:.1f}.'
    pad = state.pad
    tip, base_r, base_l = corners
    base_on_pad = all(pad.x1 <= c[0] <= pad.x2 for c in (base_r, base_l))
    if base_on_pad:
        phys = state.physics
        broken: list[str] = []
        if abs(r.vy) >= phys.landing_max_vy:
            broken.append(f'|vy|={abs(r.vy):.2f} >= {phys.landing_max_vy}')
        if abs(r.vx) >= phys.landing_max_vx:
            broken.append(f'|vx|={abs(r.vx):.2f} >= {phys.landing_max_vx}')
        if abs(normalise_angle(r.angle)) >= phys.landing_max_angle:
            broken.append(
                f'|angle|={abs(normalise_angle(r.angle)):.2f} >= {phys.landing_max_angle}'
            )
        return f'Touched the pad too hard: {", ".join(broken) or "unknown limit"}.'
    touched = [
        name
        for name, c in (('nose', tip), ('right base corner', base_r), ('left base corner', base_l))
        if c[1] <= terrain_height_at(state, c[0])
    ]
    pad_x = (pad.x1 + pad.x2) / 2
    where = f'{abs(r.x - pad_x):.1f} m {"left" if r.x < pad_x else "right"} of the pad centre'
    if abs(r.vy) < state.physics.landing_max_vy and abs(r.vx) < state.physics.landing_max_vx:
        speed = 'slowly, so it would have been a landing on the pad'
    else:
        speed = f'at vx={r.vx:.2f} vy={r.vy:.2f}'
    what = ' and '.join(touched) or 'body'
    return f'Touched the terrain with the {what} at x={r.x:.1f} y={r.y:.1f}, {where}, {speed}.'


def rocket_corners(state: State) -> list[tuple[float, float]]:
    """Nose tip, right base corner, left base corner in world space (see `physics.ts`)."""
    r = state.rocket
    phys = state.physics
    body = [
        (0.0, phys.rocket_height * 0.6),
        (phys.rocket_half_base, -phys.rocket_height * 0.4),
        (-phys.rocket_half_base, -phys.rocket_height * 0.4),
    ]
    c, s = math.cos(r.angle), math.sin(r.angle)
    return [(r.x + px * c + py * s, r.y - px * s + py * c) for px, py in body]


def terrain_height_at(state: State, x: float) -> float:
    """Linear interpolation of the terrain polyline at x."""
    pts = state.terrain
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in pairwise(pts):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 != x0 else y0
    return pts[-1][1]


def normalise_angle(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi
