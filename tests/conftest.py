"""测试公共夹具。"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.db import Database


@pytest.fixture(autouse=True)
def _disable_stale_guard(monkeypatch):
    """测试用固定历史时间戳构造消息；旧会话保护在专门用例里单独打开。"""
    monkeypatch.setattr(settings, "message_stale_seconds", 0)


@pytest.fixture()
def db(tmp_path):
    """临时 SQLite 数据库，初始化好表结构后交给测试。"""
    database = Database(tmp_path / "test.db")
    asyncio.run(database.init())
    return database
