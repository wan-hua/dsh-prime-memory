"""Web 接口的冒烟测试。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.config import settings
from app.db import Database
from app.web import create_app


class StubManager:
    """替身：完全不碰网络，只记录 sync 调用。"""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.synced = 0
        self.health_checks = 0

    async def sync(self) -> None:
        self.synced += 1

    async def stop_all(self) -> None:
        pass

    def status(self) -> list[dict[str, Any]]:
        return [{"account_id": 1, "name": "主号", "status": "online", "last_error": ""}]

    def k2018_health(self) -> dict[str, Any]:
        return {"ok": None, "error": "", "checked_at": 0.0, "instances": []}

    async def check_k2018_health(self) -> dict[str, Any]:
        self.health_checks += 1
        return {"ok": True, "error": "", "checked_at": 1.0, "instances": []}


@pytest.fixture()
async def client(tmp_path, monkeypatch):
    # 测试不依赖本机 .env 里的管理口令；个别用例要测鉴权会自己再设置
    monkeypatch.setattr(settings, "admin_token", "")
    monkeypatch.setattr(settings, "admin_allow_remote", False)
    db = Database(tmp_path / "web.db")
    app = create_app(db=db, manager=StubManager(db))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            yield ac


async def test_index_page(client):
    res = await client.get("/")
    assert res.status_code == 200
    assert "闲鱼自动发货" in res.text


async def test_manual_k2018_health_endpoint(client):
    res = await client.post("/api/k2018/check")
    assert res.status_code == 200
    assert res.json()["ok"] is True


async def test_social_settings_save(client, monkeypatch):
    for attr in ("social_auto_flower", "social_auto_remind", "social_auto_rate"):
        monkeypatch.setattr(settings, attr, False)
    monkeypatch.setattr(settings, "social_since", "")
    monkeypatch.setattr(settings, "social_rate_content", "")
    res = await client.put("/api/config/social", json={
        "auto_flower": True,
        "auto_remind_receipt": True,
        "auto_rate": False,
        "rate_content": "很好，合作愉快！",
    })
    assert res.status_code == 200
    social = (await client.get("/api/config")).json()["social"]
    assert social["auto_flower"] is True
    assert social["auto_remind_receipt"] is True
    assert social["auto_rate"] is False
    assert social["auto_receive_flower"] is True
    assert social["rate_content"] == "很好，合作愉快！"
    assert social["since"]


async def test_account_crud_hides_cookie(client):
    res = await client.post("/api/accounts", json={"name": "主号", "cookie": "unb=1; _m_h5_tk=abc_1"})
    assert res.status_code == 200
    accounts = (await client.get("/api/accounts")).json()
    assert accounts[0]["name"] == "主号"
    assert accounts[0]["cookie"] == "***"


async def test_cookie_survives_remark_only_update(client):
    await client.post("/api/accounts", json={"name": "主号", "cookie": "unb=1; _m_h5_tk=abc_1"})
    await client.post("/api/accounts", json={"name": "主号", "cookie": "", "remark": "改个备注"})
    row = await client.get("/api/accounts")
    accounts = row.json()
    assert accounts[0]["remark"] == "改个备注"


async def test_rule_and_card_flow(client):
    res = await client.post("/api/rules", json={
        "name": "月卡", "item_id": "*", "mode": "card", "content": "卡密：{card}",
    })
    assert res.status_code == 200

    res = await client.post("/api/cards", json={"item_id": "*", "raw_text": "A-1\nA-2"})
    assert res.json()["added"] == 2

    stock = (await client.get("/api/cards/stock")).json()
    assert stock[0]["available"] == 2

    rules = (await client.get("/api/rules")).json()
    assert rules[0]["mode"] == "card"


async def test_rejects_empty_cards(client):
    res = await client.post("/api/cards", json={"item_id": "*", "raw_text": "   "})
    assert res.status_code == 400


async def test_reply_rules_crud(client):
    res = await client.post("/api/reply-rules", json={
        "name": "发货时间", "kind": "keyword", "keywords": "发货, 多久",
        "match_type": "contains", "content": "24 小时内发 {buyer}", "cooldown": 30,
    })
    assert res.status_code == 200
    rule_id = res.json()["id"]

    res = await client.post("/api/reply-rules", json={
        "name": "商品默认", "kind": "item", "item_id": "900052644277",
        "content": "在的，拍下自动发货",
    })
    assert res.status_code == 200

    rules = (await client.get("/api/reply-rules")).json()
    assert [r["name"] for r in rules] == ["发货时间", "商品默认"]
    assert rules[0]["keywords"] == "发货, 多久"
    assert rules[0]["once_per_chat"] == 1

    res = await client.put(f"/api/reply-rules/{rule_id}", json={
        "name": "发货时间（改）", "kind": "keyword", "keywords": "发货",
        "content": "改好了", "enabled": False,
    })
    assert res.status_code == 200
    rules = (await client.get("/api/reply-rules")).json()
    assert rules[0]["name"] == "发货时间（改）"
    assert rules[0]["enabled"] == 0

    assert (await client.delete(f"/api/reply-rules/{rule_id}")).status_code == 200
    assert len((await client.get("/api/reply-rules")).json()) == 1


async def test_reply_rule_requires_keywords_for_keyword_kind(client):
    res = await client.post("/api/reply-rules", json={
        "name": "缺关键词", "kind": "keyword", "keywords": "  ", "content": "回复",
    })
    assert res.status_code == 400
    assert "关键词" in res.json()["detail"]


async def test_reply_rule_requires_content(client):
    res = await client.post("/api/reply-rules", json={
        "name": "缺内容", "kind": "item", "content": "   ",
    })
    assert res.status_code == 400


async def test_reply_rule_reset_clears_hits(client):
    rule_id = (await client.post("/api/reply-rules", json={
        "name": "重置", "kind": "item", "content": "在的",
    })).json()["id"]
    db = client._transport.app.state.db
    await db.record_reply_hit(1, "3149637063", "47983389096", rule_id)
    assert await db.fetch_one("SELECT id FROM reply_hits") is not None

    res = await client.post(f"/api/reply-rules/{rule_id}/reset")
    assert res.status_code == 200
    assert await db.fetch_one("SELECT id FROM reply_hits") is None


async def test_reply_rule_toggle(client):
    rule_id = (await client.post("/api/reply-rules", json={
        "name": "开关", "kind": "item", "content": "在的",
    })).json()["id"]
    res = await client.post(f"/api/reply-rules/{rule_id}/toggle?enabled=false")
    assert res.status_code == 200
    rules = (await client.get("/api/reply-rules")).json()
    assert rules[0]["enabled"] == 0
    await client.post(f"/api/reply-rules/{rule_id}/toggle?enabled=true")
    assert (await client.get("/api/reply-rules")).json()[0]["enabled"] == 1

    assert (await client.post("/api/reply-rules/999/toggle")).status_code == 404


async def test_reply_rule_update_unknown_returns_404(client):
    res = await client.put("/api/reply-rules/999", json={
        "name": "x", "kind": "item", "content": "y",
    })
    assert res.status_code == 404


async def test_config_exposes_auto_reply_switch(client):
    cfg = (await client.get("/api/config")).json()
    assert "auto_reply" in cfg
    assert cfg["auto_reply"]["enabled"] is True


async def test_config_exposes_k2018_state(client, monkeypatch):
    """/api/config 要带上 2018K 实例列表，但不能回显 OpenId。"""
    from app.config import K2018Instance, settings

    monkeypatch.setattr(settings, "k2018_openid", "656BCE7D7B384CD5A39C524E3655AA3F")
    monkeypatch.setattr(settings, "k2018_instances", [
        K2018Instance("Miracles", "B4ADC92FDF7240398D8A80A4176C80BD"),
        K2018Instance("MiraclesX64", "8574EF6DF15C4A35BDEC1D2C74194F35"),
    ])
    monkeypatch.setattr(settings, "k2018_admin_username", "u")
    monkeypatch.setattr(settings, "k2018_admin_password", "p")

    cfg = (await client.get("/api/config")).json()
    k = cfg["k2018"]
    assert k["ready"] is True
    assert k["openid_set"] is True
    assert k["admin_ready"] is True
    assert [it["name"] for it in k["instances"]] == ["Miracles", "MiraclesX64"]
    assert k["instances"][0]["software_id"] == "B4ADC92FDF7240398D8A80A4176C80BD"
    assert "openid" not in k
    assert "k2018_openid" not in cfg


async def test_rule_accepts_2018k_fields(client):
    """规则要能保存实例、时长模式和天数。"""
    res = await client.post("/api/rules", json={
        "name": "月卡", "item_id": "900052644277", "mode": "upstream",
        "content": "卡密：{card}，{days} 天", "auto_confirm": True,
        "software_id": "B4ADC92FDF7240398D8A80A4176C80BD",
        "duration_mode": "fixed", "fixed_days": 30, "days_per_cny": 1.0,
    })
    assert res.status_code == 200
    rule = (await client.get("/api/rules")).json()[0]
    assert rule["software_id"] == "B4ADC92FDF7240398D8A80A4176C80BD"
    assert rule["duration_mode"] == "fixed"
    assert rule["fixed_days"] == 30
    assert rule["days_per_cny"] == pytest.approx(1.0)


async def test_k2018_config_save_and_clear_admin(client, monkeypatch):
    """面板可保存 OpenId/实例/后台账号；实例可删空，后台账号可清空。"""
    from app.config import settings

    monkeypatch.setattr(settings, "k2018_openid", "")
    monkeypatch.setattr(settings, "k2018_instances", [])
    monkeypatch.setattr(settings, "k2018_admin_username", "")
    monkeypatch.setattr(settings, "k2018_admin_password", "")

    res = await client.put("/api/config/k2018", json={
        "openid": "A" * 32,
        "instances": [
            {"name": "Miracles", "software_id": "B" * 32},
            {"name": "MiraclesX64", "software_id": "C" * 32},
        ],
        "admin_username": "admin",
        "admin_password": "secret",
    })
    assert res.status_code == 200
    k = res.json()["k2018"]
    assert k["ready"] is True
    assert k["openid_set"] is True
    assert k["admin_ready"] is True
    assert [i["name"] for i in k["instances"]] == ["Miracles", "MiraclesX64"]
    assert "openid" not in k and "admin_password" not in k

    # 再保存：OpenID 留空保持；删掉实例、清空后台账号
    res = await client.put("/api/config/k2018", json={
        "openid": "",
        "instances": [],
        "clear_admin": True,
    })
    assert res.status_code == 200
    k = res.json()["k2018"]
    assert k["ready"] is False
    assert k["instances"] == []
    assert k["admin_ready"] is False


async def test_k2018_config_requires_openid_on_first_run(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "k2018_openid", "")
    monkeypatch.setattr(settings, "k2018_instances", [])
    res = await client.put("/api/config/k2018", json={
        "openid": "",
        "instances": [{"name": "Miracles", "software_id": "B" * 32}],
    })
    assert res.status_code == 400
    assert "OpenId" in res.json()["detail"]


async def test_k2018_config_rejects_bad_software_id(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "k2018_openid", "A" * 32)
    res = await client.put("/api/config/k2018", json={
        "instances": [{"name": "坏实例", "software_id": "not-a-software-id"}],
    })
    # Pydantic 的长度校验会先于路由返回 422；UI 侧也会先拦一次
    assert res.status_code == 422


async def test_k2018_config_survives_restart(tmp_path, monkeypatch):
    """数据库里保存过的配置，新进程启动后要覆盖 .env 的旧值。"""
    from app.config import settings

    monkeypatch.setattr(settings, "admin_token", "")
    monkeypatch.setattr(settings, "admin_allow_remote", False)
    monkeypatch.setattr(settings, "k2018_openid", "")
    monkeypatch.setattr(settings, "k2018_instances", [])
    monkeypatch.setattr(settings, "k2018_admin_username", "")
    monkeypatch.setattr(settings, "k2018_admin_password", "")

    db = Database(tmp_path / "persist.db")
    await db.init()
    await db.set_config("k2018_openid", "D" * 32)
    await db.set_config("k2018_instances", "Miracles=" + "E" * 32)
    await db.set_config("k2018_admin_username", "saved-admin")
    await db.set_config("k2018_admin_password", "saved-pass")

    app = create_app(db=db, manager=StubManager(db))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            cfg = (await ac.get("/api/config")).json()["k2018"]
    assert cfg["ready"] is True
    assert cfg["instances"][0]["name"] == "Miracles"
    assert cfg["instances"][0]["software_id"] == "E" * 32
    assert cfg["admin_ready"] is True


async def test_rule_accepts_price_tiers(client):
    res = await client.post("/api/rules", json={
        "name": "多规格", "item_id": "1089011827182", "mode": "upstream",
        "content": "卡密：{card}，{days} 天",
        "software_id": "B4ADC92FDF7240398D8A80A4176C80BD",
        "duration_mode": "tiers",
        "price_tiers": [
            {"sku_id": "s1", "label": "天卡", "price": 1.0, "days": 1},
            {"sku_id": "s2", "label": "周卡", "price": 5.0, "days": 7},
            {"sku_id": "s3", "label": "月卡", "price": 10.0, "days": 30},
        ],
    })
    assert res.status_code == 200
    rule = (await client.get("/api/rules")).json()[0]
    assert rule["duration_mode"] == "tiers"
    tiers = json.loads(rule["price_tiers"]) if isinstance(rule["price_tiers"], str) else rule["price_tiers"]
    assert [t["label"] for t in tiers] == ["天卡", "周卡", "月卡"]
    assert [t["days"] for t in tiers] == [1, 7, 30]


async def test_rule_accepts_minidaily_mode(client):
    res = await client.post("/api/rules", json={
        "name": "MiniDaily 月卡", "item_id": "1089011827182", "mode": "minidaily",
        "content": "入池链接：{qr_url}，{days} 天",
        "fixed_days": 30,
    })
    assert res.status_code == 200
    rule = (await client.get("/api/rules")).json()[0]
    assert rule["mode"] == "minidaily"
    assert rule["fixed_days"] == 30
    assert rule["software_id"] == ""


async def test_rule_rejects_invalid_price_tier(client):
    res = await client.post("/api/rules", json={
        "name": "坏档位", "mode": "upstream", "content": "y",
        "duration_mode": "tiers",
        "price_tiers": [{"label": "天卡", "price": 1.0, "days": -1}],
    })
    assert res.status_code == 422


async def test_rule_defaults_to_per_product_fixed(client):
    """不传时长字段时，默认按「本商品固定 1 天」，避免算出 0 天卡。"""
    res = await client.post("/api/rules", json={
        "name": "默认", "item_id": "1089011827182", "mode": "upstream",
        "content": "卡密：{card}", "software_id": "B4ADC92FDF7240398D8A80A4176C80BD",
    })
    assert res.status_code == 200
    rule = (await client.get("/api/rules")).json()[0]
    assert rule["duration_mode"] == "fixed"
    assert rule["fixed_days"] == 1


async def test_rule_accepts_permanent_zero_days(client):
    res = await client.post("/api/rules", json={
        "name": "永久卡", "item_id": "1089011827182", "mode": "upstream",
        "content": "有效期 {days}", "software_id": "B4ADC92FDF7240398D8A80A4176C80BD",
        "duration_mode": "fixed", "fixed_days": 0,
    })
    assert res.status_code == 200
    rule = (await client.get("/api/rules")).json()[0]
    assert rule["fixed_days"] == 0

    res = await client.post("/api/rules", json={
        "name": "永久档位", "item_id": "1089011827182", "mode": "upstream",
        "content": "有效期 {days}", "software_id": "B4ADC92FDF7240398D8A80A4176C80BD",
        "duration_mode": "tiers",
        "price_tiers": [{"label": "永久", "price": 99.0, "days": 0}],
    })
    assert res.status_code == 200


async def test_rule_rejects_invalid_duration_mode(client):
    res = await client.post("/api/rules", json={
        "name": "x", "mode": "upstream", "content": "y",
        "duration_mode": "forever",
    })
    assert res.status_code == 422


async def test_config_requires_admin_token_when_set(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "admin_token", "s3cret")
    assert (await client.get("/api/config")).status_code == 401
    res = await client.get("/api/config", headers={"X-Admin-Token": "s3cret"})
    assert res.status_code == 200
