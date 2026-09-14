"""Unit tests for Formal Launch Hardening - Phase 1A

Validates:
1. verify_starwhite_selected (Production selected-state verification for Starlight White)
2. verify_512gb_selected (Production selected-state verification for 512GB)
3. fast_select_option (Production-safe Fast Path specification selection)
4. verify_target_sku_consistency (Production-safe MK2P4CH/A target SKU consistency)
5. AppleStoreBuyer integration methods
6. Strict isolation from src.rehearsal & safety boundaries intact
"""

import ast
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.apple_store import (
    verify_starwhite_selected,
    verify_512gb_selected,
    fast_select_option,
    verify_target_sku_consistency,
    AppleStoreBuyer,
    is_payment_context,
    check_safety_boundary,
)
from src.catalog import ResolutionStatus, ResolutionResult, ResolvedProduct
from src.config import AppConfig


class TestPhase1AStarwhiteSelected(unittest.TestCase):
    """目标 1: 将星光白色 selected-state 验证抽象为 production-safe helper"""

    def test_starwhite_input_checked_returns_true(self):
        """证据 1: radio input.is_checked() == True"""
        mock_page = MagicMock()
        mock_radio = MagicMock()
        mock_radio.count = AsyncMock(return_value=1)
        mock_radio.is_checked = AsyncMock(return_value=True)
        mock_radio.first = mock_radio

        mock_page.locator = MagicMock(return_value=mock_radio)

        res = asyncio.run(verify_starwhite_selected(mock_page, target_sku="MK2P4CH/A"))
        self.assertTrue(res)

    def test_starwhite_aria_checked_returns_true(self):
        """证据 2: aria-checked == 'true'"""
        mock_page = MagicMock()
        
        # radio is not checked
        mock_radio = MagicMock()
        mock_radio.count = AsyncMock(return_value=1)
        mock_radio.is_checked = AsyncMock(return_value=False)
        mock_radio.first = mock_radio

        # locs with aria-checked
        mock_aria_el = MagicMock()
        mock_aria_el.get_attribute = AsyncMock(return_value="true")
        mock_locs = MagicMock()
        mock_locs.count = AsyncMock(return_value=1)
        mock_locs.nth = MagicMock(return_value=mock_aria_el)

        def locator_side_effect(selector):
            if "dimensionColor'][value*='starwhite']" in selector and "," not in selector:
                return mock_radio
            else:
                return mock_locs

        mock_page.locator = MagicMock(side_effect=locator_side_effect)

        res = asyncio.run(verify_starwhite_selected(mock_page, target_sku="MK2P4CH/A"))
        self.assertTrue(res)

    def test_starwhite_dom_evaluate_returns_true(self):
        """证据 3: DOM evaluate 检测为 starwhite 选中"""
        mock_page = MagicMock()
        mock_empty = MagicMock()
        mock_empty.first = mock_empty
        mock_empty.count = AsyncMock(return_value=0)
        mock_page.locator = MagicMock(return_value=mock_empty)

        # DOM evaluate 确认
        mock_page.evaluate = AsyncMock(return_value=True)
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"

        res = asyncio.run(verify_starwhite_selected(mock_page, target_sku="MK2P4CH/A"))
        self.assertTrue(res)

    def test_starwhite_url_match_returns_true(self):
        """证据 4: 页面 URL 包含 MK2P4CH/A"""
        mock_page = MagicMock()
        mock_empty = MagicMock()
        mock_empty.first = mock_empty
        mock_empty.count = AsyncMock(return_value=0)
        mock_page.locator = MagicMock(return_value=mock_empty)
        mock_page.evaluate = AsyncMock(return_value=False)
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo/MK2P4CH/A"

        res = asyncio.run(verify_starwhite_selected(mock_page, target_sku="MK2P4CH/A"))
        self.assertTrue(res)

    def test_starwhite_no_evidence_strictly_returns_false(self):
        """未获得可靠证据时绝不能 confirmed=True (严格返回 False)"""
        mock_page = MagicMock()
        mock_empty = MagicMock()
        mock_empty.first = mock_empty
        mock_empty.count = AsyncMock(return_value=0)
        mock_page.locator = MagicMock(return_value=mock_empty)
        mock_page.evaluate = AsyncMock(return_value=False)
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"

        res = asyncio.run(verify_starwhite_selected(mock_page, target_sku="MK2P4CH/A"))
        self.assertFalse(res)

    def test_starwhite_exception_safety(self):
        """DOM 或 Locator 发生异常时，安全捕获并返回 False，绝不抛出崩溃"""
        mock_page = MagicMock()
        mock_page.locator = MagicMock(side_effect=RuntimeError("DOM disconnected"))
        mock_page.evaluate = AsyncMock(side_effect=RuntimeError("Context destroyed"))
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"

        res = asyncio.run(verify_starwhite_selected(mock_page, target_sku="MK2P4CH/A"))
        self.assertFalse(res)


