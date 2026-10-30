"""Web 管理后台：FastAPI 路由 + 极简 HTML 面板。"""

from __future__ import annotations

import hmac
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .autoreply import KINDS, MATCH_TYPES
from .config import (
    K2018Instance,
    apply_saved_k2018_config,
    apply_saved_social_config,
    k2018_admin_ready,
    k2018_ready,
    settings,
    social_any_enabled,
)
from .db import Database, now_iso
from .qrlogin import STATUS_TEXT, LoginError, QrLoginManager, render_qr_png
from .runner import WorkerManager

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    cookie: str = ""
    remark: str = ""
    enabled: bool = True


class K2018InstanceIn(BaseModel):
    """面板里维护的一个 2018K 实例。"""

    name: str = Field(min_length=1, max_length=32)
    software_id: str = Field(min_length=32, max_length=32)


class SocialConfigIn(BaseModel):
    """订单自动化开关与评价内容。"""

    auto_flower: bool = False
    auto_remind_receipt: bool = False
    auto_rate: bool = False
    auto_receive_flower: bool = True
    rate_content: str = Field(default="", max_length=500)


class K2018ConfigIn(BaseModel):
    """面板保存的 2018K 接入配置；密码/OpenId 留空表示保持原值。"""

    openid: str = ""
    instances: list[K2018InstanceIn] = Field(default_factory=list)
    admin_username: str = ""
    admin_password: str = ""
    clear_admin: bool = False


class PriceTierIn(BaseModel):
    """多规格商品的一档：规格名 + 价格 + 卡密天数。"""

    sku_id: str = Field(default="", max_length=64)
    label: str = Field(default="", max_length=64)
    price: float = Field(ge=0, le=1_000_000)
    #: 0 = 永久卡（2018K 约定 day/hour/minute 全为 0）
    days: int = Field(ge=0, le=36500)


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    item_id: str = "*"
    enabled: bool = True
    mode: str = Field(default="text", pattern="^(text|card|upstream|minidaily)$")
    content: str = ""
    auto_confirm: bool = False
    priority: int = 0
    remark: str = ""
    #: 2018K 模式：规则绑定哪个实例、卡密多少天
    software_id: str = Field(default="", max_length=64)
    #: fixed = 本商品固定天数；per_amount = 订单金额 × 每元天数；tiers = 规格档位
    duration_mode: str = Field(default="fixed", pattern="^(fixed|per_amount|tiers)$")
    fixed_days: int = Field(default=1, ge=0, le=36500)
    days_per_cny: float = Field(default=1.0, gt=0, le=3650)
    #: 多规格商品：天卡/周卡/月卡各自的价格与天数
    price_tiers: list[PriceTierIn] = Field(default_factory=list)


class ReplyRuleIn(BaseModel):
    """一条自动回复规则。kind=item 是商品自动回复，kind=keyword 是关键词自动回复。"""

    name: str = Field(min_length=1, max_length=64)
    kind: str = Field(default="keyword", pattern="^(" + "|".join(KINDS) + ")$")
    item_id: str = "*"
    #: 关键词（逗号 / 分号 / 竖线 / 换行分隔），kind=keyword 时必填
    keywords: str = ""
    #: contains 包含 / exact 完全一致 / regex 正则
    match_type: str = Field(default="contains", pattern="^(" + "|".join(MATCH_TYPES) + ")$")
    content: str = ""
    enabled: bool = True
    #: 同一个会话只回一次（否则买家每发一句都会被回一遍）
    once_per_chat: bool = True
    #: 同一买家多少秒内不再回同一条规则；0 = 不限制
    cooldown: int = 60
    priority: int = 0
    remark: str = ""


class LoginIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    remark: str = ""


class CardsIn(BaseModel):
    item_id: str = "*"
    contents: list[str] = Field(default_factory=list)
    raw_text: str = ""


