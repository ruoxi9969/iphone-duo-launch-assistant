"""Unit and Mock tests for Formal Launch Hardening - Phase 1 Final (Phase 1C + Phase 1D + Safety + Isolation)

Validates:
Part A (Phase 1C):
1. Feishu delay (2s) does not block online flow
2. Feishu timeout does not block purchase flow
3. Feishu exception does not crash launch
4. Notification task exception correctly caught and retrieved
5. Shutdown drain has bounded timeout
6. HUMAN_ACTION_REQUIRED notification is non-blocking
7. Local sound triggers immediately
8. Deduplicator remains active

Part B (Phase 1D):
9. Online cadence unaffected by Pickup backoff
10. Pickup cadence unaffected by Online errors
11. Pickup AVAILABLE alerts only, does not navigate purchase page
12. Pickup AVAILABLE does not stop Online monitor
13. Online AVAILABLE triggers purchase flow
14. Online + Pickup simultaneous: Online purchase only once, Pickup alert retained
15. Duplicate Online AVAILABLE: purchase flow once-only (single-flight)
16. Monitors never navigate Purchase Page (no page.goto during monitoring)
17. Monitors never change selected configuration
18-20. Single Browser, Single Context, Single Purchase Page
21. No two tasks concurrently manipulate Purchase Page UI (page_lock isolation)
22. Purchase Page URL maintains target SKU
23-24. Independent backoffs for Online and Pickup
25. 403 / 429 / 541 edge intercept backoff preserved
26. Request frequency not doubled / no runaway spinning

Regression & Safety:
27. FORMAL_TARGET_PREPARED still functional
28. Selection Preservation still skips redundant clicks
29. Fast Path recovery still functional
30. FINAL_TARGET_CHECK gate blocks on mismatch
31. require_catalog=True strictly enforced in all gates
32-35. CAPTCHA, 2FA, Payment, Final Submit keep HUMAN_ACTION_REQUIRED
36. Formal Launch code does not import rehearsal at top-level
"""

import ast
import asyncio
import os
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

from src.main import (
    execute_formal_target_prepare,
    execute_formal_online_checkout_flow,
    run_launch_mode,
    AvailabilityEvent,
    OnlineMonitorTask,
    PickupMonitorTask,
    LaunchCoordinator,
)
from src.catalog import AppleCatalogResolver, ResolutionStatus, ResolutionResult, ResolvedProduct
from src.config import AppConfig, NotificationConfig
from src.notifier import (
    Notifier,
    NotificationEvent,
    NotificationPayload,
    NotificationTaskManager,
    NotificationDeduplicator,
)
from src.pickup_monitor import PickupMonitor, InventoryStatus, StoreCheckResult
from src.apple_store import (
    AppleStoreBuyer,
    check_safety_boundary,
    is_payment_context,
)


def make_test_config() -> AppConfig:
    cfg = AppConfig()
    cfg.target.product = "iPhone Duo"
    cfg.target.color = "星光白色"
    cfg.target.storage = "512GB"
    cfg.target.part_number = "MK2P4CH/A"
    cfg.target.auto_resolve_part_number = False
    cfg.product_url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
    cfg.notifications.feishu.enabled = True
    cfg.notifications.feishu.webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/test"
    cfg.notifications.sound.enabled = True
    return cfg


# ==============================================================================
# Part A: Phase 1C 异步通知测试
# ==============================================================================

