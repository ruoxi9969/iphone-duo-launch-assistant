"""
Unit and Integration Tests for Dashboard V1.2 & Final Launch Readiness
======================================================================
Tests:
- Strict Session Safety & Fail-Closed Gate
- Latency Optimization & Purchase Options Preselection / Preservation
- Dashboard V1.2 State Model (Checklist, Session Pill, Latency, Badges)
- Dashboard Adapter Synchronizations
- GUI Component Rendering & Safety Guard
- CLI submode and public-only behavior
"""

import unittest
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from src.dashboard_state import DashboardState, mask_secret, mask_webhook_url
from src.dashboard_adapter import DashboardAdapter
from src.config import AppConfig, TargetConfig, NotificationConfig, BrowserConfig, FeishuConfig
from src.apple_store import AppleStoreBuyer
from src.catalog import AppleCatalogResolver, ResolvedProduct, ResolutionResult, ResolutionStatus
from src.notifier import Notifier, NotificationEvent


class TestDashboardV12State(unittest.TestCase):
    """Test DashboardState V1.2 data model and business logic"""

    def setUp(self):
        self.state = DashboardState()

    def test_01_default_fields(self):
        self.assertEqual(self.state.session_last_verified_at, "--:--:--")
        self.assertFalse(self.state.session_strict_blocked)
        self.assertFalse(self.state.is_public_only)
        self.assertFalse(self.state.purchase_options_prepared)
        self.assertEqual(self.state.tradein_status, "待准备")
        self.assertEqual(self.state.applecare_status, "待准备")
        self.assertIsNone(self.state.latency_trigger_to_handoff_sec)
        self.assertEqual(len(self.state.checklist), 10)

    def test_02_checklist_status(self):
        passed, total, all_ok = self.state.get_checklist_status()
        self.assertEqual(total, 10)
        self.assertFalse(all_ok)

        for k in self.state.checklist:
            self.state.checklist[k] = True

        passed, total, all_ok = self.state.get_checklist_status()
        self.assertEqual(passed, 10)
        self.assertEqual(total, 10)
        self.assertTrue(all_ok)

    def test_03_is_ready_for_launch_strict_conditions(self):
        # Initial: not all ready
        self.state.session_status = "未就绪"
        self.assertFalse(self.state.is_ready_for_launch())

        # Setup all valid
        self.state.session_status = "已就绪"
        self.state.session_strict_blocked = False
        self.state.catalog_status = "已验证"
        self.state.target_prepare_status = "已就绪"
        self.state.purchase_options_prepared = True
        self.state.feishu_status = "已连接"
        self.state.local_alert_status = "正常"
        self.state.rate_guard_status = "已启用"
        self.state.is_public_only = False
        self.assertTrue(self.state.is_ready_for_launch())

        # Disqualifier 1: session_strict_blocked
        self.state.session_strict_blocked = True
        self.assertFalse(self.state.is_ready_for_launch())
        self.state.session_strict_blocked = False

        # Disqualifier 2: purchase_options_prepared is False
        self.state.purchase_options_prepared = False
        self.assertFalse(self.state.is_ready_for_launch())
        self.state.purchase_options_prepared = True

        # Disqualifier 3: is_public_only is True
        self.state.is_public_only = True
        self.assertFalse(self.state.is_ready_for_launch())
        self.state.is_public_only = False

        # Disqualifier 4: rate_guard_status not active
        self.state.rate_guard_status = "退避中"
        self.assertFalse(self.state.is_ready_for_launch())

    def test_04_session_pill_info(self):
        self.state.session_status = "检查中"
        txt, lvl, _ = self.state.get_session_pill_info()
        self.assertEqual(txt, "● 检查中")
        self.assertEqual(lvl, "ORANGE")

        self.state.session_status = "已就绪"
        self.state.session_strict_blocked = False
        self.state.session_last_verified_at = "15:30:00"
        txt, lvl, t_str = self.state.get_session_pill_info()
        self.assertEqual(txt, "● 已登录")
        self.assertEqual(lvl, "GREEN")
        self.assertEqual(t_str, "核验: 15:30:00")

        self.state.session_strict_blocked = True
        txt, lvl, _ = self.state.get_session_pill_info()
        self.assertEqual(txt, "● 已阻断")
        self.assertEqual(lvl, "RED")

    def test_05_mode_badge_info(self):
        self.state.dashboard_mode = "DEMO"
        txt, lvl = self.state.get_mode_badge_info()
        self.assertEqual(txt, "【界面演示 · 全部状态模拟】")
        self.assertEqual(lvl, "ORANGE")

        self.state.dashboard_mode = "RUNTIME_VERIFY"
        self.state.is_public_only = False
        txt, lvl = self.state.get_mode_badge_info()
        self.assertEqual(txt, "【受控联调 · 禁止真实加车】")
        self.assertEqual(lvl, "BLUE")

        self.state.is_public_only = True
        txt, lvl = self.state.get_mode_badge_info()
        self.assertEqual(txt, "【受控联调 · 公开页面验证】")
        self.assertEqual(lvl, "ORANGE")

        self.state.dashboard_mode = "FORMAL_LAUNCH"
        self.state.session_strict_blocked = False
        self.state.hero_level = "READY"
        txt, lvl = self.state.get_mode_badge_info()
        self.assertEqual(txt, "【首发实战 · 生产状态】")
        self.assertEqual(lvl, "GREEN")

        self.state.session_strict_blocked = True
        txt, lvl = self.state.get_mode_badge_info()
        self.assertEqual(txt, "【首发实战 · 流程阻断】")
        self.assertEqual(lvl, "RED")

    def test_06_handoff_text_specifications(self):
        self.state.dashboard_mode = "DEMO"
        txt = self.state.get_handoff_text()
        self.assertIn("【界面演示】模拟购买流程已推进至人工接管阶段。本模式未访问 Apple，也未执行真实加车。", txt)
        self.assertNotIn("已全自动加入购物袋", txt)

        self.state.dashboard_mode = "FORMAL_LAUNCH"
        txt = self.state.get_handoff_text()
        self.assertIn("商品已加入购物袋", txt)

        self.state.dashboard_mode = "RUNTIME_VERIFY"
        txt = self.state.get_handoff_text()
        self.assertIn("受控验证已完成最终规格核验", txt)

    def test_07_safety_info_session_blocked(self):
        self.state.session_strict_blocked = True
        self.state.session_fail_reason = "被重定向到登录入口"
        pill, lvl, title, desc = self.state.get_safety_info()
        self.assertEqual(pill, "流程已阻断")
        self.assertEqual(lvl, "RED")
        self.assertIn("Apple 登录已失效", title)
        self.assertIn("python -m src.main --mode prepare-session", desc)

    def test_08_safety_info_public_only(self):
        self.state.session_strict_blocked = False
        self.state.is_public_only = True
        pill, lvl, title, desc = self.state.get_safety_info()
        self.assertEqual(pill, "公开页面验证")
        self.assertEqual(lvl, "ORANGE")
        self.assertIn("公开页面受限模式", title)
        self.assertIn("仅验证公开 Catalog", desc)


