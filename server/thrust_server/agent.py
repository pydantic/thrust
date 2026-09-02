"""The pydantic-ai agent that writes the autopilot script, plus the `ai()` helper.

`write_pilot` asks a capable model for a complete Python script and returns its source;
`ask_helper` is the cheap, fast model the script itself can call mid-flight. The models
are picked with `THRUST_PILOT_MODEL` and `THRUST_HELPER_MODEL` (pydantic-ai model names
such as `anthropic:claude-sonnet-5` or `openai:gpt-5`).
"""

from __future__ import annotations

import inspect
import os
import re

from pydantic_ai import Agent

from thrust_server.autopilot import SANDBOX_TYPES, PilotMemory, RunReport

PILOT_MODEL = os.environ.get('THRUST_PILOT_MODEL', 'gateway/anthropic:claude-sonnet-5')
HELPER_MODEL = os.environ.get('THRUST_HELPER_MODEL', 'gateway/anthropic:claude-haiku-4-5-20251001')

DEFAULT_INSTRUCTIONS = (
    'Take off from the launch pad, fly to the landing pad and land on it as quickly as you can.'
    ' Time to touchdown is the score, but a crash scores nothing, so land reliably first'
    ' and fast second.'
)


def api_stub() -> str:
    """The sandbox API as the script sees it: the dataclasses' source plus the two functions."""
    classes = '\n\n'.join(inspect.getsource(cls) for cls in SANDBOX_TYPES)
    return f'''{classes}

async def update(move: Move) -> Status:
    """Apply `move` for exactly one physics tick and return the rocket afterwards.

    This is the only way to control the rocket and the only way simulated time advances.
    Call it in a loop until the returned status is no longer "flying".
    """

async def ai(query: str) -> str:
    """Ask a small, fast general-purpose language model. Optional.

    It knows nothing about this game or this flight beyond what you put in `query`.
    It takes a second or two, during which the simulation keeps running (see below).
    """

# Globals describing this flight, set before your script starts:
status: Status        # the rocket at the start: resting upright on the launch pad, v = 0
pad: Pad              # the landing pad (the target)
launch_pad: Pad       # where the rocket starts
terrain: list[tuple[float, float]]  # polyline (x, y), x strictly increasing from 0 to world.width
world: World
physics: Physics
'''


SYSTEM_PROMPT = f"""\
You write the autopilot for a 2D rocket lander game. Reply with exactly one Python script in
a ```python fenced block. It is run unchanged in a sandbox and flies one flight from start
to finish; nothing outside the fence is used.

## How the script runs

- The sandbox is pydantic-monty, a restricted Python interpreter. Available modules:
  `math` (but no `math.hypot`; use `math.sqrt(dx*dx + dy*dy)`), `json`, `dataclasses`,
  `typing`, `collections`. There is no `random`, `time`, `os`, `bisect` or anything else.
  Functions, plain classes, closures, comprehensions, try/except and f-strings all work.
  Top-level `await` is allowed and expected; do not wrap the script in `asyncio.run`.
- Not implemented, so never use them: `%` string formatting and `str.format` (only
  f-strings like `f"{{x:.1f}}"`), `yield`, `match`, class inheritance (so no custom
  exception classes; raise `ValueError` etc.), and arithmetic on booleans (`right - left`
  or `thrust * 9` fail; write `(1 if right else 0)`). A script that trips over one of these
  crashes before it has flown a metre.
- These names are predefined. Do not define, import or shadow them:

```python
{api_stub()}
```

- `await update(move)` is one tick of `physics.dt` seconds. The script must keep calling it,
  computing the next move from the returned `Status`, until the status is not `"flying"`,
  and then finish. A flight is thousands of ticks, so keep the per-tick work small and
  never sleep or busy-wait.
- The simulation does not pause while the script thinks. If more than about half a second
  passes between two `update` calls the rocket flies on with no input, and if the script
  takes more than 15 s it is abandoned. So call `ai()` only where a pause is harmless: on
  the launch pad before the first `update`, or while hovering high above any terrain.
- `print()` output is collected and shown to you after the flight. Print a compact status
  line about once a second (every 60 ticks) and at phase changes; it is your only telemetry.
- An uncaught exception ends the script; the rocket then drifts uncontrolled. Guard
  divisions and list indexing.

## The world

Units are metres and seconds, y is up, angles are radians with 0 = nose up and positive =
clockwise (nose to the right). `status.x, status.y` is the centre of the body. The body
has three corners: the nose tip `0.6 * rocket_height` above the centre along the body axis
and two base corners `0.4 * rocket_height` below it, `rocket_half_base` either side. The
terrain is a polyline; the ground height at any x is the linear interpolation between the
two surrounding points (write a helper for it). Pads are flat parts of the terrain. Leaving
the world sideways is a crash. There is no ceiling, but the world is `world.height` tall and
mountains can reach a good fraction of that, so plan a cruising altitude that clears every
peak between you and the pad with margin. The rocket starts upright at rest on the launch
pad, already `"flying"`, and the clock is running.

## Physics, exactly as the game computes each tick

```
torque = rotation_accel * ((1 if right else 0) - (1 if left else 0))
angular_velocity += (torque - angular_damping * angular_velocity) * dt
angular_velocity = clamp(angular_velocity, -max_angular_velocity, max_angular_velocity)
angle += angular_velocity * dt
ax = wind_drag * (wind_x - vx) + (sin(angle) * thrust_accel if thrust else 0)
ay = wind_drag * (wind_y - vy) - gravity + (cos(angle) * thrust_accel if thrust else 0)
vx += ax * dt;  vy += ay * dt;  x += vx * dt;  y += vy * dt
```

Thrust and rotation are on/off, so hovering means pulsing the engine on roughly
`gravity / thrust_accel` of the ticks, and a nose tilt of `angle` gives a sideways
acceleration of `thrust_accel * sin(angle)` while thrusting. Rotation has inertia and
damping: to hold an angle, steer the angular velocity toward `k * (target - angle)` and
apply left/right with a dead band, rather than flipping the inputs every tick.

## Landing rules

Contact happens when any corner is at or below the terrain. It counts as a landing only
if both base corners are within `pad.x1..pad.x2`, `|angle| < landing_max_angle`,
`|vy| < landing_max_vy` and `|vx| < landing_max_vx`. The same gentle contact on the launch
pad just rests there (still flying). Any other contact is a crash. So arrive above the
pad centre, upright, slow, and descend under control for the last few metres.

## A good approach

Cascaded control: decide a desired velocity from where you are (climb to cruise
altitude, cross to the pad, descend), turn the velocity error into a desired
acceleration, add gravity and cancel the wind drag to get the thrust vector you need,
point the nose along it (limit the tilt near the ground) and fire when the acceleration
needed along the nose exceeds about half of `thrust_accel`. Slow the descent as the
ground gets close and hold position if crosswind pushes you off the pad. Fast times
come from committing to a decisive climb and crossing, not from a timid hover.

Skeleton:

```python
import math

def ground(x):
    ...  # interpolate `terrain`

s = status
while s.status == "flying":
    move = Move(thrust=..., left=..., right=...)
    s = await update(move)
    if s.tick % 60 == 0:
        print(f"t={{s.time:.1f}} x={{s.x:.1f}} y={{s.y:.1f}} vx={{s.vx:.1f}} vy={{s.vy:.1f}}")
print("finished", s.status, f"{{s.time:.1f}} s")
```
"""

