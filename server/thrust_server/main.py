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
    PilotMemory,
    PilotScript,
    fly_script,
    make_plan,
)
from thrust_server.models import Abort, Move, Plan, State

if TYPE_CHECKING:
    from pydantic_ai import AgentRunResult

logger = logging.getLogger(__name__)

NO_PLAN_CLOSE_CODE = 1008
"""WebSocket close code (policy violation) for a connection made without `GET /plan` first."""


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
logfire.instrument_monty()


@app.get('/health')
async def health() -> dict[str, bool]:
    return {'ok': True}


@app.get('/plan')
async def plan(request: Request) -> Plan:
    """Have the agent write (and pre-check) the script for the next flight."""
    state = request.app.state
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


def take_plan(websocket: WebSocket) -> AgentRunResult[PilotScript] | None:
    """The script planned for this connection, if any; each plan flies once."""
    state = websocket.app.state
    planned: AgentRunResult[PilotScript] | None = state.plan
    state.plan = None
    return planned


@app.websocket('/ws')
async def ws(websocket: WebSocket) -> None:
    """One flight per connection: the client connects after `GET /plan` and hangs up after."""
    await websocket.accept()
    state = websocket.app.state
    pool: AsyncMonty = state.monty
    memory: PilotMemory = state.memory

    async def next_state() -> State:
        while True:
            raw = await websocket.receive_text()
            try:
                return State.model_validate_json(raw)
            except ValidationError:
                logger.warning('invalid state message', exc_info=True)

    async def send_reply(reply: Move | Abort) -> None:
        await websocket.send_text(reply.model_dump_json())

    plan = take_plan(websocket)
    if plan is None:
        logger.warning('websocket without a plan, closing')
        await websocket.close(code=NO_PLAN_CLOSE_CODE, reason='no plan: call GET /plan first')
        return
    try:
        first = await next_state()
        memory.last_state = first
        result = await fly_script(
            pool,
            plan,
            agent.ask_helper,
            first_state=first,
            next_state=next_state,
            send_reply=send_reply,
        )
        report = result.report(plan.output.code)
        memory.record(report, plan)
        logfire.info('flight over: {outcome}', outcome=report.outcome, error=report.error)
        # The client keeps sending the final state for a moment before hanging up, which
        # ends this loop with a disconnect; outside the span so it is not recorded as an error.
        while True:
            await next_state()
            await send_reply(Move())
    except WebSocketDisconnect:
        logger.info('client disconnected')
