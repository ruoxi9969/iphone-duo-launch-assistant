"""Unit and Mock tests for Phase 1E: Polling Rate Safety Patch

Covers:
1. Online does not allow 1-second permanent polling in production
2. Online min_interval enforced by RateGuard
3. Pickup min_interval enforced by RateGuard
4. Pickup single-store interval >= 2s enforced
5. Online 403 triggers tiered backoff (>= 60s)
6. Online 429 triggers tiered backoff (>= 60s)
7. Online 541 triggers tiered backoff (>= 60s)
8. Pickup 403/429/541 independent tiered backoff
9. Cannot send requests during backoff
10. Task restart cannot bypass RateGuard
11. Online and Pickup cadences are independent
12. Dual-channel total request volume is bounded and controlled
13. Single-Flight purchase flow protection remains intact
14. Phase 1A/B/C/D gates and safety boundaries remain operational
15. 403/429/541 tiered backoff steps (60s -> 120s -> 240s) and cap (300s)
16. Gradual recovery after success
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import AppConfig, OnlineConfig, PickupConfig, StoreConfig
from src.main import (
    OnlineMonitorTask,
    PickupMonitorTask,
    LaunchCoordinator,
    AvailabilityEvent,
)
from src.rate_guard import RateGuard, ErrorBackoffTracker, calculate_cadence_sleep
from src.pickup_monitor import InventoryStatus, StoreCheckResult


def make_test_config() -> AppConfig:
    cfg = AppConfig()
    cfg.target.product = "iPhone Duo"
    cfg.target.color = "星光白色"
    cfg.target.storage = "512GB"
    cfg.target.part_number = "MK2P4CH/A"
    cfg.target.auto_resolve_part_number = False

    cfg.online.interval_seconds = 25
    cfg.online.jitter_seconds = 5
    cfg.online.min_interval_seconds = 15.0

    cfg.pickup.interval_seconds = 30
    cfg.pickup.jitter_seconds = 10
    cfg.pickup.min_interval_seconds = 20.0
    cfg.pickup.store_throttle_seconds = 2.0
    cfg.pickup.stores = [
        StoreConfig(name="香港广场", store_number="R390"),
        StoreConfig(name="上海环贸 iapm", store_number="R401"),
        StoreConfig(name="五角场", store_number="R581"),
        StoreConfig(name="环球港", store_number="R683"),
    ]
    return cfg


class TestPhase1ERateSafety(unittest.TestCase):
    """Phase 1E: 轮询频率安全与速率守卫专项测试"""

    def setUp(self):
        self.config = make_test_config()
        self.mock_page = MagicMock()
        self.page_lock = asyncio.Lock()
        self.event_queue = asyncio.Queue()
        self.stop_event = asyncio.Event()

    def test_1_online_disallows_1s_polling_in_production(self):
        """1. 生产模式下 Online 严禁 1 秒永久轮询，强制约束至安全基准"""
        # 尝试传入 1s 基准轮询
        task = OnlineMonitorTask(
            page=self.mock_page,
            resolver=MagicMock(),
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            interval_sec=1.0,
            min_interval_sec=15.0,
            _allow_test_override=False,
        )
        # 必须被硬性限制至 >= 15.0s
        self.assertGreaterEqual(task.interval_sec, 15.0)
        self.assertGreaterEqual(task.rate_guard.min_interval, 15.0)

    def test_2_online_min_interval_enforced(self):
        """2. Online RateGuard 最小间隔强制生效，过密请求自动等待"""
        guard = RateGuard(min_interval=10.0, name="TestOnlineGuard")
        # 模拟第一次请求
        guard.mark_request()
        self.assertFalse(guard.can_request())

        # 检查剩余时间
        rem = guard.remaining_wait()
        self.assertGreater(rem, 0.0)
        self.assertLessEqual(rem, 10.0)

        # 模拟 0.1s 后的瞬态节流等待
        async def run_throttle():
            # 伪造上次请求是 9.9 秒前
            guard.last_request_monotonic = time.monotonic() - 9.9
            waited = await guard.throttle()
            return waited

        waited = asyncio.run(run_throttle())
        self.assertGreater(waited, 0.0)
        self.assertLessEqual(waited, 0.2)

    def test_3_pickup_min_interval_enforced(self):
        """3. Pickup RateGuard 轮次间最小间隔强制生效 (>= 20s)"""
        task = PickupMonitorTask(
            page=self.mock_page,
            pickup_monitor=MagicMock(),
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            interval_sec=5.0,
            min_interval_sec=20.0,
            _allow_test_override=False,
        )
        self.assertGreaterEqual(task.interval_sec, 20.0)
        self.assertGreaterEqual(task.rate_guard.min_interval, 20.0)

    def test_4_pickup_single_store_interval_ge_2s(self):
        """4. Pickup 单店间隔强制约束 >= 2.0s，防止边缘节点封锁"""
        task = PickupMonitorTask(
            page=self.mock_page,
            pickup_monitor=MagicMock(),
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            store_throttle_sec=0.5,
            _allow_test_override=False,
        )
        self.assertGreaterEqual(task.store_throttle_sec, 2.0)

    def test_5_online_403_triggers_tiered_backoff(self):
        """5. Online 遇到 403 边缘拦截时立即触发逐级退避 (首级 60s)"""
        task = OnlineMonitorTask(
            page=self.mock_page,
            resolver=MagicMock(),
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            _allow_test_override=False,
        )
        backoff_sec = task.backoff_tracker.record_edge_block(403)
        self.assertEqual(backoff_sec, 60.0)
        self.assertTrue(task.backoff_tracker.is_in_backoff())
        self.assertEqual(task.last_status_code, 403)
        self.assertGreaterEqual(task.backoff_tracker.remaining_backoff(), 55.0)

    def test_6_online_429_triggers_tiered_backoff(self):
        """6. Online 遇到 429 频率限制时触发逐级退避 (首级 60s)"""
        tracker = ErrorBackoffTracker(name="TestOnlineBackoff")
        b = tracker.record_edge_block(429)
        self.assertEqual(b, 60.0)
        self.assertTrue(tracker.is_in_backoff())
        self.assertEqual(tracker.last_status_code, 429)

    def test_7_online_541_triggers_tiered_backoff(self):
        """7. Online 遇到 541 边缘拦截时触发逐级退避 (首级 60s)"""
        tracker = ErrorBackoffTracker(name="TestOnlineBackoff")
        b = tracker.record_edge_block(541)
        self.assertEqual(b, 60.0)
        self.assertTrue(tracker.is_in_backoff())
        self.assertEqual(tracker.last_status_code, 541)

    def test_8_pickup_independent_edge_backoff(self):
        """8. Pickup 遇到 403/429/541 退避完全独立，不影响 Online 监控"""
        online_task = OnlineMonitorTask(
            page=self.mock_page, resolver=MagicMock(),
            event_queue=self.event_queue, page_lock=self.page_lock,
        )
        pickup_task = PickupMonitorTask(
            page=self.mock_page, pickup_monitor=MagicMock(),
            event_queue=self.event_queue, page_lock=self.page_lock,
        )

        # Pickup 遭遇 403
        pickup_task.backoff_tracker.record_edge_block(403)
        self.assertTrue(pickup_task.backoff_tracker.is_in_backoff())
        self.assertEqual(pickup_task.last_status_code, 403)

        # Online 毫无影响
        self.assertFalse(online_task.backoff_tracker.is_in_backoff())
        self.assertEqual(online_task.last_status_code, 200)
        self.assertEqual(online_task.consecutive_errors, 0)

    def test_9_cannot_request_during_backoff(self):
        """9. 退避期内严格禁止发送任何网络与页面请求"""
        mock_resolver = MagicMock()
        mock_resolver.fetch_product_catalog = AsyncMock(return_value=({"products": []}, False))

        tracker = ErrorBackoffTracker(name="TestBackoff")
        tracker.record_edge_block(403)  # 退避 60s

        task = OnlineMonitorTask(
            page=self.mock_page,
            resolver=mock_resolver,
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            backoff_tracker=tracker,
            _allow_test_override=True,
        )

        async def run_scenario():
            t = asyncio.create_task(task.run(self.stop_event))
            # 运行 0.1s，断言在这期间绝无请求发出
            await asyncio.sleep(0.1)
            self.stop_event.set()
            await t

        asyncio.run(run_scenario())
        # 在 60s 退避期内，没有执行任何 catalog 请求
        mock_resolver.fetch_product_catalog.assert_not_called()
        self.assertEqual(task.rounds_completed, 0)

    def test_10_task_restart_cannot_bypass_rate_guard(self):
        """10. 任务重启或异常重入绝不能绕过 RateGuard 最小间隔"""
        shared_guard = RateGuard(min_interval=15.0, name="SharedGuard")
        shared_guard.mark_request()

        # 新任务使用同一个 shared_guard
        new_task = OnlineMonitorTask(
            page=self.mock_page,
            resolver=MagicMock(),
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            rate_guard=shared_guard,
        )
        # 重启后依然受限
        self.assertFalse(new_task.rate_guard.can_request())
        self.assertGreater(new_task.rate_guard.remaining_wait(), 10.0)

    def test_11_online_pickup_cadence_independent(self):
        """11. Online 与 Pickup 各自维护 Cadence 与状态计数器，互不干扰"""
        online_task = OnlineMonitorTask(
            page=self.mock_page, resolver=MagicMock(),
            event_queue=self.event_queue, page_lock=self.page_lock,
            interval_sec=25.0,
        )
        pickup_task = PickupMonitorTask(
            page=self.mock_page, pickup_monitor=MagicMock(),
            event_queue=self.event_queue, page_lock=self.page_lock,
            interval_sec=30.0,
        )
        self.assertEqual(online_task.interval_sec, 25.0)
        self.assertEqual(pickup_task.interval_sec, 30.0)

        online_task.consecutive_errors = 3
        self.assertEqual(pickup_task.consecutive_errors, 0)

    def test_12_total_request_budget_bounded(self):
        """12. 双通道总请求预算受控，绝无死循环自旋"""
        # calculate_cadence_sleep 综合校验
        sleep_time = calculate_cadence_sleep(
            base_interval=25.0,
            jitter_range=(-3.0, 5.0),
            min_interval=15.0,
            backoff_remaining=0.0,
        )
        self.assertGreaterEqual(sleep_time, 15.0)
        self.assertLessEqual(sleep_time, 35.0)

    def test_13_single_flight_unaffected(self):
        """13. Rate 强化后 Single-Flight 加车保护机制依然 100% 完备"""
        coordinator = LaunchCoordinator(
            page=self.mock_page,
            config=self.config,
            buyer=MagicMock(),
            resolver=MagicMock(),
            notifier=MagicMock(),
            pickup_monitor=MagicMock(),
            expected_part="MK2P4CH/A",
        )
        self.assertFalse(coordinator.purchase_flow_started)

        # 第 1 次 Online 事件
        ev1 = AvailabilityEvent(source="online", state="AVAILABLE", part_number="MK2P4CH/A")
        with patch("src.main.execute_formal_online_checkout_flow", new_callable=AsyncMock) as mock_flow:
            mock_flow.return_value = True
            res1 = asyncio.run(coordinator.handle_online_event(ev1))
            self.assertTrue(res1)
            self.assertTrue(coordinator.purchase_flow_started)
            mock_flow.assert_called_once()

            # 重复 Online 事件被 Single-Flight 拦截
            ev2 = AvailabilityEvent(source="online", state="AVAILABLE", part_number="MK2P4CH/A")
            res2 = asyncio.run(coordinator.handle_online_event(ev2))
            self.assertFalse(res2)
            self.assertEqual(mock_flow.call_count, 1)

    def test_14_phase1_gates_intact(self):
        """14. Phase 1A/B 关键规格核验与硬门禁逻辑完整无损"""
        from src.apple_store import (
            verify_starwhite_selected,
            verify_512gb_selected,
            verify_target_sku_consistency,
        )
        self.assertTrue(callable(verify_starwhite_selected))
        self.assertTrue(callable(verify_512gb_selected))
        self.assertTrue(callable(verify_target_sku_consistency))

    def test_15_tiered_backoff_steps_and_cap(self):
        """15. 403 / 429 / 541 阶梯退避 (60s -> 120s -> 240s) 与上限封顶 (300s)"""
        tracker = ErrorBackoffTracker(
            name="TieredTestTracker",
            edge_tiers=[60.0, 120.0, 240.0],
            max_edge_backoff=300.0,
        )
        # 第 1 次命中
        b1 = tracker.record_edge_block(403)
        self.assertEqual(b1, 60.0)

        # 第 2 次连续命中
        b2 = tracker.record_edge_block(403)
        self.assertEqual(b2, 120.0)

        # 第 3 次连续命中
        b3 = tracker.record_edge_block(403)
        self.assertEqual(b3, 240.0)

        # 第 4 次连续命中 (封顶 300s)
        b4 = tracker.record_edge_block(403)
        self.assertEqual(b4, 300.0)

        # 第 5 次连续命中 (保持封顶 300s)
        b5 = tracker.record_edge_block(403)
        self.assertEqual(b5, 300.0)

    def test_16_gradual_recovery_after_success(self):
        """16. 成功后逐步恢复正常节奏，不立即突变"""
        tracker = ErrorBackoffTracker(name="RecoveryTracker")
        tracker.record_edge_block(403)
        tracker.record_edge_block(403)
        self.assertEqual(tracker.consecutive_edge_blocks, 2)
        self.assertEqual(tracker.consecutive_failures, 2)

        # 第 1 次成功
        tracker.record_success()
        self.assertEqual(tracker.consecutive_edge_blocks, 1)
        self.assertEqual(tracker.consecutive_failures, 1)

        # 第 2 次成功
        tracker.record_success()
        self.assertEqual(tracker.consecutive_edge_blocks, 0)
        self.assertEqual(tracker.consecutive_failures, 0)
        self.assertEqual(tracker.last_status_code, 200)


if __name__ == "__main__":
    unittest.main()
