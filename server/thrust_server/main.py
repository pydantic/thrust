from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import logfire
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from pydantic_monty import AsyncMonty

from thrust_server import agent
from thrust_server.autopilot import (
    MAX_ATTEMPTS,
    NaivePilot,
    Pilot,
    PilotMemory,
    PilotScript,
    ScriptPilot,
    make_plan,
)
from thrust_server.models import Plan, State

if TYPE_CHECKING:
    from pydantic_ai import AgentRunResult

logger = logging.getLogger(__name__)


def pilot_mode() -> str:
    """`agent` (default) runs LLM-written scripts, `naive` the hand-written controller."""
    return os.environ.get('THRUST_PILOT', 'agent')


def memory_file() -> Path:
    """Where the agent's conversation and flight reports persist between runs."""
    return Path(os.environ.get('THRUST_MEMORY_FILE', 'pilot_memory.json'))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # uvicorn only configures its own loggers; make the pilot's script and outcome visible.
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(name)s: %(message)s')
    async with AsyncMonty() as pool:
        app.state.monty = pool
        app.state.memory = PilotMemory.load(memory_file())
        # The script written by the last `GET /plan`, consumed by the next `/ws` connection.
        app.state.plan = None
        yield


app = FastAPI(title='thrust-server', lifespan=lifespan)
# The game is served from another origin (Vite on :5173), so `GET /plan` needs CORS.
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['GET'])

# Traces go to Logfire when LOGFIRE_TOKEN is set; otherwise they stay local.
logfire.configure(send_to_logfire='if-token-present', service_name='thrust-server')
logfire.instrument_fastapi(app)
logfire.instrument_pydantic_ai()


@app.get('/health')
async def health() -> dict[str, bool]:
    return {'ok': True}


@app.get('/plan')
async def plan(request: Request) -> Plan:
    """Have the agent write (and pre-check) the script for the next flight."""
    state = request.app.state
    if pilot_mode() == 'naive':
        state.plan = None
        return Plan(strategy='The hand-written controller flies this one.')
    pool: AsyncMonty = state.monty
    memory: PilotMemory = state.memory
    try:
        result = await make_plan(pool, memory, agent.write_pilot, agent.ask_helper)
    except Exception as exc:
        logger.exception('the agent could not write a script')
        raise HTTPException(503, f'the agent could not write a script: {exc}') from exc
    if result is None:
        detail = f'no script passed the pre-flight check in {MAX_ATTEMPTS} attempts'
        raise HTTPException(503, detail)
    state.plan = result
    return Plan(strategy=result.output.strategy)


def take_pilot(websocket: WebSocket) -> Pilot:
    """The controller for this connection: the planned script if there is one, else naive."""
    state = websocket.app.state
    planned: AgentRunResult[PilotScript] | None = state.plan
    state.plan = None
    if planned is None or pilot_mode() == 'naive':
        logger.info('no plan for this connection, flying the naive policy')
        return NaivePilot()
    pool: AsyncMonty = state.monty
    memory: PilotMemory = state.memory
    return ScriptPilot(pool, memory, planned, agent.ask_helper)


@app.websocket('/ws')
async def ws(websocket: WebSocket) -> None:
    """One flight per connection: the client connects after `GET /plan` and hangs up after."""
    await websocket.accept()
    logger.info('client connected')
    pilot = take_pilot(websocket)
    memory: PilotMemory = websocket.app.state.memory
    first = True
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                state = State.model_validate_json(raw)
            except ValidationError:
                logger.warning('invalid state message', exc_info=True)
                continue
            if first:
                first = False
                memory.last_state = state
            move = await pilot.decide(state)
            await websocket.send_text(move.model_dump_json())
    except WebSocketDisconnect:
        logger.info('client disconnected')
    finally:
        await pilot.close()
