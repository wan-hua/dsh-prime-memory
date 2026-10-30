"""消息类型与按订单补买家名的单元测试。"""

from __future__ import annotations

from app.codec import is_trade_content
from app.db import Database


def test_trade_content_classifier():
    assert is_trade_content("[卡片消息]\n去查看")
    assert is_trade_content("[买家确认收货，交易成功]")
    assert is_trade_content("快给ta一个评价吧～\n去评价")
    assert not is_trade_content("在吗")
    assert not is_trade_content("这个卡密怎么用")


async def test_backfill_buyer_name_by_order(tmp_path):
    db = Database(tmp_path / "identity.db")
    await db.init()
    await db.log_message(
        account_id=1, chat_id="c1", sender_id="system-1", sender_name="买家",
        order_id="o1", content="[卡片消息]\n去查看", kind="incoming", msg_type="trade",
    )
    await db.log_message(
        account_id=1, chat_id="c2", sender_id="system-2", sender_name="买家",
        order_id="o1", content="[买家确认收货，交易成功]", kind="incoming", msg_type="trade",
    )
    await db.log_message(
        account_id=1, chat_id="c3", sender_id="buyer-1", sender_name="xxxx",
        order_id="o1", content="在吗", kind="incoming", msg_type="chat",
    )

    changed = await db.backfill_buyer_name_by_order(1, "o1", "xxxx", buyer_id="buyer-1")
    assert changed == 2
    rows = await db.list_messages()
    trade = [r for r in rows if r["msg_type"] == "trade"]
    chat = [r for r in rows if r["msg_type"] == "chat"]
    assert len(trade) == 2 and len(chat) == 1
    assert {r["sender_name"] for r in trade} == {"xxxx"}
    assert {r["sender_id"] for r in trade} == {"buyer-1"}
    assert chat[0]["sender_name"] == "xxxx"


async def test_old_messages_get_type_backfill(tmp_path):
    path = tmp_path / "legacy.db"
    db = Database(path)
    await db.init()
    await db.log_message(
        account_id=1, chat_id="c1", sender_id="u1", sender_name="买家",
        content="快给ta一个评价吧～\n去评价", kind="incoming",
    )
    await db.log_message(
        account_id=1, chat_id="c1", sender_id="u1", sender_name="买家",
        content="在吗", kind="incoming",
    )
    # 重新 init 会触发旧消息类型回填
    await db.init()
    rows = await db.list_messages()
    by_content = {r["content"]: r["msg_type"] for r in rows}
    assert by_content["快给ta一个评价吧～\n去评价"] == "trade"
    assert by_content["在吗"] == "chat"
