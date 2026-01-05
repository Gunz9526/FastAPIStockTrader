import pytest
import asyncio
from typing import AsyncGenerator, Generator
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.core.config import settings

# Override settings for tests
settings.API_SECRET_KEY = "test_api_key"

@pytest.fixture(scope="session")
def event_loop() -> Generator:
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="module")
async def client() -> AsyncGenerator[AsyncClient, None]:
    headers = {"X-API-Key": "test_api_key"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
        yield c