class TestPhase1AStorageSelected(unittest.TestCase):
    """目标 2: 将 512GB selected-state 验证抽象为 production-safe helper"""

    def test_512gb_input_checked_returns_true(self):
        """证据 1: radio input.is_checked() == True"""
        mock_page = MagicMock()
        mock_radio = MagicMock()
        mock_radio.count = AsyncMock(return_value=1)
        mock_radio.is_checked = AsyncMock(return_value=True)
        mock_radio.first = mock_radio

        mock_page.locator = MagicMock(return_value=mock_radio)

        res = asyncio.run(verify_512gb_selected(mock_page, target_sku="MK2P4CH/A"))
        self.assertTrue(res)

    def test_512gb_aria_checked_returns_true(self):
        """证据 2: aria-checked == 'true'"""
        mock_page = MagicMock()
        
        mock_radio = MagicMock()
        mock_radio.count = AsyncMock(return_value=1)
        mock_radio.is_checked = AsyncMock(return_value=False)
        mock_radio.first = mock_radio

        mock_aria_el = MagicMock()
        mock_aria_el.get_attribute = AsyncMock(return_value="true")
        mock_locs = MagicMock()
        mock_locs.count = AsyncMock(return_value=1)
        mock_locs.nth = MagicMock(return_value=mock_aria_el)

        def locator_side_effect(selector):
            if "dimensionCapacity'][value='512gb']" in selector and "," not in selector:
                return mock_radio
            else:
                return mock_locs

        mock_page.locator = MagicMock(side_effect=locator_side_effect)

        res = asyncio.run(verify_512gb_selected(mock_page, target_sku="MK2P4CH/A"))
        self.assertTrue(res)

    def test_512gb_dom_evaluate_returns_true(self):
        """证据 3: DOM evaluate 检测为 512gb 选中"""
        mock_page = MagicMock()
        mock_empty = MagicMock()
        mock_empty.first = mock_empty
        mock_empty.count = AsyncMock(return_value=0)
        mock_page.locator = MagicMock(return_value=mock_empty)

        mock_page.evaluate = AsyncMock(return_value=True)
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"

        res = asyncio.run(verify_512gb_selected(mock_page, target_sku="MK2P4CH/A"))
        self.assertTrue(res)

    def test_512gb_url_match_returns_true(self):
        """证据 4: 页面 URL 包含 MK2P4CH/A"""
        mock_page = MagicMock()
        mock_empty = MagicMock()
        mock_empty.first = mock_empty
        mock_empty.count = AsyncMock(return_value=0)
        mock_page.locator = MagicMock(return_value=mock_empty)
        mock_page.evaluate = AsyncMock(return_value=False)
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo/mk2p4ch/a"

        res = asyncio.run(verify_512gb_selected(mock_page, target_sku="MK2P4CH/A"))
        self.assertTrue(res)

    def test_512gb_no_evidence_strictly_returns_false(self):
        """未获得可靠证据时绝不能 confirmed=True (严格返回 False)"""
        mock_page = MagicMock()
        mock_empty = MagicMock()
        mock_empty.first = mock_empty
        mock_empty.count = AsyncMock(return_value=0)
        mock_page.locator = MagicMock(return_value=mock_empty)
        mock_page.evaluate = AsyncMock(return_value=False)
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"

        res = asyncio.run(verify_512gb_selected(mock_page, target_sku="MK2P4CH/A"))
        self.assertFalse(res)

    def test_512gb_exception_safety(self):
        """发生异常时安全返回 False"""
        mock_page = MagicMock()
        mock_page.locator = MagicMock(side_effect=Exception("Browser closed"))
        mock_page.evaluate = AsyncMock(side_effect=Exception("Browser closed"))
        mock_page.url = "about:blank"

        res = asyncio.run(verify_512gb_selected(mock_page, target_sku="MK2P4CH/A"))
        self.assertFalse(res)


