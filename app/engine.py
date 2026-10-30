"""发货决策引擎：规则匹配 → （卡密出库 | 2018K 建卡密）→ 渲染发货文案。

这一层不碰网络，纯函数 + 数据库读写，方便单元测试。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from .codec import IncomingMessage, is_ignored, is_paid_trigger
from .db import Database

logger = logging.getLogger(__name__)

#: 发货被鉴权拦下时的理由（runner 靠它决定要不要留一条审计记录）
AUTH_BLOCKED_REASON = "真人文本消息，订单未经核实，不自动发货"


@dataclass(slots=True)
class Decision:
    """一条消息的处理结论。"""

    action: str  # "deliver" | "provision" | "invite" | "skip"
    reason: str = ""
    rule: dict[str, Any] | None = None
    content: str = ""
    card_id: int | None = None
    card_code: str | None = None
    amount_cny: float | None = None
    #: 2018K 卡密天数（旧的 quota_usd 已随 new-api 一起移除）
    days: int | None = None
    auto_confirm: bool = False

    @property
    def should_deliver(self) -> bool:
        return self.action in ("deliver", "provision", "invite")

    @property
    def needs_provision(self) -> bool:
        return self.action == "provision"

    @property
    def needs_invite(self) -> bool:
        return self.action == "invite"


def pick_rule(rules: list[dict[str, Any]], item_id: str | None) -> dict[str, Any] | None:
    """挑出适用于该商品的规则。

    优先级：商品专属规则 > 通配规则；同优先级下按 priority 降序、id 升序。
    rules 已按 priority DESC, id 排序。
    """
    if not rules:
        return None
    if item_id:
        for rule in rules:
            if str(rule.get("item_id")) == str(item_id):
                return rule
    for rule in rules:
        if str(rule.get("item_id")) in ("*", "", "all"):
            return rule
    return None


def render(template: str, *, buyer_name: str = "", item_id: str | None = None,
           order_id: str | None = None, card: str = "",
           amount_cny: float | None = None, days: int | None = None,
           qr_url: str = "") -> str:
    """渲染发货文案，支持 {buyer} {item_id} {order_id} {card} {qr_url} {date} {amount} {days}。

    旧模板里的 {quota} 保留为 {days} 的别名，避免升级后老文案里的占位符变成空。
    """
    values = {
        "buyer": buyer_name or "买家",
        "item_id": item_id or "",
        "order_id": order_id or "",
        "card": card,
        "qr_url": qr_url,
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "amount": "" if amount_cny is None else f"{amount_cny:.2f}",
        # 0 天在 2018K 约定为永久卡；文案里直接显示「永久」，不显示 0
        "days": "" if days is None else ("永久" if int(days) == 0 else str(int(days))),
    }
    values["quota"] = values["days"]
    out = template or ""
    if days is not None and int(days) == 0:
        # 0 天是永久卡；顺手去掉「{days} 天」后面的「天」，避免出现「永久 天」
        for key in ("days", "quota"):
            out = out.replace("{" + key + "} 天", "永久").replace("{" + key + "}天", "永久")
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def parse_price_tiers(raw: Any) -> list[dict[str, Any]]:
    """把规则里的 `price_tiers` 解析成干净的档位列表。

    存储形态是 JSON 字符串（数据库 TEXT），也允许直接传 list（测试 / 内部调用）。
    只保留 price >= 0 且 days >= 0 的档位；days=0 表示永久卡。
    坏数据跳过而不是让整条发货链路报错。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    tiers: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            price = float(item.get("price"))
            days = int(float(item.get("days")))
        except (TypeError, ValueError):
            continue
        if price < 0 or days < 0:
            continue
        tiers.append({
            "sku_id": str(item.get("sku_id") or ""),
            "label": str(item.get("label") or "规格"),
            "price": round(price, 2),
            "days": days,
        })
    return tiers


def pick_price_tier(tiers: list[dict[str, Any]], amount_cny: float) -> dict[str, Any] | None:
    """按订单实付金额匹配一个规格档位。

    先找价格完全一致的（容忍 1 分钱误差）；没有就取价格最接近的一档，
    但差距不能超过 max(1 元, 档位价 30%)，否则宁可不发也不猜。
    闲鱼可能有优惠/改价，因此不能只认严格相等。
    """
    if not tiers or amount_cny is None or amount_cny <= 0:
        return None
    exact = [t for t in tiers if abs(float(t["price"]) - amount_cny) <= 0.011]
    if exact:
        return exact[0]
    nearest = min(tiers, key=lambda t: abs(float(t["price"]) - amount_cny))
    diff = abs(float(nearest["price"]) - amount_cny)
    tolerance = max(1.0, float(nearest["price"]) * 0.3)
    return nearest if diff <= tolerance else None


def days_from_amount(amount_cny: float, days_per_cny: float = 1.0) -> int:
    """订单金额（元）→ 2018K 卡密天数。

    规则里的「每 1 元 = N 天」；四舍五入到整数天（half-up，不用银行家舍入，
    否则 9.5 元会得到 10 但 10.5 元也只得到 10，看起来像少给一天）。
    金额或倍率不是正数时返回 0，由调用方明确报错，不静默发一张 0 天卡。
    """
    if amount_cny is None or amount_cny <= 0:
        return 0
    rate = days_per_cny if days_per_cny and days_per_cny > 0 else 0.0
    if rate <= 0:
        return 0
    return max(1, int(amount_cny * rate + 0.5))


