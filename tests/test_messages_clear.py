"""消息流水清空接口测试。"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.db import Database
from app.web import create_app


class StubManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def sync(self) -> None:
        pass

    async def stop_all(self) -> None:
        pass

    def status(self) -> list[dict[str, Any]]:
        return []


async def test_clear_messages_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "")
    monkeypatch.setattr(settings, "admin_allow_remote", False)
    db = Database(tmp_path / "messages.db")
    await db.init()
    await db.log_message(account_id=1, chat_id="c1", sender_id="u1", sender_name="买家", content="一", kind="incoming")
    await db.log_message(account_id=1, chat_id="c1", sender_id="u1", sender_name="买家", content="二", kind="outgoing")

    app = create_app(db=db, manager=StubManager(db))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            res = await ac.delete("/api/messages")
            assert res.status_code == 200
            assert res.json()["deleted"] == 2
            assert (await ac.get("/api/messages")).json() == []
            assert (await ac.get("/api/orders")).json() == []
