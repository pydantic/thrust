import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from pydantic_monty import AsyncMonty

from thrust_server import agent
from thrust_server.autopilot import AgentPilot, NaivePilot, Pilot, PilotMemory
from thrust_server.models import State

logger = logging.getLogger(__name__)


def pilot_mode() -> str:
    """`agent` (default) runs LLM-written scripts, `naive` the hand-written controller."""
    return os.environ.get('THRUST_PILOT', 'agent')


def instructions() -> str:
    return os.environ.get('THRUST_INSTRUCTIONS', agent.DEFAULT_INSTRUCTIONS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # uvicorn only configures its own loggers; make the pilot's script and outcome visible.
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(name)s: %(message)s')
    async with AsyncMonty() as pool:
        app.state.monty = pool
        app.state.memory = PilotMemory()
        yield


app = FastAPI(title='thrust-server', lifespan=lifespan)


@app.get('/health')
async def health() -> dict[str, bool]:
    return {'ok': True}


def make_pilot(websocket: WebSocket) -> Pilot:
    if pilot_mode() == 'naive':
        return NaivePilot()
    state = websocket.app.state
    pool: AsyncMonty = state.monty
    memory: PilotMemory = state.memory
    return AgentPilot(
        pool,
        memory,
        instructions=instructions(),
        write_code=agent.write_pilot,
        ask_ai=agent.ask_helper,
    )


@app.websocket('/ws')
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.info('client connected')
    pilot = make_pilot(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                state = State.model_validate_json(raw)
            except ValidationError:
                logger.warning('invalid state message', exc_info=True)
                continue
            move = await pilot.decide(state)
            await websocket.send_text(move.model_dump_json())
    except WebSocketDisconnect:
        logger.info('client disconnected')
    finally:
        await pilot.close()
