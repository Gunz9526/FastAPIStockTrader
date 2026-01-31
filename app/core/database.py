from collections.abc import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

# ========== ASYNC (FastAPI) ==========
# Create Async Engine
engine = create_async_engine(
    str(settings.DATABASE_URL),
    echo=False,  # Disable SQL echo, use logging.py configuration instead
    future=True,
    pool_pre_ping=True
)

# Async Session Factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# ========== SYNC (Celery) ==========
# Convert asyncpg URL to psycopg2
sync_db_url = str(settings.DATABASE_URL).replace('postgresql+asyncpg://', 'postgresql+psycopg2://')

# Create Sync Engine
sync_engine = create_engine(
    sync_db_url,
    echo=False,  # Disable SQL echo, use logging.py configuration instead
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10
)

# Sync Session Factory
SessionLocal = sessionmaker(
    bind=sync_engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False
)

class Base(DeclarativeBase):
    pass

async def get_async_session() -> AsyncGenerator[AsyncSession]:
    """Dependency for getting async database session (FastAPI)"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

def get_sync_session() -> Session:
    """Get sync database session (Celery)"""
    return SessionLocal()
