"""上游联动（2018K 创建卡密）的接线测试。"""

from __future__ import annotations

import base64
import json

import msgpack
import pytest

from app.db import Database
from app.engine import DeliveryEngine
from app.minidaily import MiniDailyInvite
from app.runner import AccountWorker
from app.upstream import Provisioned, UpstreamError


class FakeProvisioner:
    """替身：记录调用参数，按需返回卡密或抛错。"""

    def __init__(self, fail_with: str | None = None) -> None:
        self.calls: list[tuple[float, str]] = []
        self.usernames: list[str] = []
        self.fail_with = fail_with
        self.revoked: list[str] = []

    async def provision(self, software_id: str, days: int, label: str, username: str = "") -> Provisioned:
        self.calls.append((days, label))
        self.usernames.append(username)
        if self.fail_with:
            raise UpstreamError(self.fail_with)
        return Provisioned(card_code="CARD-ABCD-EFGH", card_mask="CARD…EFGH",
                           days=days, software_id=software_id)

    async def revoke(self, provisioned: Provisioned) -> tuple[bool, str]:
        self.revoked.append(provisioned.card_code)
        return True, "已删除未送达的 2018K 卡密"


class FakeClient:
    user_id = "2202640918079"

    def __init__(self, order: dict | None = None) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.confirmed: list[str] = []
        self.acked: list[dict] = []
        self.websocket = self
        self.order = order
        self.order_lookups: list[str] = []
        self.confirm_result: tuple[bool, str] = (True, "SUCCESS")

    async def ack(self, ws, frame):
        self.acked.append(frame)

    async def send_text_safe(self, chat_id, to_id, text):
        self.sent.append((chat_id, to_id, text))

    async def confirm_shipping(self, order_id):
        self.confirmed.append(order_id)
        return self.confirm_result

    async def find_order(self, order_id):
        self.order_lookups.append(order_id)
        return self.order

    async def close(self):
        pass


def paid_frame(order_id="2503688126356636370", item_id="900052644277"):
    card = {
        "dxCard": {
            "item": {
                "main": {"targetUrl": "fleamarket://order_detail?id=" + order_id + "&role=seller"}
            }
        }
    }
    payload = {
        "1": {
            "2": "47983389096@goofish",
            "5": 1742458817389,
            "10": {
                "reminderContent": "[我已付款，等待你发货]",
                "senderUserId": "3149637063",
                "senderNick": "买家小王",
                "reminderUrl": (
                    "fleamarket://message_chat?itemId=" + item_id + "&peerUserId=3149637063"
                ),
            },
            "6": {"3": {"5": json.dumps(card)}},
        },
    }
    data = base64.b64encode(msgpack.packb(payload, use_bin_type=True)).decode()
    return json.dumps({"headers": {"mid": "m1"}, "body": {"syncPushPackage": {"data": [{"data": data}]}}})

UPSTREAM_RULE = {
    "name": "按金额建 2018K 卡密", "item_id": "*", "enabled": True, "mode": "upstream",
    "content": "你好{buyer}，卡密：{card}\n有效期 {days} 天（订单 {amount} 元）",
    "auto_confirm": True, "priority": 0, "remark": "",
    "software_id": "B4ADC92FDF7240398D8A80A4176C80BD",
    "duration_mode": "per_amount", "fixed_days": 0, "days_per_cny": 1.0,
}


ORDER = {"commonData": {"orderId": "2503688126356636370", "orderStatus": "待发货"},
         "priceVO": {"actualPayFee": "10.00"}}


async def build(db: Database, provisioner, order=ORDER):
    account_id = await db.upsert_account("主号", "unb=2202640918079; _m_h5_tk=t_1")
    account = await db.get_account(account_id)
    worker = AccountWorker(db, account, DeliveryEngine(db), provisioner)
    worker.client = FakeClient(order)
    return worker, account_id


async def test_upstream_delivery_creates_card_and_sends(db):
    await db.save_rule(dict(UPSTREAM_RULE))
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner)

    await worker._on_frame(paid_frame())

    # 1) 按订单实付金额换算了 10 天卡密
    assert len(provisioner.calls) == 1
    days, label = provisioner.calls[0]
    assert days == pytest.approx(10)
    # 卡密备注 = 买家用户名#订单号：不带价格、不带平台前缀
    assert label == "买家小王#2503688126356636370"
    assert "10.00" not in label and "元" not in label
    # 备注用买家用户名，不是「闲鱼-价格」
    assert provisioner.usernames == ["买家小王"]

    # 2) 卡密发给了买家，文案里带上天数与订单金额
    assert len(worker.client.sent) == 1
    text = worker.client.sent[0][2]
    assert "CARD-ABCD-EFGH" in text
    assert "10.00" in text
    assert "10 天" in text

    # 3) 自动确认发货被调用，订单落库为 delivered
    assert worker.client.confirmed == ["2503688126356636370"]
    orders = await db.list_orders()
    assert orders[0]["status"] == "delivered"
    assert orders[0]["amount_cny"] == pytest.approx(10.0)
    assert orders[0]["duration_days"] == 10
    assert orders[0]["card_code"] == "CARD-ABCD-EFGH"


