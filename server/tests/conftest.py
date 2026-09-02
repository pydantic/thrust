from pathlib import Path

import pytest
from pydantic_ai import models

# Tests must never reach a real model; agents are swapped for FunctionModel/TestModel.
models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


@pytest.fixture(autouse=True)
def memory_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the server's pilot memory file out of the working directory."""
    monkeypatch.setenv('THRUST_MEMORY_FILE', str(tmp_path / 'pilot_memory.json'))