class TestDashboardV12Adapter(unittest.TestCase):
    """Test DashboardAdapter V1.2 hooks and synchronization"""

    def setUp(self):
        self.adapter = DashboardAdapter()
        self.state = DashboardState()

    def test_09_on_session_status(self):
        self.adapter.on_session_status(True, "有效会话", verified_at="15:35:10")
        self.adapter.drain_to_state(self.state)
        self.assertEqual(self.state.session_status, "已就绪")
        self.assertEqual(self.state.session_last_verified_at, "15:35:10")
        self.assertTrue(self.state.checklist["session_verified"])
        self.assertFalse(self.state.session_strict_blocked)

    def test_10_on_session_strict_blocked(self):
        self.adapter.on_session_strict_blocked("重定向至 Signin")
        self.adapter.drain_to_state(self.state)
        self.assertEqual(self.state.session_status, "未就绪")
        self.assertTrue(self.state.session_strict_blocked)
        self.assertEqual(self.state.session_fail_reason, "重定向至 Signin")
        self.assertFalse(self.state.checklist["session_verified"])
        self.assertEqual(self.state.hero_title, "首发流程已阻断")
        self.assertEqual(self.state.hero_level, "ERROR")

    def test_11_on_public_only_warning(self):
        self.adapter.on_public_only_warning()
        self.adapter.drain_to_state(self.state)
        self.assertTrue(self.state.is_public_only)
        self.assertEqual(self.state.hero_title, "受限模式 · 仅公开页面验证")
        self.assertIn("禁止进入购买流程", self.state.hero_subtitle)
        self.assertEqual(self.state.hero_level, "WARNING")

    def test_12_on_purchase_options_prepared(self):
        self.adapter.on_purchase_options_prepared(tradein_ok=True, applecare_ok=True)
        self.adapter.drain_to_state(self.state)
        self.assertTrue(self.state.purchase_options_prepared)
        self.assertEqual(self.state.tradein_status, "已预选 (不折抵)")
        self.assertEqual(self.state.applecare_status, "已预选 (不加服务)")
        self.assertTrue(self.state.checklist["purchase_options_prepared"])

    def test_13_on_purchase_options_preserved(self):
        self.adapter.on_purchase_options_preserved()
        self.adapter.drain_to_state(self.state)
        self.assertTrue(self.state.checklist["selection_preservation_verified"])
        self.assertTrue(any("FORMAL_PURCHASE_OPTIONS_PRESERVED" in e.message for e in self.state.recent_events))

    def test_14_on_latency_recorded(self):
        self.adapter.on_latency_recorded(1.85, feishu_ack_ms=320.5)
        self.adapter.drain_to_state(self.state)
        self.assertEqual(self.state.latency_trigger_to_handoff_sec, 1.85)
        self.assertEqual(self.state.feishu_last_ack_ms, 320.5)
        self.assertTrue(any("LATENCY_PERF" in e.message for e in self.state.recent_events))

    def test_15_checklist_full_lifecycle_sync(self):
        # Browser
        self.adapter.on_browser_started(True)
        # Catalog
        self.adapter.on_catalog_verified("iPhone Duo", "MK2P4CH/A", 8)
        # Target
        self.adapter.on_target_prepared("iPhone Duo", "星光白色", "512GB", "MK2P4CH/A", True)
        # Online
        self.adapter.on_online_status("COMING_SOON", "即将发售")
        # Pickup
        self.adapter.on_pickup_round_completed(8.5)
        # Feishu
        self.adapter.on_feishu_status(True, 450.0)

        self.adapter.drain_to_state(self.state)
        self.assertTrue(self.state.checklist["browser_prewarmed"])
        self.assertTrue(self.state.checklist["catalog_locked"])
        self.assertTrue(self.state.checklist["target_spec_prepared"])
        self.assertTrue(self.state.checklist["online_monitor_standby"])
        self.assertTrue(self.state.checklist["pickup_monitor_standby"])
        self.assertTrue(self.state.checklist["feishu_async_ready"])


