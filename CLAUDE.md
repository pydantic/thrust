# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A canvas rocket-lander game (TypeScript + Vite, no runtime deps) that streams its state each physics
tick over a WebSocket to a FastAPI server, which replies with a move. The controller is `Policy` in
`server/thrust_server/naive_policy.py` (one instance per connection; it keeps a transit/descent phase). The
game is fully playable without the server.

## Commands

`make help` lists everything; `make main` runs format, lint, typecheck and test for both halves. The
same recipes back the prek/pre-commit hooks in `.pre-commit-config.yaml` (`make install` sets them
up), so a commit fails on anything `make lint` or `make typecheck` would reject.

Client (run from the repo root, pnpm):

```sh
pnpm dev                 # Vite dev server on http://localhost:5173
pnpm typecheck           # tsc --noEmit (strict, noUncheckedIndexedAccess, exactOptionalPropertyTypes)
pnpm lint                # biome check .   (add --write to autofix formatting)
pnpm build               # typecheck + vite build to dist/
```

Server (run from `server/`, uv):

```sh
uv sync
uv run uvicorn thrust_server.main:app --reload --port 8000
uv run ruff check . && uv run ruff format --check .
uv run basedpyright      # strict mode
uv run pytest            # uv run pytest tests/test_ws.py::test_ws_returns_move  for one test
```

Ruff runs with `select = ["ALL"]`; per-file ignores live in `server/pyproject.toml`. The test client
uses `httpx2` (Starlette deprecated `httpx` for its TestClient).

Biome only covers the TS side (`server/` is excluded in `biome.json`). There are no JS tests; physics
and world generation were verified with ad-hoc `tsx` simulations that call `step`/`generateWorld`
directly, which is the quickest way to check a physics change without a browser. The same trick
verifies the controller end to end: a `.mts` script that runs `step` and, each tick, sends the
`StateMessage` to a running server over `WebSocket` (built into Node) and applies the reply. The
simulation constants live in one `PHYSICS` object in `src/physics.ts` and are sent in every state
message, so the server reads them from `state.physics` rather than keeping copies.

## Architecture

**Units and conventions.** Everything is in world metres with y up, angle in radians where 0 is
nose-up and positive is clockwise. The world is 100 m tall; its width is derived from the viewport
aspect ratio at spawn (`worldWidthFor` in `src/world.ts`), so a window resize respawns. The renderer
is the only place that flips to screen pixels (`Renderer.toScreen`).

**Protocol is duplicated in two places on purpose.** `src/protocol.ts` (TS types + `isMoveMessage`
guard) and `server/thrust_server/models.py` (pydantic) must stay in sync; JSON keys are camelCase
(`launchPad`, `angularVelocity`), with `Field(alias=...)` on the Python side. The README documents the
wire format.

**Ping-pong pacing.** `src/client.ts` sends a state only when no reply is outstanding, so the server
sees at most one in-flight request and the socket never backs up. A server move older than 500 ms is
discarded. Inputs merge per axis in `src/main.ts`: thrust is keys OR server move, while a held
rotation key replaces the server's rotation so the player can overrule the AI. The
client only exists while "Enable AI control" is ticked in the start dialog (`src/dialog.ts`, an HTML
overlay in `index.html`); unticking it closes the socket. The game loop pauses while the dialog is
open, and a landed/crashed outcome reopens it after a short delay.

**Physics (`src/physics.ts`)** is a hand-rolled rigid body at a fixed 60 Hz (`DT`), not Box2D.
Wind is linear drag toward the local wind velocity. Ground contact tests the three triangle
vertices in `ROCKET_VERTICES` against `terrainHeightAt`; the drawn rocket shape is separate
(`Renderer.rocketPath`). Contact has three outcomes: gentle on the landing pad → `landed`; gentle on
the launch pad → keep resting (status stays `flying`, position pinned); anything else → `crashed`.
Landing thresholds are the `LANDING_MAX_*` constants.

**World generation (`src/world.ts`)** is seeded (`mulberry32`; seed shown in the HUD and replayable
from the dialog). Random draws are made in a width-independent order (terrain heights for the maximum
width first, then positions as fractions of the real width) so a seed gives the same terrain profile
at any viewport size and an identical game at the same size; the wind field is drawn from the same
RNG afterwards. Terrain is a mean-reverting random walk; the launch pad is flattened in at the spawn,
and the landing pad is the lowest of several candidates at least `MIN_PAD_SEPARATION` (40%) of the
width away. Both flats are spliced into the polyline by `withFlats`; terrain x is strictly
increasing, which `terrainHeightAt`'s binary search relies on.

**Wind (`src/wind.ts`)** is a per-spawn base vector plus low-frequency sine gusts capped below the
base speed so it never reverses. The render-side wind field is purely visual: particles advected by
`windAt` in `Renderer.drawWind`.
