"""Unit tests for configuration loading and validation (V0.4)"""

import os
import unittest
from src.config import load_config, AppConfig

class TestConfig(unittest.TestCase):
    def test_load_example_config(self):
        config_path = os.path.join(os.path.dirname(__file__), "..", "config.example.yaml")
        cfg = load_config(config_path)
        self.assertIsInstance(cfg, AppConfig)
        self.assertTrue(cfg.product_url.startswith("https://www.apple.com.cn"))
        self.assertEqual(cfg.purchase_mode, "delivery")
        self.assertEqual(cfg.target.product, "iPhone Duo")
        self.assertEqual(cfg.target.color, "星光白色")
        self.assertEqual(cfg.target.storage, "512GB")
        self.assertTrue(cfg.safety.stop_at_login)
        self.assertTrue(cfg.safety.stop_at_payment)
        self.assertFalse(cfg.browser.headless)
        self.assertTrue(cfg.notifications.feishu.enabled)

if __name__ == "__main__":
    unittest.main()