class TestAppleStoreOptionsOptimization(unittest.IsolatedAsyncioTestCase):
    """Test options fast-detection and skip logic in AppleStoreBuyer"""

    async def test_16_is_trade_in_none_selected_true(self):
        mock_page = AsyncMock()
        mock_buyer = AppleStoreBuyer(mock_page, AppConfig())

        mock_elem = AsyncMock()
        mock_elem.first = mock_elem
        mock_elem.count = AsyncMock(return_value=1)
        mock_elem.evaluate = AsyncMock(return_value="input")
        mock_elem.is_checked = AsyncMock(return_value=True)
        mock_page.locator = MagicMock(return_value=mock_elem)

        result = await mock_buyer.is_trade_in_none_selected()
        self.assertTrue(result)

    async def test_17_is_trade_in_none_selected_false(self):
        mock_page = AsyncMock()
        mock_buyer = AppleStoreBuyer(mock_page, AppConfig())

        mock_elem = AsyncMock()
        mock_elem.first = mock_elem
        mock_elem.is_checked = AsyncMock(return_value=False)
        mock_elem.get_attribute = AsyncMock(return_value="false")
        mock_elem.count = AsyncMock(return_value=0)
        mock_page.locator = MagicMock(return_value=mock_elem)
        mock_page.evaluate = AsyncMock(return_value=False)

        result = await mock_buyer.is_trade_in_none_selected()
        self.assertFalse(result)

    async def test_18_is_applecare_none_selected_true(self):
        mock_page = AsyncMock()
        mock_buyer = AppleStoreBuyer(mock_page, AppConfig())

        mock_elem = AsyncMock()
        mock_elem.first = mock_elem
        mock_elem.count = AsyncMock(return_value=1)
        mock_elem.evaluate = AsyncMock(return_value="input")
        mock_elem.is_checked = AsyncMock(return_value=True)
        mock_page.locator = MagicMock(return_value=mock_elem)

        result = await mock_buyer.is_applecare_none_selected()
        self.assertTrue(result)

    async def test_19_handle_trade_in_skips_when_already_selected(self):
        mock_page = AsyncMock()
        mock_buyer = AppleStoreBuyer(mock_page, AppConfig())

        mock_buyer.is_trade_in_none_selected = AsyncMock(return_value=True)
        mock_page.click = AsyncMock()

        res = await mock_buyer.handle_trade_in()
        self.assertTrue(res)
        mock_page.click.assert_not_called()

    async def test_20_handle_applecare_skips_when_already_selected(self):
        mock_page = AsyncMock()
        mock_buyer = AppleStoreBuyer(mock_page, AppConfig())

        mock_buyer.is_applecare_none_selected = AsyncMock(return_value=True)
        mock_page.click = AsyncMock()

        res = await mock_buyer.handle_applecare()
        self.assertTrue(res)
        mock_page.click.assert_not_called()


