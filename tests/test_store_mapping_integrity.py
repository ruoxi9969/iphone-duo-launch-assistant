"""Unit tests for Phase 1F: Store Mapping and Config Integrity Audit

Mandatory test coverage:
1. R390 == 香港广场
2. R401 == 上海环贸 iapm
3. R581 == 五角场
4. R683 == 环球港
5. Pickup event 中：store_id 与 store_name 对应正确
6. Feishu Pickup Alert：store_id / store_name 对应正确
7. config.yaml 与 config.example.yaml 门店配置完全一致且符合权威映射
8. 不允许重复 store id
9. 不允许未知 store id 静默映射成其它店
10. LaunchCoordinator 收到 Pickup Event 后：显示并推送正确店名与 store_id
11. Pickup Cadence 采用方案 A (30s base + 0~10s jitter)，严格处于 30.0s ~ 40.0s (30~60s 安全窗口)
"""

import os
import tempfile
import asyncio
import unittest
import yaml
from unittest.mock import MagicMock

from src.config import (
    OFFICIAL_SHANGHAI_STORES,
    get_authoritative_store_name,
    load_config,
    AppConfig,
    PickupConfig,
    StoreConfig,
)
from src.main import (
    PickupMonitorTask,
    LaunchCoordinator,
    AvailabilityEvent,
)
from src.pickup_monitor import (
    InventoryStatus,
    StoreCheckResult,
    parse_fulfillment_response,
)
from src.notifier import NotificationEvent, NotificationPayload
from src.rate_guard import calculate_cadence_sleep


