# Thrust

A small rocket lander. The rocket starts resting on a launch pad and has to reach
the green landing pad. Arrow keys fly the rocket, a random world is generated on
every spawn, and random wind pushes the rocket around. With AI control enabled,
every physics tick the game sends its state over a WebSocket to a FastAPI server,
which replies with a move. By default the server asks an LLM to write an autopilot
script for each flight and runs it in a sandbox; see [Autopilot](#autopilot).

## Controls

- Up arrow: thrust
- Left / right arrows: rotate
- R or space: open the "Start new game" dialog; space in the dialog starts a new game

The dialog reports how long the last flight took, which is the number to beat,
and shows its seed. "Replay seed" reruns whatever seed is in the field; terrain,
pads and wind all come from the seed alone (for a given window width). The seed
of the current game is kept in the URL as `?seed=N`, so reloading or sharing
the link brings up the same game.

The dialog has an "Enable AI control" checkbox. When ticked the game connects to
the server and applies its moves, but you can still fly: the up arrow adds
thrust on top of the AI's, and holding left or right takes the rotation axis
away from the AI while the key is down. When unticked no connection is made and
only the keyboard steers. If the
server cannot be reached within a second the dialog reopens with an error. The
choice is remembered in localStorage.

## Running

Client (Vite dev server on http://localhost:5173):

```sh
pnpm install
pnpm dev
```

Server (FastAPI on ws://localhost:8000/ws):

```sh
cd server
uv sync
uv run uvicorn thrust_server.main:app --reload --port 8000
```

The server is only contacted when AI control is enabled. Set `VITE_WS_URL` to
point the client at a different server.

The default controller needs an API key for the model provider (for example
`ANTHROPIC_API_KEY`). Without one the server logs an error and falls back to the
hand-written controller for that flight. Environment variables:

| Variable              | Default                                | Meaning                                       |
| --------------------- | -------------------------------------- | --------------------------------------------- |
| `THRUST_PILOT`        | `agent`                                | `agent` (LLM-written scripts) or `naive`      |
| `THRUST_PILOT_MODEL`  | `anthropic:claude-sonnet-5`            | Model that writes the script (pydantic-ai id) |
| `THRUST_HELPER_MODEL` | `anthropic:claude-haiku-4-5-20251001`  | Cheap model behind the script's `ai()`        |
| `THRUST_INSTRUCTIONS` | land on the pad as fast as possible    | The goal given to the script writer           |

## Checks

`make install` installs both sets of dependencies and the pre-commit hooks (via
[prek](https://github.com/j178/prek)), which run formatting, linting and
type-checking on every commit. `make help` lists the recipes; `make main` runs
everything.

```sh
make format      # biome + ruff, with fixes
make lint        # biome check, ruff, basedpyright
make typecheck   # tsc
make test        # pytest
```

## Protocol

Client to server, one message per physics tick (60 Hz) but only when no reply is
outstanding. Units are world metres with y up; angle is radians, 0 pointing up,
positive clockwise.

```json
{
  "type": "state",
  "tick": 123,
  "status": "flying",
  "rocket": {"x": 80, "y": 60, "vx": 1.2, "vy": -3.4, "angle": 0.1, "angularVelocity": 0},
  "wind": {"x": 3.1, "y": 0.2},
  "pad": {"x1": 20, "x2": 32, "y": 15},
  "launchPad": {"x1": 100, "x2": 110, "y": 20},
  "terrain": [[0, 10], [4, 12], ...],
  "world": {"width": 160, "height": 100, "gravity": 4},
  "physics": {"dt": 0.0167, "thrustAccel": 9, "rotationAccel": 6, "angularDamping": 2.5,
              "maxAngularVelocity": 3, "windDrag": 0.15, "rocketHeight": 4, "rocketHalfBase": 1.25,
              "landingMaxAngle": 0.28, "landingMaxVy": 5, "landingMaxVx": 3.2}
}
```

Server to client:

```json
{"type": "move", "thrust": true, "left": false, "right": false}
```

The TypeScript types are in `src/protocol.ts` and the pydantic models in
`server/thrust_server/models.py`.

## Autopilot

`server/thrust_server/agent.py` holds a [pydantic-ai](https://ai.pydantic.dev) agent
that writes a complete Python script for one flight. Its prompt describes the game,
the exact physics and the sandbox API; the user prompt carries the goal plus, from the
second flight on, the previous script, how that flight ended, any traceback it raised
and the last lines it printed. Every new flight (a new connection, or a restart on the
same connection) gets a freshly written script, so a session is a loop of fly, report,
rewrite.

The script runs in a [pydantic-monty](https://github.com/pydantic/monty) sandbox
(`server/thrust_server/autopilot.py`). It starts with the initial `status`, the
`terrain`, both pads, `world` and `physics` bound as globals and can call two host
functions:

```python
async def update(move: Move) -> Status   # apply the move for one tick, get the next state
async def ai(query: str) -> str          # ask the cheap helper model mid-flight
```

`update` hands the move to the game and waits for the next state message, so one
call is exactly one physics tick and the script keeps running until the returned
status is no longer `"flying"`. Before a script flies for real it is run for a few
synthetic ticks; a script that raises or exits during that check is sent back to
the writer with the error (up to three attempts), after which the naive controller
takes the flight. A script that dies mid-flight leaves the rocket idle; the error
lands in the next prompt.

The hand-written fallback, `Policy` in `server/thrust_server/naive_policy.py`
(`THRUST_PILOT=naive` to use it always), is a two-phase cascaded controller.
In transit it climbs to a cruise altitude that clears every mountain between the
rocket and the pad, then flies toward the pad centre, only moving sideways once
the terrain ahead is clear. Near the pad it switches to descent and comes straight
down, slowing as it gets close and pausing the descent if crosswind pushes it off
centre. Each tick it builds a desired acceleration from velocity errors, gravity
and the wind drag, points the nose along it (tilt limited near the ground) and
fires the engine when the acceleration needed along the nose exceeds half of what
the engine gives. The simulation constants it needs (thrust, drag, rocket size)
arrive in the `physics` block of every state message, so there is nothing to keep
in sync with the client.

## Physics

Fixed 60 Hz timestep. Gravity 4 m/s², thrust 9 m/s² along the nose, rotation
6 rad/s² with damping. Wind applies a linear drag toward the local wind velocity.
A landing counts if both base corners are on the pad, the rocket is within 0.28 rad
of upright, and the vertical and horizontal speeds are under 5 and 3.2 m/s.
