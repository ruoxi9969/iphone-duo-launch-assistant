"""Unit and Mock tests for Phase 2 Failure Injection (Part C)

Verifies all 9 failure injection scenarios using Mock/Local tests (zero danger to Apple servers/accounts):
1. color lost -> triggers Fast Path recovery
2. storage lost -> triggers Fast Path recovery
3. SKU mismatch -> FINAL_TARGET_CHECK blocks add_to_bag
4. Feishu timeout -> async non-blocking does not impede purchase flow
5. Pickup 403 -> triggers tiered backoff (60s -> 120s -> 240s)
6. Online 429 -> triggers tiered backoff (60s -> 120s -> 240s)
7. Online 541 -> triggers tiered backoff (60s -> 120s -> 240s)
8. duplicate ONLINE_AVAILABLE -> Single-Flight once-only protection blocks second execution
9. simultaneous Pickup + Online event -> Online executes purchase, Pickup triggers alert without disrupting Online
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import AppConfig
from src.main import (
    execute_formal_online_checkout_flow,
    LaunchCoordinator,
    AvailabilityEvent,
    OnlineMonitorTask,
    PickupMonitorTask,
)
from src.notifier import NotificationEvent, Notifier
from src.rate_guard import ErrorBackoffTracker
from src.pickup_monitor import InventoryStatus, StoreCheckResult


class TestPhase2FailureInjection(unittest.TestCase):
    """Part C: 异常注入与容灾鲁棒性专项测试"""

    def setUp(self):
        self.config = AppConfig()
        self.mock_page = MagicMock()
        self.mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        self.mock_resolver = MagicMock()
        self.mock_notifier = MagicMock()
        self.expected_part = "MK2P4CH/A"

    def test_1_color_lost_triggers_fast_path_recovery(self):
        """1. 初始外观颜色状态丢失 (color lost) -> 触发 Fast Path 快速恢复并重新核验通过"""
        mock_buyer = MagicMock()
        # 第一次 verify_starwhite 失败（丢失），恢复后成功
        mock_buyer.verify_starwhite_selected = AsyncMock(side_effect=[False, True, True])
        mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        mock_buyer.fast_select_color = AsyncMock(return_value=True)
        mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        mock_buyer.handle_applecare = AsyncMock(return_value=True)

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part=self.expected_part,
            runtime_verify_no_add_to_bag=True,
        ))

        self.assertTrue(res)
        mock_buyer.fast_select_color.assert_called_once()
        self.assertEqual(mock_buyer.verify_starwhite_selected.call_count, 3)

    def test_2_storage_lost_triggers_fast_path_recovery(self):
        """2. 初始存储容量状态丢失 (storage lost) -> 触发 Fast Path 快速恢复并重新核验通过"""
        mock_buyer = MagicMock()
        mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        # 第一次 512gb 校验失败（丢失），恢复后成功
        mock_buyer.verify_512gb_selected = AsyncMock(side_effect=[False, True, True])
        mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        mock_buyer.fast_select_storage = AsyncMock(return_value=True)
        mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        mock_buyer.handle_applecare = AsyncMock(return_value=True)

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part=self.expected_part,
            runtime_verify_no_add_to_bag=True,
        ))

        self.assertTrue(res)
        mock_buyer.fast_select_storage.assert_called_once()
        self.assertEqual(mock_buyer.verify_512gb_selected.call_count, 3)

    def test_3_sku_mismatch_blocks_add_to_bag_at_final_target_check(self):
        """3. SKU 不一致 (SKU mismatch) -> FINAL_TARGET_CHECK 坚决拒绝加车"""
        mock_buyer = MagicMock()
        mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        mock_buyer.handle_applecare = AsyncMock(return_value=True)
        # 终极核验时 SKU mismatch
        mock_buyer.verify_target_sku_consistency = AsyncMock(side_effect=[True, False])
        mock_buyer.add_to_bag = AsyncMock(return_value=True)

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part=self.expected_part,
            runtime_verify_no_add_to_bag=False,
        ))

        self.assertFalse(res)
        mock_buyer.add_to_bag.assert_not_called()

    def test_4_feishu_timeout_does_not_block_purchase_flow(self):
        """4. 飞书网络超时 (Feishu timeout) -> 异步后台调度，完全不阻塞加车主流程"""
        notifier = Notifier(self.config.notifications)

        async def slow_send(*args, **kwargs):
            await asyncio.sleep(5.0)
            raise TimeoutError("Feishu connection timed out")

        async def run_test():
            import time
            with patch.object(notifier, "_send_feishu_safe", side_effect=slow_send):
                t0 = time.perf_counter()
                task = notifier.notify_background(
                    event=NotificationEvent.ONLINE_AVAILABLE,
                    title="🍎 演练测试",
                    details="非阻塞测试",
                )
                t1 = time.perf_counter()
                # 立即返回，耗时 < 50ms
                self.assertLess(t1 - t0, 0.05)
                self.assertIsNotNone(task)

        asyncio.run(run_test())

    def test_5_pickup_403_triggers_tiered_backoff(self):
        """5. Pickup 接口遇到 403 边缘拦截 -> 触发分级退避 (60s -> 120s -> 240s)"""
        tracker = ErrorBackoffTracker(name="TestPickupBackoff")
        b1 = tracker.record_edge_block(403)
        self.assertEqual(b1, 60.0)
        self.assertTrue(tracker.is_in_backoff())

        b2 = tracker.record_edge_block(403)
        self.assertEqual(b2, 120.0)

        b3 = tracker.record_edge_block(403)
        self.assertEqual(b3, 240.0)

    def test_6_online_429_triggers_tiered_backoff(self):
        """6. Online 接口遇到 429 请求过多 -> 触发分级退避"""
        tracker = ErrorBackoffTracker(name="TestOnline429")
        b1 = tracker.record_edge_block(429)
        self.assertEqual(b1, 60.0)
        self.assertTrue(tracker.is_in_backoff())

        b2 = tracker.record_edge_block(429)
        self.assertEqual(b2, 120.0)

    def test_7_online_541_triggers_tiered_backoff(self):
        """7. Online 接口遇到 541 Akamai 挑战拦截 -> 触发分级退避"""
        tracker = ErrorBackoffTracker(name="TestOnline541")
        b1 = tracker.record_edge_block(541)
        self.assertEqual(b1, 60.0)
        self.assertTrue(tracker.is_in_backoff())

        b2 = tracker.record_edge_block(541)
        self.assertEqual(b2, 120.0)

    def test_8_duplicate_online_available_single_flight_blocked(self):
        """8. 重复 ONLINE_AVAILABLE 事件 -> Single-Flight 机制拦截，坚决防止重复加车"""
        coordinator = LaunchCoordinator(
            page=self.mock_page,
            config=self.config,
            buyer=MagicMock(),
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            pickup_monitor=MagicMock(),
            expected_part=self.expected_part,
        )

        ev1 = AvailabilityEvent(source="online", state="AVAILABLE", part_number=self.expected_part)
        ev2 = AvailabilityEvent(source="online", state="AVAILABLE", part_number=self.expected_part)

        with patch("src.main.execute_formal_online_checkout_flow", new_callable=AsyncMock) as mock_flow:
            mock_flow.return_value = True

            # 第一次触发
            res1 = asyncio.run(coordinator.handle_online_event(ev1))
            self.assertTrue(res1)
            self.assertTrue(coordinator.purchase_flow_started)
            self.assertEqual(mock_flow.call_count, 1)

            # 第二次重复触发
            res2 = asyncio.run(coordinator.handle_online_event(ev2))
            self.assertFalse(res2)
            # 坚决不再次调用结账流程
            self.assertEqual(mock_flow.call_count, 1)

    def test_9_simultaneous_pickup_and_online_events_arbitrated(self):
        """9. Pickup 与 Online 事件同时到达 -> Online 优先执行购买，Pickup 产生报警但不干扰 Online"""
        coordinator = LaunchCoordinator(
            page=self.mock_page,
            config=self.config,
            buyer=MagicMock(),
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            pickup_monitor=MagicMock(),
            expected_part=self.expected_part,
        )

        pickup_ev = AvailabilityEvent(
            source="pickup", state="AVAILABLE", part_number=self.expected_part,
            store_number="R390", store_name="香港广场", pickup_quote="今天可取货"
        )
        online_ev = AvailabilityEvent(
            source="online", state="AVAILABLE", part_number=self.expected_part
        )

        with patch("src.main.execute_formal_online_checkout_flow", new_callable=AsyncMock) as mock_flow:
            mock_flow.return_value = True

            # 先处理 Pickup 事件（仅报警，不停止 Online，不开始加车）
            asyncio.run(coordinator.handle_pickup_event(pickup_ev))
            self.assertFalse(coordinator.purchase_flow_started)
            self.assertEqual(len(coordinator.pickup_alerts_received), 1)
            self.mock_notifier.notify_background.assert_called()

            # 再处理 Online 事件（启动加车）
            res_online = asyncio.run(coordinator.handle_online_event(online_ev))
            self.assertTrue(res_online)
            self.assertTrue(coordinator.purchase_flow_started)
            mock_flow.assert_called_once()


if __name__ == "__main__":
    unittest.main()
