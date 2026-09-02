# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A canvas rocket-lander game (TypeScript + Vite, no runtime deps) that streams its state each physics
tick over a WebSocket to a FastAPI server, which replies with a move. By default the client first
calls `GET /plan`, where a pydantic-ai agent (`server/thrust_server/agent.py`) writes and pre-checks a
Python autopilot script, then connects `/ws` for exactly one flight, which runs the script in a
pydantic-monty sandbox (`server/thrust_server/autopilot.py`); the script calls `await update(move)`
once per tick and `await ai(query)` for a cheap helper model. The client hangs up after each flight. `THRUST_PILOT=naive`
selects the hand-written `Policy` in `server/thrust_server/naive_policy.py` instead (one instance per
connection; it keeps a transit/descent phase), which is also the fallback when the agent cannot
produce a working script. The game is fully playable without the server.

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

**Protocol is duplicated in two places on purpose.** `src/protocol.ts` (TS types + `isMoveMessage` /
`isPlanResponse` guards) and `server/thrust_server/models.py` (pydantic) must stay in sync; JSON keys are camelCase
(`launchPad`, `angularVelocity`), with `Field(alias=...)` on the Python side. The README documents the
wire format.

**Ping-pong pacing.** `src/client.ts` sends a state only when no reply is outstanding, so the server
sees at most one in-flight request and the socket never backs up. A server move older than 500 ms is
discarded. The `strategy` from `GET /plan` is word-wrapped under the HUD for the run, and the game is
held (`planning`) until the plan and the connection are both in place. The HUD text is rebuilt at
most every 100 ms (`HUD_INTERVAL_MS` in `src/render.ts`).
Inputs merge per axis in `src/main.ts`: thrust is keys OR server move, while a held
rotation key replaces the server's rotation so the player can overrule the auto-pilot. The
client only exists while "Enable auto-pilot" is ticked in the start dialog (`src/dialog.ts`, an HTML
overlay in `index.html`); unticking it closes the socket. The dialog is minimal (the two checkboxes
plus "Start" for the map already shown) until the URL carries `?seed=N`, which `show()` checks each time.
Auto-replay is never persisted; auto-pilot is. The game loop pauses while the dialog is
open, and a landed/crashed/timeout/aborted outcome reopens it after a short delay, or respawns the same seed
when auto-replay is ticked. The 90 s flight cap (`PHYSICS.maxFlightTime`) is enforced in `src/main.ts`
where game time is counted, not in `step`. The socket survives restarts, so
the server side flies one flight per connection (`ScriptPilot`): the first state starts the planned
script and a terminal state, or the disconnect, records a `RunReport` in the process-wide
`PilotMemory`; a script exception ends the flight at once with an `{type: "abort"}` reply, which the
client turns into status `aborted`. Logfire (`logfire.configure` in `main.py`, console output without a
token) carries the structure: a `flight` span per connection, inside it a `pilot script` span around
the whole sandbox run (code and strategy as attributes, every printed line as a nested log, return
value or error and ticks controlled set when it ends), then a `flight over` log with the report. `make_plan` (behind `GET /plan`) asks the agent for the next script and stores the run
result in `app.state.plan` for the next connection; `memory.last_state` is what pre-flight checks use. The agent's output is a `start_flight` tool call; `PilotMemory`
(a pydantic model persisted to `THRUST_MEMORY_FILE`, default `server/pilot_memory.json`, on every
report) keeps the message history; `write_pilot` returns the `AgentRunResult`, which travels with the
`Flight`, and `record(report, result)` stores `all_messages(output_tool_return_content=report.feedback())`
so the report becomes that call's result. The next run continues the conversation with no new user
prompt, a restart resumes it, and a flight cut short by a restart is simply absent from it. The goal
lives in the agent's instructions; there is no per-run instructions parameter. A `ProcessHistory`
capability trims the history to the opening message plus the last few script/result pairs. Scripts pass a pre-flight check (a few synthetic ticks) before
they fly; the tests drive the real monty runtime with fake code writers and `FunctionModel`, and
`tests/conftest.py` blocks real model requests.

**Physics (`src/physics.ts`)** is a hand-rolled rigid body at a fixed 60 Hz (`DT`), not Box2D.
Wind is linear drag toward the local wind velocity. Ground contact tests the three triangle
vertices in `ROCKET_VERTICES` against `terrainHeightAt`; the drawn rocket shape is separate
(`Renderer.rocketPath`). Contact has three outcomes: gentle on the landing pad → `landed`; gentle on
the launch pad → keep resting (status stays `flying`, position pinned); anything else → `crashed`.
The walls (both sides and the top) are hard: any corner past them is a crash, checked before contact.
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