pilot_agent: Agent[None, str] = Agent(
    PILOT_MODEL, name='thrust-pilot', instructions=SYSTEM_PROMPT, defer_model_check=True
)

helper_agent: Agent[None, str] = Agent(
    HELPER_MODEL,
    name='thrust-pilot-helper',
    instructions=(
        'You are a quick assistant for an autopilot script flying a 2D rocket lander game.'
        ' The script runs in a sandbox and cannot show you anything beyond its question.'
        ' Answer briefly in plain text with concrete numbers or decisions; no markdown.'
    ),
    defer_model_check=True,
)


def flight_prompt(instructions: str, memory: PilotMemory) -> str:
    """The user prompt for one flight: the goal plus what happened last time."""
    parts = [f'Goal: {instructions}']
    previous = memory.history[:-1]
    if previous:
        summary = '\n'.join(f'- flight {i + 1}: {run.outcome}' for i, run in enumerate(previous))
        parts.append(f'Earlier flights:\n{summary}')
    last = memory.last_run
    if last is None:
        parts.append('This is the first flight; there is no previous run to learn from.')
    else:
        parts.append(
            f'Previous flight (flight {len(memory.history)}): {last.outcome}\n\n'
            f'Its script:\n```python\n{last.code}\n```'
        )
        if last.error:
            parts.append(f'It raised this error:\n```\n{last.error}\n```')
        if last.output:
            parts.append(f'Last lines it printed:\n```\n{last.output}\n```')
        parts.append(
            'Fix what went wrong and improve on it, or rewrite if the approach was flawed.'
            ' Reply with the complete new script.'
        )
    return '\n\n'.join(parts)


CODE_BLOCK = re.compile(r'```(?:python|py)?[ \t]*\n(.*?)```', re.DOTALL)


def extract_code(text: str) -> str:
    """The longest fenced code block, or the whole reply if it has none."""
    blocks = CODE_BLOCK.findall(text)
    if not blocks:
        return text.strip()
    return max(blocks, key=len).strip()


async def write_pilot(instructions: str, memory: PilotMemory) -> str:
    result = await pilot_agent.run(flight_prompt(instructions, memory))
    return extract_code(result.output)


async def ask_helper(query: str) -> str:
    result = await helper_agent.run(query)
    return result.output


__all__ = [
    'DEFAULT_INSTRUCTIONS',
    'HELPER_MODEL',
    'PILOT_MODEL',
    'RunReport',
    'ask_helper',
    'extract_code',
    'flight_prompt',
    'helper_agent',
    'pilot_agent',
    'write_pilot',
]