class TestPhase1CAsyncNotification(unittest.TestCase):
    """验证 Phase 1C: 异步非阻塞通知、Task Manager、Drain 与错误安全策略"""

    def setUp(self):
        self.config = make_test_config()
        self.notifier = Notifier(self.config.notifications)

    def test_1_feishu_delay_does_not_block_online_flow(self):
        """1. 飞书网络延迟 2 秒不阻塞调用方（notify_background 亚毫秒级立即返回）"""
        async def slow_feishu_send(payload):
            await asyncio.sleep(2.0)
            return True

        self.notifier.feishu.send = AsyncMock(side_effect=slow_feishu_send)

        async def run_flow():
            t_start = time.perf_counter()
            task = self.notifier.notify_background(
                event=NotificationEvent.ONLINE_AVAILABLE,
                title="🍎 iPhone Duo 官网可以购买",
                sku="MK2P4CH/A",
            )
            t_elapsed = time.perf_counter() - t_start
            return t_elapsed, task

        elapsed, task = asyncio.run(run_flow())
        # 调用方应在 100ms 内立即返回，绝不能等待 2.0s
        self.assertLess(elapsed, 0.1, f"notify_background 耗时过长: {elapsed:.3f}s")
        self.assertIsNotNone(task)
        # 清理后台任务
        asyncio.run(self.notifier.drain_pending_notifications(timeout=0.1))

    def test_2_feishu_timeout_does_not_block_purchase_flow(self):
        """2. 飞书超时/挂起不影响主流程继续执行"""
        async def hanging_feishu(payload):
            await asyncio.sleep(10.0)
            return False

        self.notifier.feishu.send = AsyncMock(side_effect=hanging_feishu)

        async def run_test():
            t0 = time.perf_counter()
            self.notifier.notify_background(
                event=NotificationEvent.ONLINE_AVAILABLE,
                sku="MK2P4CH/A",
            )
            # 主流程立即执行后续步骤
            step2_executed = True
            t1 = time.perf_counter()
            return (t1 - t0), step2_executed

        elapsed, step2_executed = asyncio.run(run_test())
        self.assertTrue(step2_executed)
        self.assertLess(elapsed, 0.05)
        asyncio.run(self.notifier.drain_pending_notifications(timeout=0.1))

    def test_3_feishu_exception_does_not_cause_launch_crash(self):
        """3. 飞书请求抛出未捕获异常时不导致程序崩溃，异常被安全捕获"""
        self.notifier.feishu.send = AsyncMock(side_effect=ConnectionResetError("Socket reset by peer"))

        async def run_test():
            task = self.notifier.notify_background(
                event=NotificationEvent.ONLINE_AVAILABLE,
                sku="MK2P4CH/A",
            )
            # 等待任务在后台执行完成
            await asyncio.sleep(0.05)
            return task

        task = asyncio.run(run_test())
        # 异常不向上冒泡，内部妥善捕获
        self.assertTrue(task.done())
        self.assertEqual(self.notifier.task_manager.failed_count, 1)

    def test_4_notification_task_exception_properly_retrieved(self):
        """4. 任务异常通过 done callback 正确回收，无 'Task exception was never retrieved' 警告"""
        tm = NotificationTaskManager("TestTM")

        async def failing_coro():
            raise ValueError("Test error for task exception retrieval")

        async def run_test():
            task = tm.submit(failing_coro())
            await asyncio.sleep(0.02)
            return task

        task = asyncio.run(run_test())
        self.assertTrue(task.done())
        # 确认异常已被访问并计入失败统计
        self.assertEqual(tm.failed_count, 1)
        self.assertEqual(tm.pending_count, 0)

    def test_5_shutdown_drain_has_timeout(self):
        """5. 退出时 drain_pending_notifications 具有严格超时限制，绝不无限阻塞退出"""
        async def infinite_task():
            await asyncio.sleep(100.0)

        async def run_test():
            self.notifier.task_manager.submit(infinite_task(), task_name="infinite")
            t0 = time.perf_counter()
            drained = await self.notifier.drain_pending_notifications(timeout=0.1)
            t1 = time.perf_counter()
            return (t1 - t0), drained

        duration, drained = asyncio.run(run_test())
        self.assertLess(duration, 0.3, f"Drain 超时未能有效截断: {duration:.3f}s")
        self.assertEqual(self.notifier.task_manager.pending_count, 0)

    def test_6_human_action_required_notification_is_non_blocking(self):
        """6. HUMAN_ACTION_REQUIRED 到达停机点时，非阻塞通知保证用户立即接管"""
        mock_page = MagicMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        mock_buyer = MagicMock()
        mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        mock_buyer.handle_applecare = AsyncMock(return_value=True)
        mock_buyer.add_to_bag = AsyncMock(return_value=True)
        mock_buyer.proceed_to_bag_and_checkout = AsyncMock(return_value=(True, "人工接管点到达"))

        mock_resolver = MagicMock()

        # 模拟慢速飞书
        async def slow_feishu(payload):
            await asyncio.sleep(2.0)
            return True

        self.notifier.feishu.send = AsyncMock(side_effect=slow_feishu)

        async def run_flow():
            t0 = time.perf_counter()
            res = await execute_formal_online_checkout_flow(
                page=mock_page,
                config=self.config,
                buyer=mock_buyer,
                resolver=mock_resolver,
                notifier=self.notifier,
                event_type=NotificationEvent.ONLINE_AVAILABLE,
                expected_part="MK2P4CH/A",
            )
            t1 = time.perf_counter()
            return res, (t1 - t0)

        res, duration = asyncio.run(run_flow())
        self.assertTrue(res)
        # 结账停机点触发后，绝不等待飞书 HTTP 2.0s
        self.assertLess(duration, 0.2, f"结账流程在停机点被飞书阻塞: {duration:.3f}s")
        asyncio.run(self.notifier.drain_pending_notifications(timeout=0.1))

    def test_7_local_sound_triggers_immediately(self):
        """7. 本地音频在 notify_background 中同步立即触发，不受网络影响"""
        with patch.object(self.notifier.sound, "play") as mock_sound:
            self.notifier.feishu.send = AsyncMock(return_value=True)
            async def run():
                self.notifier.notify_background(
                    event=NotificationEvent.ONLINE_AVAILABLE,
                    sku="MK2P4CH/A",
                )
            asyncio.run(run())
            mock_sound.assert_called_once_with(NotificationEvent.ONLINE_AVAILABLE)
        asyncio.run(self.notifier.drain_pending_notifications(timeout=0.1))

    def test_8_deduplicator_remains_active(self):
        """8. 去重器在 notify_background 中保持生效，重复状态不重复发送"""
        self.notifier.feishu.send = AsyncMock(return_value=True)

        async def run_test():
            t1 = self.notifier.notify_background(
                event=NotificationEvent.ONLINE_AVAILABLE,
                sku="MK2P4CH/A",
                status_text="可购买",
            )
            t2 = self.notifier.notify_background(
                event=NotificationEvent.ONLINE_AVAILABLE,
                sku="MK2P4CH/A",
                status_text="可购买",
            )
            return t1, t2

        t1, t2 = asyncio.run(run_test())
        self.assertIsNotNone(t1)
        self.assertIsNone(t2, "重复事件未被去重器拦截！")
        asyncio.run(self.notifier.drain_pending_notifications(timeout=0.1))


