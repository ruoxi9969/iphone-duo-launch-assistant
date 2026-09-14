"""
Unit tests for V0.4.2 Feishu End-to-End Verification Persistence.

Tests required by prompt Section 七:
- Test 1: test-feishu 用户确认 y -> 写入 verified=true
- Test 2: 新进程/重新加载配置 -> 相同 Webhook fingerprint -> END_TO_END_OK 能够恢复
- Test 3: 更换 Webhook -> fingerprint 不匹配 -> NOT VERIFIED (FEISHU_WEBHOOK_CHANGED)
- Test 4: 状态文件不存在 -> NOT VERIFIED
- Test 5: verified=false -> NOT VERIFIED
- Test 6: 状态过期 (> 30 天) -> VERIFICATION_STALE
- Test 7: 状态文件中不得出现完整 Webhook URL 或 key
"""

import os
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src.config import FeishuConfig
from src.notifier import (
    compute_webhook_fingerprint,
    mask_fingerprint,
    save_feishu_verification_state,
    verify_feishu_status,
    is_feishu_end_to_end_verified,
)


class TestFeishuVerificationPersistence(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "feishu_verification.json")
        self.webhook_a = "https://open.feishu.cn/open-apis/bot/v2/hook/aaaa1111-bbbb-cccc-dddd-eeeeffff0001"
        self.webhook_b = "https://open.feishu.cn/open-apis/bot/v2/hook/bbbb2222-cccc-dddd-eeee-ffff00001112"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_1_user_confirm_y_writes_verified_true(self):
        """
        Test 1: test-feishu 用户确认 y -> 写入 verified=true 并记录正确的 SHA-256 fingerprint
        """
        save_feishu_verification_state(
            self.webhook_a,
            verified=True,
            last_test_status="FEISHU_END_TO_END_OK",
            state_file=self.state_file
        )
        self.assertTrue(os.path.exists(self.state_file))

        with open(self.state_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertTrue(data["verified"])
        self.assertEqual(data["last_test_status"], "FEISHU_END_TO_END_OK")
        expected_fp = compute_webhook_fingerprint(self.webhook_a)
        self.assertEqual(data["webhook_fingerprint"], expected_fp)
        self.assertIn("verified_at", data)

    def test_2_reload_same_webhook_restores_end_to_end_ok(self):
        """
        Test 2: 新进程/重新加载配置 -> 相同 Webhook fingerprint -> END_TO_END_OK 能够恢复
        """
        # 模拟前序进程已通过验证落盘
        save_feishu_verification_state(
            self.webhook_a,
            verified=True,
            last_test_status="FEISHU_END_TO_END_OK",
            state_file=self.state_file
        )

        # 模拟新进程启动，仅持有 Webhook URL 配置与本地状态文件
        is_valid, status, desc = verify_feishu_status(self.webhook_a, state_file=self.state_file)
        self.assertTrue(is_valid)
        self.assertEqual(status, "FEISHU_END_TO_END_OK")

        # 验证辅助函数
        cfg = FeishuConfig(enabled=True, webhook_url=self.webhook_a)
        self.assertTrue(is_feishu_end_to_end_verified(cfg, state_file=self.state_file))

    def test_3_changed_webhook_fails_and_reports_webhook_changed(self):
        """
        Test 3: 更换 Webhook -> fingerprint 不匹配 -> NOT VERIFIED (FEISHU_WEBHOOK_CHANGED)
        绝不能错误继承旧 Webhook 的已验证状态
        """
        # 验证 Webhook A
        save_feishu_verification_state(
            self.webhook_a,
            verified=True,
            last_test_status="FEISHU_END_TO_END_OK",
            state_file=self.state_file
        )

        # 用户配置更换为 Webhook B
        is_valid, status, desc = verify_feishu_status(self.webhook_b, state_file=self.state_file)
        self.assertFalse(is_valid)
        self.assertEqual(status, "FEISHU_WEBHOOK_CHANGED")

        cfg = FeishuConfig(enabled=True, webhook_url=self.webhook_b)
        self.assertFalse(is_feishu_end_to_end_verified(cfg, state_file=self.state_file))

    def test_4_missing_state_file_not_verified(self):
        """
        Test 4: 状态文件不存在 -> NOT VERIFIED
        """
        non_existent_file = os.path.join(self.temp_dir.name, "not_found.json")
        is_valid, status, desc = verify_feishu_status(self.webhook_a, state_file=non_existent_file)
        self.assertFalse(is_valid)
        self.assertEqual(status, "FEISHU_NOT_VERIFIED_END_TO_END")

    def test_5_verified_false_not_verified(self):
        """
        Test 5: 用户输入 n (verified=false) -> NOT VERIFIED
        """
        save_feishu_verification_state(
            self.webhook_a,
            verified=False,
            last_test_status="FEISHU_WEBHOOK_REQUEST_OK",
            state_file=self.state_file
        )
        is_valid, status, desc = verify_feishu_status(self.webhook_a, state_file=self.state_file)
        self.assertFalse(is_valid)
        self.assertEqual(status, "FEISHU_NOT_VERIFIED_END_TO_END")

    def test_6_stale_verification_more_than_30_days(self):
        """
        Test 6: 状态过期 (> 30 天) -> VERIFICATION_STALE
        """
        old_time = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        save_feishu_verification_state(
            self.webhook_a,
            verified=True,
            last_test_status="FEISHU_END_TO_END_OK",
            state_file=self.state_file,
            verified_at=old_time
        )
        is_valid, status, desc = verify_feishu_status(self.webhook_a, state_file=self.state_file, max_age_days=30)
        self.assertFalse(is_valid)
        self.assertEqual(status, "FEISHU_VERIFICATION_STALE")

    def test_7_state_file_never_contains_full_webhook_or_key(self):
        """
        Test 7: 状态文件中不得出现完整 Webhook URL、Token 或敏感 Key
        """
        secret_key = "aaaa1111-bbbb-cccc-dddd-eeeeffff0001"
        save_feishu_verification_state(
            self.webhook_a,
            verified=True,
            last_test_status="FEISHU_END_TO_END_OK",
            state_file=self.state_file
        )

        with open(self.state_file, "r", encoding="utf-8") as f:
            raw_content = f.read()

        # 绝对严禁出现完整 URL 和敏感 Key
        self.assertNotIn(self.webhook_a, raw_content)
        self.assertNotIn(secret_key, raw_content)
        self.assertNotIn("https://open.feishu.cn", raw_content)

        # 确保只包含安全的结构化元数据
        data = json.loads(raw_content)
        self.assertCountEqual(list(data.keys()), ["verified", "verified_at", "webhook_fingerprint", "last_test_status"])


if __name__ == "__main__":
    unittest.main()
