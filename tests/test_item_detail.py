"""闲鱼商品详情 / 多规格（SKU）解析测试。

列表接口只返回最低价，天卡/周卡/月卡必须从 `mtop.taobao.idle.pc.detail`
的 `data.itemDO.skuList` 取；价格单位是分，要在 client 层换算成元。
"""

from __future__ import annotations

import httpx

from app.client import XianyuClient, XianyuError

ITEM_ID = "1089011827182"

DETAIL = {
    "ret": ["SUCCESS::调用成功"],
    "data": {
        "itemDO": {
            "itemId": ITEM_ID,
            "title": "pc端迷你世界辅助工具64位版本",
            "soldPrice": "1",
            "skuList": [
                {
                    "skuId": "6310445158135",
                    "price": 100,
                    "priceInCent": 100,
                    "quantity": 994,
                    "propertyList": [{"valueText": "天卡"}],
                },
                {
                    "skuId": "6310445158136",
                    "price": 500,
                    "priceInCent": 500,
                    "quantity": 998,
                    "propertyList": [{"valueText": "周卡"}],
                },
                {
                    "skuId": "6310445158137",
                    "price": 1000,
                    "priceInCent": 1000,
                    "quantity": 997,
                    "propertyList": [{"valueText": "月卡"}],
                },
            ],
        }
    },
}

LIST = {
    "ret": ["SUCCESS::调用成功"],
    "data": {
        "cardList": [
            {
                "cardData": {
                    "id": ITEM_ID,
                    "title": "pc端迷你世界辅助工具64位版本",
                    "priceInfo": {"price": "1"},
                    "detailParams": {"isSKU": "1", "soldPrice": "1"},
                }
            }
        ]
    },
}


async def test_item_detail_converts_sku_prices_to_yuan():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/mtop.taobao.idle.pc.detail/1.0/")
        return httpx.Response(200, json=DETAIL)

    client = XianyuClient("unb=1; _m_h5_tk=tok_1", transport=httpx.MockTransport(handler))
    try:
        detail = await client.item_detail(ITEM_ID)
    finally:
        await client.close()

    assert detail["item_id"] == ITEM_ID
    assert detail["is_sku"] is True
    assert [s["label"] for s in detail["skus"]] == ["天卡", "周卡", "月卡"]
    assert [s["price"] for s in detail["skus"]] == [1.0, 5.0, 10.0]
    assert [s["quantity"] for s in detail["skus"]] == [994, 998, 997]
    assert detail["skus"][0]["sku_id"] == "6310445158135"


async def test_item_detail_rejects_empty_item_id():
    client = XianyuClient("unb=1; _m_h5_tk=tok_1")
    try:
        try:
            await client.item_detail("")
        except XianyuError as exc:
            assert "item_id" in str(exc)
        else:  # pragma: no cover - 明确失败，不要静默
            raise AssertionError("空 item_id 应该报错")
    finally:
        await client.close()


async def test_list_items_marks_multi_sku():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/mtop.idle.web.xyh.item.list/1.0/")
        return httpx.Response(200, json=LIST)

    client = XianyuClient("unb=1; _m_h5_tk=tok_1", transport=httpx.MockTransport(handler))
    try:
        items = await client.list_items()
    finally:
        await client.close()

    assert len(items) == 1
    assert items[0]["is_sku"] is True
    assert items[0]["sold_price"] == "1"
