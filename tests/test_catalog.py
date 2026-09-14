"""Apple Catalog 与 SKU 自动解析单元测试 (V0.3)"""

import os
import json
import unittest
import tempfile
from unittest.mock import AsyncMock, MagicMock
from src.catalog import (
    AppleCatalogResolver,
    ResolutionStatus,
    normalize_color,
    normalize_storage,
    normalize_product_name,
)

# 真实抓取的 iPhone 17 Catalog 示例数据
MOCK_IPHONE17_CATALOG = {
    "products": [
        {
            "partNumber": "MG6X4CH/A",
            "dimensionColor": "white",
            "dimensionCapacity": "256gb",
            "familyType": "iphone17",
            "comingSoon": False,
            "fullPrice": "mg6x4ch_a"
        },
        {
            "partNumber": "MG734CH/A",
            "dimensionColor": "white",
            "dimensionCapacity": "512gb",
            "familyType": "iphone17",
            "comingSoon": False,
            "fullPrice": "mg734ch_a"
        },
        {
            "partNumber": "MG6W4CH/A",
            "dimensionColor": "black",
            "dimensionCapacity": "256gb",
            "familyType": "iphone17",
            "comingSoon": False,
            "fullPrice": "mg6w4ch_a"
        }
    ],
    "displayValues": {
        "dimensionColor": {
            "white": {"value": "白色"},
            "black": {"value": "黑色"}
        },
        "dimensionCapacity": {
            "256gb": {"value": "256GB"},
            "512gb": {"value": "512GB"}
        },
        "prices": {
            "mg734ch_a": {
                "currentPrice": {"amount": "RMB 8,799", "raw_amount": "8799.00"}
            }
        }
    }
}


