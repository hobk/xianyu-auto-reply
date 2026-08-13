import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEBSOCKET_ROOT = PROJECT_ROOT / "websocket"
for path in (str(PROJECT_ROOT), str(WEBSOCKET_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.services.xianyu.auto_delivery_handler import (
    ApiDeliveryCacheUnavailable,
    AutoDeliveryHandler,
)
from common.models.xy_order import XYOrder
from common.services.order_service import OrderService


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def first(self):
        return self.value


class _FakeSession:
    def __init__(self, order):
        self.order = order
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def execute(self, _statement):
        return _ScalarResult(self.order)


class _FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return '{"code": 0, "data": "CARD-001"}'


class _FakeClientSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, *_args, **_kwargs):
        return _FakeResponse()


class _FailedResponse(_FakeResponse):
    status = 400

    async def text(self):
        return '{"message": "temporary business failure"}'


class _FailedClientSession(_FakeClientSession):
    call_count = 0

    def get(self, *_args, **_kwargs):
        type(self).call_count += 1
        return _FailedResponse()


class ApiDeliveryIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_result_skips_external_api(self):
        handler = object.__new__(AutoDeliveryHandler)
        handler._load_api_delivery_result = AsyncMock(return_value="CARD-CACHED")

        rule = {"card_id": 7}
        with patch(
            "app.services.xianyu.auto_delivery_handler.aiohttp.ClientSession",
            side_effect=AssertionError("缓存命中时不应调用外部API"),
        ):
            content = await handler._get_api_card_content(
                rule, order_id="ORDER-1", delivery_slot=2
            )

        self.assertEqual(content, "CARD-CACHED")
        handler._load_api_delivery_result.assert_awaited_once_with(
            "ORDER-1", 7, 2, "api"
        )

    async def test_successful_api_result_is_saved_immediately(self):
        handler = object.__new__(AutoDeliveryHandler)
        handler._load_api_delivery_result = AsyncMock(return_value=None)
        handler._save_api_delivery_result = AsyncMock(return_value="CARD-001")

        rule = {
            "card_id": 8,
            "card_name": "test",
            "card_api_config": {"url": "https://card.example/api", "method": "GET"},
        }
        with (
            patch(
                "app.services.xianyu.auto_delivery_handler.aiohttp.ClientSession",
                return_value=_FakeClientSession(),
            ),
            patch(
                "app.services.xianyu.auto_delivery_handler.extract_card_api_response_content",
                return_value="CARD-001",
            ),
        ):
            content = await handler._get_api_card_content(
                rule, order_id="ORDER-2", delivery_slot=1
            )

        self.assertEqual(content, "CARD-001")
        handler._save_api_delivery_result.assert_awaited_once_with(
            "ORDER-2", 8, "CARD-001", 1, "api"
        )

    async def test_cache_lookup_error_does_not_call_external_api(self):
        handler = object.__new__(AutoDeliveryHandler)
        handler._load_api_delivery_result = AsyncMock(
            side_effect=ApiDeliveryCacheUnavailable("database offline")
        )

        with patch(
            "app.services.xianyu.auto_delivery_handler.aiohttp.ClientSession",
            side_effect=AssertionError("缓存状态不明时不应调用外部API"),
        ):
            with self.assertRaises(ApiDeliveryCacheUnavailable):
                await handler._get_api_card_content(
                    {"card_id": 10}, order_id="ORDER-4", delivery_slot=0
                )

    async def test_failed_api_result_is_not_saved_and_can_retry_later(self):
        handler = object.__new__(AutoDeliveryHandler)
        handler._load_api_delivery_result = AsyncMock(return_value=None)
        handler._save_api_delivery_result = AsyncMock()
        _FailedClientSession.call_count = 0
        rule = {
            "card_id": 11,
            "card_api_config": {"url": "https://card.example/api", "method": "GET"},
        }

        with patch(
            "app.services.xianyu.auto_delivery_handler.aiohttp.ClientSession",
            return_value=_FailedClientSession(),
        ):
            first = await handler._get_api_card_content(rule, order_id="ORDER-5")
            second = await handler._get_api_card_content(rule, order_id="ORDER-5")

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(_FailedClientSession.call_count, 2)
        handler._save_api_delivery_result.assert_not_awaited()

    async def test_order_metadata_keeps_each_quantity_slot_separate(self):
        order = XYOrder(
            owner_id=1,
            order_no="ORDER-3",
            account_id="ACCOUNT-1",
            status="pending",
            metadata_json={},
        )
        session = _FakeSession(order)
        service = OrderService(session)

        await service.save_api_delivery_result(
            "ORDER-3", "ACCOUNT-1", 9, "CARD-A", delivery_slot=0
        )
        await service.save_api_delivery_result(
            "ORDER-3", "ACCOUNT-1", 9, "CARD-B", delivery_slot=1
        )

        entries = order.metadata_json["api_delivery_results"]
        self.assertEqual(entries["api:9:0"]["content"], "CARD-A")
        self.assertEqual(entries["api:9:1"]["content"], "CARD-B")
        self.assertEqual(session.commit.await_count, 2)


if __name__ == "__main__":
    unittest.main()
