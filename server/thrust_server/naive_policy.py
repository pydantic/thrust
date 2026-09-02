"""The control policy: given the current game state, decide what the rocket does.

The client sends one ``State`` per physics tick (60 Hz, paced so at most one
request is in flight) and applies the returned ``Move`` until the next reply.

The controller works in two phases:

* **transit**: climb to a cruise altitude that clears every mountain between the
  rocket and the pad, then fly horizontally toward the pad centre.
* **descent**: once roughly above the pad and slow, come straight down with a
  descent rate that shrinks as the pad gets closer.

Both phases share the same inner loops. A desired acceleration vector is built
from velocity errors, gravity and an estimate of the wind drag; the rocket is
then pointed along that vector (rotation is bang-bang toward a desired angular
velocity) and the engine fires when the acceleration required along the nose
exceeds half of what the engine can give. Tilt is limited near the ground so
the rocket never touches down at an angle.

Physics constants (thrust, drag, rocket size) come with every state message in
``state.physics``; nothing here has to be kept in sync with the client.
"""

from __future__ import annotations

import math
from typing import Literal

from thrust_server.models import Move, Pad, Physics, State

# ---- tuning
CRUISE_CLEARANCE = 12.0
"""Metres above the tallest terrain between rocket and pad to fly at."""
LOOKAHEAD = 15.0
"""How far ahead (m) to check terrain before allowing horizontal motion."""
LOOKAHEAD_CLEARANCE = 6.0
MAX_HORIZONTAL_SPEED = 10.0
MAX_HORIZONTAL_ACCEL = 3.0
MAX_TILT = 0.6
MAX_TILT_DESCENT = 0.3
MAX_TILT_LANDING = 0.15
FINAL_APPROACH_HEIGHT = 4.0
"""Below this height above the pad the tilt is clamped hard for touchdown."""
DESCENT_ENTER_DX = 3.0
DESCENT_ENTER_VX = 2.0
DESCENT_EXIT_DX = 5.0
DESCENT_ALIGN_DX = 2.0
"""Beyond this horizontal error the descent pauses until the rocket recentres."""
ANGULAR_DEADBAND = 0.12
MAX_ANGULAR_VELOCITY = 2.4

Phase = Literal["transit", "descent"]


def resting_y(pad: Pad, physics: Physics) -> float:
    """Rocket centre height when sitting upright on a pad."""
    return pad.y + physics.rocket_height * 0.4


