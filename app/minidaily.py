"""MiniDailyReward 扫码入池邀请客户端。

MiniDailyReward 的控制台默认只监听 127.0.0.1:8790，使用 Bearer token。
这里只调用生成邀请的接口，不直接操作账号池；二维码/链接由对方服务生成。
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import settings


class MiniDailyError(RuntimeError):
    """MiniDailyReward 接口调用失败。"""


@dataclass(slots=True)
class MiniDailyInvite:
    token: str
    url: str
    days: int
    expire_text: str = ""
    svg: str = ""


class MiniDailyClient:
    """极简客户端：每次生成邀请时复用一次短连接。"""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or settings.minidaily_api_base).rstrip("/")
        self.token = (token if token is not None else settings.minidaily_token).strip()
        self.transport = transport

    async def create_invite(self, days: int, note: str = "") -> MiniDailyInvite:
        """创建一次性扫码入池邀请；days=0 表示永久挂载。"""
        if not self.base_url:
            raise MiniDailyError("未配置 MiniDailyReward 地址（XY_MINIDAILY_API_BASE）")
        if not self.token:
            raise MiniDailyError("未配置 MiniDailyReward 令牌（XY_MINIDAILY_TOKEN）")
        try:
            days = int(days)
        except (TypeError, ValueError) as exc:
            raise MiniDailyError("二维码挂载天数必须是整数") from exc
        if days < 0:
            raise MiniDailyError("二维码挂载天数不能为负数")

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                transport=self.transport,
            ) as client:
                resp = await client.post(
                    self.base_url + "/api/invites",
                    headers={
                        "Authorization": "Bearer " + self.token,
                        "Content-Type": "application/json",
                    },
                    json={"days": days, "note": note},
                )
        except httpx.HTTPError as exc:
            raise MiniDailyError("MiniDailyReward 网络请求失败：" + str(exc)) from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise MiniDailyError(
                "MiniDailyReward 返回非 JSON（HTTP " + str(resp.status_code) + "）"
            ) from exc
        if resp.status_code != 200 or not payload.get("ok"):
            raise MiniDailyError(
                "MiniDailyReward 生成二维码失败："
                + str(payload.get("error") or ("HTTP " + str(resp.status_code)))
            )
        data = payload.get("data") or {}
        token = str(data.get("token") or "").strip()
        url = str(data.get("url") or "").strip()
        if not token or not url:
            raise MiniDailyError("MiniDailyReward 生成成功但没有返回邀请链接")
        try:
            result_days = int(data.get("days", days))
        except (TypeError, ValueError):
            result_days = days
        return MiniDailyInvite(
            token=token,
            url=url,
            days=result_days,
            expire_text=str(data.get("expire_text") or ""),
            svg=str(data.get("svg") or ""),
        )
