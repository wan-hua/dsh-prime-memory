"""上游联动：按发货规则在 2018K 创建卡密。

链路：

    订单实付金额(元) ──规则换算──> 天数
      ──> 2018K createNetworkAuthentication ──> 卡密 ID（authId）
      ──> 作为发货内容发给买家

2018K 的 OpenId 只负责 AES 加解密，不是登录凭据；实例（softwareId）在规则里选。
发货失败时如果配置了后台账号，走 admin API 删除卡密；没配就记录卡密 ID 交人工处理。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from .config import find_k2018_instance, k2018_admin_ready, settings
from .k2018 import K2018AdminClient, K2018Client, K2018Error

logger = logging.getLogger(__name__)

#: 卡密备注上限（与 2018K 后台备注长度保持一致）
CARD_LABEL_MAX_CHARS = 100


class UpstreamError(RuntimeError):
    """上游建卡失败。"""


@dataclass(slots=True)
class Provisioned:
    """一次自动建卡的产物。"""

    card_code: str
    card_mask: str
    days: int
    software_id: str
    remark: str = ""


class Provisioner:
    """把「天数」变成 2018K 卡密的入口，供发货流程调用。"""

    def __init__(
        self,
        *,
        client: K2018Client | None = None,
        admin: K2018AdminClient | None = None,
        enable_admin: bool = True,
    ) -> None:
        if client is None and settings.k2018_openid:
            try:
                client = K2018Client(
                    settings.k2018_openid,
                    base_url=settings.k2018_api_base,
                )
            except K2018Error as exc:
                logger.error("2018K 客户端初始化失败：%s", exc)
        self.client = client
        if admin is None and enable_admin and k2018_admin_ready():
            admin = K2018AdminClient(
                base_url=settings.k2018_admin_base,
                username=settings.k2018_admin_username,
                password=settings.k2018_admin_password,
            )
        self.admin = admin
        #: 最近一次自检结果；None = 还没检测过。面板靠它区分「配置了」和「真能连上」。
        self.health_ok: bool | None = None
        self.health_error = ""
        self.health_checked_at = 0.0
        self.health_instances: list[dict[str, Any]] = []
        self.admin_ok: bool | None = None
        self.admin_error = ""

    def health(self) -> dict[str, Any]:
        """返回最近一次连接自检结果，供后台展示。"""
        return {
            "ok": self.health_ok,
            "error": self.health_error,
            "checked_at": self.health_checked_at,
            "instances": self.health_instances,
            "admin_ok": self.admin_ok,
            "admin_error": self.admin_error,
        }

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
        if self.admin is not None:
            await self.admin.close()

    async def warmup(self, *, quiet: bool = False, check_admin: bool = True) -> None:
        """核对 OpenId 与实例是否匹配，并把结果写进 health()。

        只读：调用 obtainSoftware。失败不阻塞启动，但面板要能立刻看到
        「哪个实例配错了 / 2018K 连不上」，别等买家下单时才报错。
        quiet 用于后台定时探测：成功的日常日志不刷屏，失败仍记录。
        """
        if self.client is None:
            self.health_ok = False
            self.health_error = "未配置 2018K OpenId"
            self.health_checked_at = time.time()
            self.health_instances = []
            if not quiet:
                logger.error("2018K 未配置 XY_2018K_OPENID，自动建卡密会全部失败")
            return

        details: list[dict[str, Any]] = []
        errors: list[str] = []
        for inst in settings.k2018_instances:
            try:
                info = await self.client.software_info(inst.software_id)
                details.append({
                    "name": inst.name,
                    "software_id": inst.software_id,
                    "tail": inst.tail,
                    "ok": True,
                    "error": "",
                })
                if not quiet:
                    logger.info(
                        "2018K 实例已就绪：%s（%s，版本 %s）",
                        inst.name,
                        info.get("softwareName") or "-",
                        info.get("versionNumber") or "-",
                    )
            except K2018Error as exc:
                message = str(exc)
                errors.append(f"{inst.name}: {message}")
                details.append({
                    "name": inst.name,
                    "software_id": inst.software_id,
                    "tail": inst.tail,
                    "ok": False,
                    "error": message,
                })
                logger.error("2018K 实例 %s（%s）自检失败：%s", inst.name, inst.tail, exc)

        self.health_instances = details
        self.health_checked_at = time.time()
        if not details:
            self.health_ok = False
            self.health_error = "没有配置 2018K 实例"
        elif errors:
            self.health_ok = False
            self.health_error = errors[0]
        else:
            self.health_ok = True
            self.health_error = ""

        if self.admin is not None and check_admin:
            try:
                await self.admin.login()
                self.admin_ok = True
                self.admin_error = ""
                if not quiet:
                    logger.info("2018K 后台账号已登录，发货失败时可自动删卡")
            except K2018Error as exc:
                self.admin_ok = False
                self.admin_error = str(exc)
                logger.error("2018K 后台账号登录失败，失败回滚将不可用：%s", exc)

    async def provision(
        self,
        software_id: str,
        days: int,
        label: str,
        username: str = "",
    ) -> Provisioned:
        """在指定实例上创建一张 days 天的卡密。

        label 形如「买家用户名#订单号」，写进卡密备注，方便后台对账。
        """
        if self.client is None:
            raise UpstreamError("2018K 未配置（检查 XY_2018K_OPENID）")
        instance = find_k2018_instance(software_id)
        if instance is None:
            raise UpstreamError(
                "规则选择的 2018K 实例不在 XY_2018K_INSTANCES 里："
                + (software_id or "（空）")
            )
        if days < 0:
            raise UpstreamError("卡密天数不能为负数；永久卡请填 0")
        remark = build_card_label(username, order_id_from_label(label)) or label
        try:
            card = await self.client.create_card(
                instance.software_id,
                days=int(days),
                remark=remark,
            )
        except K2018Error as exc:
            # 建卡失败是「这一单」的业务结果，不等于实例连接异常。
            # 例如 2018K 返回「请求数据已经过期」时，obtainSoftware 自检
            # 仍然是正常的；这里不能把全局连接状态翻红，否则面板会一直
            # 显示异常，直到用户手动重新检测。
            logger.warning("2018K 创建卡密失败（连接状态保持上次自检结果）：%s", exc)
            raise UpstreamError(str(exc)) from exc
        return Provisioned(
            card_code=card.auth_id,
            card_mask=mask_card(card.auth_id),
            days=int(days),
            software_id=instance.software_id,
            remark=remark,
        )

    async def revoke(self, provisioned: Provisioned) -> tuple[bool, str]:
        """删除未送达的卡密。

        返回 (是否已删除, 说明)。没配后台账号时返回 (False, 提示手动删除)，
        调用方要把卡密 ID 写进订单详情，别让它静默消失。
        """
        if self.admin is None:
            note = (
                "未配置 2018K 后台账号，无法自动删卡；"
                "请到 2018K 后台手动删除卡密 " + provisioned.card_code
            )
            logger.warning(note)
            return False, note
        try:
            await self.admin.delete_card(provisioned.card_code)
            return True, "已删除未送达的 2018K 卡密"
        except K2018Error as exc:
            note = "2018K 删卡失败：" + str(exc) + "；卡密 " + provisioned.card_code
            logger.error(note)
            return False, note


# ---------------------------------------------------------------- 命名工具


def order_id_from_label(label: str) -> str:
    """从卡密名称里取出订单号（名称形如「买家用户名#2503…」）。"""
    parts = (label or "").split("#")
    return parts[-1].strip() if len(parts) >= 2 else ""


def _clean_name_part(text: str) -> str:
    """清理名称里的一段：去控制字符、合并空白、把 # 换成全角，避免和分隔符打架。"""
    return " ".join(str(text or "").replace("#", "＃").split())


def build_card_label(username: str, order_id: str) -> str:
    """拼卡密备注：`买家用户名#订单号`。

    名称里不放价格，也不放平台前缀 —— 后台要认的是「哪个买家、哪一单」。
    超长时优先截短用户名，订单号一个字符都不动。
    """
    user = _clean_name_part(username) or "买家"
    order = _clean_name_part(order_id)
    if not order:
        return user[:CARD_LABEL_MAX_CHARS]
    tail = "#" + order
    full = user + tail
    if len(full) <= CARD_LABEL_MAX_CHARS:
        return full
    budget = CARD_LABEL_MAX_CHARS - len(tail)
    if budget > 0:
        return user[:budget].rstrip() + tail
    return "#" + order[-(CARD_LABEL_MAX_CHARS - 1):]


def mask_card(card_code: str) -> str:
    """打码展示：保留前 6 位与后 4 位。"""
    text = str(card_code or "")
    if len(text) <= 10:
        return text[:3] + "***"
    return text[:6] + "…" + text[-4:]
