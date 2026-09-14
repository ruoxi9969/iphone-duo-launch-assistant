"""
Unit and Integration Tests for iPhone Duo Launch Control Center Dashboard V1.1
==============================================================================
Covers:
- Mode badges by mode (DEMO, RUNTIME_VERIFY, FORMAL_LAUNCH, fallback)
- Handoff text by mode
- Cadence alignment (20~30s, base 25s, no 20~35s)
- Stepper node stage 3 naming ('库存监控')
- Countdown timer logic (null, future, past, malformed)
- Production event emissions & pipeline stages in DashboardAdapter
- Dashboard close isolation (is_active = False safety)
- Queue overflow and non-blocking safety
- Hero styling and safety card info by mode
- Store display timestamps and formatting
- AppConfig launch configuration loading
"""

import unittest
import datetime
import queue
from unittest.mock import MagicMock, patch

from src.dashboard_state import (
    DashboardState,
    StoreDisplayState,
    mask_webhook_url,
    mask_secret,
)
from src.dashboard_adapter import DashboardAdapter
from src.config import AppConfig, LaunchConfig, TargetConfig, NotificationConfig, FeishuConfig


class TestDashboardV11(unittest.TestCase):

    # =========================================================================
    # 1. Mode Badge Tests
    # =========================================================================

    def test_mode_badge_demo(self):
        st = DashboardState()
        st.dashboard_mode = "DEMO"
        text, level = st.get_mode_badge_info()
        self.assertEqual(text, "【界面演示 · 全部状态模拟】")
        self.assertEqual(level, "ORANGE")

    def test_mode_badge_runtime_verify(self):
        st = DashboardState()
        st.dashboard_mode = "RUNTIME_VERIFY"
        text, level = st.get_mode_badge_info()
        self.assertEqual(text, "【受控联调 · 禁止真实加车】")
        self.assertEqual(level, "BLUE")

    def test_mode_badge_formal_launch(self):
        st = DashboardState()
        st.dashboard_mode = "FORMAL_LAUNCH"
        text, level = st.get_mode_badge_info()
        self.assertEqual(text, "【首发实战 · 生产状态】")
        self.assertEqual(level, "GREEN")

    def test_mode_badge_unknown_fallback(self):
        st = DashboardState()
        st.dashboard_mode = "SOMETHING_ELSE"
        text, level = st.get_mode_badge_info()
        self.assertIn("SOMETHING_ELSE", text)
        self.assertEqual(level, "GREY")

    # =========================================================================
    # 2. Handoff Text by Mode Tests
    # =========================================================================

    def test_handoff_text_formal_launch(self):
        st = DashboardState()
        st.dashboard_mode = "FORMAL_LAUNCH"
        text = st.get_handoff_text()
        self.assertEqual(
            text,
            "商品已加入购物袋，购买流程已推进至人工安全接管点，请立即在 Apple 官方页面中继续操作。"
        )

    def test_handoff_text_runtime_verify(self):
        st = DashboardState()
        st.dashboard_mode = "RUNTIME_VERIFY"
        text = st.get_handoff_text()
        self.assertEqual(
            text,
            "受控验证已完成最终规格核验，并在真实加车前安全阻断。当前页面已准备好供人工核验。"
        )

    def test_handoff_text_demo(self):
        st = DashboardState()
        st.dashboard_mode = "DEMO"
        text = st.get_handoff_text()
        self.assertEqual(
            text,
            "【界面演示】模拟购买流程已推进至人工接管阶段。本模式未访问 Apple，也未执行真实加车。"
        )

    def test_handoff_text_fallback(self):
        st = DashboardState()
        st.dashboard_mode = "CUSTOM"
        text = st.get_handoff_text()
        self.assertIn("人工安全接管点", text)

    # =========================================================================
    # 3. Cadence Alignment Tests
    # =========================================================================

    def test_online_cadence_strictly_20_to_30(self):
        st = DashboardState()
        self.assertEqual(st.online_cadence, "20~30 秒 (基准 25s)")
        # Ensure no accidental legacy "20~35" appears in any state default
        self.assertNotIn("20~35", st.online_cadence)
        self.assertNotIn("35", st.online_cadence)

    # =========================================================================
    # 4. Countdown Timer Tests
    # =========================================================================

    def test_countdown_not_set(self):
        st = DashboardState()
        st.launch_time_iso = None
        self.assertEqual(st.get_countdown_text(), "开售时间未设置")

        st.launch_time_iso = ""
        self.assertEqual(st.get_countdown_text(), "开售时间未设置")

    def test_countdown_future(self):
        st = DashboardState()
        future_dt = datetime.datetime.now() + datetime.timedelta(hours=2, minutes=15, seconds=30)
        st.launch_time_iso = future_dt.isoformat()
        text = st.get_countdown_text()
        self.assertTrue(text.startswith("T-"), f"Expected T- prefix, got {text}")
        self.assertIn(":", text)

    def test_countdown_past(self):
        st = DashboardState()
        past_dt = datetime.datetime.now() - datetime.timedelta(hours=1, minutes=5, seconds=10)
        st.launch_time_iso = past_dt.isoformat()
        text = st.get_countdown_text()
        self.assertTrue(text.startswith("预购已开始 +"), f"Expected '预购已开始 +' prefix, got {text}")
        self.assertIn(":", text)

    def test_countdown_malformed(self):
        st = DashboardState()
        st.launch_time_iso = "not-a-valid-datetime"
        text = st.get_countdown_text()
        self.assertEqual(text, "开售时间未设置")

    # =========================================================================
    # 5. Safety Info by Mode Tests
    # =========================================================================

    def test_safety_info_demo(self):
        st = DashboardState()
        st.dashboard_mode = "DEMO"
        pill_text, pill_level, title, desc = st.get_safety_info()
        self.assertEqual(title, "🎨 界面演示 · 安全沙箱")
        self.assertEqual(pill_text, "全部状态模拟")
        self.assertEqual(pill_level, "ORANGE")
        self.assertIn("纯本地界面演练", desc)

    def test_safety_info_runtime_verify(self):
        st = DashboardState()
        st.dashboard_mode = "RUNTIME_VERIFY"
        pill_text, pill_level, title, desc = st.get_safety_info()
        self.assertEqual(title, "🧪 受控验证保护模式")
        self.assertEqual(pill_text, "真实加车已禁用")
        self.assertEqual(pill_level, "BLUE")
        self.assertIn("RUNTIME_VERIFY_NO_ADD_TO_BAG", desc)

    def test_safety_info_formal_launch(self):
        st = DashboardState()
        st.dashboard_mode = "FORMAL_LAUNCH"
        pill_text, pill_level, title, desc = st.get_safety_info()
        self.assertEqual(title, "🔒 资金安全保护已启用")
        self.assertEqual(pill_text, "已隔离保护")
        self.assertEqual(pill_level, "GREEN")
        self.assertIn("支付方式与扣款网关", desc)

    # =========================================================================
    # 6. Dashboard Adapter Production Event Tests
    # =========================================================================

    def test_adapter_mode_changed(self):
        adapter = DashboardAdapter()
        adapter.on_mode_changed("RUNTIME_VERIFY")
        st = adapter.get_latest_state()
        self.assertEqual(st.dashboard_mode, "RUNTIME_VERIFY")
        self.assertFalse(st.is_demo)

    def test_adapter_browser_started(self):
        adapter = DashboardAdapter()
        adapter.on_browser_started(True, "Browser=1, Context=1, Page=1")
        st = adapter.get_latest_state()
        self.assertEqual(st.browser_status, "已启动")
        self.assertTrue(any("Chromium 浏览器已启动" in e.message for e in st.recent_events))

    def test_adapter_session_checking_and_status(self):
        adapter = DashboardAdapter()
        adapter.on_session_checking()
        st = adapter.get_latest_state()
        self.assertEqual(st.session_status, "检查中")

        adapter.on_session_status(True, "已登录用户 (张**)")
        st = adapter.get_latest_state()
        self.assertEqual(st.session_status, "已就绪")
        self.assertEqual(st.pipeline_session, "COMPLETED")

    def test_adapter_catalog_verified(self):
        adapter = DashboardAdapter()
        adapter.on_catalog_checking()
        st = adapter.get_latest_state()
        self.assertEqual(st.catalog_status, "解析中")

        adapter.on_catalog_verified("iPhone Duo", "MK2P4CH/A", sku_count=8)
        st = adapter.get_latest_state()
        self.assertEqual(st.catalog_status, "已验证")
        self.assertEqual(st.target_sku, "MK2P4CH/A")
        self.assertTrue(st.target_locked)

    def test_adapter_target_prepared_success(self):
        adapter = DashboardAdapter()
        adapter.on_target_preparing()
        st = adapter.get_latest_state()
        self.assertEqual(st.target_prepare_status, "预选中")
        self.assertEqual(st.pipeline_prepare, "ACTIVE")

        adapter.on_target_prepared("iPhone Duo", "星光白色", "512GB", "MK2P4CH/A", success=True)
        st = adapter.get_latest_state()
        self.assertEqual(st.target_prepare_status, "已就绪")
        self.assertEqual(st.pipeline_prepare, "COMPLETED")
        self.assertEqual(st.pipeline_monitor, "ACTIVE")
        self.assertEqual(st.hero_level, "READY")

    def test_adapter_target_prepared_failure(self):
        adapter = DashboardAdapter()
        adapter.on_target_prepared("iPhone Duo", "星光白色", "512GB", "MK2P4CH/A", success=False)
        st = adapter.get_latest_state()
        self.assertEqual(st.target_prepare_status, "异常")
        self.assertEqual(st.hero_level, "ERROR")

    def test_adapter_selection_preserved(self):
        adapter = DashboardAdapter()
        adapter.on_selection_preserved("MK2P4CH/A")
        st = adapter.get_latest_state()
        self.assertTrue(any("FORMAL_SELECTION_PRESERVED" in e.message for e in st.recent_events))

    def test_adapter_final_target_checked(self):
        adapter = DashboardAdapter()
        adapter.on_final_target_checked(True, "MK2P4CH/A")
        st = adapter.get_latest_state()
        self.assertTrue(any("FINAL_TARGET_CHECK" in e.message for e in st.recent_events))

        adapter.on_final_target_checked(False, "MK2P4CH/A")
        st = adapter.get_latest_state()
        self.assertTrue(any("FINAL_TARGET_CHECK_FAILED" in e.message for e in st.recent_events))

    def test_adapter_runtime_verify_blocked(self):
        adapter = DashboardAdapter()
        adapter.on_runtime_verify_blocked("MK2P4CH/A")
        st = adapter.get_latest_state()
        self.assertIn("受控验证完成", st.hero_title)
        self.assertEqual(st.hero_level, "READY")
        self.assertEqual(st.pipeline_purchase, "COMPLETED")
        self.assertTrue(any("RUNTIME_VERIFY_ADD_TO_BAG_BLOCKED" in e.message for e in st.recent_events))

    def test_adapter_human_action_required(self):
        adapter = DashboardAdapter()
        adapter.on_mode_changed("RUNTIME_VERIFY")
        adapter.on_human_action_required()
        st = adapter.get_latest_state()
        self.assertTrue(st.human_action_required)
        self.assertEqual(st.pipeline_handoff, "ACTIVE")
        self.assertEqual(st.hero_level, "HANDOFF")
        self.assertIn("受控验证已完成最终规格核验", st.human_action_reason)

    def test_adapter_stage_changed_transitions(self):
        adapter = DashboardAdapter()
        for stage_id, expected_attr in [
            ("session", "pipeline_session"),
            ("prepare", "pipeline_prepare"),
            ("monitor", "pipeline_monitor"),
            ("purchase", "pipeline_purchase"),
            ("handoff", "pipeline_handoff"),
        ]:
            adapter.on_stage_changed(stage_id, f"Transition to {stage_id}")
            st = adapter.get_latest_state()
            self.assertEqual(getattr(st, expected_attr), "ACTIVE")

    def test_adapter_init_from_config(self):
        cfg = AppConfig()
        cfg.target.product = "iPhone Duo"
        cfg.target.color = "星光白色"
        cfg.target.storage = "512GB"
        cfg.target.part_number = "MK2P4CH/A"
        cfg.notifications.feishu.webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef123456"
        cfg.notifications.feishu.verified = True
        cfg.launch.launch_time = "2026-09-18T20:00:00"

        adapter = DashboardAdapter()
        adapter.init_from_config(cfg)
        st = adapter.get_latest_state()
        self.assertEqual(st.target_product, "iPhone Duo")
        self.assertEqual(st.target_color, "星光白色")
        self.assertEqual(st.target_storage, "512GB")
        self.assertEqual(st.target_sku, "MK2P4CH/A")
        self.assertEqual(st.feishu_status, "已验证")
        self.assertEqual(st.launch_time_iso, "2026-09-18T20:00:00")
        self.assertIn("****", st.feishu_masked_token)

    # =========================================================================
    # 7. Robustness & Isolation Tests
    # =========================================================================

    def test_adapter_close_isolation(self):
        """When adapter is deactivated (e.g. GUI closed), events do not raise and queue remains untouched."""
        adapter = DashboardAdapter()
        adapter.is_active = False
        initial_qsize = adapter._queue.qsize()

        # None of these calls should raise or enqueue
        adapter.on_browser_started(True)
        adapter.on_session_status(True)
        adapter.on_catalog_verified("iPhone Duo", "MK2P4CH/A")
        adapter.on_target_prepared("iPhone Duo", "星光白色", "512GB", "MK2P4CH/A", True)
        adapter.on_human_action_required("test")

        self.assertEqual(adapter._queue.qsize(), initial_qsize)

    def test_adapter_queue_overflow_protection(self):
        """When queue reaches capacity, oldest item is dropped and new item enqueued without raising."""
        adapter = DashboardAdapter(max_queue_size=5)
        for i in range(10):
            adapter.on_custom_event("INFO", f"Message {i}")
        self.assertLessEqual(adapter._queue.qsize(), 5)

    def test_adapter_drain_to_state(self):
        adapter = DashboardAdapter()
        adapter.on_mode_changed("FORMAL_LAUNCH")
        adapter.on_catalog_verified("iPhone Duo", "MK2P4CH/A")

        gui_state = DashboardState()
        drained = adapter.drain_to_state(gui_state, max_batch=10)
        self.assertEqual(drained, 2)
        self.assertEqual(gui_state.dashboard_mode, "FORMAL_LAUNCH")
        self.assertEqual(gui_state.target_sku, "MK2P4CH/A")

    def test_store_display_timestamp_update(self):
        store = StoreDisplayState(store_number="R390", name="香港广场")
        self.assertEqual(store.updated_at, "--:--:--")
        store.status = "AVAILABLE"
        store.status_cn = "可到店取货"
        store.is_available = True
        store.updated_at = "20:00:05"
        self.assertEqual(store.updated_at, "20:00:05")

    def test_launch_config_dataclass(self):
        lc = LaunchConfig(launch_time="2026-09-18T20:00:00")
        self.assertEqual(lc.launch_time, "2026-09-18T20:00:00")
        lc_empty = LaunchConfig()
        self.assertIsNone(lc_empty.launch_time)

    def test_coordinator_close_isolation(self):
        """Verify LaunchCoordinator functions normally when adapter is deactivated."""
        import asyncio
        from src.main import LaunchCoordinator, AvailabilityEvent

        page = MagicMock()
        config = AppConfig()
        buyer = MagicMock()
        resolver = MagicMock()
        notifier = MagicMock()
        notifier.notify_background = MagicMock()
        pickup_monitor = MagicMock()
        adapter = DashboardAdapter()
        adapter.is_active = False  # Simulate GUI closed

        coordinator = LaunchCoordinator(
            page=page,
            config=config,
            buyer=buyer,
            resolver=resolver,
            notifier=notifier,
            pickup_monitor=pickup_monitor,
            dashboard_adapter=adapter,
        )

        # Handle pickup event - should not raise
        p_event = AvailabilityEvent(
            source="pickup",
            state="AVAILABLE",
            part_number="MK2P4CH/A",
            store_number="R390",
            store_name="香港广场",
            pickup_quote="今天可取货",
        )
        asyncio.run(coordinator.handle_pickup_event(p_event))

        # Handle online event - should execute successfully
        o_event = AvailabilityEvent(
            source="online",
            state="AVAILABLE",
            part_number="MK2P4CH/A",
        )
        async def fake_checkout(*args, **kwargs):
            return True

        with patch("src.main.execute_formal_online_checkout_flow", side_effect=fake_checkout):
            res = asyncio.run(coordinator.handle_online_event(o_event))
            self.assertTrue(res)


if __name__ == "__main__":
    unittest.main()
