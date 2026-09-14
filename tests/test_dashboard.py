"""
Dashboard V1 专项单元测试套件 (Dashboard Unit Tests)
=================================================
覆盖 25+ 项核心功能、状态模型、适配器隔离、防泄漏与安全性测试。
"""

import unittest
import queue
import time
from unittest.mock import MagicMock, patch

from src.dashboard_state import (
    DashboardState,
    StoreDisplayState,
    RecentEvent,
    mask_secret,
    mask_webhook_url,
)
from src.dashboard_adapter import DashboardAdapter


class TestDashboardStateAndAdapter(unittest.TestCase):
    """Dashboard 核心测试集"""

    # 1. State 默认值
    def test_01_state_defaults(self):
        state = DashboardState()
        self.assertEqual(state.version, "V0.4.2 RC2")
        self.assertEqual(state.target_product, "iPhone Duo")
        self.assertEqual(state.target_color, "星光白色")
        self.assertEqual(state.target_storage, "512GB")
        self.assertEqual(state.target_sku, "MK2P4CH/A")
        self.assertTrue(state.target_locked)
        self.assertFalse(state.target_mismatch)
        self.assertEqual(len(state.stores), 4)
        self.assertIn("R390", state.stores)
        self.assertIn("R401", state.stores)
        self.assertIn("R581", state.stores)
        self.assertIn("R683", state.stores)

    # 2. Target mapping
    def test_02_target_mapping(self):
        state = DashboardState()
        state.set_target("iPhone 17 Pro", "沙漠色钛金属", "1TB", "MYXX3CH/A", locked=True, mismatch=False)
        self.assertEqual(state.target_product, "iPhone 17 Pro")
        self.assertEqual(state.target_color, "沙漠色钛金属")
        self.assertEqual(state.target_storage, "1TB")
        self.assertEqual(state.target_sku, "MYXX3CH/A")
        self.assertEqual(state.target_status_text, "目标已锁定")

        state.set_target("iPhone Duo", "星光白色", "512GB", "MISMATCH_SKU", locked=False, mismatch=True)
        self.assertEqual(state.target_status_text, "目标异常")

    # 3. 上海四店 mapping
    def test_03_shanghai_stores_mapping(self):
        state = DashboardState()
        self.assertEqual(state.stores["R390"].name, "香港广场")
        self.assertEqual(state.stores["R401"].name, "上海环贸 iapm")
        self.assertEqual(state.stores["R581"].name, "五角场")
        self.assertEqual(state.stores["R683"].name, "环球港")

    # 4. State update store
    def test_04_state_update_store(self):
        state = DashboardState()
        state.update_store("R390", "AVAILABLE", "可到店取货", "今天可取货")
        s = state.stores["R390"]
        self.assertEqual(s.status, "AVAILABLE")
        self.assertEqual(s.status_cn, "可到店取货")
        self.assertEqual(s.pickup_quote, "今天可取货")
        self.assertTrue(s.is_available)
        self.assertFalse(s.has_error)

    # 5. Event append
    def test_05_event_append(self):
        state = DashboardState()
        state.add_event("SUCCESS", "测试成功事件")
        self.assertEqual(len(state.recent_events), 1)
        self.assertEqual(state.recent_events[0].level, "SUCCESS")
        self.assertEqual(state.recent_events[0].level_cn, "成功")
        self.assertEqual(state.recent_events[0].message, "测试成功事件")

    # 6. Event max length (最多 20 条)
    def test_06_event_max_length(self):
        state = DashboardState()
        for i in range(35):
            state.add_event("INFO", f"事件 {i}")
        self.assertEqual(len(state.recent_events), 20)
        self.assertEqual(state.recent_events[-1].message, "事件 34")

    # 7. Online state update
    def test_07_online_state(self):
        adapter = DashboardAdapter()
        adapter.on_online_status("AVAILABLE", "可购买", failures=0, backoff_sec=0.0)
        state = adapter.get_latest_state()
        self.assertEqual(state.online_state_token, "AVAILABLE")
        self.assertEqual(state.online_state_cn, "可购买")
        self.assertIn("开放购买", state.hero_title)

    # 8. Pickup state update
    def test_08_pickup_state(self):
        adapter = DashboardAdapter()
        adapter.on_pickup_round_completed(8.88, next_check_text="约 25 秒后", backoff_sec=0.0)
        state = adapter.get_latest_state()
        self.assertEqual(state.pickup_round_duration, "8.88 秒")
        self.assertEqual(state.pickup_next_check, "约 25 秒后")

    # 9. Feishu ACK update
    def test_09_feishu_ack(self):
        adapter = DashboardAdapter()
        adapter.on_feishu_status(True, last_ack_ms=512.4, pending_count=0)
        state = adapter.get_latest_state()
        self.assertEqual(state.feishu_status, "已连接")
        self.assertEqual(state.feishu_last_ack_ms, 512.4)

    # 10. Secret masking
    def test_10_secret_masking(self):
        token = "f1837a2849c24f"
        masked = mask_secret(token, keep_start=2, keep_end=4)
        self.assertEqual(masked, "f1****c24f")

        url = "https://open.feishu.cn/open-apis/bot/v2/hook/abc1234567xyz"
        masked_url = mask_webhook_url(url)
        self.assertTrue(masked_url.startswith("ab****"))
        self.assertTrue(masked_url.endswith("7xyz"))
        self.assertNotIn("https://", masked_url)

    # 11. Human handoff
    def test_11_human_handoff(self):
        adapter = DashboardAdapter()
        adapter.on_human_action_required("已推进至结账安全停机点")
        state = adapter.get_latest_state()
        self.assertTrue(state.human_action_required)
        self.assertIn("人工接管", state.hero_title)
        self.assertEqual(state.hero_level, "HANDOFF")
        self.assertEqual(state.pipeline_handoff, "ACTIVE")

    # 12. Safety state
    def test_12_safety_state(self):
        state = DashboardState()
        self.assertEqual(state.security_gate_status, "已启用")
        self.assertFalse(state.human_action_required)

    # 13. RateGuard state
    def test_13_rate_guard_state(self):
        state = DashboardState()
        self.assertEqual(state.rate_guard_status, "已启用")

    # 14. Dashboard Adapter init
    def test_14_dashboard_adapter_init(self):
        adapter = DashboardAdapter(max_queue_size=100)
        self.assertTrue(adapter.is_active)
        self.assertEqual(adapter._queue.qsize(), 0)

    # 15. Queue bridge thread safety
    def test_15_queue_bridge(self):
        adapter = DashboardAdapter()
        adapter.on_session_status(True, "登录有效")
        adapter.on_catalog_verified("iPhone Duo", "MK2P4CH/A")
        
        gui_state = DashboardState()
        drained = adapter.drain_to_state(gui_state)
        self.assertEqual(drained, 2)
        self.assertEqual(gui_state.session_status, "已就绪")
        self.assertEqual(gui_state.catalog_status, "已验证")

    # 16. GUI closed does not stop launch
    def test_16_gui_closed_does_not_stop_launch(self):
        adapter = DashboardAdapter(max_queue_size=2)
        # 停用 adapter
        adapter.is_active = False
        # 调用各种事件，绝不报错
        adapter.on_online_status("AVAILABLE", "可购买")
        adapter.on_human_action_required("接管")
        adapter.add_event("INFO", "普通事件")
        # 激活后再塞满队列
        adapter.is_active = True
        for i in range(10):
            adapter.add_event("INFO", f"事件 {i}")
        # 不发生任何崩溃，队列有界丢弃
        self.assertLessEqual(adapter._queue.qsize(), 2)

    # 17. Unknown state fallback
    def test_17_unknown_state_fallback(self):
        state = DashboardState()
        state.update_store("R390", "UNKNOWN_STATUS", "未知状态")
        self.assertFalse(state.stores["R390"].is_available)
        self.assertFalse(state.stores["R390"].has_error)

    # 18. Chinese labels integrity
    def test_18_chinese_labels_integrity(self):
        state = DashboardState()
        self.assertIn("控制中心", state.app_title)
        self.assertIn("准备就绪", state.hero_title)
        for s in state.stores.values():
            self.assertIn(s.name, ["香港广场", "上海环贸 iapm", "五角场", "环球港"])

    # 19. Store available state
    def test_19_store_available(self):
        state = DashboardState()
        state.update_store("R401", "AVAILABLE", "可到店取货", "今天可取货")
        self.assertTrue(state.stores["R401"].is_available)
        self.assertEqual(state.stores["R401"].status_cn, "可到店取货")

    # 20. Store coming soon state
    def test_20_store_coming_soon(self):
        state = DashboardState()
        state.update_store("R581", "COMING_SOON", "即将发售", "目前暂不提供取货服务")
        self.assertFalse(state.stores["R581"].is_available)
        self.assertEqual(state.stores["R581"].status_cn, "即将发售")

    # 21. Store error state
    def test_21_store_error(self):
        state = DashboardState()
        state.update_store("R683", "403", "接口异常", "HTTP 403 边缘拦截")
        self.assertTrue(state.stores["R683"].has_error)
        self.assertFalse(state.stores["R683"].is_available)

    # 22. Dashboard demo no Apple requests
    def test_22_dashboard_demo_no_apple(self):
        from src.dashboard_demo import run_dashboard_demo
        # 确认 demo 模块内部不包含对 Apple 的 urllib / requests / playwright 网络调用
        import inspect
        src_code = inspect.getsource(run_dashboard_demo)
        self.assertNotIn("playwright", src_code)
        self.assertNotIn("page.goto", src_code)
        self.assertNotIn("apple.com", src_code)

    # 23. Dashboard demo no Chromium
    def test_23_dashboard_demo_no_chromium(self):
        from src.dashboard_demo import run_dashboard_demo
        import inspect
        src_code = inspect.getsource(run_dashboard_demo)
        self.assertNotIn("launch_persistent_context", src_code)
        self.assertNotIn("BrowserManager", src_code)

    # 24. Dashboard demo no real Feishu
    def test_24_dashboard_demo_no_real_feishu(self):
        from src.dashboard_demo import run_dashboard_demo
        import inspect
        src_code = inspect.getsource(run_dashboard_demo)
        self.assertNotIn("feishu.send", src_code)
        self.assertNotIn("requests.post", src_code)

    # 25. Adapter drain empty queue
    def test_25_adapter_drain_empty(self):
        adapter = DashboardAdapter()
        gui_state = DashboardState()
        count = adapter.drain_to_state(gui_state)
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