class DeliveryEngine:
    """根据消息决定是否发货、发什么。"""

    def __init__(self, db: Database, cooldown: int = 600) -> None:
        self.db = db
        self.cooldown = cooldown
        #: (account_id, order_id) -> 上次发货时间戳
        self._last_delivery: dict[tuple[int, str], float] = {}

    def _in_cooldown(self, account_id: int, order_id: str | None) -> bool:
        if not order_id:
            return False
        last = self._last_delivery.get((account_id, order_id))
        return last is not None and (time.time() - last) < self.cooldown

    def mark_delivered(self, account_id: int, order_id: str | None) -> None:
        if order_id:
            self._last_delivery[(account_id, order_id)] = time.time()

    async def decide_order(self, account_id: int, msg: IncomingMessage) -> Decision:
        """订单触发的决策：跳过「是否发货触发语」这一步。

        订单轮询拿到的「待发货」本身就是触发条件，不需要聊天里再出现一句话。
        """
        if not msg.sender_id:
            return Decision("skip", "缺少买家 ID")

        if msg.order_id and await self.db.delivered_order_exists(account_id, msg.order_id):
            return Decision("skip", "订单 " + str(msg.order_id) + " 已发过货")

        if self._in_cooldown(account_id, msg.order_id):
            return Decision("skip", "订单 " + str(msg.order_id) + " 处于冷却期")

        rules = await self.db.list_rules(only_enabled=True)
        rule = pick_rule(rules, msg.item_id)
        if rule is None:
            return Decision("skip", "没有命中发货规则")

        if rule["mode"] == "upstream":
            return Decision("provision", reason="订单触发，命中上游规则 " + str(rule["name"]), rule=rule,
                            auto_confirm=bool(rule["auto_confirm"]))
        if rule["mode"] == "minidaily":
            return Decision("invite", reason="订单触发，命中 MiniDaily 二维码规则 " + str(rule["name"]),
                            rule=rule, auto_confirm=bool(rule["auto_confirm"]))

        return await self._materialize(rule, msg)

    async def decide(self, account_id: int, msg: IncomingMessage) -> Decision:
        """核心判断：要不要给这条消息发货。"""
        if not msg.sender_id:
            return Decision("skip", "缺少买家 ID")
        if is_ignored(msg.content) and not is_paid_trigger(msg.content):
            return Decision("skip", "系统提示消息")

        if not is_paid_trigger(msg.content):
            return Decision("skip", "非发货触发消息")

        # 关键鉴权：不能只看「正文里有没有那几句话」。
        # 「我已付款，等待你发货」买家自己也能在聊天框里打出来，
        # 只按文本判定等于把卡密池和上游 Key 交给任何人。
        #
        # 两条可信来源：
        #   1. 系统交易卡片（contentType=26 + dxCard/order_detail，买家造不出来）；
        #   2. 真人文本，但 runner 已经拿订单号去闲鱼核实过（msg.order_verified）。
        # 两种都要有订单号：没有订单号既不能去重，也不能核对归属。
        if not msg.order_id:
            return Decision("skip", "缺少订单号，不自动发货")
        if not msg.is_system_card and not msg.order_verified:
            return Decision("skip", AUTH_BLOCKED_REASON)

        if await self.db.delivered_order_exists(account_id, msg.order_id):
            return Decision("skip", f"订单 {msg.order_id} 已发过货")

        if self._in_cooldown(account_id, msg.order_id):
            return Decision("skip", f"订单 {msg.order_id} 处于冷却期")

        rules = await self.db.list_rules(only_enabled=True)
        rule = pick_rule(rules, msg.item_id)
        if rule is None:
            logger.info("商品 %s（订单 %s）没有命中发货规则", msg.item_id, msg.order_id)
            return Decision("skip", "没有命中发货规则")

        if rule["mode"] == "upstream":
            # 上游模式要查订单金额，网络动作交给 runner，这里只判定「可以走了」
            return Decision("provision", reason="命中上游规则 " + str(rule["name"]), rule=rule,
                            auto_confirm=bool(rule["auto_confirm"]))
        if rule["mode"] == "minidaily":
            return Decision("invite", reason="命中 MiniDaily 二维码规则 " + str(rule["name"]), rule=rule,
                            auto_confirm=bool(rule["auto_confirm"]))

        return await self._materialize(rule, msg)

    async def _materialize(self, rule: dict[str, Any], msg: IncomingMessage) -> Decision:
        """按规则出内容（卡密池 / 固定文案），不含上游建 Key。"""
        card_id: int | None = None
        card_text = ""
        if rule["mode"] == "card":
            item_for_pool = msg.item_id or rule["item_id"]
            allocation = await self.db.take_card(item_for_pool or "*", msg.order_id, msg.sender_id)
            if allocation.content is None:
                return Decision("skip", "卡密库存不足", rule=rule)
            card_id = allocation.card_id
            card_text = allocation.content

        content = render(
            rule["content"],
            buyer_name=msg.sender_name,
            item_id=msg.item_id,
            order_id=msg.order_id,
            card=card_text,
        )
        if not content.strip():
            if card_id is not None:
                await self.db.release_card(card_id)
            return Decision("skip", "发货内容为空", rule=rule)
        return Decision(
            "deliver",
            reason="命中规则 " + str(rule["name"]),
            rule=rule,
            content=content,
            card_id=card_id,
            auto_confirm=bool(rule["auto_confirm"]),
        )