class TestStoreMappingIntegrity(unittest.TestCase):
    """Phase 1F: 门店映射权威性与配置完整性专项测试套件"""

    def test_1_r390_maps_to_hongkong_plaza(self):
        """1. 严格核验 R390 映射为 '香港广场'"""
        self.assertEqual(OFFICIAL_SHANGHAI_STORES["R390"], "香港广场")
        self.assertEqual(get_authoritative_store_name("R390"), "香港广场")

    def test_2_r401_maps_to_iapm(self):
        """2. 严格核验 R401 映射为 '上海环贸 iapm'"""
        self.assertEqual(OFFICIAL_SHANGHAI_STORES["R401"], "上海环贸 iapm")
        self.assertEqual(get_authoritative_store_name("R401"), "上海环贸 iapm")

    def test_3_r581_maps_to_wujiaochang(self):
        """3. 严格核验 R581 映射为 '五角场'"""
        self.assertEqual(OFFICIAL_SHANGHAI_STORES["R581"], "五角场")
        self.assertEqual(get_authoritative_store_name("R581"), "五角场")

    def test_4_r683_maps_to_global_harbor(self):
        """4. 严格核验 R683 映射为 '环球港'"""
        self.assertEqual(OFFICIAL_SHANGHAI_STORES["R683"], "环球港")
        self.assertEqual(get_authoritative_store_name("R683"), "环球港")

    def test_5_pickup_event_store_id_and_name_match(self):
        """5. Pickup event 中 store_id 与 store_name 必须严格对应，杜绝错配"""
        event_queue = asyncio.Queue()
        mock_pickup_monitor = MagicMock()
        mock_pickup_monitor.target_cfg.part_number = "MK2P4CH/A"
        mock_pickup_monitor.pickup_cfg.stores = [
            StoreConfig(name="香港广场", store_number="R390"),
            StoreConfig(name="上海环贸 iapm", store_number="R401"),
            StoreConfig(name="五角场", store_number="R581"),
            StoreConfig(name="环球港", store_number="R683"),
        ]

        async def mock_query(page, store_no, part_no):
            return StoreCheckResult(
                store_number=store_no,
                store_name=OFFICIAL_SHANGHAI_STORES.get(store_no, store_no),
                part_number=part_no,
                status=InventoryStatus.AVAILABLE,
                pickup_quote="今天可取货",
            )
        mock_pickup_monitor.query_store = mock_query

        task = PickupMonitorTask(
            page=MagicMock(),
            pickup_monitor=mock_pickup_monitor,
            event_queue=event_queue,
            page_lock=asyncio.Lock(),
            interval_sec=0.1,
            store_throttle_sec=0.005,
            _allow_test_override=True,
        )

        stop_event = asyncio.Event()

        async def run_one():
            t = asyncio.create_task(task.run(stop_event))
            await asyncio.sleep(0.08)
            stop_event.set()
            await t

        asyncio.run(run_one())

        events = []
        while not event_queue.empty():
            events.append(event_queue.get_nowait())

        self.assertEqual(len(events), 4)
        for ev in events:
            self.assertEqual(ev.source, "pickup")
            self.assertEqual(ev.state, "AVAILABLE")
            self.assertEqual(ev.store_name, OFFICIAL_SHANGHAI_STORES[ev.store_number])
            self.assertIn(ev.store_number, ["R390", "R401", "R581", "R683"])

    def test_6_feishu_pickup_alert_formatting(self):
        """6. Feishu Pickup Alert 中 store_id 与 store_name 必须正确呈现"""
        for store_no, store_name in OFFICIAL_SHANGHAI_STORES.items():
            payload = NotificationPayload(
                event=NotificationEvent.PICKUP_AVAILABLE,
                product="iPhone Duo",
                color="星光白色",
                storage="512GB",
                sku="MK2P4CH/A",
                store=f"{store_name} [{store_no}]",
                channel="Apple Store 自提",
                status_text="✅ 可取货",
                quote="今天可取货",
                url="https://www.apple.com.cn/shop/buy-iphone/iphone-duo",
                details=f"门店 {store_name} 出现可预约自提库存。",
            )
            card = payload.format_interactive_card()
            content = card["card"]["elements"][0]["text"]["content"]
            self.assertIn(f"**门店**：{store_name} [{store_no}]", content)
            self.assertIn(f"门店 {store_name} 出现可预约自提库存", content)

            plain = payload.format_plain_text()
            self.assertIn(f"门店：{store_name} [{store_no}]", plain)

    def test_7_config_files_stores_match_authoritative(self):
        """7. config.yaml 与 config.example.yaml 门店列表完全一致且符合权威映射"""
        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        cfg_path = os.path.join(root_dir, "config.yaml")
        cfg_example_path = os.path.join(root_dir, "config.example.yaml")

        cfg = load_config(cfg_path)
        cfg_ex = load_config(cfg_example_path)

        self.assertEqual(len(cfg.pickup.stores), 4)
        self.assertEqual(len(cfg_ex.pickup.stores), 4)

        for s, s_ex in zip(cfg.pickup.stores, cfg_ex.pickup.stores):
            self.assertEqual(s.store_number, s_ex.store_number)
            self.assertEqual(s.name, s_ex.name)
            self.assertEqual(s.name, OFFICIAL_SHANGHAI_STORES[s.store_number])

    def test_8_disallow_duplicate_store_id(self):
        """8. 不允许重复 store id，无论显式实例化还是加载 YAML 配置均抛出异常"""
        # 1) PickupConfig 直接实例化重复检测
        with self.assertRaises(ValueError) as ctx:
            PickupConfig(stores=[
                StoreConfig(name="香港广场", store_number="R390"),
                StoreConfig(name="香港广场第二入口", store_number="R390"),
            ])
        self.assertIn("发现重复的门店编号", str(ctx.exception))

        # 2) load_config 加载重复门店配置检测
        dup_data = {
            "pickup": {
                "stores": [
                    {"name": "香港广场", "store_number": "R390"},
                    {"name": "香港广场2", "store_number": "R390"},
                ]
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tf:
            yaml.dump(dup_data, tf)
            tf_path = tf.name

        try:
            with self.assertRaises(ValueError) as ctx2:
                load_config(tf_path)
            self.assertIn("重复的门店编号", str(ctx2.exception))
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    def test_9_disallow_unknown_store_id_silent_mapping(self):
        """9. 不允许未知 store id 静默映射成其它店"""
        # get_authoritative_store_name 未知 id 必须返回 None
        self.assertIsNone(get_authoritative_store_name("R999"))
        self.assertIsNone(get_authoritative_store_name("R000"))

        # Apple fulfillment 响应解析未知门店不能静默变成 4 店中任意一家
        res = parse_fulfillment_response(
            {"body": {"stores": [{"storeNumber": "R999", "storeName": "未知测试店", "partsAvailability": {}}]}},
            "R999",
            "MK2P4CH/A",
        )
        self.assertNotIn(res.store_name, list(OFFICIAL_SHANGHAI_STORES.values()))
        self.assertEqual(res.store_name, "未知测试店")

        # PickupConfig 遇到自定义门店保留其原名
        custom_cfg = PickupConfig(stores=[StoreConfig(name="特约演示店", store_number="R999")])
        self.assertEqual(custom_cfg.stores[0].name, "特约演示店")
        self.assertEqual(custom_cfg.stores[0].store_number, "R999")
        self.assertNotIn(custom_cfg.stores[0].name, list(OFFICIAL_SHANGHAI_STORES.values()))

    def test_10_launch_coordinator_displays_correct_store_name(self):
        """10. LaunchCoordinator 收到 Pickup Event 后推送并显示正确店名"""
        mock_notifier = MagicMock()
        mock_notifier.notify_background = MagicMock()

        coordinator = LaunchCoordinator(
            page=MagicMock(),
            config=AppConfig(),
            buyer=MagicMock(),
            resolver=MagicMock(),
            notifier=mock_notifier,
            pickup_monitor=MagicMock(),
            expected_part="MK2P4CH/A",
        )

        for store_no, store_name in OFFICIAL_SHANGHAI_STORES.items():
            ev = AvailabilityEvent(
                source="pickup",
                state="AVAILABLE",
                part_number="MK2P4CH/A",
                store_number=store_no,
                store_name=store_name,
                pickup_quote="今天可取货",
                details=f"门店 {store_name} 出现可预约自提库存",
            )
            asyncio.run(coordinator.handle_pickup_event(ev))

            mock_notifier.notify_background.assert_called()
            last_call_kwargs = mock_notifier.notify_background.call_args.kwargs
            self.assertEqual(last_call_kwargs["store"], f"{store_name} [{store_no}]")
            self.assertIn(store_name, last_call_kwargs["details"])

        self.assertEqual(len(coordinator.pickup_alerts_received), 4)

    def test_11_pickup_cadence_option_a_range(self):
        """11. Pickup Cadence 采用方案 A (30s base, 0~10s jitter)，实际轮次间隔严格在 30.0s~40.0s"""
        for _ in range(50):
            sleep_sec = calculate_cadence_sleep(
                base_interval=30.0,
                jitter_range=(0.0, 10.0),
                min_interval=20.0,
                backoff_remaining=0.0,
            )
            self.assertGreaterEqual(sleep_sec, 30.0)
            self.assertLessEqual(sleep_sec, 40.0)


if __name__ == "__main__":
    unittest.main()
