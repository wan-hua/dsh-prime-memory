"""2018K 卡密备注（买家用户名#订单号）的规则测试。"""

from __future__ import annotations

from app.upstream import CARD_LABEL_MAX_CHARS, build_card_label, order_id_from_label

ORDER = "5127382623029001342"
USERNAME = "靓小小虫"


def test_card_label_is_username_plus_order():
    assert build_card_label(USERNAME, ORDER) == USERNAME + "#" + ORDER


def test_card_label_has_no_price_or_prefix():
    label = build_card_label(USERNAME, ORDER)
    assert "元" not in label
    assert "闲鱼" not in label
    assert "1.00" not in label


def test_card_label_without_order_id():
    assert build_card_label(USERNAME, "") == USERNAME
    assert build_card_label(USERNAME, "   ") == USERNAME


def test_card_label_falls_back_when_username_missing():
    assert build_card_label("", ORDER) == "买家#" + ORDER
    assert build_card_label("   ", ORDER) == "买家#" + ORDER


def test_card_label_sanitizes_separator_and_whitespace():
    assert build_card_label("  小\n王 #1  ", " 123 ") == "小 王 ＃1#123"


def test_card_label_keeps_full_order_id_when_username_is_long():
    # 卡密备注上限 100 字符，用户名要真的超长才会被截
    username = "一个特别特别特别特别特别特别长特别特别特别特别特别特别长特别特别特别特别特别特别长" * 2
    label = build_card_label(username, ORDER)
    assert ORDER in label
    assert len(label) <= CARD_LABEL_MAX_CHARS
    assert username not in label


def test_card_label_shrinks_absurdly_long_order_id():
    order = "9" * 200
    label = build_card_label(USERNAME, order)
    assert len(label) <= CARD_LABEL_MAX_CHARS
    assert order[-10:] in label


def test_order_id_from_label():
    assert order_id_from_label(USERNAME + "#" + ORDER) == ORDER
    assert order_id_from_label("闲鱼自动发货 1.00元 #" + ORDER) == ORDER  # 老数据兼容
    assert order_id_from_label("闲鱼自动发货 10.00元") == ""
    assert order_id_from_label("") == ""
