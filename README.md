# Thrust

A small rocket lander. The rocket starts resting on a launch pad and has to reach
the green landing pad. Arrow keys fly the rocket, a random world is generated on
every spawn, and random wind pushes the rocket around. With AI control enabled,
every physics tick the game sends its state over a WebSocket to a FastAPI server,
which replies with a move from the controller in `server/thrust_server/naive_policy.py`.

## Controls

- Up arrow: thrust
- Left / right arrows: rotate
- R or space: open the "Start new game" dialog; space in the dialog starts a new game

The dialog reports how long the last flight took, which is the number to beat,
and shows its seed. "Replay seed" reruns whatever seed is in the field; terrain,
pads and wind all come from the seed alone (for a given window width).

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

## Checks

```sh
pnpm typecheck && pnpm lint && pnpm build
cd server && uv run ruff check . && uv run ruff format --check . && uv run basedpyright && uv run pytest
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

## Controller

`Policy` in `server/thrust_server/naive_policy.py` is a two-phase cascaded controller.
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