class Policy:
    """One controller per connection; keeps the current phase between ticks."""

    def __init__(self) -> None:
        self.phase: Phase = "transit"

    def decide(self, state: State) -> Move:
        if state.status != "flying":
            self.phase = "transit"
            return Move()

        r = state.rocket
        pad = state.pad
        phys = state.physics
        g = state.world.gravity
        thrust_accel = phys.thrust_accel
        base_offset = phys.rocket_height * 0.4
        pad_x = (pad.x1 + pad.x2) / 2
        dx = pad_x - r.x
        height_above_pad = r.y - resting_y(pad, phys)

        vx_des, vy_des = self._desired_velocity(state, dx, height_above_pad, base_offset)

        ax_des = clamp(1.2 * (vx_des - r.vx), -MAX_HORIZONTAL_ACCEL, MAX_HORIZONTAL_ACCEL)
        ay_des = clamp(1.5 * (vy_des - r.vy), -g, thrust_accel - g)

        # ---- required engine acceleration: net target, minus gravity and wind help
        drag_x = phys.wind_drag * (state.wind.x - r.vx)
        drag_y = phys.wind_drag * (state.wind.y - r.vy)
        tx = ax_des - drag_x
        ty = ay_des + g - drag_y

        # ---- attitude: point the nose along the required thrust, tilt limited near ground
        clearance = r.y - base_offset - terrain_height_at(state, r.x)
        max_tilt = clamp(0.06 + 0.1 * clearance, 0.06, MAX_TILT)
        if self.phase == "descent":
            final = height_above_pad < FINAL_APPROACH_HEIGHT
            landing_tilt = MAX_TILT_LANDING if final else MAX_TILT_DESCENT
            max_tilt = min(max_tilt, landing_tilt)
        angle_des = clamp(math.atan2(tx, max(ty, 0.5)), -max_tilt, max_tilt)

        angle = normalise_angle(r.angle)
        w_des = clamp(4.0 * (angle_des - angle), -MAX_ANGULAR_VELOCITY, MAX_ANGULAR_VELOCITY)
        w_err = w_des - r.angularVelocity
        left = w_err < -ANGULAR_DEADBAND
        right = w_err > ANGULAR_DEADBAND

        # ---- engine: fire when the required acceleration along the nose exceeds half thrust
        along_nose = tx * math.sin(angle) + ty * math.cos(angle)
        thrust = along_nose > thrust_accel / 2

        # Never fire while tipped over far enough that thrust would push us down.
        if abs(angle) > math.pi / 2:
            thrust = False

        return Move(thrust=thrust, left=left, right=right)

    def _desired_velocity(
        self, state: State, dx: float, height_above_pad: float, base_offset: float
    ) -> tuple[float, float]:
        """Phase selection (with hysteresis) and the outer velocity loops."""
        r = state.rocket
        pad_x = (state.pad.x1 + state.pad.x2) / 2
        # phase selection with hysteresis
        if self.phase == "transit":
            if abs(dx) < DESCENT_ENTER_DX and abs(r.vx) < DESCENT_ENTER_VX and height_above_pad > 0:
                self.phase = "descent"
        elif abs(dx) > DESCENT_EXIT_DX:
            self.phase = "transit"

        # outer loops: desired velocities
        if self.phase == "transit":
            cruise = self._cruise_altitude(state, pad_x)
            vy_des = clamp(0.5 * (cruise - r.y), -4.0, 5.0)

            # Only move sideways once there is room over the terrain ahead
            # (but never look past the pad: the far side may be a cliff).
            ahead = terrain_max(state, r.x, r.x + math.copysign(min(LOOKAHEAD, abs(dx)), dx))
            if r.y - base_offset < ahead + LOOKAHEAD_CLEARANCE:
                vx_des = 0.0
            else:
                stop_speed = math.sqrt(2 * MAX_HORIZONTAL_ACCEL * abs(dx))
                v_max = min(MAX_HORIZONTAL_SPEED, stop_speed)
                vx_des = clamp(0.35 * dx, -v_max, v_max)
        else:
            vy_des = -clamp(0.3 * height_above_pad + 0.8, 1.2, 4.0)
            vx_des = clamp(0.4 * dx, -2.0, 2.0)
            # Drifted off centre (crosswind): hold altitude until realigned.
            if abs(dx) > DESCENT_ALIGN_DX:
                vy_des = max(vy_des, -0.3)

        return vx_des, vy_des

    def _cruise_altitude(self, state: State, pad_x: float) -> float:
        peak = terrain_max(state, state.rocket.x, pad_x)
        cruise = max(peak + CRUISE_CLEARANCE, resting_y(state.pad, state.physics) + 10.0)
        return min(cruise, state.world.height - 6.0)


def terrain_height_at(state: State, x: float) -> float:
    """Linear interpolation of the terrain polyline at x."""
    pts = state.terrain
    if not pts:
        return 0.0
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    lo, hi = 0, len(pts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if pts[mid][0] <= x:
            lo = mid
        else:
            hi = mid
    x0, y0 = pts[lo]
    x1, y1 = pts[hi]
    t = 0.0 if x1 == x0 else (x - x0) / (x1 - x0)
    return y0 + (y1 - y0) * t


def terrain_max(state: State, xa: float, xb: float) -> float:
    """Highest terrain point between xa and xb, including both ends."""
    lo, hi = min(xa, xb), max(xa, xb)
    best = max(terrain_height_at(state, lo), terrain_height_at(state, hi))
    for x, y in state.terrain:
        if lo <= x <= hi and y > best:
            best = y
    return best


def normalise_angle(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
