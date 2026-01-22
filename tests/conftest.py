import pytest
import pytest_asyncio
from typing import AsyncGenerator
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.core.config import settings

# Override settings for tests
settings.API_SECRET_KEY = "test_api_key"

# Python 3.14+: Use pytest_asyncio.fixture for async fixtures
# This ensures proper event loop management

@pytest_asyncio.fixture(scope="module")
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client for testing FastAPI endpoints."""
    headers = {"X-API-Key": "test_api_key"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as c:
        yield c
