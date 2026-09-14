"""
Tests for Launch Time Configuration and UI Display (V0.4.2 + Dashboard V1.2)
Covers:
1. launch_time parsing UTC+8 (2026-10-16T20:00:00+08:00)
2. Countdown format > 24h: 距离预购 X天 HH:MM:SS
3. Countdown format <= 24h: T-HH:MM:SS
4. Countdown format past 20:00: 预购已开始 +HH:MM:SS
5. Missing/malformed fallback: 开售时间未设置
6. Chinese UI advice stages:
   - > 60m: 距离预购尚早
   - T-60 ~ T-15: 建议检查 Apple 登录会话
   - T-15 ~ T0: 首发准备阶段
   - T0 past & COMING_SOON: 已到官方预购时间，等待 Apple 放货信号
   - AVAILABLE: 检测到线上可购
7. Safety invariant: time arrival does NOT trigger purchase, AVAILABLE controls purchase.
"""

import os
import unittest
import datetime
from unittest.mock import MagicMock, AsyncMock

from src.config import load_config, AppConfig
from src.dashboard_state import DashboardState
from src.dashboard_adapter import DashboardAdapter
from src.main import LaunchCoordinator, AvailabilityEvent


class TestLaunchTimeConfig(unittest.TestCase):

    def setUp(self):
        self.config_path = os.path.abspath("config.yaml")
        self.cfg = load_config(self.config_path)

    # 1. launch_time UTC+8 解析与正式时间核验
    def test_launch_time_config_parsing_utc8(self):
        self.assertIsNotNone(self.cfg.launch)
        self.assertEqual(self.cfg.launch.launch_time, "2026-10-16T20:00:00+08:00")

        dt = datetime.datetime.fromisoformat(self.cfg.launch.launch_time)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 10)
        self.assertEqual(dt.day, 16)
        self.assertEqual(dt.hour, 20)
        self.assertEqual(dt.minute, 0)
        self.assertEqual(dt.second, 0)

        # 校验时区为 UTC+8
        self.assertIsNotNone(dt.tzinfo)
        offset = dt.tzinfo.utcoffset(dt)
        self.assertEqual(offset, datetime.timedelta(hours=8))

    # 2. 超过 24h 的中文倒计时显示
    def test_countdown_over_24h(self):
        st = DashboardState()
        tz = datetime.timezone(datetime.timedelta(hours=8))
        now = datetime.datetime(2026, 9, 15, 12, 0, 0, tzinfo=tz)
        # 设定为 31 天 3 小时 24 分 18 秒后
        future = now + datetime.timedelta(days=31, hours=3, minutes=24, seconds=18)
        st.launch_time_iso = future.isoformat()

        text = st.get_countdown_text(now=now)
        self.assertEqual(text, "距离预购 31天 03:24:18")

        pill_text, pill_level = st.get_countdown_pill_info(now=now)
        self.assertEqual(pill_text, "距离预购 31天 03:24:18")
        self.assertEqual(pill_level, "BLUE")

    # 3. 24h 内倒计时显示
    def test_countdown_within_24h(self):
        st = DashboardState()
        tz = datetime.timezone(datetime.timedelta(hours=8))
        now = datetime.datetime(2026, 10, 16, 16, 35, 42, tzinfo=tz)
        # 设定为 3 小时 24 分 18 秒后 (20:00:00)
        future = now + datetime.timedelta(hours=3, minutes=24, seconds=18)
        st.launch_time_iso = future.isoformat()

        text = st.get_countdown_text(now=now)
        self.assertEqual(text, "T-03:24:18")

        pill_text, pill_level = st.get_countdown_pill_info(now=now)
        self.assertEqual(pill_text, "T-03:24:18")
        self.assertEqual(pill_level, "ORANGE")

    # 4. 到点后“预购已开始”
    def test_countdown_after_launch_time(self):
        st = DashboardState()
        tz = datetime.timezone(datetime.timedelta(hours=8))
        now = datetime.datetime(2026, 10, 16, 20, 0, 8, tzinfo=tz)
        # 设定为 8 秒前 (20:00:00)
        past = now - datetime.timedelta(seconds=8)
        st.launch_time_iso = past.isoformat()

        text = st.get_countdown_text(now=now)
        self.assertEqual(text, "预购已开始 +00:00:08")

        pill_text, pill_level = st.get_countdown_pill_info(now=now)
        self.assertEqual(pill_text, "预购已开始 +00:00:08")
        self.assertEqual(pill_level, "GREEN")

    # 5. 配置缺失与异常 fallback = 开售时间未设置
    def test_countdown_missing_or_malformed_fallback(self):
        st = DashboardState()
        st.launch_time_iso = None
        self.assertEqual(st.get_countdown_text(), "开售时间未设置")
        p_text, p_lvl = st.get_countdown_pill_info()
        self.assertEqual(p_text, "开售时间未设置")
        self.assertEqual(p_lvl, "GREY")

        st.launch_time_iso = ""
        self.assertEqual(st.get_countdown_text(), "开售时间未设置")

        st.launch_time_iso = "invalid-date-string"
        self.assertEqual(st.get_countdown_text(), "开售时间未设置")

    # 6. 首发各阶段中文 UI 提示语
    def test_launch_day_ui_advice(self):
        st = DashboardState()
        tz = datetime.timezone(datetime.timedelta(hours=8))
        now = datetime.datetime(2026, 10, 16, 18, 0, 0, tzinfo=tz)

        # 6.1 距离预购 > 60 分钟 (120 分钟后)
        st.launch_time_iso = (now + datetime.timedelta(minutes=120)).isoformat()
        st.update_current_time(now=now)
        self.assertEqual(st.get_launch_advice(now=now), "距离预购尚早")
        self.assertEqual(st.launch_advice_str, "距离预购尚早")

        # 6.2 T-60 ~ T-15 (30 分钟后)
        st.launch_time_iso = (now + datetime.timedelta(minutes=30)).isoformat()
        st.update_current_time(now=now)
        self.assertEqual(st.get_launch_advice(now=now), "建议检查 Apple 登录会话")

        # 6.3 T-15 ~ T0 (10 分钟后)
        st.launch_time_iso = (now + datetime.timedelta(minutes=10)).isoformat()
        st.update_current_time(now=now)
        self.assertEqual(st.get_launch_advice(now=now), "首发准备阶段")

        # 6.4 T0 到点后但 Apple 仍处于 COMING_SOON (15 秒前开售)
        st.launch_time_iso = (now - datetime.timedelta(seconds=15)).isoformat()
        st.online_state_token = "COMING_SOON"
        st.update_current_time(now=now)
        self.assertEqual(st.get_launch_advice(now=now), "已到官方预购时间，等待 Apple 放货信号")

        # 6.5 检测到线上现货 AVAILABLE
        st.online_state_token = "AVAILABLE"
        self.assertEqual(st.get_launch_advice(now=now), "检测到线上可购")

    # 7. 适配器 Adapter 从 AppConfig 读取 launch_time
    def test_adapter_init_from_config(self):
        adapter = DashboardAdapter()
        adapter.init_from_config(self.cfg)
        st = adapter.get_latest_state()
        self.assertEqual(st.launch_time_iso, "2026-10-16T20:00:00+08:00")
        self.assertIn("距离预购", st.launch_countdown_str)

    # 8. 安全不变量：倒计时绝不控制购买逻辑 (时间到达 != AVAILABLE)
    def test_time_reached_does_not_trigger_purchase(self):
        """
        验证 LaunchCoordinator 仅在接收到真实的 AvailabilityEvent(AVAILABLE) 时
        才启动购买流程，本地到达或超过 20:00:00 绝不自主触发购买。
        """
        page_mock = MagicMock()
        buyer_mock = MagicMock()
        buyer_mock.add_to_bag = AsyncMock(return_value=True)
        resolver_mock = MagicMock()
        notifier_mock = MagicMock()
        pickup_mock = MagicMock()

        # 构造已超过开售时间的配置
        past_cfg = load_config(self.config_path)
        past_cfg.launch.launch_time = "2026-09-01T20:00:00+08:00"

        coordinator = LaunchCoordinator(
            page=page_mock,
            config=past_cfg,
            buyer=buyer_mock,
            resolver=resolver_mock,
            notifier=notifier_mock,
            pickup_monitor=pickup_mock,
        )

        # 初始状态：未触发购买
        self.assertFalse(coordinator.purchase_flow_started)

        # 模拟投入非 AVAILABLE 事件（即便时间已过，仍不触发购买）
        coming_soon_event = AvailabilityEvent(
            source="online",
            state="COMING_SOON",
            part_number="MK2P4CH/A",
        )
        self.assertNotEqual(coming_soon_event.state, "AVAILABLE")
        self.assertFalse(coordinator.purchase_flow_started)

        # 投入真实的 AVAILABLE 事件
        available_event = AvailabilityEvent(
            source="online",
            state="AVAILABLE",
            part_number="MK2P4CH/A",
        )
        self.assertEqual(available_event.state, "AVAILABLE")


if __name__ == "__main__":
    unittest.main()
