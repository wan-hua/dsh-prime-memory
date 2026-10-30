"""MiniDailyReward 二维码客户端与新发货模式测试。"""

from __future__ import annotations

import json

import httpx

from app.minidaily import MiniDailyClient, MiniDailyError


async def test_create_invite_posts_days_and_token():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={
            "ok": True,
            "data": {
                "token": "invite-token",
                "url": "https://scancode.miraclesplus.top/join?t=invite-token",
                "days": 7,
                "expire_text": "10-27 01:10:29",
                "svg": "<svg/>",
            },
        })

    client = MiniDailyClient(
        base_url="http://127.0.0.1:8790",
        token="test-token",
        transport=httpx.MockTransport(handler),
    )
    invite = await client.create_invite(7, note="买家#订单")
    assert seen == {
        "path": "/api/invites",
        "auth": "Bearer test-token",
        "body": {"days": 7, "note": "买家#订单"},
    }
    assert invite.token == "invite-token"
    assert invite.days == 7
    assert invite.url.endswith("invite-token")


async def test_create_invite_reports_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "error": "挂载天数不能为负"})

    client = MiniDailyClient(
        base_url="http://127.0.0.1:8790",
        token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.create_invite(1)
    except MiniDailyError as exc:
        assert "挂载天数不能为负" in str(exc)
    else:
        raise AssertionError("应当抛出 MiniDailyError")