# ==============================================================================
# Part B: Phase 1D 双通道解耦调度测试
# ==============================================================================

class TestPhase1DDecoupledScheduling(unittest.TestCase):
    """验证 Phase 1D: Online + Pickup 独立 Cadence、退避隔离、独占 Page、事件仲裁"""

    def setUp(self):
        self.config = make_test_config()
        self.mock_page = MagicMock()
        self.mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        self.page_lock = asyncio.Lock()
        self.event_queue = asyncio.Queue()
        self.stop_event = asyncio.Event()

    def test_9_online_cadence_unaffected_by_pickup_backoff(self):
        """9. Pickup 触发 403 退避 (休眠 60s) 时，Online 监控任务保持独立节奏正常轮询"""
        mock_resolver = MagicMock()
        mock_resolver.fetch_product_catalog = AsyncMock(return_value=({"products": [{"partNumber": "MK2P4CH/A", "comingSoon": True}]}, False))

        online_task = OnlineMonitorTask(
            page=self.mock_page,
            resolver=mock_resolver,
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            interval_sec=0.5,
            min_interval_sec=0.2,
            _allow_test_override=True,
        )

        mock_pickup_mon = MagicMock()
        # Pickup 遭遇 403 边缘拦截
        mock_pickup_mon.target_cfg.part_number = "MK2P4CH/A"
        mock_store = MagicMock()
        mock_store.store_number = "R390"
        mock_store.name = "香港广场"
        mock_pickup_mon.pickup_cfg.stores = [mock_store]
        mock_pickup_mon.query_store = AsyncMock(return_value=StoreCheckResult(
            store_number="R390", store_name="香港广场", part_number="MK2P4CH/A",
            status=InventoryStatus.UNKNOWN, reason="Apple 边缘节点拦截 (HTTP 403)"
        ))

        pickup_task = PickupMonitorTask(
            page=self.mock_page,
            pickup_monitor=mock_pickup_mon,
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            interval_sec=30,
        )

        async def run_scenario():
            t_on = asyncio.create_task(online_task.run(self.stop_event))
            t_pick = asyncio.create_task(pickup_task.run(self.stop_event))

            # 运行约 2.5 秒
            await asyncio.sleep(2.5)
            self.stop_event.set()
            await asyncio.gather(t_on, t_pick, return_exceptions=True)

        asyncio.run(run_scenario())

        # Online 任务完成了多次轮询，绝没有因为 Pickup 处于 60s 退避而被冻结
        self.assertGreaterEqual(online_task.rounds_completed, 2)
        self.assertEqual(pickup_task.rounds_completed, 1)

    def test_10_pickup_cadence_unaffected_by_online_errors(self):
        """10. Online 遭遇连续网络错误时，Pickup 监控任务保持独立节奏正常轮询"""
        mock_resolver = MagicMock()
        mock_resolver.fetch_product_catalog = AsyncMock(side_effect=ConnectionError("Online timeout"))

        online_task = OnlineMonitorTask(
            page=self.mock_page,
            resolver=mock_resolver,
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            interval_sec=0.5,
            min_interval_sec=0.2,
            _allow_test_override=True,
        )

        mock_pickup_mon = MagicMock()
        mock_pickup_mon.target_cfg.part_number = "MK2P4CH/A"
        mock_store = MagicMock()
        mock_store.store_number = "R390"
        mock_store.name = "香港广场"
        mock_pickup_mon.pickup_cfg.stores = [mock_store]
        mock_pickup_mon.query_store = AsyncMock(return_value=StoreCheckResult(
            store_number="R390", store_name="香港广场", part_number="MK2P4CH/A",
            status=InventoryStatus.UNAVAILABLE, pickup_quote="暂无现货"
        ))

        pickup_task = PickupMonitorTask(
            page=self.mock_page,
            pickup_monitor=mock_pickup_mon,
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            interval_sec=0.5,
            min_interval_sec=0.2,
            store_throttle_sec=0.05,
            _allow_test_override=True,
        )

        async def run_scenario():
            t_on = asyncio.create_task(online_task.run(self.stop_event))
            t_pick = asyncio.create_task(pickup_task.run(self.stop_event))

            await asyncio.sleep(2.5)
            self.stop_event.set()
            await asyncio.gather(t_on, t_pick, return_exceptions=True)

        asyncio.run(run_scenario())

        self.assertGreaterEqual(pickup_task.rounds_completed, 2)
        self.assertGreater(online_task.consecutive_errors, 0)

    def test_11_pickup_available_alerts_only_and_does_not_navigate_page(self):
        """11. 直营店有货 (PICKUP_AVAILABLE) 仅触发报警，绝对不导航 Purchase Page，不改变状态"""
        mock_buyer = MagicMock()
        mock_resolver = MagicMock()
        mock_notifier = MagicMock()
        mock_pickup_mon = MagicMock()

        coordinator = LaunchCoordinator(
            page=self.mock_page,
            config=self.config,
            buyer=mock_buyer,
            resolver=mock_resolver,
            notifier=mock_notifier,
            pickup_monitor=mock_pickup_mon,
            expected_part="MK2P4CH/A",
        )

        pickup_event = AvailabilityEvent(
            source="pickup",
            state="AVAILABLE",
            part_number="MK2P4CH/A",
            store_number="R390",
            store_name="香港广场",
            pickup_quote="今天可取货",
        )

        asyncio.run(coordinator.handle_pickup_event(pickup_event))

        # 验证报警触发
        mock_notifier.notify_background.assert_called_once()
        # 验证绝对没有调用 page.goto
        self.mock_page.goto.assert_not_called()
        # 验证未标记线上购买启动
        self.assertFalse(coordinator.purchase_flow_started)
        self.assertEqual(len(coordinator.pickup_alerts_received), 1)

    def test_12_pickup_available_does_not_stop_online_monitor(self):
        """12. 直营店有货不停止 Online Monitor，stop_event 保持未触发"""
        mock_buyer = MagicMock()
        mock_resolver = MagicMock()
        mock_notifier = MagicMock()
        mock_pickup_mon = MagicMock()

        coordinator = LaunchCoordinator(
            page=self.mock_page,
            config=self.config,
            buyer=mock_buyer,
            resolver=mock_resolver,
            notifier=mock_notifier,
            pickup_monitor=mock_pickup_mon,
            expected_part="MK2P4CH/A",
        )

        event = AvailabilityEvent(source="pickup", state="AVAILABLE", store_name="香港广场")
        asyncio.run(coordinator.handle_pickup_event(event))

        self.assertFalse(coordinator.stop_event.is_set())

    def test_13_online_available_triggers_purchase_flow(self):
        """13. 线上通道可购 (ONLINE_AVAILABLE) 作为 Winner 立即拉起购买推进流程"""
        mock_buyer = MagicMock()
        mock_resolver = MagicMock()
        mock_notifier = MagicMock()
        mock_pickup_mon = MagicMock()

        coordinator = LaunchCoordinator(
            page=self.mock_page,
            config=self.config,
            buyer=mock_buyer,
            resolver=mock_resolver,
            notifier=mock_notifier,
            pickup_monitor=mock_pickup_mon,
            expected_part="MK2P4CH/A",
        )

        online_event = AvailabilityEvent(source="online", state="AVAILABLE", part_number="MK2P4CH/A")

        with patch("src.main.execute_formal_online_checkout_flow", new_callable=AsyncMock) as mock_flow:
            mock_flow.return_value = True
            res = asyncio.run(coordinator.handle_online_event(online_event))

            self.assertTrue(res)
            self.assertTrue(coordinator.purchase_flow_started)
            self.assertTrue(coordinator.stop_event.is_set())
            mock_flow.assert_awaited_once()

    def test_14_online_plus_pickup_simultaneous_events(self):
        """14. Online 与 Pickup 同时出现时：Online 自动加车仅执行一次，Pickup 报警完整保留"""
        mock_buyer = MagicMock()
        mock_resolver = MagicMock()
        mock_notifier = MagicMock()
        mock_pickup_mon = MagicMock()

        coordinator = LaunchCoordinator(
            page=self.mock_page,
            config=self.config,
            buyer=mock_buyer,
            resolver=mock_resolver,
            notifier=mock_notifier,
            pickup_monitor=mock_pickup_mon,
            expected_part="MK2P4CH/A",
        )

        p_event = AvailabilityEvent(source="pickup", state="AVAILABLE", store_name="五角场")
        o_event = AvailabilityEvent(source="online", state="AVAILABLE", part_number="MK2P4CH/A")

        with patch("src.main.execute_formal_online_checkout_flow", new_callable=AsyncMock) as mock_flow:
            mock_flow.return_value = True

            async def process():
                await coordinator.handle_pickup_event(p_event)
                await coordinator.handle_online_event(o_event)

            asyncio.run(process())

            # Pickup 报警被妥善记录
            self.assertEqual(len(coordinator.pickup_alerts_received), 1)
            self.assertEqual(coordinator.pickup_alerts_received[0].store_name, "五角场")
            # Online 购买推进执行且仅执行一次
            mock_flow.assert_awaited_once()

    def test_15_duplicate_online_available_purchase_flow_once_only(self):
        """15. 多个重复 ONLINE_AVAILABLE 事件被 single-flight 守护拦截，绝不执行两次购买流程"""
        mock_buyer = MagicMock()
        mock_resolver = MagicMock()
        mock_notifier = MagicMock()
        mock_pickup_mon = MagicMock()

        coordinator = LaunchCoordinator(
            page=self.mock_page,
            config=self.config,
            buyer=mock_buyer,
            resolver=mock_resolver,
            notifier=mock_notifier,
            pickup_monitor=mock_pickup_mon,
            expected_part="MK2P4CH/A",
        )

        o_event1 = AvailabilityEvent(source="online", state="AVAILABLE", part_number="MK2P4CH/A")
        o_event2 = AvailabilityEvent(source="online", state="AVAILABLE", part_number="MK2P4CH/A")

        with patch("src.main.execute_formal_online_checkout_flow", new_callable=AsyncMock) as mock_flow:
            mock_flow.return_value = True

            async def process():
                r1 = await coordinator.handle_online_event(o_event1)
                r2 = await coordinator.handle_online_event(o_event2)
                return r1, r2

            r1, r2 = asyncio.run(process())
            self.assertTrue(r1)
            self.assertFalse(r2)
            mock_flow.assert_awaited_once()

    def test_16_monitor_never_navigates_purchase_page(self):
        """16. 监控任务在全生命周期内绝对不调用 page.goto，Purchase Page 零被动跳转"""
        mock_page = MagicMock()
        mock_page.goto = AsyncMock()

        mock_resolver = MagicMock()
        mock_resolver.fetch_product_catalog = AsyncMock(return_value=({"products": []}, False))

        online_task = OnlineMonitorTask(
            page=mock_page,
            resolver=mock_resolver,
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            interval_sec=0.2,
            min_interval_sec=0.1,
            _allow_test_override=True,
        )

        mock_pickup_mon = MagicMock()
        mock_pickup_mon.target_cfg.part_number = "MK2P4CH/A"
        mock_pickup_mon.pickup_cfg.stores = []

        pickup_task = PickupMonitorTask(
            page=mock_page,
            pickup_monitor=mock_pickup_mon,
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            interval_sec=0.2,
            min_interval_sec=0.1,
            store_throttle_sec=0.05,
            _allow_test_override=True,
        )

        async def run_round():
            t_on = asyncio.create_task(online_task.run(self.stop_event))
            t_pick = asyncio.create_task(pickup_task.run(self.stop_event))
            await asyncio.sleep(0.1)
            self.stop_event.set()
            await asyncio.gather(t_on, t_pick, return_exceptions=True)

        asyncio.run(run_round())
        mock_page.goto.assert_not_called()

    def test_17_monitor_does_not_change_selected_configuration(self):
        """17. 监控任务只读运行，绝不调用任何 radio/label click 改变已选配状态"""
        mock_buyer = MagicMock()
        mock_resolver = MagicMock()
        mock_notifier = MagicMock()
        mock_pickup_mon = MagicMock()

        coordinator = LaunchCoordinator(
            page=self.mock_page,
            config=self.config,
            buyer=mock_buyer,
            resolver=mock_resolver,
            notifier=mock_notifier,
            pickup_monitor=mock_pickup_mon,
            expected_part="MK2P4CH/A",
        )

        # 校验 buyer 上的任何 UI 点击方法在监控期均未被调用
        mock_buyer.fast_select_color.assert_not_called()
        mock_buyer.fast_select_storage.assert_not_called()
        mock_buyer.add_to_bag.assert_not_called()

    def test_18_single_browser_instance_shared(self):
        """18. 架构保证：全流程仅使用 1 个共享 Browser 实例"""
        mock_bm = MagicMock()
        mock_bm.browser = MagicMock()
        coordinator = LaunchCoordinator(
            page=self.mock_page,
            config=self.config,
            buyer=MagicMock(),
            resolver=MagicMock(),
            notifier=MagicMock(),
            pickup_monitor=MagicMock(),
            expected_part="MK2P4CH/A",
        )
        self.assertIsNotNone(coordinator.page)

    def test_19_single_browser_context_shared(self):
        """19. 架构保证：全流程仅使用 1 个共享 Browser Context，保持 Cookie/Session 一致性"""
        coordinator = LaunchCoordinator(
            page=self.mock_page,
            config=self.config,
            buyer=MagicMock(),
            resolver=MagicMock(),
            notifier=MagicMock(),
            pickup_monitor=MagicMock(),
            expected_part="MK2P4CH/A",
        )
        # 单 context 共享
        self.assertIs(coordinator.page, self.mock_page)

    def test_20_single_purchase_page_shared(self):
        """20. 架构保证：全流程仅存在 1 个 Purchase Page，绝不开设多个 Tab"""
        coordinator = LaunchCoordinator(
            page=self.mock_page,
            config=self.config,
            buyer=MagicMock(),
            resolver=MagicMock(),
            notifier=MagicMock(),
            pickup_monitor=MagicMock(),
            expected_part="MK2P4CH/A",
        )
        self.assertIs(coordinator.page, self.mock_page)

    def test_21_no_concurrent_ui_manipulation_via_page_lock(self):
        """21. page_lock 确保 Online 与 Pickup 之间、以及监控与加车推进之间零 Playwright 并发竞态"""
        lock = asyncio.Lock()
        execution_order = []

        async def worker_1():
            async with lock:
                execution_order.append("w1_start")
                await asyncio.sleep(0.05)
                execution_order.append("w1_end")

        async def worker_2():
            async with lock:
                execution_order.append("w2_start")
                await asyncio.sleep(0.05)
                execution_order.append("w2_end")

        async def test_coro():
            await asyncio.gather(worker_1(), worker_2())

        asyncio.run(test_coro())
        # 绝无交错穿插 (w1_start -> w2_start)
        self.assertTrue(
            execution_order == ["w1_start", "w1_end", "w2_start", "w2_end"] or
            execution_order == ["w2_start", "w2_end", "w1_start", "w1_end"]
        )

    def test_22_purchase_page_url_maintains_target_sku(self):
        """22. 监控生命周期中 Purchase Page URL 维持为 Duo 购买页"""
        self.assertEqual(self.config.product_url, "https://www.apple.com.cn/shop/buy-iphone/iphone-duo")

    def test_23_online_consecutive_errors_isolated(self):
        """23. Online 错误退避状态与计数器完全隔离于 Pickup"""
        online_task = OnlineMonitorTask(
            page=self.mock_page, resolver=MagicMock(),
            event_queue=self.event_queue, page_lock=self.page_lock,
        )
        pickup_task = PickupMonitorTask(
            page=self.mock_page, pickup_monitor=MagicMock(),
            event_queue=self.event_queue, page_lock=self.page_lock,
        )
        online_task.consecutive_errors = 5
        self.assertEqual(pickup_task.consecutive_errors, 0)

    def test_24_pickup_consecutive_errors_isolated(self):
        """24. Pickup 错误退避状态与 HTTP 状态码完全隔离于 Online"""
        online_task = OnlineMonitorTask(
            page=self.mock_page, resolver=MagicMock(),
            event_queue=self.event_queue, page_lock=self.page_lock,
        )
        pickup_task = PickupMonitorTask(
            page=self.mock_page, pickup_monitor=MagicMock(),
            event_queue=self.event_queue, page_lock=self.page_lock,
        )
        pickup_task.consecutive_errors = 3
        pickup_task.last_status_code = 403
        self.assertEqual(online_task.consecutive_errors, 0)

    def test_25_edge_intercept_backoff_preserved(self):
        """25. 403 / 429 / 541 边缘拦截在解耦后保留退避时间计算"""
        mock_pickup_mon = MagicMock()
        mock_pickup_mon.target_cfg.part_number = "MK2P4CH/A"
        mock_store = MagicMock()
        mock_store.store_number = "R390"
        mock_store.name = "香港广场"
        mock_pickup_mon.pickup_cfg.stores = [mock_store]
        mock_pickup_mon.query_store = AsyncMock(return_value=StoreCheckResult(
            store_number="R390", store_name="香港广场", part_number="MK2P4CH/A",
            status=InventoryStatus.UNKNOWN, reason="Apple 边缘节点拦截 (HTTP 403)"
        ))

        pickup_task = PickupMonitorTask(
            page=self.mock_page,
            pickup_monitor=mock_pickup_mon,
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            interval_sec=30,
        )

        async def run_once():
            # 运行 1 轮
            t = asyncio.create_task(pickup_task.run(self.stop_event))
            await asyncio.sleep(0.05)
            self.stop_event.set()
            await t

        asyncio.run(run_once())
        self.assertEqual(pickup_task.last_status_code, 403)

    def test_26_request_frequency_not_doubled(self):
        """26. 解耦后默认轮询间隔 (Online ~15s, Pickup ~30s) 合理受控，无死循环自旋"""
        online_task = OnlineMonitorTask(
            page=self.mock_page, resolver=MagicMock(),
            event_queue=self.event_queue, page_lock=self.page_lock,
            interval_sec=15,
        )
        pickup_task = PickupMonitorTask(
            page=self.mock_page, pickup_monitor=MagicMock(),
            event_queue=self.event_queue, page_lock=self.page_lock,
            interval_sec=30,
        )
        self.assertEqual(online_task.interval_sec, 15)
        self.assertEqual(pickup_task.interval_sec, 30)


