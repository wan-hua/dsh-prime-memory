"""价格档位（天卡/周卡/月卡）的解析与匹配测试。"""

from __future__ import annotations

import json

from app.engine import parse_price_tiers, pick_price_tier

TIERS = [
    {"sku_id": "s1", "label": "天卡", "price": 1.0, "days": 1},
    {"sku_id": "s2", "label": "周卡", "price": 5.0, "days": 7},
    {"sku_id": "s3", "label": "月卡", "price": 10.0, "days": 30},
]


def test_parse_price_tiers_accepts_json_and_skips_bad_rows():
    raw = json.dumps([
        {"sku_id": "s1", "label": "天卡", "price": "1", "days": 1},
        {"sku_id": "bad", "price": "x", "days": 7},
        {"sku_id": "zero", "price": 5, "days": 0},
        {"label": "周卡", "price": 5, "days": 7},
    ])
    tiers = parse_price_tiers(raw)
    # days=0 是永久卡，必须保留；没有 label 的档位用「规格」兜底
    assert [(t["label"], t["days"]) for t in tiers] == [("天卡", 1), ("规格", 0), ("周卡", 7)]
    assert tiers[2]["price"] == 5.0
    assert tiers[2]["days"] == 7


def test_parse_price_tiers_handles_invalid_json():
    assert parse_price_tiers("not-json") == []
    assert parse_price_tiers(None) == []


def test_pick_price_tier_exact_match():
    assert pick_price_tier(TIERS, 1.0)["label"] == "天卡"
    assert pick_price_tier(TIERS, 5.0)["label"] == "周卡"
    assert pick_price_tier(TIERS, 10.0)["label"] == "月卡"


def test_pick_price_tier_nearest_for_small_discount():
    # 卖家改价/优惠后实付 4.5，仍应匹配周卡
    assert pick_price_tier(TIERS, 4.5)["label"] == "周卡"


def test_pick_price_tier_rejects_absurd_amount():
    assert pick_price_tier(TIERS, 100.0) is None
    assert pick_price_tier(TIERS, 0) is None
    assert pick_price_tier([], 5.0) is None