async def test_confirm_failure_does_not_send_card(db):
    await db.save_rule(dict(UPSTREAM_RULE))
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner)
    worker.client.confirm_result = (False, "ORDER_STATUS_ERROR::订单状态不正确")

    await worker._on_frame(paid_frame())

    assert worker.client.sent == []
    assert worker.client.confirmed == ["2503688126356636370"]
    assert provisioner.revoked == ["CARD-ABCD-EFGH"]
    orders = await db.list_orders()
    assert orders[0]["status"] == "confirm_failed"
    assert orders[0]["card_code"] == "CARD-ABCD-EFGH"


async def test_already_delivered_order_still_sends_card(db):
    await db.save_rule(dict(UPSTREAM_RULE))
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner)
    worker.client.confirm_result = (
        False, "ORDER_ALREADY_DELIVERY::已发货成功，请刷新页面~"
    )

    await worker._on_frame(paid_frame())

    assert len(worker.client.sent) == 1
    assert provisioner.revoked == []
    orders = await db.list_orders()
    assert orders[0]["status"] == "delivered"


async def test_upstream_days_per_cny(db):
    await db.save_rule({**UPSTREAM_RULE, "days_per_cny": 2.0})
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner)
    await worker._on_frame(paid_frame())
    assert provisioner.calls[0][0] == pytest.approx(20.0)


async def test_upstream_price_tiers_uses_matching_days(db):
    # 商品：天卡 ¥1 / 周卡 ¥5 / 月卡 ¥10；买家实付 ¥5 → 7 天
    tiers = [
        {"sku_id": "s1", "label": "天卡", "price": 1.0, "days": 1},
        {"sku_id": "s2", "label": "周卡", "price": 5.0, "days": 7},
        {"sku_id": "s3", "label": "月卡", "price": 10.0, "days": 30},
    ]
    await db.save_rule({**UPSTREAM_RULE, "duration_mode": "tiers", "price_tiers": tiers})
    provisioner = FakeProvisioner()
    order = {"commonData": {"orderId": "2503688126356636370", "orderStatus": "待发货"},
             "priceVO": {"actualPayFee": "5.00"}}
    worker, _ = await build(db, provisioner, order=order)

    await worker._on_frame(paid_frame())

    assert provisioner.calls[0][0] == 7
    assert "7 天" in worker.client.sent[0][2]
    orders = await db.list_orders()
    assert orders[0]["status"] == "delivered"
    assert orders[0]["duration_days"] == 7


async def test_upstream_price_tiers_rejects_unmatched_amount(db):
    tiers = [{"sku_id": "s1", "label": "天卡", "price": 1.0, "days": 1}]
    await db.save_rule({**UPSTREAM_RULE, "duration_mode": "tiers", "price_tiers": tiers})
    provisioner = FakeProvisioner()
    order = {"commonData": {"orderId": "2503688126356636370", "orderStatus": "待发货"},
             "priceVO": {"actualPayFee": "100.00"}}
    worker, _ = await build(db, provisioner, order=order)

    await worker._on_frame(paid_frame())

    assert provisioner.calls == []
    assert worker.client.sent == []
    orders = await db.list_orders()
    assert orders[0]["status"] == "upstream_failed"
    assert "规格" in orders[0]["detail"]


class FlakyClient(FakeClient):
    """第一次查订单返回空，模拟订单列表延迟，用来验 10 秒后的重试。"""

    async def find_order(self, order_id):
        self.order_lookups.append(order_id)
        if len(self.order_lookups) == 1:
            return None
        return self.order


class FlakyProvisioner(FakeProvisioner):
    """第一次建卡网络失败，第二次成功。"""

    async def provision(self, software_id: str, days: int, label: str, username: str = "") -> Provisioned:
        self.calls.append((days, label))
        self.usernames.append(username)
        if len(self.calls) == 1:
            raise UpstreamError("2018K 创建卡密网络请求失败：timeout")
        return Provisioned(card_code="CARD-ABCD-EFGH", card_mask="CARD…EFGH",
                           days=days, software_id=software_id)


