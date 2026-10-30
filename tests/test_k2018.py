"""2018K 客户端与建卡门面的单元测试（全部离线）。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import K2018Instance, settings
from app.k2018 import K2018AdminClient, K2018Card, K2018Client, K2018Error
from app.upstream import Provisioned, Provisioner, UpstreamError

OPENID = "656BCE7D7B384CD5A39C524E3655AA3F"
SID = "B4ADC92FDF7240398D8A80A4176C80BD"

#: 用固定输入算出的密文（AES-128-CBC / 零 IV / PKCS7）
VECTOR = {
    "day": 1,
    "remark": "probe",
    "times": 1700000000000,
}
VECTOR_B64 = (
    "sdBVvdMHItZKYbfZQclu8ykumFSF+ratb7LiuKp8Ov6rNXBRikCU1wkP0T3OCnqyo+"
    "X+ASDAX3/TYzuuAQ4+7w=="
)


def make_client(handler) -> K2018Client:
    client = K2018Client(OPENID)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def test_encrypt_matches_known_vector():
    client = K2018Client(OPENID)
    assert client.encrypt(VECTOR) == VECTOR_B64
    await client.close()


async def test_decrypt_roundtrip():
    client = K2018Client(OPENID)
    assert client.decrypt(VECTOR_B64) == VECTOR


async def test_create_card_sends_encrypted_info_and_returns_auth_id():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        params = dict(request.url.params)
        seen.update(params)
        client = K2018Client(OPENID)
        info = json.loads(json.dumps(client.decrypt(params["info"])))
        assert info["day"] == 7
        assert info["remark"] == "买家#1"
        return httpx.Response(200, json={
            "code": 0, "success": True, "message": "创建成功",
            "data": "63682AD530C04ABDAD0521C50544F90F",
        })

    client = make_client(handler)
    card = await client.create_card(SID, days=7, remark="买家#1")
    assert card.auth_id == "63682AD530C04ABDAD0521C50544F90F"
    assert card.days == 7
    assert seen["path"].endswith("/createNetworkAuthentication")
    assert seen["softwareId"] == SID
    assert seen["isAPI"] == "y"
    await client.close()


async def test_create_card_reads_gbk_error_message():
    def handler(request: httpx.Request) -> httpx.Response:
        body = '{"code":1007,"success":false,"message":"创建卡密:卡密信息不能为空"}'.encode("gbk")
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    client = make_client(handler)
    with pytest.raises(K2018Error) as exc:
        await client.create_card(SID, days=1, remark="x")
    assert "卡密信息不能为空" in str(exc.value)
    await client.close()


async def test_create_card_accepts_permanent_zero_days():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        info = K2018Client(OPENID).decrypt(params["info"])
        seen.update(info)
        return httpx.Response(200, json={
            "code": 0, "success": True, "message": "创建成功",
            "data": "63682AD530C04ABDAD0521C50544F90F",
        })

    client = make_client(handler)
    card = await client.create_card(SID, days=0, remark="永久卡")
    assert card.days == 0
    assert seen["day"] == 0
    assert seen["hour"] == 0
    assert seen["minute"] == 0
    await client.close()


async def test_create_card_rejects_negative_days():
    client = K2018Client(OPENID)
    with pytest.raises(K2018Error):
        await client.create_card(SID, days=-1, remark="x")
    await client.close()


async def test_admin_login_and_delete_card():
    calls: list[tuple[str, str | None, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        calls.append((request.url.path, request.headers.get("token"), body))
        if request.url.path.endswith("/adm/login"):
            return httpx.Response(200, json={"code": 0, "success": True, "data": {"token": "T-1"}})
        return httpx.Response(200, json={"code": 0, "success": True, "message": "删除成功"})

    admin = K2018AdminClient(username="u", password="p")
    admin._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://admin.2018k.cn"
    )
    assert await admin.delete_card("63682AD530C04ABDAD0521C50544F90F") is True
    assert calls[0][0].endswith("/adm/login")
    assert calls[0][2] == {"user": "u", "password": "p"}
    assert calls[1][0].endswith("/adm/delAuth")
    assert calls[1][1] == "T-1"
    assert calls[1][2] == {"authId": "63682AD530C04ABDAD0521C50544F90F"}
    await admin.close()


class FakeK2018:
    def __init__(self, days: int = 10, info_fail: bool = False,
                 create_fail: bool = False) -> None:
        self.calls: list[tuple[str, int, str]] = []
        self.days = days
        self.info_fail = info_fail
        self.create_fail = create_fail

    async def software_info(self, software_id: str) -> dict:
        if self.info_fail:
            raise K2018Error("2018K 查询实例网络请求失败")
        return {"softwareName": "Miracles", "versionNumber": "1.0"}

    async def create_card(self, software_id: str, *, days: int, remark: str) -> K2018Card:
        self.calls.append((software_id, days, remark))
        if self.create_fail:
            raise K2018Error("创建失败:请求数据已经过期")
        return K2018Card("CARD-1", days, remark, software_id)

    async def close(self) -> None:
        pass


class FakeAdmin:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_card(self, auth_id: str) -> bool:
        self.deleted.append(auth_id)
        return True

    async def close(self) -> None:
        pass


async def test_provisioner_health_tracks_instance_probe(monkeypatch):
    monkeypatch.setattr(settings, "k2018_instances", [K2018Instance("Miracles", SID)])
    provisioner = Provisioner(client=FakeK2018(), enable_admin=False)  # type: ignore[arg-type]
    await provisioner.warmup(quiet=True)
    health = provisioner.health()
    assert health["ok"] is True
    assert health["instances"][0]["name"] == "Miracles"

    bad = Provisioner(client=FakeK2018(info_fail=True), enable_admin=False)  # type: ignore[arg-type]
    await bad.warmup(quiet=True)
    assert bad.health()["ok"] is False
    assert "网络请求失败" in bad.health()["error"]


async def test_create_card_failure_does_not_mark_connection_unhealthy(monkeypatch):
    """建卡失败是单笔业务失败，不能把实例自检状态翻成异常。"""
    monkeypatch.setattr(settings, "k2018_instances", [K2018Instance("Miracles", SID)])
    provisioner = Provisioner(
        client=FakeK2018(create_fail=True), enable_admin=False  # type: ignore[arg-type]
    )
    await provisioner.warmup(quiet=True)
    assert provisioner.health()["ok"] is True

    with pytest.raises(UpstreamError):
        await provisioner.provision(SID, 1, "买家#1", username="买家")
    assert provisioner.health()["ok"] is True
    assert provisioner.health()["error"] == ""


async def test_provisioner_uses_configured_instance(monkeypatch):
    monkeypatch.setattr(settings, "k2018_instances", [K2018Instance("Miracles", SID)])
    fake = FakeK2018()
    provisioner = Provisioner(client=fake, enable_admin=False)  # type: ignore[arg-type]
    result = await provisioner.provision(SID, 10, "买家小王#123", username="买家小王")
    assert result.card_code == "CARD-1"
    assert result.days == 10
    assert result.software_id == SID
    assert fake.calls[0][0] == SID
    assert fake.calls[0][1] == 10
    assert fake.calls[0][2] == "买家小王#123"


async def test_provisioner_rejects_unknown_instance(monkeypatch):
    monkeypatch.setattr(settings, "k2018_instances", [K2018Instance("Miracles", SID)])
    provisioner = Provisioner(client=FakeK2018(), enable_admin=False)  # type: ignore[arg-type]
    with pytest.raises(UpstreamError):
        await provisioner.provision("00000000000000000000000000000000", 1, "x")


async def test_revoke_without_admin_keeps_card_for_manual_delete():
    provisioner = Provisioner(client=FakeK2018(), enable_admin=False)  # type: ignore[arg-type]
    ok, note = await provisioner.revoke(
        Provisioned("CARD-1", "CARD…1", 10, SID, "买家#1")
    )
    assert ok is False
    assert "手动删除" in note
    assert "CARD-1" in note


async def test_revoke_with_admin_deletes_card():
    admin = FakeAdmin()
    provisioner = Provisioner(client=FakeK2018(), admin=admin, enable_admin=False)  # type: ignore[arg-type]
    ok, note = await provisioner.revoke(
        Provisioned("CARD-1", "CARD…1", 10, SID, "买家#1")
    )
    assert ok is True
    assert admin.deleted == ["CARD-1"]
    assert "已删除" in note