class TestCheckoutFlowOptimization(unittest.IsolatedAsyncioTestCase):
    """Test execute_formal_target_prepare and execute_formal_online_checkout_flow"""

    async def test_21_execute_formal_target_prepare_preselects_options(self):
        from src.main import execute_formal_target_prepare

        mock_page = AsyncMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        mock_buyer = MagicMock()
        mock_buyer.open_product_page = AsyncMock(return_value=True)
        mock_buyer.fast_select_color = AsyncMock()
        mock_buyer.fast_select_storage = AsyncMock()
        mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        mock_buyer.handle_applecare = AsyncMock(return_value=True)
        mock_buyer.is_trade_in_none_selected = AsyncMock(return_value=True)
        mock_buyer.is_applecare_none_selected = AsyncMock(return_value=True)

        mock_resolver = MagicMock()
        adapter = DashboardAdapter()
        config = AppConfig()

        ok = await execute_formal_target_prepare(
            page=mock_page,
            config=config,
            buyer=mock_buyer,
            resolver=mock_resolver,
            expected_part="MK2P4CH/A",
            dashboard_adapter=adapter,
        )
        self.assertTrue(ok)
        mock_buyer.handle_trade_in.assert_called_once()
        mock_buyer.handle_applecare.assert_called_once()
        st = adapter.get_latest_state()
        self.assertTrue(st.purchase_options_prepared)

    async def test_22_execute_formal_online_checkout_flow_options_preserved(self):
        from src.main import execute_formal_online_checkout_flow

        mock_page = AsyncMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        mock_buyer = MagicMock()
        mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        mock_buyer.is_trade_in_none_selected = AsyncMock(return_value=True)
        mock_buyer.is_applecare_none_selected = AsyncMock(return_value=True)
        mock_buyer.handle_trade_in = AsyncMock()
        mock_buyer.handle_applecare = AsyncMock()

        mock_resolver = MagicMock()
        mock_notifier = MagicMock()
        adapter = DashboardAdapter()
        config = AppConfig()

        ok = await execute_formal_online_checkout_flow(
            page=mock_page,
            config=config,
            buyer=mock_buyer,
            resolver=mock_resolver,
            notifier=mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
            runtime_verify_no_add_to_bag=True,
            dashboard_adapter=adapter,
        )
        self.assertTrue(ok)
        mock_buyer.handle_trade_in.assert_not_called()
        mock_buyer.handle_applecare.assert_not_called()
        st = adapter.get_latest_state()
        self.assertTrue(st.checklist["selection_preservation_verified"])
        self.assertIsNotNone(st.latency_trigger_to_handoff_sec)
        self.assertLess(st.latency_trigger_to_handoff_sec, 2.0)

    async def test_23_execute_formal_online_checkout_flow_reselects_if_option_lost(self):
        from src.main import execute_formal_online_checkout_flow

        mock_page = AsyncMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        mock_buyer = MagicMock()
        mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        mock_buyer.is_trade_in_none_selected = AsyncMock(return_value=False)
        mock_buyer.is_applecare_none_selected = AsyncMock(return_value=True)
        mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        mock_buyer.handle_applecare = AsyncMock(return_value=True)

        mock_resolver = MagicMock()
        mock_notifier = MagicMock()
        adapter = DashboardAdapter()
        config = AppConfig()

        ok = await execute_formal_online_checkout_flow(
            page=mock_page,
            config=config,
            buyer=mock_buyer,
            resolver=mock_resolver,
            notifier=mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
            runtime_verify_no_add_to_bag=True,
            dashboard_adapter=adapter,
        )
        self.assertTrue(ok)
        mock_buyer.handle_trade_in.assert_called_once()
        mock_buyer.handle_applecare.assert_not_called()

    async def test_24_execute_formal_online_checkout_flow_final_target_check_strict(self):
        from src.main import execute_formal_online_checkout_flow

        mock_page = AsyncMock()
        mock_buyer = MagicMock()
        mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        # Fail on final target check
        mock_buyer.verify_target_sku_consistency = AsyncMock(side_effect=[True, False])
        mock_buyer.is_trade_in_none_selected = AsyncMock(return_value=True)
        mock_buyer.is_applecare_none_selected = AsyncMock(return_value=True)

        mock_resolver = MagicMock()
        mock_notifier = MagicMock()
        adapter = DashboardAdapter()
        config = AppConfig()

        ok = await execute_formal_online_checkout_flow(
            page=mock_page,
            config=config,
            buyer=mock_buyer,
            resolver=mock_resolver,
            notifier=mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
            runtime_verify_no_add_to_bag=True,
            dashboard_adapter=adapter,
        )
        self.assertFalse(ok)
        self.assertTrue(any("FINAL_TARGET_CHECK_FAILED" in e.message for e in adapter.get_latest_state().recent_events))


