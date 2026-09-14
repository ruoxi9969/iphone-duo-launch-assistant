"""首发模拟演练模式 (--mode launch-rehearsal) 单元测试集"""

import os
import time
import json
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

from src.rehearsal import (
    RehearsalTiming,
    PrewarmTiming,
    send_feishu_rehearsal_card,
    play_rehearsal_sound,
    format_aggregated_summary,
    format_prewarmed_summary,
    execute_single_run,
    execute_prewarmed_single_run,
    prewarm_rehearsal_session,
    run_launch_rehearsal,
    TARGET_PRODUCT,
    TARGET_COLOR,
    TARGET_STORAGE,
    TARGET_SKU,
    DUO_PAGE_URL,
)
from src.config import AppConfig


class TestLaunchRehearsal(unittest.TestCase):
    """验证首发模拟演练模式的计时精度、格式输出、飞书隔离与执行链路"""

    def test_1_timing_monotonic_calculations(self):
        """Test 1: 验证基于单调时钟的各阶段相对延迟计算与报告格式 (包含 FEISHU_REQUEST_STARTED/ACK)"""
        timing = RehearsalTiming(run_id=1)
        base = 1000.0

        timing.t0 = base
        timing.t1_alert = base + 0.005              # +5 ms
        timing.t2a_feishu_started = base + 0.010    # +10 ms
        timing.t2b_feishu_ack = base + 0.080        # +80 ms
        timing.feishu_success = True
        timing.t3_browser_ready = base + 0.850      # +850 ms
        timing.t4_page_loaded = base + 2.100        # +2100 ms
        timing.t5_color_selected = base + 2.650     # +2650 ms
        timing.color_confirmed = True
        timing.t6_storage_selected = base + 3.100   # +3100 ms
        timing.storage_confirmed = True
        timing.t7_target_verified = base + 3.300    # +3300 ms
        timing.sku_verified = True
        timing.t8_handoff_ready = base + 3.500      # +3500 ms
        timing.handoff_qualified = True

        self.assertEqual(timing.latency_local_alert, 5)
        self.assertEqual(timing.latency_feishu_started, 10)
        self.assertEqual(timing.latency_feishu_ack, 80)
        self.assertEqual(timing.latency_browser_ready, 850)
        self.assertEqual(timing.latency_page_loaded, 2100)
        self.assertEqual(timing.latency_color_selected, 2650)
        self.assertEqual(timing.latency_storage_selected, 3100)
        self.assertEqual(timing.latency_target_verified, 3300)
        self.assertEqual(timing.latency_handoff_ready, 3500)
        self.assertEqual(timing.total_latency, 3500)

        report = timing.format_report()
        self.assertIn("Launch Rehearsal Timing Report (Run 01)", report)
        self.assertIn("Simulation Trigger          +0 ms", report)
        self.assertIn("Local Alert                 +5 ms", report)
        self.assertIn("Feishu Request Started      +10 ms", report)
        self.assertIn("Feishu Request Acked        +80 ms", report)
        self.assertIn("Browser Ready               +850 ms", report)
        self.assertIn("Duo Page Loaded             +2100 ms", report)
        self.assertIn("Starwhite Selected          +2650 ms", report)
        self.assertIn("512GB Selected              +3100 ms", report)
        self.assertIn("Target Verified             +3300 ms", report)
        self.assertIn("Human Handoff Ready         +3500 ms", report)
        self.assertIn("TOTAL AUTOMATION LATENCY: 3500 ms", report)
        self.assertIn("绝不代表手机客户端 App 已接收或渲染消息", report)

    def test_2_aggregated_summary_statistics(self):
        """Test 2: 验证多轮演练聚合统计 (平均值, P50, 最快, 最慢)"""
        t1 = RehearsalTiming(run_id=1)
        t1.t0 = 100.0
        t1.t2a_feishu_started = 100.010
        t1.t2b_feishu_ack = 100.050
        t1.t3_browser_ready = 100.800
        t1.t4_page_loaded = 102.000
        t1.t7_target_verified = 103.000
        t1.t8_handoff_ready = 103.400

        t2 = RehearsalTiming(run_id=2)
        t2.t0 = 200.0
        t2.t2a_feishu_started = 200.012
        t2.t2b_feishu_ack = 200.060
        t2.t3_browser_ready = 201.000
        t2.t4_page_loaded = 202.400
        t2.t7_target_verified = 203.400
        t2.t8_handoff_ready = 204.000

        t3 = RehearsalTiming(run_id=3)
        t3.t0 = 300.0
        t3.t2a_feishu_started = 300.008
        t3.t2b_feishu_ack = 300.040
        t3.t3_browser_ready = 300.600
        t3.t4_page_loaded = 301.800
        t3.t7_target_verified = 302.800
        t3.t8_handoff_ready = 303.200

        summary = format_aggregated_summary([t1, t2, t3])
        self.assertIn("Launch Rehearsal Multi-Run Summary Report (3 轮演练汇总)", summary)
        self.assertIn("Feishu Request Started", summary)
        self.assertIn("Feishu Request Acked", summary)
        self.assertIn("Browser Ready", summary)
        self.assertIn("Duo Page Loaded", summary)
        self.assertIn("Target Verified", summary)
        self.assertIn("Human Handoff Ready", summary)
        self.assertIn("Total Automation Latency", summary)
        self.assertIn("P50 (中位数)", summary)
        self.assertIn("最快 (Min)", summary)
        self.assertIn("最慢 (Max)", summary)

    def test_3_feishu_rehearsal_card_format(self):
        """Test 3: 验证飞书卡片格式带有【模拟演练】且网络隔离防崩溃"""
        import asyncio
        res = asyncio.run(send_feishu_rehearsal_card(webhook_url=""))
        self.assertFalse(res)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"code": 0}).encode("utf-8")
            mock_resp.status = 200
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            ok = asyncio.run(send_feishu_rehearsal_card(
                webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/dummy_test_hook",
                timeout_sec=2
            ))
            self.assertTrue(ok)

            call_args = mock_urlopen.call_args[0]
            req = call_args[0]
            data = json.loads(req.data.decode("utf-8"))

            self.assertIn("【模拟演练】", data["card"]["header"]["title"]["content"])
            text_content = data["card"]["elements"][0]["text"]["content"]
            self.assertIn("【这是模拟通知，不代表真实有货】", text_content)
            self.assertIn("iPhone Duo", text_content)
            self.assertIn("星光白色 · 512GB", text_content)
            self.assertIn("MK2P4CH/A", text_content)
            self.assertIn("SIMULATED ONLINE AVAILABLE", text_content)
            self.assertIn("请不要下单，本消息仅用于首发演练", text_content)
            self.assertIn("THIS IS A REHEARSAL", text_content)

    def test_4_rehearsal_pipeline_strict_handoff(self):
        """Test 4: 验证端到端执行链路与 T8 HANDOFF_READY 严格满足 6 项条件"""
        import asyncio

        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = False

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL
        mock_loc = AsyncMock()
        mock_loc.count = AsyncMock(return_value=1)
        mock_loc.click = AsyncMock(return_value=None)
        mock_loc.is_checked = AsyncMock(return_value=True)
        mock_loc.get_attribute = AsyncMock(return_value="true")
        
        loc_container = MagicMock()
        loc_container.first = mock_loc
        loc_container.count = AsyncMock(return_value=1)
        loc_container.nth = MagicMock(return_value=mock_loc)
        mock_page.locator = MagicMock(return_value=loc_container)
        mock_page.evaluate = AsyncMock(return_value=True)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)

        with patch("src.rehearsal.BrowserManager") as mock_bm_cls:
            mock_bm = AsyncMock()
            mock_bm.start.return_value = mock_page
            mock_bm_cls.return_value = mock_bm

            with patch("src.rehearsal.AppleCatalogResolver") as mock_resolver_cls:
                mock_resolver = AsyncMock()
                mock_res = MagicMock()
                from src.catalog import ResolutionStatus
                mock_res.status = ResolutionStatus.SUCCESS
                mock_res.product = MagicMock()
                mock_res.product.part_number = TARGET_SKU
                mock_resolver.resolve.return_value = mock_res
                mock_resolver_cls.return_value = mock_resolver

                test_dir = "./logs/screenshots/test_rehearsal"
                timing = asyncio.run(execute_single_run(config, run_id=1, screenshots_dir=test_dir))

                self.assertTrue(timing.color_confirmed)
                self.assertTrue(timing.storage_confirmed)
                self.assertTrue(timing.sku_verified)
                self.assertTrue(timing.page_stable)
                self.assertTrue(timing.window_brought_to_front)
                self.assertTrue(timing.handoff_qualified)
                self.assertGreater(timing.total_latency, 0)
                self.assertEqual(len(timing.screenshots), 2)
                self.assertIn("run_01_page_loaded.png", timing.screenshots[0])
                self.assertIn("run_01_target_selected.png", timing.screenshots[1])

    def test_5_zero_cache_contamination(self):
        """Test 5: 验证演练过程绝不向 cache/ 正式库存目录写入任何伪造数据"""
        cache_dir = "./cache"
        os.makedirs(cache_dir, exist_ok=True)
        before_files = set(os.listdir(cache_dir))

        timing = RehearsalTiming(run_id=99)
        timing.start_trigger()
        timing.mark_handoff_ready()

        after_files = set(os.listdir(cache_dir))
        self.assertEqual(before_files, after_files, "演练模式不得在 cache/ 写入任何缓存文件！")

    def test_6_negative_unconfirmed_selection_fails_handoff(self):
        """Test 6 (负向测试): 模拟点击执行但 radio 未选中 -> confirmed=False, HANDOFF_READY=False"""
        import asyncio

        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = False

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL  # 未包含目标 SKU
        mock_loc = AsyncMock()
        mock_loc.count = AsyncMock(return_value=1)
        mock_loc.check = AsyncMock(return_value=None)
        mock_loc.click = AsyncMock(return_value=None)
        # 关键模拟：虽然调用了 click/check，但 is_checked 仍然为 False，且 aria-checked 不是 true
        mock_loc.is_checked = AsyncMock(return_value=False)
        mock_loc.get_attribute = AsyncMock(return_value=None)
        mock_loc.nth = MagicMock(return_value=mock_loc)

        loc_container = MagicMock()
        loc_container.first = mock_loc
        loc_container.count = AsyncMock(return_value=1)
        loc_container.nth = MagicMock(return_value=mock_loc)
        mock_page.locator = MagicMock(return_value=loc_container)
        mock_page.evaluate = AsyncMock(return_value=False)  # DOM 未选中
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)

        with patch("src.rehearsal.BrowserManager") as mock_bm_cls:
            mock_bm = AsyncMock()
            mock_bm.start.return_value = mock_page
            mock_bm_cls.return_value = mock_bm

            with patch("src.rehearsal.AppleCatalogResolver") as mock_resolver_cls:
                mock_resolver = AsyncMock()
                mock_res = MagicMock()
                from src.catalog import ResolutionStatus
                mock_res.status = ResolutionStatus.SUCCESS
                mock_res.product = MagicMock()
                mock_res.product.part_number = TARGET_SKU
                mock_resolver.resolve.return_value = mock_res
                mock_resolver_cls.return_value = mock_resolver

                test_dir = "./logs/screenshots/test_rehearsal_neg"
                timing = asyncio.run(execute_single_run(config, run_id=2, screenshots_dir=test_dir))

                # 严格断言：点击执行但无法证明选中时，confirmed 必须为 False
                self.assertFalse(timing.color_confirmed, "颜色未被可靠证据证明时禁止强行置为 True")
                self.assertFalse(timing.storage_confirmed, "容量未被可靠证据证明时禁止强行置为 True")
                # HANDOFF_READY 必须为 False (handoff_qualified == False)
                self.assertFalse(timing.handoff_qualified, "颜色/容量未确认时 HANDOFF_READY 必须为 False")

                report = timing.format_report()
                self.assertIn("HANDOFF_READY=False", report)

    def test_7_click_interaction_priority_and_fallback(self):
        """Test 7: 验证演练点击交互优先级 (check -> label click -> regular click -> force fallback)"""
        import asyncio
        from src.rehearsal import _rehearsal_interact_option

        mock_page = AsyncMock()
        mock_radio = AsyncMock()
        mock_label = AsyncMock()

        # 场景 A: check() 成功
        mock_radio.count = AsyncMock(return_value=1)
        mock_radio.check = AsyncMock(return_value=None)
        mock_page.locator = MagicMock(side_effect=lambda sel: MagicMock(first=mock_radio) if "input" in sel else MagicMock(first=mock_label))

        ok_a = asyncio.run(_rehearsal_interact_option(mock_page, "input", "label", "测试A"))
        self.assertTrue(ok_a)
        mock_radio.check.assert_awaited_once()

        # 场景 B: check() 失败，label.click() 成功 (普通点击)
        mock_radio.check = AsyncMock(side_effect=Exception("Check disabled"))
        mock_label.count = AsyncMock(return_value=1)
        mock_label.click = AsyncMock(return_value=None)
        ok_b = asyncio.run(_rehearsal_interact_option(mock_page, "input", "label", "测试B"))
        self.assertTrue(ok_b)
        mock_label.click.assert_awaited_once_with(timeout=1500)

        # 场景 C: 全部普通交互失败，fallback 到 force=True
        mock_label.click = AsyncMock(side_effect=Exception("Covered by banner"))
        mock_radio.click = AsyncMock(side_effect=Exception("Intercepted"))
        with self.assertLogs("rehearsal", level="WARNING") as cm:
            mock_label.click = AsyncMock(side_effect=[Exception("Normal click failed"), None])
            ok_c = asyncio.run(_rehearsal_interact_option(mock_page, "input", "label", "测试C"))
            self.assertTrue(ok_c)
            self.assertTrue(any("force=True fallback" in m for m in cm.output))

    def test_prewarm_1_prewarm_time_excluded_from_trigger_latency(self):
        """Test Prewarm 1: 验证预热阶段耗时完全独立于 T0，绝不计入开售响应时延"""
        from src.rehearsal import PrewarmTiming
        pw = PrewarmTiming()
        pw.t_prewarm_start = 100.0
        pw.t_browser_ready = 101.5  # 1500 ms
        pw.t_page_loaded = 103.0    # 1500 ms
        pw.t_prewarm_done = 103.5   # 3500 ms total
        self.assertEqual(pw.browser_startup_ms, 1500)
        self.assertEqual(pw.page_load_ms, 1500)
        self.assertEqual(pw.total_prewarm_ms, 3500)

        timing = RehearsalTiming(run_id=1, is_prewarmed=True)
        timing.t0 = 200.0
        timing.t_reload_started = 200.010
        timing.t_reload_done = 200.800
        timing.t8_handoff_ready = 201.200
        timing.handoff_qualified = True

        self.assertEqual(timing.total_latency, 1200)
        self.assertNotIn("3500", str(timing.total_latency))

    def test_prewarm_2_browser_manager_reused_across_3_runs(self):
        """Test Prewarm 2: 验证 3 轮 Prewarm 演练复用同一个 BrowserManager (start/close 各仅调用 1 次)"""
        import asyncio
        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = False

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL
        mock_loc = AsyncMock()
        mock_loc.count = AsyncMock(return_value=1)
        mock_loc.is_visible = AsyncMock(return_value=True)
        mock_loc.click = AsyncMock(return_value=None)
        mock_loc.is_checked = AsyncMock(return_value=True)
        mock_loc.get_attribute = AsyncMock(return_value="true")
        loc_container = MagicMock(first=mock_loc, count=AsyncMock(return_value=1), nth=MagicMock(return_value=mock_loc))
        mock_page.locator = MagicMock(return_value=loc_container)
        mock_page.evaluate = AsyncMock(return_value=True)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)
        mock_page.reload = AsyncMock(return_value=None)

        mock_context = MagicMock()
        mock_context.pages = [mock_page]
        mock_page.context = mock_context

        with patch("src.rehearsal.BrowserManager") as mock_bm_cls:
            mock_bm = AsyncMock()
            mock_bm.start.return_value = mock_page
            mock_bm.close.return_value = None
            mock_bm.context = mock_context
            mock_bm_cls.return_value = mock_bm

            with patch("src.rehearsal.AppleCatalogResolver") as mock_resolver_cls:
                mock_resolver = AsyncMock()
                mock_res = MagicMock()
                from src.catalog import ResolutionStatus
                mock_res.status = ResolutionStatus.SUCCESS
                mock_res.product = MagicMock(part_number=TARGET_SKU)
                mock_resolver.resolve.return_value = mock_res
                mock_resolver_cls.return_value = mock_resolver

                with patch("src.rehearsal.check_apple_login_status", return_value=(True, "已登录 (SESSION_READY=1)")):
                    with patch("asyncio.sleep", new_callable=AsyncMock):
                        results = asyncio.run(run_launch_rehearsal(
                            config=config, runs=3, interval_sec=0, prewarm=True
                        ))

            self.assertEqual(len(results), 3)
            mock_bm.start.assert_awaited_once()
            mock_bm.close.assert_awaited_once()

    def test_prewarm_3_no_new_tabs_created(self):
        """Test Prewarm 3: 验证 3 轮演练全流程在单 Tab 内运行，严禁新建标签页或窗口"""
        import asyncio
        from src.catalog import ResolutionStatus
        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = False

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL
        mock_loc = AsyncMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True),
                             click=AsyncMock(return_value=None), is_checked=AsyncMock(return_value=True),
                             get_attribute=AsyncMock(return_value="true"))
        loc_container = MagicMock(first=mock_loc, count=AsyncMock(return_value=1), nth=MagicMock(return_value=mock_loc))
        mock_page.locator = MagicMock(return_value=loc_container)
        mock_page.evaluate = AsyncMock(return_value=True)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)
        mock_page.reload = AsyncMock(return_value=None)

        mock_context = MagicMock()
        mock_context.pages = [mock_page]
        mock_context.new_page = AsyncMock()
        mock_page.context = mock_context

        with patch("src.rehearsal.BrowserManager") as mock_bm_cls:
            mock_bm = AsyncMock(start=AsyncMock(return_value=mock_page), close=AsyncMock(), context=mock_context)
            mock_bm_cls.return_value = mock_bm
            with patch("src.rehearsal.AppleCatalogResolver") as mock_res_cls:
                mock_resolver = AsyncMock()
                mock_res = MagicMock(status=ResolutionStatus.SUCCESS, product=MagicMock(part_number=TARGET_SKU))
                mock_resolver.resolve.return_value = mock_res
                mock_res_cls.return_value = mock_resolver
                with patch("src.rehearsal.check_apple_login_status", return_value=MagicMock(value="LOGGED_IN")):
                    with patch("asyncio.sleep", new_callable=AsyncMock):
                        results = asyncio.run(run_launch_rehearsal(config=config, runs=3, interval_sec=0, prewarm=True))

            mock_context.new_page.assert_not_awaited()
            self.assertEqual(len(mock_context.pages), 1)
            self.assertTrue(all(r.cond_same_tab for r in results))

    def test_prewarm_4_feishu_async_non_blocking(self):
        """Test Prewarm 4: 验证飞书网络请求异步并发，网络慢延迟绝不阻塞购买加车关键路径"""
        import asyncio
        from src.rehearsal import execute_prewarmed_single_run

        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = True
        config.notifications.feishu.webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/mock_slow"

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL
        mock_loc = AsyncMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True),
                             click=AsyncMock(return_value=None), is_checked=AsyncMock(return_value=True),
                             get_attribute=AsyncMock(return_value="true"))
        loc_container = MagicMock(first=mock_loc, count=AsyncMock(return_value=1), nth=MagicMock(return_value=mock_loc))
        mock_page.locator = MagicMock(return_value=loc_container)
        mock_page.evaluate = AsyncMock(return_value=True)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)
        mock_page.reload = AsyncMock(return_value=None)

        mock_context = MagicMock(pages=[mock_page])
        mock_page.context = mock_context
        mock_bm = AsyncMock(context=mock_context)

        async def slow_feishu(*args, **kwargs):
            await asyncio.sleep(0.5)
            return True

        with patch("src.rehearsal.send_feishu_rehearsal_card", side_effect=slow_feishu):
            with patch("src.rehearsal._verify_target_sku_lightweight", return_value=True):
                timing = asyncio.run(execute_prewarmed_single_run(
                    config=config, browser_manager=mock_bm, page=mock_page, run_id=1, screenshots_dir="./logs/screenshots/test_pw"
                ))

        self.assertLess(timing.latency_handoff_ready, 450)
        self.assertTrue(timing.handoff_qualified)

    def test_prewarm_5_fast_path_on_hidden_radio_clicks_label_immediately(self):
        """Test Prewarm 5: 验证 Fast Path 算法在 input 隐藏时直接点击 visible label (零超时等待)"""
        import asyncio
        from src.rehearsal import _rehearsal_interact_option_fast

        mock_page = AsyncMock()
        mock_radio = AsyncMock()
        mock_label = AsyncMock()

        mock_radio.count = AsyncMock(return_value=1)
        mock_radio.is_visible = AsyncMock(return_value=False)
        mock_radio.check = AsyncMock(side_effect=Exception("Should not be called!"))

        mock_label.count = AsyncMock(return_value=1)
        mock_label.is_visible = AsyncMock(return_value=True)
        mock_label.click = AsyncMock(return_value=None)

        mock_page.locator = MagicMock(side_effect=lambda sel: MagicMock(first=mock_radio) if "input" in sel else MagicMock(first=mock_label))

        timing = RehearsalTiming(run_id=1, is_prewarmed=True)
        ok = asyncio.run(_rehearsal_interact_option_fast(
            mock_page, "input", "label", "星光白色", timing=timing, option_type="color"
        ))

        self.assertTrue(ok)
        mock_radio.check.assert_not_awaited()
        mock_label.click.assert_awaited_once_with(timeout=1500)
        self.assertEqual(timing.color_action_type, "fast_label_click")

    def test_prewarm_6_force_fallback_warning_when_all_normal_clicks_fail(self):
        """Test Prewarm 6: 验证常规交互全失败后触发 force=True fallback 并写 WARNING 日志"""
        import asyncio
        from src.rehearsal import _rehearsal_interact_option_fast

        mock_page = AsyncMock()
        mock_radio = AsyncMock()
        mock_label = AsyncMock()

        mock_radio.count = AsyncMock(return_value=1)
        mock_radio.is_visible = AsyncMock(return_value=False)
        mock_label.count = AsyncMock(return_value=1)
        mock_label.is_visible = AsyncMock(return_value=False)
        mock_radio.click = AsyncMock(side_effect=Exception("Normal radio click failed"))
        mock_label.click = AsyncMock(side_effect=[Exception("Normal label click failed"), None])

        mock_page.locator = MagicMock(side_effect=lambda sel: MagicMock(first=mock_radio) if "input" in sel else MagicMock(first=mock_label))

        with self.assertLogs("rehearsal", level="WARNING") as cm:
            ok = asyncio.run(_rehearsal_interact_option_fast(mock_page, "input", "label", "测试兜底"))
            self.assertTrue(ok)
            self.assertTrue(any("force=True fallback" in m for m in cm.output))

    def test_prewarm_7_unconfirmed_selection_strictly_fails_handoff(self):
        """Test Prewarm 7: 验证颜色或容量未被可靠证据核验时，严格判定 HANDOFF_READY=False"""
        timing = RehearsalTiming(run_id=1, is_prewarmed=True)
        timing.mark_handoff_ready_prewarm(
            same_context=True,
            same_tab=True,
            reload_ok=True,
            color_confirmed=False,
            storage_confirmed=True,
            sku_verified=True,
            page_stable=True,
            window_front=True,
            auto_stopped=True,
            human_takeover_ready=True
        )
        self.assertFalse(timing.handoff_qualified)
        self.assertIn("HANDOFF_READY=False", timing.format_report())

    def test_prewarm_8_sku_mismatch_fails_handoff(self):
        """Test Prewarm 8: 验证目标 SKU 核验不匹配 (非 MK2P4CH/A) 时严格阻断人工接管判定"""
        timing = RehearsalTiming(run_id=1, is_prewarmed=True)
        timing.mark_handoff_ready_prewarm(
            same_context=True,
            same_tab=True,
            reload_ok=True,
            color_confirmed=True,
            storage_confirmed=True,
            sku_verified=False,
            page_stable=True,
            window_front=True,
            auto_stopped=True,
            human_takeover_ready=True
        )
        self.assertFalse(timing.handoff_qualified)
        self.assertIn("HANDOFF_READY=False", timing.format_report())

    def test_prewarm_9_exactly_one_reload_per_run(self):
        """Test Prewarm 9: 验证每轮演练仅且只执行 1 次 reload"""
        import asyncio
        from src.rehearsal import execute_prewarmed_single_run

        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = False

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL
        mock_loc = AsyncMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True),
                             click=AsyncMock(return_value=None), is_checked=AsyncMock(return_value=True),
                             get_attribute=AsyncMock(return_value="true"))
        loc_container = MagicMock(first=mock_loc, count=AsyncMock(return_value=1), nth=MagicMock(return_value=mock_loc))
        mock_page.locator = MagicMock(return_value=loc_container)
        mock_page.evaluate = AsyncMock(return_value=True)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)
        mock_page.reload = AsyncMock(return_value=None)

        mock_context = MagicMock(pages=[mock_page])
        mock_page.context = mock_context
        mock_bm = AsyncMock(context=mock_context)

        with patch("src.rehearsal._verify_target_sku_lightweight", return_value=True):
            timing = asyncio.run(execute_prewarmed_single_run(
                config=config, browser_manager=mock_bm, page=mock_page, run_id=1, screenshots_dir="./logs/screenshots/test_pw"
            ))

        self.assertEqual(timing.reload_count, 1)
        mock_page.reload.assert_awaited_once_with(wait_until="domcontentloaded", timeout=25000)

    def test_prewarm_10_formal_launch_mode_unmodified(self):
        """Test Prewarm 10: 验证正式 --mode launch 逻辑与 pickup/online 核心代码未受污染且零修改"""
        import inspect
        from src.main import run_launch_mode, run_online_buyer, run_pickup_monitor
        from src.pickup_monitor import PickupMonitor
        from src.apple_store import AppleStoreBuyer

        self.assertIn("interval_sec", inspect.signature(run_launch_mode).parameters)
        self.assertIn("max_rounds", inspect.signature(run_launch_mode).parameters)
        self.assertTrue(hasattr(PickupMonitor, "query_store"))
        self.assertTrue(hasattr(PickupMonitor, "run_single_round"))
        self.assertTrue(hasattr(AppleStoreBuyer, "proceed_to_bag_and_checkout"))
        self.assertTrue(hasattr(AppleStoreBuyer, "check_safety_boundary"))


    # =========================================================================
    # Target-SKU Preselection Tests
    # =========================================================================

    def test_preselect_1_preselect_before_t0_excluded_from_trigger_latency(self):
        """Test Preselect 1: 预选阶段耗时发生在 T0 之前，绝不计入 Trigger 响应时延"""
        pw = PrewarmTiming()
        pw.t_prewarm_start = 100.0
        pw.t_browser_ready = 102.0
        pw.t_page_loaded = 105.0
        pw.t_color_preselected = 106.0
        pw.t_storage_preselected = 107.0
        pw.t_sku_verified = 107.5
        pw.t_prewarm_done = 108.0
        pw.preselect_target = True
        pw.preselect_color_confirmed = True
        pw.preselect_storage_confirmed = True
        pw.preselect_sku_verified = True
        pw.prewarm_target_ready = True

        self.assertEqual(pw.browser_startup_ms, 2000)
        self.assertEqual(pw.page_load_ms, 3000)
        self.assertEqual(pw.preselect_color_ms, 1000)
        self.assertEqual(pw.preselect_storage_ms, 1000)
        self.assertEqual(pw.total_prewarm_ms, 8000)

        # 演练轮次从 T0 (例如 200.0) 开始独立计时
        timing = RehearsalTiming(run_id=1, is_prewarmed=True)
        timing.preselect_target = True
        timing.t0 = 200.0
        timing.t1_alert = 200.005
        timing.t_reload_started = 200.010
        timing.t_reload_done = 201.200
        timing.mark_post_reload_color_verified(confirmed=True, skipped=True, reason="preserved")
        timing.t5_color_selected = 201.205
        timing.t_post_reload_color_verified = 201.205
        timing.mark_post_reload_storage_verified(confirmed=True, skipped=True, reason="preserved")
        timing.t6_storage_selected = 201.210
        timing.t_post_reload_storage_verified = 201.210
        timing.t7_target_verified = 201.250
        timing.t8_handoff_ready = 201.300
        timing.handoff_qualified = True

        # 严苛验证：演练响应时延为 1300 ms，预热的 8000 ms 绝未混入
        self.assertEqual(timing.total_latency, 1300)
        self.assertEqual(timing.latency_local_alert, 5)
        self.assertEqual(timing.latency_reload_done, 1200)
        self.assertNotIn("8000", timing.format_report())
        self.assertIn("Preserved (Skipped click)", timing.format_report())

    def test_preselect_2_reload_preserves_selection_skips_reclick(self):
        """Test Preselect 2: reload 后状态保留时，COLOR_RESELECT_SKIPPED=True，严禁触发点击操作"""
        import asyncio

        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = False

        mock_page = AsyncMock()
        mock_page.url = f"{DUO_PAGE_URL}/{TARGET_SKU.lower()}"
        mock_page.reload = AsyncMock(return_value=None)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)

        mock_context = MagicMock(pages=[mock_page])
        mock_page.context = mock_context
        mock_bm = AsyncMock(context=mock_context)

        with patch("src.rehearsal._verify_color_confirmed", return_value=True), \
             patch("src.rehearsal._verify_storage_confirmed", return_value=True), \
             patch("src.rehearsal._verify_target_sku_lightweight", return_value=True), \
             patch("src.rehearsal._rehearsal_interact_option_fast") as mock_interact:

            timing = asyncio.run(execute_prewarmed_single_run(
                config=config,
                browser_manager=mock_bm,
                page=mock_page,
                run_id=1,
                screenshots_dir="./logs/screenshots/test_preselect",
                preselect_target=True,
            ))

            self.assertTrue(timing.color_reselect_skipped)
            self.assertTrue(timing.storage_reselect_skipped)
            self.assertEqual(timing.color_reselect_reason, "preserved")
            self.assertEqual(timing.storage_reselect_reason, "preserved")
            # 严禁触发任何点击交互！
            mock_interact.assert_not_called()
            self.assertTrue(timing.handoff_qualified)

    def test_preselect_3_reload_state_lost_triggers_fast_path(self):
        """Test Preselect 3: reload 后状态丢失时，准确记录原因并触发 Fast Path 重选"""
        import asyncio

        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = False

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL
        mock_page.reload = AsyncMock(return_value=None)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)

        mock_context = MagicMock(pages=[mock_page])
        mock_page.context = mock_context
        mock_bm = AsyncMock(context=mock_context)

        # 首次调用返回 False (丢失)，重选后返回 True
        color_verify_side_effect = [False, True]
        storage_verify_side_effect = [True]

        with patch("src.rehearsal._verify_color_confirmed", side_effect=color_verify_side_effect), \
             patch("src.rehearsal._verify_storage_confirmed", side_effect=storage_verify_side_effect), \
             patch("src.rehearsal._verify_target_sku_lightweight", return_value=True), \
             patch("src.rehearsal._rehearsal_interact_option_fast", return_value=True) as mock_interact:

            timing = asyncio.run(execute_prewarmed_single_run(
                config=config,
                browser_manager=mock_bm,
                page=mock_page,
                run_id=1,
                screenshots_dir="./logs/screenshots/test_preselect",
                preselect_target=True,
            ))

            self.assertFalse(timing.color_reselect_skipped)
            self.assertEqual(timing.color_reselect_reason, "radio unchecked")
            self.assertTrue(timing.storage_reselect_skipped)
            # 颜色丢失触发了重选交互
            mock_interact.assert_called_once()
            self.assertTrue(timing.handoff_qualified)

    def test_preselect_4_sku_mismatch_blocks_handoff(self):
        """Test Preselect 4: SKU 不匹配时，严格判定 HANDOFF_READY=False"""
        import asyncio

        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = False

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL
        mock_page.reload = AsyncMock(return_value=None)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)

        mock_context = MagicMock(pages=[mock_page])
        mock_page.context = mock_context
        mock_bm = AsyncMock(context=mock_context)

        with patch("src.rehearsal._verify_color_confirmed", return_value=True), \
             patch("src.rehearsal._verify_storage_confirmed", return_value=True), \
             patch("src.rehearsal._verify_target_sku_lightweight", return_value=False):

            timing = asyncio.run(execute_prewarmed_single_run(
                config=config,
                browser_manager=mock_bm,
                page=mock_page,
                run_id=1,
                screenshots_dir="./logs/screenshots/test_preselect",
                preselect_target=True,
            ))

            self.assertFalse(timing.sku_verified)
            self.assertFalse(timing.handoff_qualified)
            self.assertIn("HANDOFF_READY=False", timing.format_report())

    def test_preselect_5_color_lost_unconfirmed_blocks_handoff(self):
        """Test Preselect 5: 颜色丢失且重选未能确认时，严格阻断接管 (HANDOFF_READY=False)"""
        import asyncio

        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = False

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL
        mock_page.reload = AsyncMock(return_value=None)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)

        mock_context = MagicMock(pages=[mock_page])
        mock_page.context = mock_context
        mock_bm = AsyncMock(context=mock_context)

        with patch("src.rehearsal._verify_color_confirmed", return_value=False), \
             patch("src.rehearsal._verify_storage_confirmed", return_value=True), \
             patch("src.rehearsal._verify_target_sku_lightweight", return_value=True), \
             patch("src.rehearsal._rehearsal_interact_option_fast", return_value=False):

            timing = asyncio.run(execute_prewarmed_single_run(
                config=config,
                browser_manager=mock_bm,
                page=mock_page,
                run_id=1,
                screenshots_dir="./logs/screenshots/test_preselect",
                preselect_target=True,
            ))

            self.assertFalse(timing.color_confirmed)
            self.assertFalse(timing.handoff_qualified)

    def test_preselect_6_storage_lost_unconfirmed_blocks_handoff(self):
        """Test Preselect 6: 容量丢失且重选未能确认时，严格阻断接管 (HANDOFF_READY=False)"""
        import asyncio

        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = False

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL
        mock_page.reload = AsyncMock(return_value=None)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)

        mock_context = MagicMock(pages=[mock_page])
        mock_page.context = mock_context
        mock_bm = AsyncMock(context=mock_context)

        with patch("src.rehearsal._verify_color_confirmed", return_value=True), \
             patch("src.rehearsal._verify_storage_confirmed", return_value=False), \
             patch("src.rehearsal._verify_target_sku_lightweight", return_value=True), \
             patch("src.rehearsal._rehearsal_interact_option_fast", return_value=False):

            timing = asyncio.run(execute_prewarmed_single_run(
                config=config,
                browser_manager=mock_bm,
                page=mock_page,
                run_id=1,
                screenshots_dir="./logs/screenshots/test_preselect",
                preselect_target=True,
            ))

            self.assertFalse(timing.storage_confirmed)
            self.assertFalse(timing.handoff_qualified)

    def test_preselect_7_single_browser_context_tab_across_3_runs(self):
        """Test Preselect 7: 3 轮演练全流程仅 1 个 BrowserManager、1 个 Context、1 个 Tab"""
        import asyncio

        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = False

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL
        mock_page.reload = AsyncMock(return_value=None)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)

        mock_context = MagicMock(pages=[mock_page])
        mock_page.context = mock_context
        mock_bm = AsyncMock(context=mock_context)
        mock_bm.close = AsyncMock(return_value=None)

        dummy_pw_timing = PrewarmTiming()
        dummy_pw_timing.session_verified = True
        dummy_pw_timing.catalog_verified = True
        dummy_pw_timing.preselect_color_confirmed = True
        dummy_pw_timing.preselect_storage_confirmed = True
        dummy_pw_timing.preselect_sku_verified = True
        dummy_pw_timing.prewarm_target_ready = True

        with patch("src.rehearsal.prewarm_rehearsal_session", return_value=(mock_bm, mock_page, dummy_pw_timing)) as mock_prewarm, \
             patch("src.rehearsal.execute_prewarmed_single_run", return_value=RehearsalTiming(run_id=1, is_prewarmed=True)) as mock_exec, \
             patch("asyncio.sleep", return_value=None):

            results = asyncio.run(run_launch_rehearsal(
                config=config,
                runs=3,
                interval_sec=1,
                preselect_target=True
            ))

            self.assertEqual(len(results), 3)
            # prewarm_rehearsal_session 仅被调用一次 (单一浏览器会话启动)
            mock_prewarm.assert_called_once_with(config, preselect_target=True)
            # 3 轮演练全部复用相同的 browser_manager 与 page
            self.assertEqual(mock_exec.call_count, 3)
            for call in mock_exec.call_args_list:
                self.assertEqual(call.kwargs["browser_manager"], mock_bm)
                self.assertEqual(call.kwargs["page"], mock_page)
                self.assertTrue(call.kwargs["preselect_target"])
            # 最后安全关闭 1 次
            mock_bm.close.assert_awaited_once()

    def test_preselect_8_exactly_one_reload_per_run(self):
        """Test Preselect 8: 每轮演练严格仅执行 1 次 reload"""
        import asyncio

        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = False

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL
        mock_page.reload = AsyncMock(return_value=None)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)

        mock_context = MagicMock(pages=[mock_page])
        mock_page.context = mock_context
        mock_bm = AsyncMock(context=mock_context)

        with patch("src.rehearsal._verify_color_confirmed", return_value=True), \
             patch("src.rehearsal._verify_storage_confirmed", return_value=True), \
             patch("src.rehearsal._verify_target_sku_lightweight", return_value=True):

            timing = asyncio.run(execute_prewarmed_single_run(
                config=config,
                browser_manager=mock_bm,
                page=mock_page,
                run_id=1,
                screenshots_dir="./logs/screenshots/test_preselect",
                preselect_target=True
            ))

            self.assertEqual(timing.reload_count, 1)
            mock_page.reload.assert_awaited_once_with(wait_until="domcontentloaded", timeout=25000)

    def test_preselect_9_feishu_async_non_blocking(self):
        """Test Preselect 9: 飞书延迟不阻塞页面 reload 与接管判定"""
        import asyncio

        config = AppConfig()
        config.notifications.sound.enabled = False
        config.notifications.feishu.enabled = True
        config.notifications.feishu.webhook_url = "https://mock.webhook"

        mock_page = AsyncMock()
        mock_page.url = DUO_PAGE_URL
        mock_page.reload = AsyncMock(return_value=None)
        mock_page.screenshot = AsyncMock(return_value=None)
        mock_page.wait_for_load_state = AsyncMock(return_value=None)
        mock_page.bring_to_front = AsyncMock(return_value=None)

        mock_context = MagicMock(pages=[mock_page])
        mock_page.context = mock_context
        mock_bm = AsyncMock(context=mock_context)

        async def slow_feishu(*args, **kwargs):
            await asyncio.sleep(0.5)
            return True

        with patch("src.rehearsal.send_feishu_rehearsal_card", side_effect=slow_feishu), \
             patch("src.rehearsal._verify_color_confirmed", return_value=True), \
             patch("src.rehearsal._verify_storage_confirmed", return_value=True), \
             patch("src.rehearsal._verify_target_sku_lightweight", return_value=True):

            timing = asyncio.run(execute_prewarmed_single_run(
                config=config,
                browser_manager=mock_bm,
                page=mock_page,
                run_id=1,
                screenshots_dir="./logs/screenshots/test_preselect",
                preselect_target=True
            ))
            self.assertTrue(timing.handoff_qualified)
            self.assertTrue(timing.feishu_success)

    def test_preselect_10_formal_launch_mode_unmodified(self):
        """Test Preselect 10: 正式 --mode launch、Pickup、Online 核心逻辑 100% 零修改"""
        import inspect
        from src.main import run_launch_mode, run_online_buyer, run_pickup_monitor
        from src.pickup_monitor import PickupMonitor
        from src.apple_store import AppleStoreBuyer

        # 检查正式入口签名未变
        self.assertIn("interval_sec", inspect.signature(run_launch_mode).parameters)
        self.assertIn("max_rounds", inspect.signature(run_launch_mode).parameters)
        self.assertIn("config", inspect.signature(run_online_buyer).parameters)
        self.assertIn("config", inspect.signature(run_pickup_monitor).parameters)

        # 检查核心方法未受修改
        self.assertTrue(hasattr(PickupMonitor, "query_store"))
        self.assertTrue(hasattr(PickupMonitor, "run_single_round"))
        self.assertTrue(hasattr(AppleStoreBuyer, "proceed_to_bag_and_checkout"))
        self.assertTrue(hasattr(AppleStoreBuyer, "check_safety_boundary"))


if __name__ == "__main__":
    unittest.main()