class TestPhase1AFastSelectOption(unittest.TestCase):
    """目标 3: 将 rehearsal 已验证的 Fast Path 规格选择抽象为 production-safe helper"""

    def test_fast_select_radio_visible_calls_check(self):
        """阶梯 1: 若 radio 可见，直接 radio.check() 并返回 True"""
        mock_page = MagicMock()
        mock_radio = MagicMock()
        mock_radio.first = mock_radio
        mock_radio.count = AsyncMock(return_value=1)
        mock_radio.is_visible = AsyncMock(return_value=True)
        mock_radio.check = AsyncMock(return_value=None)
        mock_radio.scroll_into_view_if_needed = AsyncMock(return_value=None)

        mock_label = MagicMock()
        mock_label.first = mock_label
        mock_label.count = AsyncMock(return_value=1)

        def locator_side_effect(sel):
            if "input" in sel:
                return mock_radio
            else:
                return mock_label

        mock_page.locator = MagicMock(side_effect=locator_side_effect)

        res = asyncio.run(fast_select_option(
            page=mock_page,
            radio_locator_str="input[name='dimensionColor']",
            label_locator_str="label:has-text('星光白色')",
            option_desc="外观颜色: 星光白色"
        ))
        self.assertTrue(res)
        mock_radio.check.assert_called_once()
        mock_label.click.assert_not_called()

    def test_fast_select_radio_hidden_clicks_visible_label(self):
        """阶梯 2: radio 不可见 (Apple DOM 标准)，极速点击 visible label，零超时等待"""
        mock_page = MagicMock()
        mock_radio = MagicMock()
        mock_radio.first = mock_radio
        mock_radio.count = AsyncMock(return_value=1)
        mock_radio.is_visible = AsyncMock(return_value=False)

        mock_label = MagicMock()
        mock_label.first = mock_label
        mock_label.count = AsyncMock(return_value=1)
        mock_label.is_visible = AsyncMock(return_value=True)
        mock_label.scroll_into_view_if_needed = AsyncMock(return_value=None)
        mock_label.click = AsyncMock(return_value=None)

        def locator_side_effect(sel):
            if "input" in sel:
                return mock_radio
            else:
                return mock_label

        mock_page.locator = MagicMock(side_effect=locator_side_effect)

        res = asyncio.run(fast_select_option(
            page=mock_page,
            radio_locator_str="input[name='dimensionColor']",
            label_locator_str="label:has-text('星光白色')",
            option_desc="外观颜色: 星光白色"
        ))
        self.assertTrue(res)
        mock_radio.check.assert_not_called()
        mock_label.click.assert_called_once_with(timeout=1500)

    def test_fast_select_fallback_regular_click(self):
        """阶梯 3: 若 visible label 未直接命中，尝试常规 click"""
        mock_page = MagicMock()
        mock_radio = MagicMock()
        mock_radio.first = mock_radio
        mock_radio.count = AsyncMock(return_value=0)

        mock_label = MagicMock()
        mock_label.first = mock_label
        mock_label.count = AsyncMock(return_value=1)
        mock_label.is_visible = AsyncMock(return_value=False)
        mock_label.scroll_into_view_if_needed = AsyncMock(return_value=None)
        mock_label.click = AsyncMock(return_value=None)

        def locator_side_effect(sel):
            if "input" in sel:
                return mock_radio
            else:
                return mock_label

        mock_page.locator = MagicMock(side_effect=locator_side_effect)

        res = asyncio.run(fast_select_option(
            page=mock_page,
            radio_locator_str="input[name='dimensionColor']",
            label_locator_str="label:has-text('星光白色')",
            option_desc="外观颜色: 星光白色"
        ))
        self.assertTrue(res)
        mock_label.click.assert_called_once()

    def test_fast_select_fallback_force_click(self):
        """阶梯 4: 常规点击失败，触发 force=True fallback 并返回 True"""
        mock_page = MagicMock()
        mock_radio = MagicMock()
        mock_radio.first = mock_radio
        mock_radio.count = AsyncMock(return_value=0)

        mock_label = MagicMock()
        mock_label.first = mock_label
        mock_label.count = AsyncMock(return_value=1)
        mock_label.is_visible = AsyncMock(return_value=False)
        mock_label.scroll_into_view_if_needed = AsyncMock(return_value=None)
        # 常规点击抛出异常，force=True 成功
        mock_label.click = AsyncMock(side_effect=[Exception("Element covered"), None])

        def locator_side_effect(sel):
            if "input" in sel:
                return mock_radio
            else:
                return mock_label

        mock_page.locator = MagicMock(side_effect=locator_side_effect)

        res = asyncio.run(fast_select_option(
            page=mock_page,
            radio_locator_str="input[name='dimensionColor']",
            label_locator_str="label:has-text('星光白色')",
            option_desc="外观颜色: 星光白色"
        ))
        self.assertTrue(res)
        self.assertEqual(mock_label.click.call_count, 2)

    def test_fast_select_all_fail_returns_false(self):
        """所有阶梯均无法点击，安全返回 False"""
        mock_page = MagicMock()
        mock_radio = MagicMock()
        mock_radio.first = mock_radio
        mock_radio.count = AsyncMock(return_value=0)

        mock_label = MagicMock()
        mock_label.first = mock_label
        mock_label.count = AsyncMock(return_value=1)
        mock_label.is_visible = AsyncMock(return_value=False)
        mock_label.scroll_into_view_if_needed = AsyncMock(return_value=None)
        mock_label.click = AsyncMock(side_effect=Exception("Click failed"))

        def locator_side_effect(sel):
            if "input" in sel:
                return mock_radio
            else:
                return mock_label

        mock_page.locator = MagicMock(side_effect=locator_side_effect)

        res = asyncio.run(fast_select_option(
            page=mock_page,
            radio_locator_str="input[name='dimensionColor']",
            label_locator_str="label:has-text('星光白色')",
            option_desc="外观颜色: 星光白色"
        ))
        self.assertFalse(res)