class TestLaunchModeAndRuntimeVerifyFailClosed(unittest.IsolatedAsyncioTestCase):
    """Test session fail-closed handling in launch and runtime verify"""

    @patch("src.main.evaluate_launch_readiness")
    @patch("src.main.BrowserManager")
    async def test_25_run_launch_mode_fails_closed_on_session_error(self, mock_bm_cls, mock_eval):
        from src.main import run_launch_mode

        mock_bm = MagicMock()
        mock_page = AsyncMock()
        mock_bm.start = AsyncMock(return_value=mock_page)
        mock_bm.close = AsyncMock()
        mock_bm_cls.return_value = mock_bm

        mock_eval.return_value = (False, ["SESSION NOT READY"], ["SESSION_NOT_READY: 未登录"])
        adapter = DashboardAdapter()

        config = AppConfig()
        await run_launch_mode(config, dashboard_adapter=adapter)

        st = adapter.get_latest_state()
        self.assertTrue(st.session_strict_blocked)
        self.assertEqual(st.hero_title, "首发流程已阻断")
        self.assertEqual(st.hero_level, "ERROR")

    @patch("src.main.check_apple_login_status")
    @patch("src.main.BrowserManager")
    async def test_26_run_formal_runtime_verify_strict_fails_closed(self, mock_bm_cls, mock_check_login):
        from src.main import run_formal_runtime_verify

        mock_bm = MagicMock()
        mock_page = AsyncMock()
        mock_bm.start = AsyncMock(return_value=mock_page)
        mock_bm.close = AsyncMock()
        mock_bm.context = MagicMock()
        mock_bm.context.pages = [mock_page]
        mock_bm_cls.return_value = mock_bm

        mock_check_login.return_value = (False, "Redirected to login")
        adapter = DashboardAdapter()

        config = AppConfig()
        ok = await run_formal_runtime_verify(
            config,
            ignore_session_check=False,
            submode="strict",
            dashboard_adapter=adapter,
        )
        self.assertFalse(ok)
        st = adapter.get_latest_state()
        self.assertTrue(st.session_strict_blocked)
        self.assertEqual(st.hero_level, "ERROR")

    @patch("src.main.check_apple_login_status")
    @patch("src.main.BrowserManager")
    @patch("src.main.AppleCatalogResolver")
    @patch("src.main.AppleStoreBuyer")
    @patch("src.main.execute_formal_target_prepare")
    @patch("src.main.PickupMonitor")
    @patch("src.main.execute_formal_online_checkout_flow")
    async def test_27_run_formal_runtime_verify_public_only_warns_never_ready(
        self, mock_checkout, mock_pickup, mock_prep, mock_buyer_cls, mock_resolver_cls, mock_bm_cls, mock_check_login
    ):
        from src.main import run_formal_runtime_verify

        mock_bm = MagicMock()
        mock_page = AsyncMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        mock_bm.start = AsyncMock(return_value=mock_page)
        mock_bm.close = AsyncMock()
        mock_bm.context = MagicMock()
        mock_bm.context.pages = [mock_page]
        mock_bm_cls.return_value = mock_bm

        mock_check_login.return_value = (False, "Redirected to login")

        mock_buyer = MagicMock()
        mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        mock_buyer.open_product_page = AsyncMock(return_value=True)
        mock_buyer_cls.return_value = mock_buyer

        mock_resolver = MagicMock()
        mock_cat_res = MagicMock()
        mock_cat_res.product = MagicMock()
        mock_cat_res.product.part_number = "MK2P4CH/A"
        mock_cat_res.product.skus = ["MK2P4CH/A"]
        mock_resolver.resolve = AsyncMock(return_value=mock_cat_res)
        mock_resolver.fetch_product_catalog = AsyncMock(return_value=({}, None))
        mock_resolver_cls.return_value = mock_resolver

        from src.pickup_monitor import InventoryStatus
        mock_prep.return_value = True
        mock_pm = MagicMock()
        mock_pm.store_throttle_seconds = 0.0
        mock_pm.ensure_session = AsyncMock()
        mock_res = MagicMock(store_name="香港广场", store_number="R390", status=InventoryStatus.COMING_SOON, pickup_quote="目前暂不提供")
        mock_res.summary.return_value = "目前暂不提供"
        mock_pm.check_store_pickup = AsyncMock(return_value=(mock_res, "COMING_SOON", "目前暂不提供"))
        mock_pm.query_store = AsyncMock(return_value=mock_res)
        mock_pickup.return_value = mock_pm

        mock_checkout.return_value = True
        adapter = DashboardAdapter()

        config = AppConfig()
        with patch("asyncio.sleep", AsyncMock(return_value=None)):
            ok = await run_formal_runtime_verify(
                config,
                submode="public-only",
                dashboard_adapter=adapter,
            )
        self.assertTrue(ok)
        st = adapter.get_latest_state()
        self.assertTrue(st.is_public_only)
        self.assertFalse(st.is_ready_for_launch())
        self.assertIn("仅公开页面验证", st.hero_title)


