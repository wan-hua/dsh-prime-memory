"""订单社交自动化（求小红花 / 提醒收货 / 评价）测试。"""

from __future__ import annotations

import time
from typing import Any

from app.client import XianyuClient
from app.config import settings
from app.db import Database
from app.social import SocialAutomation, social_event


class FakeSocialClient:
    def __init__(self, orders: list[dict[str, Any]], remind_msg: str = "SUCCESS") -> None:
        self.orders = orders
        self.remind_msg = remind_msg
        self.calls: list[tuple[str, str]] = []

    async def list_sold_orders(self) -> list[dict[str, Any]]:
        return self.orders

    async def require_flower(self, order_id: str) -> tuple[bool, str]:
        self.calls.append(("flower", order_id))
        return True, "SUCCESS"

    async def remind_receipt(self, order_id: str) -> tuple[bool, str]:
        self.calls.append(("remind", order_id))
        return self.remind_msg.upper() == "SUCCESS", self.remind_msg

    async def rate_buyer(self, order_id: str, feedback: str, rate: int = 1) -> tuple[bool, str]:
        self.calls.append(("rate", order_id))
        return True, "SUCCESS"

    async def receive_flower(self, order_id: str) -> tuple[bool, str]:
        self.calls.append(("receive_flower", order_id))
        return True, "SUCCESS"


def _now_ms() -> int:
    return int(time.time() * 1000)


async def test_social_actions_once_per_order_and_daily(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "social_auto_flower", True)
    monkeypatch.setattr(settings, "social_auto_remind", True)
    monkeypatch.setattr(settings, "social_auto_rate", True)
    monkeypatch.setattr(settings, "social_rate_content", "很好，合作愉快！")
    monkeypatch.setattr(settings, "social_since", "2026-01-01 00:00:00")

    db = Database(tmp_path / "social.db")
    await db.init()
    order = {
        "order_id": "o1",
        "status": "已发货",
        "seller_rate_status": 5,
        "create_time": "2026-10-27 11:00:00",
        "actions": {
            "REMIND_BUYER_TO_CONFIRM": {"name": "提醒收货"},
            "REQUIRE_FLOWER": {"name": "求小红花"},
        },
        "buyer_name": "买家",
    }
    client = FakeSocialClient([order])
    auto = SocialAutomation(db, client, 1, "主号")

    first = await auto.run_once()
    assert first == {"flower": 1, "remind": 1, "rate": 0}
    assert ("flower", "o1") in client.calls
    assert ("remind", "o1") in client.calls

    # 同一订单当天不重复求花/提醒
    second = await auto.run_once()
    assert second == {"flower": 0, "remind": 0, "rate": 0}

    # 交易成功后自动好评
    order["status"] = "交易成功"
    order["seller_rate_status"] = 5
    third = await auto.run_once()
    assert third["rate"] == 1
    assert ("rate", "o1") in client.calls


async def test_social_action_payloads(monkeypatch):
    client = XianyuClient("unb=1; _m_h5_tk=abc_1")
    calls: list[tuple[str, dict[str, Any], str]] = []

    async def fake_mtop(api: str, data: dict[str, Any], version: str = "1.0") -> dict[str, Any]:
        calls.append((api, data, version))
        return {"ret": ["SUCCESS::调用成功"], "data": {"module": {"issued": True}}}

    monkeypatch.setattr(client, "mtop", fake_mtop)
    assert (await client.require_flower("o1"))[0] is True
    assert (await client.remind_receipt("o1"))[0] is True
    assert (await client.rate_buyer("o1", "好评", rate=1))[0] is True
    assert (await client.receive_flower("o1"))[0] is True

    assert calls[0] == ("mtop.taobao.idlemessage.red.flower", {"orderId": "o1"}, "1.0")
    assert calls[1] == ("com.taobao.idle.trade.consign.remind", {"orderId": "o1"}, "1.0")
    rate_api, rate_data, rate_version = calls[2]
    assert rate_api == "mtop.taobao.idle.rate.create"
    assert rate_version == "4.0"
    assert rate_data["tradeId"] == "o1"
    assert rate_data["feedback"] == "好评"
    assert rate_data["rate"] == 1
    receive_api, receive_data, receive_version = calls[3]
    assert receive_api == "mtop.taobao.red.flower.seller.receive"
    assert receive_data["orderNo"] == "o1"
    assert receive_data["asac"] == "2A24124SCWTNPW40G6NQW9"
    assert receive_version == "1.0"


def test_social_event_classifier():
    assert social_event("[卡片消息]\n去查看\n买家已确认收货，交易成功", True) == "confirmed"
    assert social_event("你人真不错，送你闲鱼小红花\n立即收下", True) == "received_flower"
    assert social_event("[你已发货]", True) == "shipped"
    assert social_event("在吗", False) == ""
    assert social_event("买家已确认收货，交易成功", False) == ""


async def test_social_event_confirmation_triggers_flower_and_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "social_auto_flower", True)
    monkeypatch.setattr(settings, "social_auto_remind", False)
    monkeypatch.setattr(settings, "social_auto_rate", True)
    monkeypatch.setattr(settings, "social_rate_content", "很好")
    db = Database(tmp_path / "social3.db")
    await db.init()
    client = FakeSocialClient([])
    auto = SocialAutomation(db, client, 1, "主号")

    counts = await auto.on_event("o3", "confirmed")
    assert counts == {"flower": 1, "remind": 0, "rate": 1}
    assert ("flower", "o3") in client.calls
    assert ("rate", "o3") in client.calls


async def test_social_event_received_flower_is_claimed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "social_auto_receive_flower", True)
    monkeypatch.setattr(settings, "social_auto_flower", False)
    monkeypatch.setattr(settings, "social_auto_remind", False)
    monkeypatch.setattr(settings, "social_auto_rate", False)
    db = Database(tmp_path / "social5.db")
    await db.init()
    client = FakeSocialClient([])
    auto = SocialAutomation(db, client, 1, "主号")

    counts = await auto.on_event("o5", "received_flower")
    assert counts == {"flower": 1, "remind": 0, "rate": 0}
    assert ("receive_flower", "o5") in client.calls


async def test_social_event_shipped_triggers_flower_and_remind(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "social_auto_flower", True)
    monkeypatch.setattr(settings, "social_auto_remind", True)
    monkeypatch.setattr(settings, "social_auto_rate", False)
    db = Database(tmp_path / "social4.db")
    await db.init()
    client = FakeSocialClient([])
    auto = SocialAutomation(db, client, 1, "主号")

    counts = await auto.on_event("o4", "shipped")
    assert counts == {"flower": 1, "remind": 1, "rate": 0}


async def test_remind_already_today_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "social_auto_flower", False)
    monkeypatch.setattr(settings, "social_auto_remind", True)
    monkeypatch.setattr(settings, "social_auto_rate", False)
    monkeypatch.setattr(settings, "social_since", "2026-01-01 00:00:00")

    db = Database(tmp_path / "social2.db")
    await db.init()
    order = {
        "order_id": "o2",
        "status": "已发货",
        "create_time": "2026-10-27 11:00:00",
        "actions": {"REMIND_BUYER_TO_CONFIRM": {}},
    }
    client = FakeSocialClient([order], remind_msg="今日已提醒过，请勿重复提醒")
    auto = SocialAutomation(db, client, 1, "主号")

    await auto.run_once()
    row = await db.get_order_action(1, "o2", "remind")
    assert row is not None and row["detail"].startswith("already:")
    # 当天再次运行不会再发
    await auto.run_once()
    assert [c for c in client.calls if c[0] == "remind"] == [("remind", "o2")]
