import os
import uuid

import psycopg
import pytest

from stock_trader.config import Settings
from stock_trader.storage import Store


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        _secrets_dir=None,
        data_dir=tmp_path / "data",
        artifact_dir=tmp_path / "artifacts",
        report_dir=tmp_path / "reports",
    )


@pytest.fixture
async def store():
    if os.environ.get("TEST_DB") != "1":
        pytest.skip("integration test requires TEST_DB=1 and a disposable PostgreSQL database")
    base = Settings()
    name = "test_" + uuid.uuid4().hex[:16]
    conn = await psycopg.AsyncConnection.connect(**base.db_kwargs(), autocommit=True)
    await conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
    database = Store(base.model_copy(update={"db_name": name}))
    await database.open()
    try:
        yield database
    finally:
        await database.close()
        await conn.execute(
            psycopg.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(psycopg.sql.Identifier(name))
        )
        await conn.close()
