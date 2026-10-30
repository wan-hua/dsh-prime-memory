"""2018K 卡密验证平台客户端。

只做两件事：
  1. 用 OpenId 作为 AES 密钥，加密创建卡密所需的 info；
  2. 调 2018K 的 HTTP API 创建卡密，以及（可选）用后台账号删除卡密做回滚。

协议来自官方 Python 示例与 C# SDK 的 `Tools.ServerEncrypt`：
  AES-CBC / PKCS7，IV 为 16 字节全零，密钥为 OpenId 的十六进制字节。
  OpenId 既不是登录凭据也不是 token，它只承担加解密。

2018K 的公开 API 没有「列出实例」和「删除卡密」接口：
  - 实例 ID（softwareId）必须在后台复制，配置到本项目的 .env；
  - 删除卡密只能走后台账号（admin.2018k.cn/api/adm/delAuth），
    没配后台账号时由调用方记录卡密 ID，交人工处理。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

#: 公开 API 与后台 API 的默认地址
DEFAULT_API_BASE = "https://api.2018k.cn/v3"
DEFAULT_ADMIN_BASE = "https://admin.2018k.cn"

_OPENID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_CARD_FIELDS = ("authId", "networkVerificationId", "cardKey", "key")


class K2018Error(RuntimeError):
    """2018K 接口调用或加解密失败。"""


@dataclass(slots=True)
class K2018Card:
    """一张刚创建出来的 2018K 卡密。"""

    auth_id: str
    days: int
    remark: str
    software_id: str
    #: 接口原始 data（字符串或对象），排查字段变化时用
    raw: Any = None


def _json_of(resp: httpx.Response, what: str) -> dict[str, Any]:
    """2018K 的接口会返回 GBK 编码的中文 message，不能直接 resp.json()。

    先按 UTF-8 解，失败再按 GBK 解；两者都失败才用 replace 兜底，
    这样错误信息不会变成一堆问号，排查时能看懂。
    """
    raw = resp.content
    data: Any = None
    last_error: Exception | None = None
    for encoding in ("utf-8", "gbk"):
        try:
            data = json.loads(raw.decode(encoding))
            break
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = exc
    if data is None:
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise K2018Error(
                what + "返回非 JSON（HTTP " + str(resp.status_code) + "）"
            ) from (last_error or exc)
    if not isinstance(data, dict):
        raise K2018Error(what + "返回结构异常")
    return data


def _message_of(data: dict[str, Any], fallback: str) -> str:
    return str(data.get("message") or fallback)


def _mask(value: str) -> str:
    """日志里只露头尾，避免整串卡密进日志。"""
    text = str(value or "")
    if len(text) <= 10:
        return text[:3] + "***"
    return text[:6] + "…" + text[-4:]


class K2018Client:
    """2018K 公开 API 的最小封装。"""

    def __init__(
        self,
        openid: str,
        *,
        base_url: str = DEFAULT_API_BASE,
        timeout: float = 10.0,
    ) -> None:
        openid = (openid or "").strip()
        if not _OPENID_RE.match(openid):
            raise K2018Error(
                "XY_2018K_OPENID 必须是 32 位十六进制字符串（后台个人中心可见）"
            )
        #: 关键：OpenId 是 AES 密钥，不是登录凭据
        self.openid = openid
        self.base_url = (base_url or DEFAULT_API_BASE).rstrip("/")
        self.timeout = timeout
        self._http: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=5.0),
                headers={"user-agent": "xianyu-auto-reply/0.1"},
                follow_redirects=True,
                limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=90.0),
            )
        return self._http

    # ------------------------------------------------------------ 加解密

    def encrypt(self, data: dict[str, Any]) -> str:
        """按 2018K 协议加密 info：AES-CBC/PKCS7，全零 IV，Base64。"""
        key = bytes.fromhex(self.openid)
        if len(key) not in (16, 24, 32):
            raise K2018Error("OpenId 长度不是合法的 AES 密钥长度")
        plaintext = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        padder = padding.PKCS7(algorithms.AES.block_size).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        return base64.b64encode(ciphertext).decode("ascii")

    def decrypt(self, encoded: str) -> dict[str, Any]:
        """解密 2018K 返回的 data（obtainSoftware 等信息是加密的）。"""
        key = bytes.fromhex(self.openid)
        raw = base64.b64decode(encoded)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).decryptor()
        padded = decryptor.update(raw) + decryptor.finalize()
        unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
        plaintext = (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
        data = json.loads(plaintext)
        if not isinstance(data, dict):
            raise K2018Error("2018K 解密结果不是对象")
        return data

    # -------------------------------------------------------------- 接口

    async def _get(self, path: str, params: dict[str, Any], what: str) -> dict[str, Any]:
        client = await self._client()
        try:
            resp = await client.get(self.base_url + path, params=params)
        except httpx.HTTPError as exc:
            raise K2018Error(what + "网络请求失败：" + str(exc)) from exc
        data = _json_of(resp, what)
        if resp.status_code >= 400:
            raise K2018Error(what + "HTTP " + str(resp.status_code) + "：" + _message_of(data, ""))
        if not data.get("success"):
            raise K2018Error(what + "失败：" + _message_of(data, "code=" + str(data.get("code"))))
        return data

    async def software_info(self, software_id: str) -> dict[str, Any]:
        """读实例信息（只读，用于启动预热和配置自检）。

        返回的 data 是加密的，解密后是实例详情（softwareName 等）。
        """
        software_id = (software_id or "").strip()
        if not software_id:
            raise K2018Error("softwareId 不能为空")
        data = await self._get(
            "/obtainSoftware",
            {"softwareId": software_id, "isAPI": "y"},
            "2018K 查询实例",
        )
        payload = data.get("data")
        if not isinstance(payload, str) or not payload:
            raise K2018Error("2018K 查询实例没有返回加密数据")
        return self.decrypt(payload)

    async def create_card(
        self,
        software_id: str,
        *,
        days: int,
        remark: str = "",
        bind_count: int | None = None,
    ) -> K2018Card:
        """创建一个按天计时的卡密，返回卡密 ID（authId）。"""
        software_id = (software_id or "").strip()
        if not software_id:
            raise K2018Error("创建卡密缺少 softwareId")
        try:
            days = int(days)
        except (TypeError, ValueError) as exc:
            raise K2018Error("卡密天数必须是整数") from exc
        # 2018K 后台把 day/hour/minute 全为 0 视为「永久有效」；
        # 负数不是合法取值，避免把 -1 之类的客户端标记误传给平台。
        if days < 0:
            raise K2018Error("卡密天数不能为负数；永久卡请填 0")
        if days > 36500:
            raise K2018Error("卡密天数过大（" + str(days) + "），请检查换算规则")

        payload: dict[str, Any] = {
            "day": days,
            "remark": (remark or "")[:100],
            "times": int(time.time() * 1000),
        }
        if days == 0:
            # 2018K 后台用 day/hour/minute 全 0 判定永久；正常天数不额外带 hour/minute
            payload["hour"] = 0
            payload["minute"] = 0
        params: dict[str, Any] = {
            "softwareId": software_id,
            "info": self.encrypt(payload),
            "isAPI": "y",
        }
        if bind_count is not None:
            params["bindCount"] = str(int(bind_count))

        data = await self._get("/createNetworkAuthentication", params, "2018K 创建卡密")
        raw = data.get("data")
        auth_id = ""
        if isinstance(raw, str):
            auth_id = raw.strip()
        elif isinstance(raw, dict):
            for key in _CARD_FIELDS:
                value = raw.get(key)
                if value:
                    auth_id = str(value).strip()
                    break
        if not auth_id:
            raise K2018Error("2018K 创建卡密成功但没返回卡密 ID：" + json.dumps(raw, ensure_ascii=False)[:200])
        logger.info(
            "2018K 已创建卡密 %s（实例 %s，%s，备注 %s）",
            _mask(auth_id), software_id[-8:], "永久" if days == 0 else f"{days} 天",
            payload["remark"] or "-",
        )
        return K2018Card(auth_id=auth_id, days=days, remark=payload["remark"],
                         software_id=software_id, raw=raw)

    async def delete_card(self, auth_id: str) -> None:
        """公开 API 没有删除卡密的能力，这里显式失败，避免误以为删掉了。"""
        raise K2018Error(
            "2018K 公开 API 不提供删除卡密接口；请配置 XY_2018K_ADMIN_USERNAME / "
            "XY_2018K_ADMIN_PASSWORD 用后台账号回滚，或到后台手动删除卡密 "
            + _mask(auth_id)
        )


class K2018AdminClient:
    """2018K 后台 API（admin.2018k.cn/api）的最小封装，仅用于删卡回滚。

    后台接口与公开 API 是两套东西：登录拿 token，后续请求带 `token` 头。
    没有后台账号时不要实例化本类。
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_ADMIN_BASE,
        username: str = "",
        password: str = "",
        token: str = "",
        timeout: float = 10.0,
    ) -> None:
        self.base_url = (base_url or DEFAULT_ADMIN_BASE).rstrip("/")
        self.username = username
        self.password = password
        self.token = token
        self.timeout = timeout
        self._http: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=5.0),
                headers={"user-agent": "xianyu-auto-reply/0.1"},
                follow_redirects=True,
                limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=90.0),
            )
        return self._http

    async def login(self) -> None:
        if self.token:
            return
        if not (self.username and self.password):
            raise K2018Error("2018K 后台未配置账号密码")
        client = await self._client()
        try:
            resp = await client.post(
                "/api/adm/login", json={"user": self.username, "password": self.password}
            )
        except httpx.HTTPError as exc:
            raise K2018Error("2018K 后台登录网络请求失败：" + str(exc)) from exc
        data = _json_of(resp, "2018K 后台登录")
        if not data.get("success"):
            raise K2018Error("2018K 后台登录失败：" + _message_of(data, str(resp.status_code)))
        payload = data.get("data") or {}
        token = payload.get("token") if isinstance(payload, dict) else ""
        if not token:
            raise K2018Error("2018K 后台登录成功但没返回 token")
        self.token = str(token)
        logger.info("已登录 2018K 后台")

    async def delete_card(self, auth_id: str) -> bool:
        """按 authId 删除卡密。返回是否成功；失败抛 K2018Error。"""
        auth_id = (auth_id or "").strip()
        if not auth_id:
            raise K2018Error("删除卡密缺少 authId")
        await self.login()
        client = await self._client()
        try:
            resp = await client.post(
                "/api/adm/delAuth",
                json={"authId": auth_id},
                headers={"token": self.token},
            )
        except httpx.HTTPError as exc:
            raise K2018Error("2018K 后台删卡网络请求失败：" + str(exc)) from exc
        data = _json_of(resp, "2018K 后台删卡")
        if not data.get("success"):
            raise K2018Error("2018K 后台删卡失败：" + _message_of(data, str(resp.status_code)))
        logger.info("2018K 已删除卡密 %s", _mask(auth_id))
        return True
