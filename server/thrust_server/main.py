import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from thrust_server.models import State
from thrust_server.naive_policy import Policy

logger = logging.getLogger(__name__)

app = FastAPI(title="thrust-server")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    logger.info("client connected")
    policy = Policy()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                state = State.model_validate_json(raw)
            except ValidationError:
                logger.warning("invalid state message", exc_info=True)
                continue
            move = policy.decide(state)
            await websocket.send_text(move.model_dump_json())
    except WebSocketDisconnect:
        logger.info("client disconnected")
