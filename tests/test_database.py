from unittest.mock import AsyncMock

import pytest

import app.database as database


class _SessionContext:
    def __init__(self):
        self.rollback = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
async def test_get_db_rolls_back_and_propagates_exception(monkeypatch):
    session = _SessionContext()
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: session)
    dependency = database.get_db()

    assert await anext(dependency) is session

    with pytest.raises(RuntimeError, match="fallo controlado"):
        await dependency.athrow(RuntimeError("fallo controlado"))

    session.rollback.assert_awaited_once()