class TestPhase1ATargetSkuConsistency(unittest.TestCase):
    """目标 4: 增加 MK2P4CH/A 正式目标一致性验证 helper"""

    def test_sku_consistency_url_match_returns_true(self):
        """证据 1: 轻量级 URL 快速命中目标 SKU"""
        mock_page = MagicMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo/mk2p4ch/a"

        res = asyncio.run(verify_target_sku_consistency(
            page=mock_page,
            expected_sku="MK2P4CH/A",
            require_catalog=False,
        ))
        self.assertTrue(res)

    def test_sku_consistency_catalog_success(self):
        """证据 2: URL 未携带 SKU，Catalog 权威解析为 MK2P4CH/A 且状态为 SUCCESS"""
        mock_page = MagicMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"

        mock_resolver = MagicMock()
        mock_prod = ResolvedProduct(
            product_name="iPhone Duo",
            color="星光白色",
            storage="512GB",
            part_number="MK2P4CH/A",
            product_url="https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        )
        mock_resolver.resolve = AsyncMock(return_value=ResolutionResult(
            status=ResolutionStatus.SUCCESS,
            product=mock_prod
        ))

        res = asyncio.run(verify_target_sku_consistency(
            page=mock_page,
            expected_sku="MK2P4CH/A",
            resolver=mock_resolver,
            require_catalog=False,
        ))
        self.assertTrue(res)

    def test_sku_consistency_catalog_stale_allowed(self):
        """证据 3: Catalog 处于 CATALOG_STALE 缓存数据状态，但零件号确认一致，允许通过"""
        mock_page = MagicMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"

        mock_resolver = MagicMock()
        mock_prod = ResolvedProduct(
            product_name="iPhone Duo",
            color="星光白色",
            storage="512GB",
            part_number="MK2P4CH/A",
            product_url="https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        )
        mock_resolver.resolve = AsyncMock(return_value=ResolutionResult(
            status=ResolutionStatus.CATALOG_STALE,
            product=mock_prod,
            is_stale=True,
        ))

        res = asyncio.run(verify_target_sku_consistency(
            page=mock_page,
            expected_sku="MK2P4CH/A",
            resolver=mock_resolver,
        ))
        self.assertTrue(res)

    def test_sku_consistency_mismatch_returns_false(self):
        """反例 1: Catalog 解析出不同零件号 (TARGET_SKU_MISMATCH)，坚决返回 False"""
        mock_page = MagicMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"

        mock_resolver = MagicMock()
        mock_resolver.resolve = AsyncMock(return_value=ResolutionResult(
            status=ResolutionStatus.TARGET_SKU_MISMATCH,
            error_message="零件号冲突"
        ))

        res = asyncio.run(verify_target_sku_consistency(
            page=mock_page,
            expected_sku="MK2P4CH/A",
            resolver=mock_resolver,
        ))
        self.assertFalse(res)

    def test_sku_consistency_target_changed_returns_false(self):
        """反例 2: 官方零件号变更 (TARGET_CHANGED)，坚决返回 False"""
        mock_page = MagicMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"

        mock_resolver = MagicMock()
        mock_resolver.resolve = AsyncMock(return_value=ResolutionResult(
            status=ResolutionStatus.TARGET_CHANGED,
            error_message="官方配置更新为新零件号"
        ))

        res = asyncio.run(verify_target_sku_consistency(
            page=mock_page,
            expected_sku="MK2P4CH/A",
            resolver=mock_resolver,
        ))
        self.assertFalse(res)

    def test_sku_consistency_require_catalog_flag(self):
        """require_catalog=True 时，即使 URL 命中也必须通过 Catalog 核验"""
        mock_page = MagicMock()
        mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo/mk2p4ch/a"

        mock_resolver = MagicMock()
        mock_resolver.resolve = AsyncMock(return_value=ResolutionResult(
            status=ResolutionStatus.TARGET_CHANGED,
            error_message="官方目标发生变更"
        ))

        res = asyncio.run(verify_target_sku_consistency(
            page=mock_page,
            expected_sku="MK2P4CH/A",
            resolver=mock_resolver,
            require_catalog=True,
        ))
        self.assertFalse(res)

    def test_sku_consistency_exception_safety(self):
        """Catalog 解析器异常时安全返回 False"""
        mock_page = MagicMock()
        mock_page.url = "about:blank"
        mock_resolver = MagicMock()
        mock_resolver.resolve = AsyncMock(side_effect=RuntimeError("Network error"))

        res = asyncio.run(verify_target_sku_consistency(
            page=mock_page,
            expected_sku="MK2P4CH/A",
            resolver=mock_resolver,
        ))
        self.assertFalse(res)


class TestPhase1ABuyerIntegration(unittest.TestCase):
    """验证 AppleStoreBuyer 集成方法"""

    def setUp(self):
        self.config = AppConfig()
        self.mock_page = MagicMock()
        self.buyer = AppleStoreBuyer(self.mock_page, self.config)

    def test_buyer_verify_starwhite_delegates(self):
        with patch("src.apple_store.verify_starwhite_selected", return_value=True) as mock_helper:
            res = asyncio.run(self.buyer.verify_starwhite_selected("MK2P4CH/A"))
            self.assertTrue(res)
            mock_helper.assert_called_once_with(self.mock_page, target_sku="MK2P4CH/A")

    def test_buyer_verify_512gb_delegates(self):
        with patch("src.apple_store.verify_512gb_selected", return_value=True) as mock_helper:
            res = asyncio.run(self.buyer.verify_512gb_selected("MK2P4CH/A"))
            self.assertTrue(res)
            mock_helper.assert_called_once_with(self.mock_page, target_sku="MK2P4CH/A")

    def test_buyer_fast_select_option_delegates(self):
        with patch("src.apple_store.fast_select_option", return_value=True) as mock_helper:
            res = asyncio.run(self.buyer.fast_select_option("r_sel", "l_sel", "desc", 1000))
            self.assertTrue(res)
            mock_helper.assert_called_once_with(
                page=self.mock_page,
                radio_locator_str="r_sel",
                label_locator_str="l_sel",
                option_desc="desc",
                timeout_ms=1000
            )

    def test_buyer_verify_target_sku_consistency_delegates(self):
        with patch("src.apple_store.verify_target_sku_consistency", return_value=True) as mock_helper:
            res = asyncio.run(self.buyer.verify_target_sku_consistency(expected_sku="MK2P4CH/A"))
            self.assertTrue(res)
            mock_helper.assert_called_once()

    def test_buyer_fast_select_color_delegates(self):
        with patch.object(self.buyer, "fast_select_option", return_value=True) as mock_opt:
            res = asyncio.run(self.buyer.fast_select_color("星光白色"))
            self.assertTrue(res)
            mock_opt.assert_called_once()
            args, kwargs = mock_opt.call_args
            self.assertIn("starwhite", kwargs["radio_locator_str"])

    def test_buyer_fast_select_storage_delegates(self):
        with patch.object(self.buyer, "fast_select_option", return_value=True) as mock_opt:
            res = asyncio.run(self.buyer.fast_select_storage("512GB"))
            self.assertTrue(res)
            mock_opt.assert_called_once()
            args, kwargs = mock_opt.call_args
            self.assertIn("512gb", kwargs["radio_locator_str"])


class TestPhase1ASafetyAndIsolation(unittest.TestCase):
    """安全要求核验：禁止 import src.rehearsal 且保持全量安全边界完整"""

    def test_strictly_no_rehearsal_import_in_apple_store(self):
        """正式代码禁止 import src.rehearsal"""
        import src.apple_store
        with open(src.apple_store.__file__, "r", encoding="utf-8") as f:
            source = f.read()

        parsed = ast.parse(source)
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("rehearsal", alias.name, "严禁在正式代码中 import rehearsal")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertNotIn("rehearsal", module, "严禁在正式代码中 from ... import rehearsal")

    def test_safety_boundary_intact(self):
        """CAPTCHA / 2FA / Payment / Final Submit 安全边界完全不变"""
        mock_page = MagicMock()
        mock_page.url = "https://www.apple.com.cn/shop/checkout/payment"
        mock_loc = MagicMock()
        mock_loc.count = AsyncMock(return_value=0)
        mock_loc.first = mock_loc
        mock_page.locator = MagicMock(return_value=mock_loc)

        # URL 处于 payment 路径
        self.assertTrue(asyncio.run(is_payment_context(mock_page)))
        reason = asyncio.run(check_safety_boundary(mock_page))
        self.assertIsNotNone(reason)
        self.assertIn("支付方式/账单选择页面", reason)


if __name__ == "__main__":
    unittest.main()