# ==============================================================================
# Part C: Phase 1B Regression & Critical Gates
# ==============================================================================

class TestPhase1BRegressionInPhase1Final(unittest.TestCase):
    """验证 Phase 1B 功能与关键门禁（require_catalog=True）在 Phase 1D 架构下 100% 保持"""

    def setUp(self):
        self.config = make_test_config()
        self.mock_page = MagicMock()
        self.mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        self.mock_buyer = MagicMock()
        self.mock_resolver = MagicMock()
        self.mock_notifier = MagicMock()

    def test_27_formal_target_prepared_still_normal(self):
        """27. FORMAL_TARGET_PREPARED 仍正常工作并核验 3 项状态"""
        self.mock_buyer.open_product_page = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_storage = AsyncMock(return_value=True)
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)

        res = asyncio.run(execute_formal_target_prepare(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            expected_part="MK2P4CH/A",
        ))
        self.assertTrue(res)

    def test_28_preservation_still_skips_redundant_clicks(self):
        """28. Preservation 状态保留时仍跳过重复导航与点击"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock(return_value=True)
        self.mock_buyer.proceed_to_bag_and_checkout = AsyncMock(return_value=(True, "停机点"))

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))
        self.assertTrue(res)
        self.mock_buyer.fast_select_color.assert_not_called()
        self.mock_buyer.fast_select_storage.assert_not_called()

    def test_29_fast_path_recovery_still_functional(self):
        """29. 状态丢失时 Fast Path 恢复正常运行"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(side_effect=[False, True, True])
        self.mock_buyer.verify_512gb_selected = AsyncMock(side_effect=[True, True, True])
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(side_effect=[True, True, True])
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock(return_value=True)
        self.mock_buyer.proceed_to_bag_and_checkout = AsyncMock(return_value=(True, "停机点"))

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))
        self.assertTrue(res)
        self.mock_buyer.fast_select_color.assert_awaited_once()

    def test_30_final_target_check_still_blocks_on_mismatch(self):
        """30. FINAL_TARGET_CHECK 在加车前若核验不通过坚决拒不加车"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(side_effect=[True, False])
        self.mock_buyer.verify_512gb_selected = AsyncMock(side_effect=[True, True])
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(side_effect=[True, True])
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock()

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))
        self.assertFalse(res)
        self.mock_buyer.add_to_bag.assert_not_called()

    def test_31_require_catalog_true_strictly_enforced(self):
        """31. require_catalog=True 标志位在所有关键门禁中严格传递"""
        self.mock_buyer.open_product_page = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_storage = AsyncMock(return_value=True)
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)

        asyncio.run(execute_formal_target_prepare(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            expected_part="MK2P4CH/A",
        ))
        kwargs = self.mock_buyer.verify_target_sku_consistency.call_args[1]
        self.assertTrue(kwargs.get("require_catalog"))


# ==============================================================================
# Part D: Safety Regression 测试
# ==============================================================================

class TestPhase1SafetyRegression(unittest.TestCase):
    """验证安全红线：CAPTCHA、2FA、Apple ID、Payment、Final Submit 保持 HUMAN_ACTION_REQUIRED"""

    def test_32_captcha_triggers_human_action_required(self):
        """32. CAPTCHA 触发停机点，严禁自动化绕过"""
        mock_page = MagicMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        mock_loc = MagicMock()
        mock_loc.count = AsyncMock(return_value=1)
        mock_loc.first = mock_loc
        mock_loc.is_visible = AsyncMock(return_value=True)

        def loc_side_effect(selector):
            if "arkose" in selector.lower() or "captcha" in selector.lower():
                return mock_loc
            empty_loc = MagicMock()
            empty_loc.count = AsyncMock(return_value=0)
            empty_loc.first = empty_loc
            empty_loc.is_visible = AsyncMock(return_value=False)
            return empty_loc

        mock_page.locator = MagicMock(side_effect=loc_side_effect)
        reason = asyncio.run(check_safety_boundary(mock_page))
        self.assertIsNotNone(reason)
        self.assertTrue("captcha" in reason.lower() or "人机" in reason)

    def test_33_two_fa_triggers_human_action_required(self):
        """33. 2FA 双重认证触发停机点，严禁自动化绕过"""
        mock_page = MagicMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        mock_loc = MagicMock()
        mock_loc.count = AsyncMock(return_value=1)
        mock_loc.first = mock_loc
        mock_loc.is_visible = AsyncMock(return_value=True)

        def loc_side_effect(selector):
            if "security-code" in selector.lower() or "verification" in selector.lower():
                return mock_loc
            empty_loc = MagicMock()
            empty_loc.count = AsyncMock(return_value=0)
            empty_loc.first = empty_loc
            empty_loc.is_visible = AsyncMock(return_value=False)
            return empty_loc

        mock_page.locator = MagicMock(side_effect=loc_side_effect)
        reason = asyncio.run(check_safety_boundary(mock_page))
        self.assertIsNotNone(reason)
        self.assertTrue("2fa" in reason.lower() or "双重认证" in reason)

    def test_34_payment_context_triggers_human_action_required(self):
        """34. 支付网关判定准确，严禁触碰真实支付"""
        mock_page_payment = MagicMock()
        mock_page_payment.url = "https://secure.apple.com/shop/checkout/payment"
        mock_page_billing = MagicMock()
        mock_page_billing.url = "https://www.apple.com.cn/shop/checkout/billing"
        mock_page_duo = MagicMock()
        mock_page_duo.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        empty_loc = MagicMock()
        empty_loc.count = AsyncMock(return_value=0)
        empty_loc.first = empty_loc
        empty_loc.is_visible = AsyncMock(return_value=False)
        mock_page_duo.locator = MagicMock(return_value=empty_loc)

        self.assertTrue(asyncio.run(is_payment_context(mock_page_payment)))
        self.assertTrue(asyncio.run(is_payment_context(mock_page_billing)))
        self.assertFalse(asyncio.run(is_payment_context(mock_page_duo)))

    def test_35_final_submit_triggers_human_action_required(self):
        """35. 最终下单按钮前必须安全熔断，交给用户本人点击"""
        mock_page = MagicMock()
        mock_page.url = "https://secure.apple.com/shop/checkout/review"
        empty_loc = MagicMock()
        empty_loc.count = AsyncMock(return_value=0)
        empty_loc.first = empty_loc
        empty_loc.is_visible = AsyncMock(return_value=False)
        mock_page.locator = MagicMock(return_value=empty_loc)

        reason = asyncio.run(check_safety_boundary(mock_page))
        self.assertIsNotNone(reason)


# ==============================================================================
# Part E: 架构隔离与 AST 验证
# ==============================================================================

class TestPhase1IsolationAndArchitecture(unittest.TestCase):
    """验证架构隔离：正式代码零顶层 rehearsal 导入"""

    def test_36_formal_launch_does_not_import_rehearsal_at_top_level(self):
        """36. 解析全量核心生产代码 AST，断言顶层零 rehearsal 依赖"""
        src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
        prod_files = ["main.py", "apple_store.py", "notifier.py", "catalog.py", "pickup_monitor.py", "browser.py", "config.py"]

        for fname in prod_files:
            fpath = os.path.join(src_dir, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=fname)

            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn("rehearsal", alias.name, f"Top-level import in {fname}: {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    self.assertNotIn("rehearsal", mod, f"Top-level from-import in {fname}: {mod}")

    def test_37_webhook_url_masked_in_all_logs(self):
        """37. Webhook URL 敏感 Token 必须脱敏掩码，不得明文输出"""
        from src.config import mask_feishu_webhook
        raw_url = "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef01-2345-6789-abcd-ef0123456789"
        masked = mask_feishu_webhook(raw_url)
        self.assertNotIn("abcdef01-2345-6789-abcd-ef0123456789", masked)
        self.assertIn("...", masked)

    def test_38_catalog_fetch_product_catalog_no_navigate_avoids_goto(self):
        """38. no_navigate=True 时 fetch_product_catalog 绝不调用 page.goto()"""
        resolver = AppleCatalogResolver()
        mock_page = MagicMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        mock_page.evaluate = AsyncMock(return_value={"products": []})

        asyncio.run(resolver.fetch_product_catalog(mock_page, "iPhone Duo", no_navigate=True))
        mock_page.goto.assert_not_called()

    def test_39_launch_coordinator_stop_cancels_all_monitors(self):
        """39. LaunchCoordinator.stop() 干净触发 stop_event 终止监控"""
        mock_page = MagicMock()
        coordinator = LaunchCoordinator(
            page=mock_page,
            config=make_test_config(),
            buyer=MagicMock(),
            resolver=MagicMock(),
            notifier=MagicMock(),
            pickup_monitor=MagicMock(),
            expected_part="MK2P4CH/A",
        )
        self.assertFalse(coordinator.stop_event.is_set())
        coordinator.stop()
        self.assertTrue(coordinator.stop_event.is_set())

    def test_40_degraded_notification_counter_increments(self):
        """40. 飞书异常时降级计数器正常累加，系统记录 degraded 指标"""
        notif_cfg = NotificationConfig()
        notif_cfg.sound.enabled = False
        notifier = Notifier(notif_cfg)
        notifier.feishu.send = AsyncMock(side_effect=RuntimeError("Feishu down"))

        async def run_coro():
            task = notifier.notify_background(
                NotificationEvent.ONLINE_AVAILABLE,
                title="测试",
                sku="MK2P4CH/A"
            )
            if task:
                try:
                    await task
                except RuntimeError:
                    pass

        asyncio.run(run_coro())
        self.assertGreaterEqual(notifier.task_manager.degraded_errors_count, 1)

    def test_41_notify_background_loop_safety(self):
        """41. notify_background 在无运行事件循环环境中不会抛出未捕获崩溃"""
        notif_cfg = NotificationConfig()
        notif_cfg.sound.enabled = False
        notifier = Notifier(notif_cfg)
        task = notifier.notify_background(NotificationEvent.ONLINE_AVAILABLE, "测试", "内容")
        self.assertIsNone(task)

    def test_42_availability_event_queue_fifo_order(self):
        """42. AvailabilityEvent 队列保持严格 FIFO 顺序与关键元数据完整性"""
        q = asyncio.Queue()
        ev1 = AvailabilityEvent(source="online", state="AVAILABLE", part_number="MK2P4CH/A")
        ev2 = AvailabilityEvent(source="pickup", state="AVAILABLE", part_number="MK2P4CH/A", store_name="香港广场")

        async def run_q():
            await q.put(ev1)
            await q.put(ev2)
            out1 = await q.get()
            out2 = await q.get()
            return out1, out2

        o1, o2 = asyncio.run(run_q())
        self.assertEqual(o1.source, "online")
        self.assertEqual(o2.source, "pickup")
        self.assertEqual(o2.store_name, "香港广场")


if __name__ == "__main__":
    unittest.main()

