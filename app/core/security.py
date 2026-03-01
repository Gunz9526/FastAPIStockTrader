import secrets

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_api_key(
    api_key: str | None = Security(_api_key_header),
) -> str:
    """Validate X-API-Key header using constant-time comparison."""
    if api_key is not None and secrets.compare_digest(api_key, settings.API_SECRET_KEY):
        return api_key
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Could not validate credentials",
    )
