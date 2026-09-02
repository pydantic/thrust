import pytest
from pydantic_ai import models

# Tests must never reach a real model; agents are swapped for FunctionModel/TestModel.
models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'