class TestDashboardV12GUIComponents(unittest.TestCase):
    """Test GUI rendering logic without display errors"""

    def test_28_gui_elements_initialized(self):
        import tkinter as tk
        from src.dashboard_gui import LaunchControlCenterGUI

        root = tk.Tk()
        root.withdraw()
        try:
            adapter = DashboardAdapter()
            gui = LaunchControlCenterGUI(root, adapter=adapter, is_demo=False)
            self.assertIsNotNone(gui.header_session_pill)
            self.assertIsNotNone(gui.target_options_pill)
            self.assertIsNotNone(gui.lat_val_lbl)
            self.assertIsNotNone(gui.checklist_summary_pill)
            self.assertEqual(len(gui.checklist_widgets), 10)
        finally:
            root.destroy()

    def test_29_gui_apply_state_updates_widgets(self):
        import tkinter as tk
        from src.dashboard_gui import LaunchControlCenterGUI

        root = tk.Tk()
        root.withdraw()
        try:
            adapter = DashboardAdapter()
            gui = LaunchControlCenterGUI(root, adapter=adapter, is_demo=False)

            st = adapter.get_latest_state()
            st.session_last_verified_at = "15:40:22"
            st.session_status = "已就绪"
            st.purchase_options_prepared = True
            st.latency_trigger_to_handoff_sec = 1.92
            st.checklist["browser_prewarmed"] = True

            gui.apply_state_to_widgets()
            self.assertIn("15:40:22", gui.header_session_time_lbl.cget("text"))
            self.assertIn("1.92", gui.lat_val_lbl.cget("text"))
            self.assertIn("已预选", gui.target_options_pill.cget("text"))
        finally:
            root.destroy()

    def test_30_gui_session_blocked_shows_command(self):
        import tkinter as tk
        from src.dashboard_gui import LaunchControlCenterGUI

        root = tk.Tk()
        root.withdraw()
        try:
            adapter = DashboardAdapter()
            gui = LaunchControlCenterGUI(root, adapter=adapter, is_demo=False)

            adapter.on_session_strict_blocked("Apple ID 未登录")
            adapter.drain_to_state(gui.state)
            gui.apply_state_to_widgets()

            self.assertTrue(gui.state.session_strict_blocked)
            self.assertIn("prepare-session", gui.hero_cmd_lbl.cget("text"))
            self.assertIn("流程已阻断", gui.checklist_summary_pill.cget("text"))
        finally:
            root.destroy()

    def test_31_gui_never_shows_ready_in_public_only(self):
        import tkinter as tk
        from src.dashboard_gui import LaunchControlCenterGUI

        root = tk.Tk()
        root.withdraw()
        try:
            adapter = DashboardAdapter()
            gui = LaunchControlCenterGUI(root, adapter=adapter, is_demo=False)

            adapter.on_public_only_warning()
            for k in gui.state.checklist:
                gui.state.checklist[k] = True
            adapter.drain_to_state(gui.state)
            gui.apply_state_to_widgets()

            self.assertFalse(gui.state.is_ready_for_launch())
            self.assertNotIn("首发准备完成", gui.checklist_summary_pill.cget("text"))
            self.assertIn("首发暂不可启动", gui.checklist_summary_pill.cget("text"))
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
