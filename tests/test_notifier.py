"""通知模块单元测试 (飞书 Webhook、卡片规范、通知去重与降级容错 - V0.4)"""

import unittest
import asyncio
from src.config import FeishuConfig, SoundConfig, NotificationConfig, mask_feishu_webhook
from src.notifier import (
    NotificationEvent,
    NotificationPayload,
    FeishuNotifier,
    LocalSoundNotifier,
    NotificationDeduplicator,
    Notifier,
)

class TestNotifier(unittest.TestCase):

    def test_feishu_masking(self):
        """测试飞书 Webhook 凭证严格脱敏"""
        raw_url = "https://open.feishu.cn/open-apis/bot/v2/hook/12345678-abcd-ef01-2345-6789abcdef01"
        masked = mask_feishu_webhook(raw_url)
        self.assertNotIn("6789abcdef01", masked)
        self.assertIn("https://open.feishu.cn/.../", masked)
        self.assertTrue(masked.endswith("01") or "****" in masked)
        self.assertEqual(mask_feishu_webhook(""), "(未配置)")

    def test_payload_card_and_apple_button_compliance(self):
        """测试飞书交互式卡片格式与按钮必须指向 Apple 官网"""
        payload = NotificationPayload(
            event=NotificationEvent.PICKUP_AVAILABLE,
            product="iPhone Duo",
            color="星光白色",
            storage="512GB",
            sku="MK2P4CH/A",
            store="上海五角场",
            status_text="✅ 可取货",
            quote="今天可取",
            url="https://www.apple.com.cn/shop/buy-iphone/iphone-duo",
        )
        card_data = payload.format_interactive_card()
        self.assertEqual(card_data["msg_type"], "interactive")
        self.assertEqual(card_data["card"]["header"]["title"]["content"], "🍎 iPhone Duo 上海门店有货")
        self.assertEqual(card_data["card"]["header"]["template"], "green")
        
        # 按钮 URL 校验
        actions = card_data["card"]["elements"][1]["actions"]
        btn_url = actions[0]["url"]
        self.assertTrue(btn_url.startswith("https://www.apple.com.cn"))

        # 尝试恶意第三方 URL 注入测试 (必须被强制收拢回官方 Apple 站点)
        fake_payload = NotificationPayload(
            event=NotificationEvent.ONLINE_AVAILABLE,
            product="iPhone Duo",
            url="https://malicious-buy-site.com/iphone",
        )
        fake_card = fake_payload.format_interactive_card()
        fake_btn_url = fake_card["card"]["elements"][1]["actions"][0]["url"]
        self.assertTrue(fake_btn_url.startswith("https://www.apple.com.cn"))

    def test_feishu_deduplication(self):
        """
        测试通知去重状态机 (Test 9):
        1. 连续 3 次相同 AVAILABLE 状态，仅首次允许发送
        2. 变迁为 UNAVAILABLE
        3. 再次变迁为 AVAILABLE 时重新允许发送
        """
        dedup = NotificationDeduplicator()
        p1 = NotificationPayload(
            event=NotificationEvent.PICKUP_AVAILABLE,
            sku="MK2P4CH/A",
            store="上海五角场",
            status_text="✅ 可取货",
        )

        # 第一次：应允许发送
        self.assertTrue(dedup.should_send(p1))
        dedup.record_sent(p1)

        # 第二次、第三次：完全相同状态，应被去重拦截
        self.assertFalse(dedup.should_send(p1))
        self.assertFalse(dedup.should_send(p1))

        # 变为无货 UNAVAILABLE
        p_unavail = NotificationPayload(
            event=NotificationEvent.PICKUP_AVAILABLE,
            sku="MK2P4CH/A",
            store="上海五角场",
            status_text="❌ 暂不可取",
        )
        self.assertTrue(dedup.should_send(p_unavail))
        dedup.record_sent(p_unavail)

        # 再次变回 AVAILABLE：应允许重新发送提醒！
        self.assertTrue(dedup.should_send(p1))

    def test_feishu_disabled_or_empty_url(self):
        """测试未配置 Webhook 时正确处理，绝不虚假上报成功"""
        cfg = FeishuConfig(enabled=False, webhook_url="")
        notifier = FeishuNotifier(cfg)
        payload = NotificationPayload(event=NotificationEvent.TARGET_AVAILABLE)
        result = asyncio.run(notifier.send(payload))
        self.assertFalse(result)

        cfg.enabled = True
        cfg.webhook_url = ""
        notifier = FeishuNotifier(cfg)
        result = asyncio.run(notifier.send(payload))
        self.assertFalse(result)

    def test_feishu_fault_tolerance_and_degraded(self):
        """
        测试飞书断网降级容错 (Test 10):
        Webhook 超时或网络异常时，FeishuNotifier 报错但不崩溃，
        系统标记降级模式 (FEISHU_DEGRADED)，本地声音依然安全触发。
        """
        # 配置无效端口模拟网络断开
        cfg = NotificationConfig(
            feishu=FeishuConfig(enabled=True, webhook_url="http://127.0.0.1:9999/invalid_hook", timeout_seconds=1),
            sound=SoundConfig(enabled=True)
        )
        notifier = Notifier(cfg)
        
        # 触发通知，验证主程序绝不抛出异常崩溃
        for _ in range(3):
            result = asyncio.run(notifier.notify(
                event=NotificationEvent.ONLINE_AVAILABLE,
                product="iPhone Duo",
                sku="MK2P4CH/A",
                status_text="测试",
                force=True
            ))
            self.assertFalse(result)

        # 校验连续失败后已自动标记为降级状态
        self.assertTrue(notifier.feishu.is_degraded)

if __name__ == "__main__":
    unittest.main()