def k2018_view() -> dict[str, Any]:
    """2018K 对外展示状态：只给实例名/ID 后 8 位，不回显 OpenId 和密码。"""
    return {
        "ready": k2018_ready(),
        "api_base": settings.k2018_api_base,
        "openid_set": bool(settings.k2018_openid),
        "admin_ready": k2018_admin_ready(),
        "instances": [
            {"name": item.name, "software_id": item.software_id, "tail": item.tail}
            for item in settings.k2018_instances
        ],
    }


def social_view() -> dict[str, Any]:
    """订单自动化开关（不包含任何凭据）。"""
    return {
        "auto_flower": bool(settings.social_auto_flower),
        "auto_remind_receipt": bool(settings.social_auto_remind),
        "auto_rate": bool(settings.social_auto_rate),
        "auto_receive_flower": bool(settings.social_auto_receive_flower),
        "rate_content": settings.social_rate_content,
        "since": settings.social_since,
    }


def create_app(db: Database | None = None, manager: WorkerManager | None = None) -> FastAPI:
    database = db or Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await database.init()
        # 面板保存过就以数据库为准；没有保存过则继续用 .env 的初始值
        try:
            saved = await database.get_all_config()
            apply_saved_k2018_config({k: v for k, v in saved.items() if k.startswith("k2018_")})
            apply_saved_social_config({k: v for k, v in saved.items() if k.startswith("social_")})
        except Exception as exc:  # pragma: no cover - 数据库异常时不阻塞启动
            logger.warning("读取 2018K 面板配置失败：%s", exc)
        mgr = manager or WorkerManager(database)
        app.state.manager = mgr
        await mgr.sync()
        try:
            yield
        finally:
            await mgr.stop_all()
            await app.state.qr.close()

    app = FastAPI(title="闲鱼自动发货工具", version="0.1.0", lifespan=lifespan)
    app.state.db = database
    app.state.qr = QrLoginManager()
    #: session_id -> 用户填的账号名/备注，扫码成功后用来建账号
    _pending: dict[str, LoginIn] = {}

    def _qr(request: Request) -> QrLoginManager:
        return request.app.state.qr

    def _is_loopback(request: Request) -> bool:
        host = (request.client.host if request.client else "") or ""
        return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")

    async def require_admin(
        request: Request,
        x_admin_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> None:
        """管理接口的鉴权。

        行为：
          * 配了 XY_ADMIN_TOKEN → 必须带对的口令（X-Admin-Token 头，
            或 Authorization: Bearer <口令>）。
          * 没配口令 → 只放行本机来源；非本机一律 401，
            除非显式打开 XY_ADMIN_ALLOW_REMOTE。

        为什么默认要拦：这些接口能改账号 Cookie、读买家昵称、导出卡密、
        删规则。以前「没配口令 = 谁都能调」，只要放到反代后面就是一扇敞开的门。
        """
        token = x_admin_token or ""
        if not token and authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()

        expected = settings.admin_token
        if expected:
            # 恒定时间比较：普通 != 会按「第几个字符不同」泄漏时间差
            if not hmac.compare_digest(token, expected):
                raise HTTPException(status_code=401, detail="管理口令错误")
            return

        if settings.admin_allow_remote or _is_loopback(request):
            return
        raise HTTPException(
            status_code=401,
            detail="未配置 XY_ADMIN_TOKEN，管理接口只允许本机访问；"
                   "请在 .env 里设置 XY_ADMIN_TOKEN 后再远程使用",
        )

    guard = [Depends(require_admin)]

    def mgr(request: Request) -> WorkerManager:
        return request.app.state.manager

    # -------------------------------------------------------------- 页面

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((TEMPLATE_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # -------------------------------------------------------------- 账号

    @app.get("/api/accounts", dependencies=guard)
    async def list_accounts() -> list[dict[str, Any]]:
        accounts = await database.list_accounts()
        for account in accounts:
            account["cookie"] = "***" if account["cookie"] else ""
        return accounts

    @app.post("/api/accounts", dependencies=guard)
    async def save_account(payload: AccountIn, request: Request) -> dict[str, Any]:
        cookie = payload.cookie.strip()
        if not cookie or cookie == "***":
            # 只改备注/开关时前端拿不到原 Cookie，沿用数据库里的旧值
            existing = await database.fetch_one(
                "SELECT cookie FROM accounts WHERE name = ?", (payload.name,)
            )
            cookie = (existing or {}).get("cookie", "")
        account_id = await database.upsert_account(
            payload.name, cookie, payload.remark, payload.enabled
        )
        await mgr(request).sync()
        return {"id": account_id}

    @app.delete("/api/accounts/{account_id}", dependencies=guard)
    async def delete_account(account_id: int, request: Request) -> dict[str, bool]:
        await database.delete_account(account_id)
        await mgr(request).sync()
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/toggle", dependencies=guard)
    async def toggle_account(account_id: int, request: Request, enabled: bool = True) -> dict[str, bool]:
        account = await database.get_account(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        await database.upsert_account(
            account["name"], account["cookie"], account["remark"], enabled
        )
        await mgr(request).sync()
        return {"ok": True}

    # ---------------------------------------------------------- 扫码登录

    @app.post("/api/login/start", dependencies=guard)
    async def login_start(payload: LoginIn, request: Request) -> dict[str, Any]:
        """生成一个闲鱼登录二维码，返回会话 id。"""
        qr = _qr(request)
        session_id = uuid.uuid4().hex
        try:
            session = await qr.start(session_id)
        except LoginError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # 把账号名先记在会话上，扫码成功后直接建账号
        _pending[session_id] = payload
        return {
            "session_id": session_id,
            "qr_url": session.qr_url,
            "status": session.status,
            "status_text": STATUS_TEXT.get(session.status, session.status),
        }

    @app.get("/api/login/{session_id}/qrcode")
    async def login_qrcode(session_id: str, request: Request, _: None = Depends(require_admin)) -> Response:
        """二维码图片（PNG），前端直接 <img src> 显示。"""
        session = _qr(request).get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="二维码会话不存在或已过期")
        return Response(content=render_qr_png(session.qr_url), media_type="image/png")

    @app.get("/api/login/{session_id}/poll", dependencies=guard)
    async def login_poll(session_id: str, request: Request) -> dict[str, Any]:
        """轮询扫码状态；确认后自动把账号存进数据库并开始监听。"""
        qr = _qr(request)
        session = qr.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="二维码会话不存在或已过期")

        try:
            session = await qr.poll(session_id)
        except LoginError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        result: dict[str, Any] = {
            "status": session.status,
            "status_text": STATUS_TEXT.get(session.status, session.status),
            "error": session.error,
        }

        if session.status in ("EXPIRED", "CANCELED", "ERROR"):
            # 不清理的话，每扫一次失败就往内存里留一条永远不走的记录
            _pending.pop(session_id, None)

        if session.status == "CONFIRMED" and session.cookie and not session.error:
            pending = _pending.pop(session_id, None)
            name = pending.name if pending else (session.nick or "闲鱼账号")
            remark = pending.remark if pending else ""
            account_id = await database.upsert_account(name, session.cookie, remark, True)
            await qr.cancel(session_id)
            await mgr(request).sync()
            result.update({"account_id": account_id, "nick": session.nick, "unb": session.unb})
            logger.info("扫码登录成功，账号 %s 已保存（id=%s）", name, account_id)

        return result

    # -------------------------------------------------------------- 规则

    @app.get("/api/rules", dependencies=guard)
    async def list_rules() -> list[dict[str, Any]]:
        return await database.list_rules()

    @app.post("/api/rules", dependencies=guard)
    async def create_rule(payload: RuleIn) -> dict[str, Any]:
        rule_id = await database.save_rule(payload.model_dump())
        return {"id": rule_id}

    @app.put("/api/rules/{rule_id}", dependencies=guard)
    async def update_rule(rule_id: int, payload: RuleIn) -> dict[str, Any]:
        if await database.get_rule(rule_id) is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        await database.save_rule(payload.model_dump(), rule_id)
        return {"id": rule_id}

    @app.delete("/api/rules/{rule_id}", dependencies=guard)
    async def delete_rule(rule_id: int) -> dict[str, bool]:
        await database.delete_rule(rule_id)
        return {"ok": True}

    # ---------------------------------------------------------- 自动回复

    @app.get("/api/reply-rules", dependencies=guard)
    async def list_reply_rules() -> list[dict[str, Any]]:
        return await database.list_reply_rules()

    def _check_reply_payload(payload: ReplyRuleIn) -> dict[str, Any]:
        data = payload.model_dump()
        if payload.kind == "keyword" and not payload.keywords.strip():
            raise HTTPException(status_code=400, detail="关键词自动回复必须填关键词")
        if not payload.content.strip():
            raise HTTPException(status_code=400, detail="回复内容不能为空，否则买家会收到空消息")
        return data

    @app.post("/api/reply-rules", dependencies=guard)
    async def create_reply_rule(payload: ReplyRuleIn) -> dict[str, Any]:
        rule_id = await database.save_reply_rule(_check_reply_payload(payload))
        return {"id": rule_id}

    @app.put("/api/reply-rules/{rule_id}", dependencies=guard)
    async def update_reply_rule(rule_id: int, payload: ReplyRuleIn) -> dict[str, Any]:
        if await database.get_reply_rule(rule_id) is None:
            raise HTTPException(status_code=404, detail="自动回复规则不存在")
        await database.save_reply_rule(_check_reply_payload(payload), rule_id)
        # 改完规则想立刻重新生效：顺手清掉这条规则的回复记录
        await database.clear_reply_hits(rule_id)
        return {"id": rule_id}

    @app.delete("/api/reply-rules/{rule_id}", dependencies=guard)
    async def delete_reply_rule(rule_id: int) -> dict[str, bool]:
        await database.delete_reply_rule(rule_id)
        await database.clear_reply_hits(rule_id)
        return {"ok": True}

    @app.post("/api/reply-rules/{rule_id}/toggle", dependencies=guard)
    async def toggle_reply_rule(rule_id: int, enabled: bool = True) -> dict[str, bool]:
        """开关一条规则。和「编辑」分开，改开关不清回复记录。"""
        rule = await database.get_reply_rule(rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="自动回复规则不存在")
        rule["enabled"] = enabled
        await database.save_reply_rule(rule, rule_id)
        return {"ok": True}

    @app.post("/api/reply-rules/{rule_id}/reset", dependencies=guard)
    async def reset_reply_rule(rule_id: int) -> dict[str, bool]:
        """清掉这条规则的回复记录，让「同一会话只回一次」重新生效。"""
        if await database.get_reply_rule(rule_id) is None:
            raise HTTPException(status_code=404, detail="自动回复规则不存在")
        await database.clear_reply_hits(rule_id)
        return {"ok": True}

    @app.get("/api/accounts/{account_id}/items", dependencies=guard)
    async def list_account_items(account_id: int) -> list[dict[str, Any]]:
        """拉这个账号「在售」的商品，方便在规则里选商品 ID 而不是手抄。"""
        account = await database.get_account(account_id)
        if account is None or not account["cookie"]:
            raise HTTPException(status_code=404, detail="账号不存在或没有 Cookie")

        from .client import CookieExpired, XianyuClient, XianyuError

        try:
            client = XianyuClient(account["cookie"])
        except XianyuError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            return await client.list_items()
        except CookieExpired as exc:
            raise HTTPException(status_code=400, detail="Cookie 已失效，请重新扫码登录") from exc
        finally:
            await client.close()

    @app.get("/api/accounts/{account_id}/items/{item_id}/skus", dependencies=guard)
    async def item_skus(account_id: int, item_id: str) -> dict[str, Any]:
        """拉一个商品的规格（天卡/周卡/月卡）与各自价格。

        列表接口只给最低价，多规格必须走商品详情；价格在详情里是「分」，
        client.item_detail 已经换算成元。
        """
        account = await database.get_account(account_id)
        if account is None or not account["cookie"]:
            raise HTTPException(status_code=404, detail="账号不存在或没有 Cookie")

        from .client import CookieExpired, XianyuClient, XianyuError

        try:
            client = XianyuClient(account["cookie"])
        except XianyuError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            return await client.item_detail(item_id)
        except CookieExpired as exc:
            raise HTTPException(status_code=400, detail="Cookie 已失效，请重新扫码登录") from exc
        except XianyuError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await client.close()

    # -------------------------------------------------------------- 卡密

    @app.get("/api/cards", dependencies=guard)
    async def list_cards(status: str | None = None) -> list[dict[str, Any]]:
        return await database.list_cards(status)

    @app.get("/api/cards/stock", dependencies=guard)
    async def card_stock() -> list[dict[str, Any]]:
        return await database.card_stock()

    @app.post("/api/cards", dependencies=guard)
    async def add_cards(payload: CardsIn) -> dict[str, Any]:
        contents = list(payload.contents)
        if payload.raw_text.strip():
            contents.extend(line for line in payload.raw_text.splitlines() if line.strip())
        if not contents:
            raise HTTPException(status_code=400, detail="没有可导入的卡密")
        added = await database.add_cards(contents, payload.item_id)
        return {"added": added}

    @app.delete("/api/cards/{card_id}", dependencies=guard)
    async def delete_card(card_id: int) -> dict[str, bool]:
        await database.delete_card(card_id)
        return {"ok": True}

    # -------------------------------------------------------------- 记录

    @app.get("/api/orders", dependencies=guard)
    async def list_orders(limit: int = 100) -> list[dict[str, Any]]:
        return await database.list_orders(limit)

    @app.get("/api/messages", dependencies=guard)
    async def list_messages(limit: int = 100) -> list[dict[str, Any]]:
        return await database.list_messages(limit)

    @app.delete("/api/messages", dependencies=guard)
    async def clear_messages() -> dict[str, int]:
        """清空消息流水；只删 messages，不影响订单、规则和卡密。"""
        return {"deleted": await database.clear_messages()}

    # -------------------------------------------------------------- 设置

    @app.get("/api/config", dependencies=guard)
    async def show_config(request: Request) -> dict[str, Any]:
        """前端展示用的运行状态；只暴露实例名称/ID 后 8 位，不回显 OpenId 和后台密码。"""
        k2018 = k2018_view()
        health = getattr(mgr(request), "k2018_health", None)
        if health is not None:
            k2018["health"] = health()
        else:
            k2018["health"] = {"ok": None, "error": "", "checked_at": 0.0, "instances": []}
        return {
            "dry_run": settings.dry_run,
            "auto_reply": {
                "enabled": settings.auto_reply_enabled,
                "delay": settings.auto_reply_delay,
            },
            "max_order_cny": settings.max_order_cny,
            "k2018": k2018,
            "social": social_view(),
        }

    @app.post("/api/k2018/check", dependencies=guard)
    async def check_k2018_health(request: Request) -> dict[str, Any]:
        """手动检测 2018K 连接；不会启动定时轮询。"""
        manager = mgr(request)
        checker = getattr(manager, "check_k2018_health", None)
        if checker is not None:
            return await checker()
        health = getattr(manager, "k2018_health", None)
        if health is not None:
            return health()
        return {"ok": None, "error": "", "checked_at": 0.0, "instances": []}

    @app.put("/api/config/social", dependencies=guard)
    async def save_social_config(payload: SocialConfigIn) -> dict[str, Any]:
        """保存订单自动化开关；默认全部关闭，开启时只处理之后的新订单。"""
        was_any = social_any_enabled()
        now_any = bool(
            payload.auto_flower
            or payload.auto_remind_receipt
            or payload.auto_rate
            or payload.auto_receive_flower
        )
        settings.social_auto_flower = bool(payload.auto_flower)
        settings.social_auto_remind = bool(payload.auto_remind_receipt)
        settings.social_auto_rate = bool(payload.auto_rate)
        settings.social_auto_receive_flower = bool(payload.auto_receive_flower)
        settings.social_rate_content = (payload.rate_content or "").strip()[:500]
        if now_any and (not was_any or not settings.social_since):
            settings.social_since = now_iso()
        values = {
            "social_auto_flower": "1" if settings.social_auto_flower else "0",
            "social_auto_remind": "1" if settings.social_auto_remind else "0",
            "social_auto_rate": "1" if settings.social_auto_rate else "0",
            "social_auto_receive_flower": "1" if settings.social_auto_receive_flower else "0",
            "social_rate_content": settings.social_rate_content,
            "social_since": settings.social_since,
        }
        for key, value in values.items():
            await database.set_config(key, value)
        return social_view()

    @app.put("/api/config/k2018", dependencies=guard)
    async def save_k2018_config(payload: K2018ConfigIn, request: Request) -> dict[str, Any]:
        """面板保存 2018K 接入配置：OpenId、实例增删、可选后台账号。"""
        openid = payload.openid.strip() or settings.k2018_openid
        if not re.fullmatch(r"[0-9a-fA-F]{32}", openid or ""):
            raise HTTPException(status_code=400, detail="OpenId 必须是 32 位十六进制字符串")

        instances: list[K2018Instance] = []
        seen_names: set[str] = set()
        seen_ids: set[str] = set()
        for item in payload.instances:
            name = item.name.strip()
            software_id = item.software_id.strip().upper()
            if not name:
                raise HTTPException(status_code=400, detail="实例名不能为空")
            if re.search(r"[,=;|\n]", name):
                raise HTTPException(status_code=400, detail="实例名不能包含逗号、等号、分号或竖线")
            if not re.fullmatch(r"[0-9A-F]{32}", software_id):
                raise HTTPException(status_code=400, detail=f"实例「{name}」的 softwareId 不是 32 位十六进制")
            if name.casefold() in seen_names:
                raise HTTPException(status_code=400, detail=f"实例名重复：{name}")
            if software_id in seen_ids:
                raise HTTPException(status_code=400, detail=f"softwareId 重复：{software_id}")
            seen_names.add(name.casefold())
            seen_ids.add(software_id)
            instances.append(K2018Instance(name=name, software_id=software_id))

        if payload.clear_admin:
            admin_username = ""
            admin_password = ""
        else:
            admin_username = payload.admin_username.strip() or settings.k2018_admin_username
            admin_password = payload.admin_password or settings.k2018_admin_password
            if bool(admin_username) != bool(admin_password):
                raise HTTPException(
                    status_code=400,
                    detail="后台账号和密码必须同时填写；不用自动删卡可以都留空",
                )

        values = {
            "k2018_openid": openid,
            "k2018_instances": ",".join(f"{i.name}={i.software_id}" for i in instances),
            "k2018_admin_username": admin_username,
            "k2018_admin_password": admin_password,
        }
        for key, value in values.items():
            await database.set_config(key, value)
        apply_saved_k2018_config(values)

        # 让已经跑着的 worker 也换到新实例/新账号；换完再关旧连接
        reload = getattr(mgr(request), "reload_k2018", None)
        if reload is not None:
            try:
                await reload()
            except Exception as exc:  # pragma: no cover - 配置已落库，重载失败要能看见
                logger.error("重载 2018K 配置失败：%s", exc)
        return {"ok": True, "k2018": k2018_view()}

    @app.get("/api/status", dependencies=guard)
    async def worker_status(request: Request) -> list[dict[str, Any]]:
        return mgr(request).status()

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        """兜底 500。

        以前这里把 str(exc) 直接回给前端 —— 上游报错里可能带着 URL、
        SQL、甚至凭据片段，等于把内部细节交给任何一个打开页面的人。
        现在完整堆栈只进日志，前端只拿到一个可以拿去 grep 的编号。
        """
        request_id = uuid.uuid4().hex[:12]
        logger.exception("未处理异常（request_id=%s）%s %s: %s",
                         request_id, request.method, request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": f"服务内部错误（request_id={request_id}），请查看服务端日志"},
        )

    return app
