import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from thrust_server.main import NO_PLAN_CLOSE_CODE, app


def test_health() -> None:
    with TestClient(app) as client:
        assert client.get('/health').json() == {'ok': True}


def test_ws_without_a_plan_is_refused() -> None:
    with (
        TestClient(app) as client,
        client.websocket_connect('/ws') as ws,
        pytest.raises(WebSocketDisconnect) as info,
    ):
        ws.receive_text()
    assert info.value.code == NO_PLAN_CLOSE_CODE
    assert info.value.reason == 'no plan: call GET /plan first'
