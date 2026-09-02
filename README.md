# Thrust

A small rocket lander. The rocket starts resting on a launch pad and has to reach
the green landing pad. Arrow keys fly the rocket, a random world is generated on
every spawn, and random wind pushes the rocket around. Every physics tick the
game sends its state over a WebSocket to a FastAPI server, which replies with a
move. The server's policy is currently a no-op; the idea is to replace it with a
real control algorithm.

## Controls

- Up arrow: thrust
- Left / right arrows: rotate
- R or space: open the "Start new game" dialog; space in the dialog starts the game

The dialog has an "Enable AI control" checkbox. When ticked the game connects to
the server and the applied move is the union of held keys and the latest server
move; when unticked no connection is made and only the keyboard steers. The
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
  "world": {"width": 160, "height": 100, "gravity": 4}
}
```

Server to client:

```json
{"type": "move", "thrust": true, "left": false, "right": false}
```

The policy lives in `server/thrust_server/policy.py`; the TypeScript types are in
`src/protocol.ts` and the pydantic models in `server/thrust_server/models.py`.

## Physics

Fixed 60 Hz timestep. Gravity 4 m/s², thrust 9 m/s² along the nose, rotation
6 rad/s² with damping. Wind applies a linear drag toward the local wind velocity.
A landing counts if both base corners are on the pad, the rocket is within 0.28 rad
of upright, and the vertical and horizontal speeds are under 5 and 3.2 m/s.
