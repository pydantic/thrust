# Thrust

A small rocket lander. The rocket starts resting on a launch pad and has to reach
the green landing pad. Arrow keys fly the rocket, a random world is generated on
every spawn, and random wind pushes the rocket around. With the auto-pilot enabled,
every physics tick the game sends its state over a WebSocket to a FastAPI server,
which replies with a move. By default the server asks an LLM to write an autopilot
script for each flight and runs it in a sandbox; see [Autopilot](#autopilot).

## Controls

- Up arrow: thrust
- Left / right arrows: rotate
- R or space: open the "New game" dialog; space in the dialog starts a new game

On a fresh load without `?seed=N` the dialog is minimal: the two checkboxes and a
"Start" button that flies the map already on screen. Once a seed is pinned in the
URL (which happens as soon as a game starts) the dialog also offers the seed field,
"Replay" and "New game".

The dialog reports how long the last flight took, which is the number to beat,
and shows its seed. "Replay" (shown once a seed is in the URL) reruns whatever seed
is in the field; terrain,
pads and wind all come from the seed alone (for a given window width). The seed
of the current game is kept in the URL as `?seed=N`, so reloading or sharing
the link brings up the same game.

A flight that is still going after 90 s ends as "Out of time", which counts as a
failure. With "Enable auto-replay" ticked (always shown, off by default and not
remembered), a finished flight restarts the same map after a moment instead of
opening the dialog, so the auto-pilot can iterate on one seed hands-free; R brings
the dialog back.

The dialog has an "Enable auto-pilot" checkbox. When ticked the game connects to
the server and applies its moves, but you can still fly: the up arrow adds
thrust on top of the auto-pilot's, and holding left or right takes the rotation axis
away from the auto-pilot while the key is down. When unticked no connection is made and
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

The server is only contacted when the auto-pilot is enabled. Set `VITE_SERVER_URL`
(default `http://localhost:8000`) to point the client at a different server; the
client derives `GET /plan` and the `/ws` socket from it.

The default controller needs an API key for the model provider (for example
`OPENAI_API_KEY`). Without one `GET /plan` fails and the game shows the error in the
dialog. Environment variables:

| Variable              | Default                                | Meaning                                       |
| --------------------- | -------------------------------------- | --------------------------------------------- |
| `THRUST_PILOT_MODEL`  | `openai:gpt-5.6-terra`                 | Model that writes the script (pydantic-ai id) |
| `THRUST_HELPER_MODEL` | `openai:gpt-5.6-terra`                 | Model behind the script's `ai()`              |
| `THRUST_MEMORY_FILE`  | `pilot_memory.json`                    | Where the agent's conversation is persisted   |
| `LOGFIRE_TOKEN`       | unset                                  | Send FastAPI and pydantic-ai traces to Logfire |

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

If the script raises, the server answers the next state with an abort instead of a
move and the game ends the flight with status `aborted`, showing the exception:

```json
{"type": "abort", "reason": "AttributeError: 'Physics' object has no attribute 'gravity'"}
```

Before connecting, the client calls `GET /plan`, which has the agent write and
pre-check the script for the next flight and answers with the script's own summary,
shown under the HUD for the whole run:

```json
{"strategy": "Climb to 70 m, cross, then descend over the pad."}
```

A `503` with a `detail` string means no usable script could be produced. Each
websocket connection flies exactly one flight with the most recent plan; the client
hangs up after the flight and plans again before the next one. While the plan request is live the game is held
and the HUD's `pilot` line reads "planning flight".

The TypeScript types are in `src/protocol.ts` and the pydantic models in
`server/thrust_server/models.py`.

## Autopilot

`server/thrust_server/agent.py` holds a [pydantic-ai](https://ai.pydantic.dev) agent
that writes a complete Python script for one flight. Its instructions describe the
game, the exact physics, the goal and the sandbox API. The agent holds one conversation
across all flights: the first message asks for a script, each script is submitted
through a `start_flight` tool call, and the flight's outcome (how it ended, any
traceback, the last lines printed) goes back as that tool call's result. `GET /plan`
writes the script for the next flight and the following websocket connection flies
it, so a session is a loop of plan, fly, report, with the opening message and the
last six script/result pairs kept in the history. The conversation and the flight
reports are written to `THRUST_MEMORY_FILE` (indented JSON, relative to `server/`) after
every change and loaded on start, so restarting the server, including uvicorn's reloads,
keeps iterating on the same script. Delete the file to start over.

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
status is no longer `"flying"`. Before a script flies for real, `GET /plan` runs it for a few
synthetic ticks against the first state of the previous flight; a script that raises
or exits during that check is sent back to the writer with the error (up to three
attempts), after which the request fails with a 503 and the game shows the error. A script that dies mid-flight leaves the rocket idle; the error
lands in the next prompt.

A websocket connection made without a plan (nothing called `GET /plan`, or the plan
has already flown) is closed with code 1008 and the reason "no plan: call GET /plan
first"; the game shows that as a failed flight.

## Physics

Fixed 60 Hz timestep. Gravity 4 m/s², thrust 9 m/s² along the nose, rotation
6 rad/s² with damping. Wind applies a linear drag toward the local wind velocity.
Flights are capped at 90 s (`maxFlightTime`), after which the status becomes `timeout`.
A landing counts if both base corners are on the pad, the rocket is within 0.28 rad
of upright, and the vertical and horizontal speeds are under 5 and 3.2 m/s. The walls
are hard: any corner of the rocket touching a side of the world or its top is a crash.