class FakeMiniDailyClient:
    """替身：记录天数，返回固定邀请链接。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def create_invite(self, days: int, note: str = "") -> MiniDailyInvite:
        self.calls.append((days, note))
        return MiniDailyInvite(
            token="INVITE-TOKEN",
            url="https://scancode.miraclesplus.top/join?t=INVITE-TOKEN",
            days=days,
            expire_text="10-27 01:10:29",
        )


async def test_minidaily_invite_delivery(db, monkeypatch):
    fake = FakeMiniDailyClient()
    monkeypatch.setattr("app.runner.MiniDailyClient", lambda: fake)
    await db.save_rule({
        **UPSTREAM_RULE,
        "mode": "minidaily",
        "duration_mode": "fixed",
        "fixed_days": 7,
        "content": "你好 {buyer}，二维码：{qr_url}\n挂载 {days} 天",
    })
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner)

    await worker._on_frame(paid_frame())

    assert fake.calls == [(7, "买家小王#2503688126356636370")]
    assert provisioner.calls == []
    assert len(worker.client.sent) == 1
    text = worker.client.sent[0][2]
    assert "https://scancode.miraclesplus.top/join?t=INVITE-TOKEN" in text
    assert "挂载 7 天" in text
    orders = await db.list_orders()
    assert orders[0]["status"] == "delivered"
    assert orders[0]["card_code"] == "INVITE-TOKEN"
    assert "MiniDaily 二维码" in orders[0]["detail"]


async def test_minidaily_invite_uses_tier_days(db, monkeypatch):
    fake = FakeMiniDailyClient()
    monkeypatch.setattr("app.runner.MiniDailyClient", lambda: fake)
    tiers = [
        {"sku_id": "s1", "label": "天卡", "price": 1.0, "days": 1},
        {"sku_id": "s2", "label": "周卡", "price": 5.0, "days": 7},
        {"sku_id": "s3", "label": "月卡", "price": 10.0, "days": 30},
    ]
    await db.save_rule({
        **UPSTREAM_RULE,
        "mode": "minidaily",
        "duration_mode": "tiers",
        "price_tiers": tiers,
        "content": "入池：{qr_url}",
    })
    order = {"commonData": {"orderId": "2503688126356636370", "orderStatus": "待发货"},
             "priceVO": {"actualPayFee": "5.00"}}
    worker, _ = await build(db, FakeProvisioner(), order=order)

    await worker._on_frame(paid_frame())

    assert fake.calls and fake.calls[0][0] == 7
    orders = await db.list_orders()
    assert orders[0]["status"] == "delivered"
    assert orders[0]["duration_days"] == 7


async def test_upstream_retries_order_lookup_once(db, monkeypatch):
    monkeypatch.setattr("app.runner.PROVISION_RETRY_DELAY", 0)
    await db.save_rule(dict(UPSTREAM_RULE))
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner)
    worker.client = FlakyClient(ORDER)

    await worker._on_frame(paid_frame())

    assert len(worker.client.order_lookups) == 2
    assert provisioner.calls and len(provisioner.calls) == 1
    orders = await db.list_orders()
    assert orders[0]["status"] == "delivered"


async def test_upstream_retries_transient_card_failure(db, monkeypatch):
    monkeypatch.setattr("app.runner.PROVISION_RETRY_DELAY", 0)
    await db.save_rule(dict(UPSTREAM_RULE))
    provisioner = FlakyProvisioner()
    worker, _ = await build(db, provisioner)

    await worker._on_frame(paid_frame())

    assert len(provisioner.calls) == 2
    orders = await db.list_orders()
    assert orders[0]["status"] == "delivered"
    assert orders[0]["card_code"] == "CARD-ABCD-EFGH"


async def test_upstream_failure_sends_nothing(db):
    await db.save_rule(dict(UPSTREAM_RULE))
    provisioner = FakeProvisioner(fail_with="2018K 建卡密失败：实例不存在")
    worker, _ = await build(db, provisioner)

    await worker._on_frame(paid_frame())

    assert worker.client.sent == []
    assert worker.client.confirmed == []
    orders = await db.list_orders()
    assert orders[0]["status"] == "upstream_failed"
    assert "建卡密失败" in orders[0]["detail"]
    # 金额是已经解析出来的，失败记录也要带上：否则页面上只显示「—」，
    # 事后看不出这单值多少钱、也就没法判断是不是金额算错导致的失败
    assert orders[0]["amount_cny"] == pytest.approx(10.0)
    assert orders[0]["duration_days"] == 10


async def test_upstream_requires_order_id(db):
    await db.save_rule(dict(UPSTREAM_RULE))
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner)

    payload = {
        "1": {
            "2": "47983389096@goofish",
            "10": {"reminderContent": "[我已付款，等待你发货]", "senderUserId": "3149637063"},
        }
    }
    data = base64.b64encode(msgpack.packb(payload, use_bin_type=True)).decode()
    frame = json.dumps({"headers": {}, "body": {"syncPushPackage": {"data": [{"data": data}]}}})
    await worker._on_frame(frame)

    # 没有订单号 → 鉴权直接拦下，既不建卡密也不发消息
    assert provisioner.calls == []
    assert worker.client.sent == []
    # 也不落库：订单号为空时 ORDER 表没有去重键，落库等于让人刷屏写表
    assert await db.list_orders() == []


async def test_upstream_rejects_amount_over_limit(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_order_cny", 5.0)
    await db.save_rule(dict(UPSTREAM_RULE))
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner)
    await worker._on_frame(paid_frame())

    assert provisioner.calls == []
    orders = await db.list_orders()
    assert orders[0]["status"] == "upstream_failed"
    assert "上限" in orders[0]["detail"]


async def test_upstream_missing_order_data(db):
    await db.save_rule(dict(UPSTREAM_RULE))
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner, order=None)
    await worker._on_frame(paid_frame())

    assert provisioner.calls == []
    orders = await db.list_orders()
    assert orders[0]["status"] == "upstream_failed"
    assert "金额" in orders[0]["detail"]


async def test_same_order_not_provisioned_twice(db):
    await db.save_rule(dict(UPSTREAM_RULE))
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner)
    frame = paid_frame()
    await worker._on_frame(frame)
    await worker._on_frame(frame)
    assert len(provisioner.calls) == 1


async def test_dry_run_never_calls_upstream(db, monkeypatch):
    """DRY-RUN 下绝不能真的去 2018K 建卡密。"""
    from app.config import settings

    monkeypatch.setattr(settings, "dry_run", True)
    await db.save_rule(dict(UPSTREAM_RULE))
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner)

    await worker._on_frame(paid_frame())

    assert provisioner.calls == []
    assert worker.client.sent == []
    assert await db.list_orders() == []


async def test_fixed_days_works_without_order_amount(db):
    """规则里填了固定天数时，查不到订单金额也能发货。"""
    await db.save_rule({**UPSTREAM_RULE, "duration_mode": "fixed", "fixed_days": 5})
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner, order=None)

    await worker._on_frame(paid_frame())

    assert provisioner.calls and provisioner.calls[0][0] == 5
    assert len(worker.client.sent) == 1
    orders = await db.list_orders()
    assert orders[0]["status"] == "delivered"
    assert orders[0]["duration_days"] == 5


async def test_fixed_zero_days_is_permanent_and_works_without_amount(db):
    """固定 0 天 = 永久卡；不依赖订单金额，也允许 0 天建卡。"""
    await db.save_rule({**UPSTREAM_RULE, "duration_mode": "fixed", "fixed_days": 0})
    provisioner = FakeProvisioner()
    worker, _ = await build(db, provisioner, order=None)

    await worker._on_frame(paid_frame())

    assert provisioner.calls and provisioner.calls[0][0] == 0
    assert "永久" in worker.client.sent[0][2]
    orders = await db.list_orders()
    assert orders[0]["status"] == "delivered"
    assert orders[0]["duration_days"] == 0


async def test_failed_order_can_be_retried_after_config_fixed(db, monkeypatch):
    """失败记录不能把订单永久锁死：修好配置后同一单还要能重发。"""
    monkeypatch.setattr("app.runner.PROVISION_RETRY_DELAY", 0)
    await db.save_rule(dict(UPSTREAM_RULE))

    failing = FakeProvisioner(fail_with="卡密站挂了")
    worker, _ = await build(db, failing)
    await worker._on_frame(paid_frame())
    assert worker.client.sent == []
    assert (await db.list_orders())[0]["status"] == "upstream_failed"

    # 修好配置后，同一单再推一次应该能发出去（失败不会进冷却期，也不会占用记录）
    worker.engine = DeliveryEngine(db)
    worker.provisioner = FakeProvisioner()
    await worker._on_frame(paid_frame())

    assert len(worker.client.sent) == 1
    orders = await db.list_orders()
    # 同一订单只有一行，状态被更新成 delivered
    assert len(orders) == 1
    assert orders[0]["status"] == "delivered"


async def test_upstream_failure_does_not_consume_card_pool(db, monkeypatch):
    """上游失败不能影响本地卡密池。"""
    monkeypatch.setattr("app.runner.PROVISION_RETRY_DELAY", 0)
    await db.add_cards(["LOCAL-1"], "*")
    await db.save_rule(dict(UPSTREAM_RULE))
    worker, _ = await build(db, FakeProvisioner(fail_with="boom"))
    await worker._on_frame(paid_frame())
    cards = await db.list_cards()
    assert cards[0]["status"] == "available"