class TestAppleCatalog(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.resolver = AppleCatalogResolver(cache_dir=self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_normalizers(self):
        self.assertEqual(normalize_color("白色"), "white")
        self.assertEqual(normalize_color("White"), "white")
        self.assertEqual(normalize_color("星光白色"), "starwhite")
        self.assertEqual(normalize_color("紫金色"), "紫金色")  # 未知颜色不盲猜

        self.assertEqual(normalize_storage("512GB"), "512gb")
        self.assertEqual(normalize_storage("512g"), "512gb")
        self.assertEqual(normalize_storage("1TB"), "1tb")

        name, slug = normalize_product_name("iPhone 17")
        self.assertEqual(name, "iPhone 17")
        self.assertEqual(slug, "iphone-17")

    async def test_1_resolve_iphone17_white_512gb_success(self):
        """Test 1: iPhone 17 白色 512GB 成功解析为 MG734CH/A"""
        page = MagicMock()
        self.resolver.fetch_product_catalog = AsyncMock(return_value=(MOCK_IPHONE17_CATALOG, False))

        res = await self.resolver.resolve(
            page=page,
            product="iPhone 17",
            color="白色",
            storage="512GB"
        )
        self.assertEqual(res.status, ResolutionStatus.SUCCESS)
        self.assertIsNotNone(res.product)
        self.assertEqual(res.product.part_number, "MG734CH/A")
        self.assertEqual(res.product.color, "白色")
        self.assertEqual(res.product.storage, "512GB")
        self.assertEqual(res.product.price, "RMB 8,799")
        self.assertFalse(res.is_stale)

    async def test_2_part_number_mismatch_fails(self):
        """Test 2: 手工填写错误 Part Number 触发 TARGET_SKU_MISMATCH 强校验阻断"""
        page = MagicMock()
        self.resolver.fetch_product_catalog = AsyncMock(return_value=(MOCK_IPHONE17_CATALOG, False))

        res = await self.resolver.resolve(
            page=page,
            product="iPhone 17",
            color="白色",
            storage="512GB",
            expected_part_number="WRONG_PART_CH/A"  # 故意填错
        )
        self.assertEqual(res.status, ResolutionStatus.TARGET_SKU_MISMATCH)
        self.assertIn("配置冲突", res.error_message)

    async def test_3_invalid_color_no_match(self):
        """Test 3: 无效颜色 '紫金色' 明确返回 NO_MATCH，绝不盲猜白色"""
        page = MagicMock()
        self.resolver.fetch_product_catalog = AsyncMock(return_value=(MOCK_IPHONE17_CATALOG, False))

        res = await self.resolver.resolve(
            page=page,
            product="iPhone 17",
            color="紫金色",
            storage="512GB"
        )
        self.assertEqual(res.status, ResolutionStatus.NO_MATCH)
        self.assertIn("不在 [iPhone 17] 的可选规格中", res.error_message)

    async def test_4_catalog_cache_fallback_stale(self):
        """Test 4: 网络拉取失败时自动回退本地缓存并标记 CATALOG_STALE"""
        # 预置本地缓存
        self.resolver._save_cache("iPhone 17", {
            "url": "https://www.apple.com.cn/shop/buy-iphone/iphone-17",
            "fetched_at": "2026-09-13T12:00:00",
            "productSelectionData": MOCK_IPHONE17_CATALOG
        })

        # 模拟实时页面访问抛出网络超时
        page = MagicMock()
        page.goto = AsyncMock(side_effect=Exception("Connection timeout"))

        res = await self.resolver.resolve(
            page=page,
            product="iPhone 17",
            color="白色",
            storage="512GB"
        )
        self.assertEqual(res.status, ResolutionStatus.CATALOG_STALE)
        self.assertTrue(res.is_stale)
        self.assertEqual(res.product.part_number, "MG734CH/A")
        self.assertTrue(res.product.is_stale)

    async def test_5_resolve_iphone_duo_starwhite_512gb(self):
        """Test 5 (Prompt Test 1): iPhone Duo 星光白色 512GB 精准解析为 MK2P4CH/A"""
        # 构造真实的 Duo Mock 数据
        mock_duo_catalog = {
            "products": [
                {
                    "partNumber": "MK2P4CH/A",
                    "dimensionColor": "starwhite",
                    "dimensionCapacity": "512gb",
                    "familyType": "iphoneduo",
                    "comingSoon": True,
                    "fullPrice": "mk2p4ch_a"
                },
                {
                    "partNumber": "MK2Q4CH/A",
                    "dimensionColor": "nightsky",
                    "dimensionCapacity": "512gb",
                    "familyType": "iphoneduo",
                    "comingSoon": True,
                    "fullPrice": "mk2q4ch_a"
                }
            ],
            "displayValues": {
                "dimensionColor": {
                    "starwhite": {"value": "星光白色"},
                    "nightsky": {"value": "夜空色"}
                },
                "dimensionCapacity": {
                    "512gb": {"value": "512GB"}
                },
                "prices": {
                    "mk2p4ch_a": {
                        "currentPrice": {"amount": "RMB 17,999"}
                    }
                }
            }
        }
        page = MagicMock()
        self.resolver.fetch_product_catalog = AsyncMock(return_value=(mock_duo_catalog, False))

        res = await self.resolver.resolve(
            page=page,
            product="iPhone Duo",
            color="星光白色",
            storage="512GB"
        )
        self.assertEqual(res.status, ResolutionStatus.SUCCESS)
        self.assertIsNotNone(res.product)
        self.assertEqual(res.product.part_number, "MK2P4CH/A")
        self.assertEqual(res.product.color, "星光白色")
        self.assertEqual(res.product.storage, "512GB")
        self.assertEqual(res.product.price, "RMB 17,999")
        self.assertTrue(res.product.coming_soon)
        self.assertEqual(res.product.readiness_state.value, "COMING_SOON")

    async def test_6_target_changed_detection(self):
        """测试锁定 SKU 发生官方变更时触发 TARGET_CHANGED 熔断告警"""
        mock_changed_catalog = {
            "products": [
                {
                    "partNumber": "NEW_DUO_SKU/A",  # 官方变更为新零件号
                    "dimensionColor": "starwhite",
                    "dimensionCapacity": "512gb",
                    "comingSoon": True,
                }
            ],
            "displayValues": {
                "dimensionColor": {"starwhite": {"value": "星光白色"}},
                "dimensionCapacity": {"512gb": {"value": "512GB"}},
                "prices": {}
            }
        }
        page = MagicMock()
        self.resolver.fetch_product_catalog = AsyncMock(return_value=(mock_changed_catalog, False))

        # 锁定预期为 MK2P4CH/A
        res = await self.resolver.resolve(
            page=page,
            product="iPhone Duo",
            color="星光白色",
            storage="512GB",
            expected_part_number="MK2P4CH/A"
        )
        self.assertEqual(res.status, ResolutionStatus.TARGET_CHANGED)
        self.assertIn("官方 SKU 发生重大变更", res.error_message)


if __name__ == "__main__":
    unittest.main()
