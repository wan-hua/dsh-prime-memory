"""订单社交自动化：求小红花、提醒收货、自动评价。

只处理「开启自动化之后创建」的订单，避免一开开关就把历史订单全部打扰一遍。
每个动作都有本地记录；提醒收货按自然日限制一次，评价/求花按订单去重。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from .config import settings, social_any_enabled
from .db import Database

logger = logging.getLogger(__name__)

_WAITING_RECEIPT = {"已发货", "等待见面交易", "待收货"}


def social_event(content: str | None, is_system_card: bool = False) -> str:
    """从交易消息识别触发事件；只认系统/交易消息，别把买家聊天当通知。"""
    text = str(content or "")
    if not is_system_card and not text.lstrip().startswith("["):
        return ""
    if "买家已确认收货" in text and "交易成功" in text:
        return "confirmed"
    if "送你闲鱼小红花" in text and ("立即收下" in text or "已收下" in text):
        return "received_flower"
    if "[你已发货]" in text or "等待见面交易" in text:
        return "shipped"
    return ""


class SocialAutomation:
    def __init__(self, db: Database, client: Any, account_id: int, name: str) -> None:
        self.db = db
        self.client = client
        self.account_id = account_id
        self.name = name

    async def on_event(self, order_id: str, event: str) -> dict[str, int]:
        """由系统交易消息触发的动作；不再依赖订单轮询。"""
        counts = {"flower": 0, "remind": 0, "rate": 0}
        if not order_id:
            return counts
        if event == "shipped":
            if settings.social_auto_flower and await self._flower(order_id):
                counts["flower"] += 1
            if settings.social_auto_remind and await self._remind(order_id):
                counts["remind"] += 1
        elif event == "confirmed":
            if settings.social_auto_flower and await self._flower(order_id):
                counts["flower"] += 1
            if settings.social_auto_rate and await self._rate_order(order_id):
                counts["rate"] += 1
        elif event == "received_flower":
            if await self.receive_flower(order_id):
                counts["flower"] += 1
        return counts

    async def run_once(self) -> dict[str, int]:
        """扫一遍订单并执行已启用的动作；返回本轮成功动作数。"""
        counts = {"flower": 0, "remind": 0, "rate": 0}
        if not social_any_enabled():
            return counts
        since_ms = self._since_ms()
        if since_ms <= 0:
            logger.warning("[%s] 社交自动化已开启但没有生效时间（social_since），本轮跳过", self.name)
            return counts
        orders = await self.client.list_sold_orders()
        for order in orders:
            if not isinstance(order, dict):
                continue
            created = _as_ms(order.get("create_time"))
            # 没有创建时间的订单无法判断新旧，宁可不处理，避免误打扰历史订单
            if not created or created < since_ms:
                continue
            order_id = str(order.get("order_id") or "")
            if not order_id:
                continue
            status = str(order.get("status") or "")
            actions = order.get("actions") or {}
            flower_allowed = (
                (status in _WAITING_RECEIPT and "REQUIRE_FLOWER" in actions)
                or status == "交易成功"
            )
            if (settings.social_auto_flower and flower_allowed
                    and await self._flower(order_id)):
                counts["flower"] += 1
            if (settings.social_auto_remind and status in _WAITING_RECEIPT
                    and "REMIND_BUYER_TO_CONFIRM" in actions
                    and await self._remind(order_id)):
                counts["remind"] += 1
            if (settings.social_auto_rate and status == "交易成功"
                    and await self._rate(order, actions)):
                counts["rate"] += 1
        if any(counts.values()):
            logger.info("[%s] 社交自动化本轮完成：求花 %s，提醒 %s，评价 %s",
                        self.name, counts["flower"], counts["remind"], counts["rate"])
        return counts

    @staticmethod
    def _since_ms() -> int:
        raw = str(settings.social_since or "").strip()
        if not raw:
            return 0
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return int(datetime.strptime(raw[:19], fmt).timestamp() * 1000)
            except ValueError:
                continue
        return 0

    async def receive_flower(self, order_id: str) -> bool:
        """领取买家送来的小红花；只做这一件事，不触发求花/评价。"""
        if not settings.social_auto_receive_flower:
            return False
        if not await self._due(order_id, "receive_flower"):
            return False
        ok, msg = await self.client.receive_flower(order_id)
        await self.db.set_order_action(
            self.account_id, order_id, "receive_flower",
            detail=("ok:" if ok else "failed:") + msg,
        )
        if not ok:
            logger.warning("[%s] 订单 %s 领取小红花失败：%s", self.name, order_id, msg)
        return ok

    async def _flower(self, order_id: str) -> bool:
        if not await self._due(order_id, "flower"):
            return False
        ok, msg = await self.client.require_flower(order_id)
        await self.db.set_order_action(
            self.account_id, order_id, "flower", detail=("ok:" if ok else "failed:") + msg
        )
        if not ok:
            logger.warning("[%s] 订单 %s 求小红花失败：%s", self.name, order_id, msg)
        return ok

    async def _remind(self, order_id: str) -> bool:
        if not await self._due(order_id, "remind", daily=True):
            return False
        ok, msg = await self.client.remind_receipt(order_id)
        already = "今日已提醒过" in msg or "请勿重复提醒" in msg
        await self.db.set_order_action(
            self.account_id, order_id, "remind",
            detail=("already:" if already else "ok:" if ok else "failed:") + msg,
        )
        if already:
            logger.info("[%s] 订单 %s 今日已提醒过，跳过", self.name, order_id)
        elif not ok:
            logger.warning("[%s] 订单 %s 提醒收货失败：%s", self.name, order_id, msg)
        return ok

    async def _rate(self, order: dict[str, Any], actions: dict[str, Any]) -> bool:
        order_id = str(order.get("order_id") or "")
        try:
            rate_status = int(order.get("seller_rate_status"))
        except (TypeError, ValueError):
            rate_status = 0
        # 4 = 已评价（按钮显示「查看评价」）；5 = 未评价（按钮显示「去评价」）
        if rate_status == 4 and "RATE" not in actions:
            return False
        return await self._rate_order(order_id)

    async def _rate_order(self, order_id: str) -> bool:
        if not await self._due(order_id, "rate"):
            return False
        content = str(settings.social_rate_content or "").strip()
        if not content:
            logger.warning("[%s] 自动评价已开启但评价内容为空，跳过", self.name)
            return False
        ok, msg = await self.client.rate_buyer(order_id, content, rate=1)
        await self.db.set_order_action(
            self.account_id, order_id, "rate", detail=("ok:" if ok else "failed:") + msg
        )
        if not ok:
            logger.warning("[%s] 订单 %s 自动评价失败：%s", self.name, order_id, msg)
        return ok

    async def _due(self, order_id: str, action: str, *, daily: bool = False) -> bool:
        record = await self.db.get_order_action(self.account_id, order_id, action)
        if not record:
            return True
        last = str(record.get("last_at") or "")
        if daily:
            return last[:10] != time.strftime("%Y-%m-%d")
        try:
            ts = datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            return True
        return (time.time() - ts) >= 86400


def _as_ms(value: Any) -> int:
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return int(datetime.strptime(text[:19], fmt).timestamp() * 1000)
            except ValueError:
                continue
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if n and n < 10**12:
        n *= 1000
    return n
