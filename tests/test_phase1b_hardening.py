"""Unit tests for Formal Launch Hardening - Phase 1B

Validates:
1. execute_formal_target_prepare (Target Prepare Gate with require_catalog=True)
2. execute_formal_online_checkout_flow Selection Preservation (zero redundant clicks)
3. execute_formal_online_checkout_flow Fast Path Recovery (targeted reselect + re-verification)
4. FINAL_TARGET_CHECK Gate (hard block before add_to_bag if any check fails)
5. run_launch_mode Integration and Gating
6. Strict architectural isolation (no top-level rehearsal import, lazy-only for rehearsal mode)
"""

import ast
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

from src.main import (
    execute_formal_target_prepare,
    execute_formal_online_checkout_flow,
    run_launch_mode,
)
from src.catalog import ResolutionStatus, ResolutionResult, ResolvedProduct
from src.config import AppConfig
from src.notifier import NotificationEvent, Notifier


def make_test_config() -> AppConfig:
    cfg = AppConfig()
    cfg.target.product = "iPhone Duo"
    cfg.target.color = "星光白色"
    cfg.target.storage = "512GB"
    cfg.target.part_number = "MK2P4CH/A"
    cfg.target.auto_resolve_part_number = False
    cfg.product_url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
    cfg.notifications.feishu_webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/test"
    return cfg


class TestPhase1BTargetPrepare(unittest.TestCase):
    """验证 Formal Target Prepare Gate (首发目标页面预热与规格预选门禁)"""

    def setUp(self):
        self.config = make_test_config()
        self.mock_page = MagicMock()
        self.mock_buyer = MagicMock()
        self.mock_resolver = MagicMock()

    def test_target_prepare_success(self):
        """当页面正常打开、两项规格与 SKU 均核验通过时，返回 True"""
        self.mock_buyer.open_product_page = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_storage = AsyncMock(return_value=True)
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)

        res = asyncio.run(execute_formal_target_prepare(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            expected_part="MK2P4CH/A",
        ))

        self.assertTrue(res)
        self.mock_buyer.open_product_page.assert_awaited_once()
        self.mock_buyer.fast_select_color.assert_awaited_once()
        self.mock_buyer.fast_select_storage.assert_awaited_once()
        self.mock_buyer.verify_starwhite_selected.assert_awaited_once_with(target_sku="MK2P4CH/A")
        self.mock_buyer.verify_512gb_selected.assert_awaited_once_with(target_sku="MK2P4CH/A")
        self.mock_buyer.verify_target_sku_consistency.assert_awaited_once_with(
            expected_sku="MK2P4CH/A",
            resolver=self.mock_resolver,
            require_catalog=True,
        )

    def test_target_prepare_fails_when_page_open_fails(self):
        """打开页面失败时直接阻断返回 False，不执行后续选择"""
        self.mock_buyer.open_product_page = AsyncMock(return_value=False)
        self.mock_buyer.fast_select_color = AsyncMock()
        self.mock_buyer.fast_select_storage = AsyncMock()

        res = asyncio.run(execute_formal_target_prepare(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            expected_part="MK2P4CH/A",
        ))

        self.assertFalse(res)
        self.mock_buyer.open_product_page.assert_awaited_once()
        self.mock_buyer.fast_select_color.assert_not_called()
        self.mock_buyer.fast_select_storage.assert_not_called()

    def test_target_prepare_fails_when_color_unverified(self):
        """星光白色未选中时阻断返回 False"""
        self.mock_buyer.open_product_page = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_storage = AsyncMock(return_value=True)
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=False)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)

        res = asyncio.run(execute_formal_target_prepare(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            expected_part="MK2P4CH/A",
        ))

        self.assertFalse(res)

    def test_target_prepare_fails_when_storage_unverified(self):
        """512GB 未选中时阻断返回 False"""
        self.mock_buyer.open_product_page = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_storage = AsyncMock(return_value=True)
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=False)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)

        res = asyncio.run(execute_formal_target_prepare(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            expected_part="MK2P4CH/A",
        ))

        self.assertFalse(res)

    def test_target_prepare_fails_when_sku_unverified(self):
        """SKU 一致性未通过时阻断返回 False"""
        self.mock_buyer.open_product_page = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_storage = AsyncMock(return_value=True)
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=False)

        res = asyncio.run(execute_formal_target_prepare(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            expected_part="MK2P4CH/A",
        ))

        self.assertFalse(res)

    def test_target_prepare_enforces_require_catalog_flag(self):
        """验证严格传入了 require_catalog=True 参数"""
        self.mock_buyer.open_product_page = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_storage = AsyncMock(return_value=True)
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)

        asyncio.run(execute_formal_target_prepare(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            expected_part="MK2P4CH/A",
        ))

        call_kwargs = self.mock_buyer.verify_target_sku_consistency.call_args[1]
        self.assertTrue(call_kwargs.get("require_catalog"))


class TestPhase1BSelectionPreservation(unittest.TestCase):
    """验证正式线上结账流程中的 Selection Preservation (保留状态跳过重复操作)"""

    def setUp(self):
        self.config = make_test_config()
        self.mock_page = MagicMock()
        self.mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        self.mock_buyer = MagicMock()
        self.mock_resolver = MagicMock()
        self.mock_notifier = MagicMock()
        self.mock_notifier.notify = AsyncMock()

    def test_selection_preserved_skips_reload_and_reselect(self):
        """当星光白色、512GB 与 MK2P4CH/A 均完好保留时，跳过重复导航与选择"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock(return_value=True)
        self.mock_buyer.proceed_to_bag_and_checkout = AsyncMock(return_value=(True, "人工接管点到达"))
        self.mock_buyer.fast_select_color = AsyncMock()
        self.mock_buyer.fast_select_storage = AsyncMock()
        self.mock_buyer.open_product_page = AsyncMock()

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        self.assertTrue(res)
        # 验证未发生重复导航和重复点击
        self.mock_buyer.open_product_page.assert_not_called()
        self.mock_buyer.fast_select_color.assert_not_called()
        self.mock_buyer.fast_select_storage.assert_not_called()
        # 验证处理了折抵、AppleCare 并加车推进
        self.mock_buyer.handle_trade_in.assert_awaited_once()
        self.mock_buyer.handle_applecare.assert_awaited_once()
        self.mock_buyer.add_to_bag.assert_awaited_once()
        self.mock_buyer.proceed_to_bag_and_checkout.assert_awaited_once()
        self.mock_notifier.notify.assert_awaited_once()

    def test_selection_preserved_enforces_require_catalog_true(self):
        """验证 Preservation 检查严格调用 require_catalog=True"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock(return_value=True)
        self.mock_buyer.proceed_to_bag_and_checkout = AsyncMock(return_value=(True, "人工接管点到达"))

        asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        # 检查 verify_target_sku_consistency 的调用（步骤 1 和步骤 3）
        for call_item in self.mock_buyer.verify_target_sku_consistency.call_args_list:
            self.assertTrue(call_item[1].get("require_catalog"))


class TestPhase1BFastPathRecovery(unittest.TestCase):
    """验证正式线上结账流程中的 Fast Path 规格快速恢复机制"""

    def setUp(self):
        self.config = make_test_config()
        self.mock_page = MagicMock()
        self.mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        self.mock_buyer = MagicMock()
        self.mock_resolver = MagicMock()
        self.mock_notifier = MagicMock()
        self.mock_notifier.notify = AsyncMock()

    def test_recovery_when_color_lost_only(self):
        """当仅星光白色丢失时，仅针对性调用 fast_select_color，不调用 fast_select_storage"""
        # 初次检测：颜色 False，容量 True，SKU True
        # 重选后核验：颜色 True，容量 True，SKU True
        # 终极门禁：全部 True
        self.mock_buyer.verify_starwhite_selected = AsyncMock(side_effect=[False, True, True])
        self.mock_buyer.verify_512gb_selected = AsyncMock(side_effect=[True, True, True])
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(side_effect=[True, True, True])
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_storage = AsyncMock(return_value=True)
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock(return_value=True)
        self.mock_buyer.proceed_to_bag_and_checkout = AsyncMock(return_value=(True, "人工接管点到达"))

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        self.assertTrue(res)
        self.mock_buyer.fast_select_color.assert_awaited_once()
        self.mock_buyer.fast_select_storage.assert_not_called()
        self.mock_buyer.add_to_bag.assert_awaited_once()

    def test_recovery_when_storage_lost_only(self):
        """当仅 512GB 丢失时，仅针对性调用 fast_select_storage，不调用 fast_select_color"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(side_effect=[True, True, True])
        self.mock_buyer.verify_512gb_selected = AsyncMock(side_effect=[False, True, True])
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(side_effect=[True, True, True])
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_storage = AsyncMock(return_value=True)
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock(return_value=True)
        self.mock_buyer.proceed_to_bag_and_checkout = AsyncMock(return_value=(True, "人工接管点到达"))

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        self.assertTrue(res)
        self.mock_buyer.fast_select_color.assert_not_called()
        self.mock_buyer.fast_select_storage.assert_awaited_once()
        self.mock_buyer.add_to_bag.assert_awaited_once()

    def test_recovery_when_both_lost(self):
        """当颜色与容量均丢失时，依次调用 fast_select_color 与 fast_select_storage"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(side_effect=[False, True, True])
        self.mock_buyer.verify_512gb_selected = AsyncMock(side_effect=[False, True, True])
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(side_effect=[True, True, True])
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.fast_select_storage = AsyncMock(return_value=True)
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock(return_value=True)
        self.mock_buyer.proceed_to_bag_and_checkout = AsyncMock(return_value=(True, "人工接管点到达"))

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        self.assertTrue(res)
        self.mock_buyer.fast_select_color.assert_awaited_once()
        self.mock_buyer.fast_select_storage.assert_awaited_once()
        self.mock_buyer.add_to_bag.assert_awaited_once()

    def test_recovery_aborts_when_reverification_fails_color(self):
        """重选后若颜色仍未核验通过，立即中止流程，坚决禁止加车"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(side_effect=[False, False])
        self.mock_buyer.verify_512gb_selected = AsyncMock(side_effect=[True, True])
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(side_effect=[True, True])
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock()

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        self.assertFalse(res)
        self.mock_buyer.fast_select_color.assert_awaited_once()
        self.mock_buyer.add_to_bag.assert_not_called()

    def test_recovery_aborts_when_reverification_fails_sku(self):
        """重选后若 SKU 一致性核验未通过，立即中止流程，坚决禁止加车"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(side_effect=[False, True])
        self.mock_buyer.verify_512gb_selected = AsyncMock(side_effect=[True, True])
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(side_effect=[True, False])
        self.mock_buyer.fast_select_color = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock()

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        self.assertFalse(res)
        self.mock_buyer.add_to_bag.assert_not_called()

    def test_recovery_reloads_page_if_wrong_url(self):
        """恢复时若页面非 Duo 购买页，重新加载购买页；若加载失败则中止"""
        self.mock_page.url = "https://www.apple.com.cn/shop"
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=False)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        self.mock_buyer.open_product_page = AsyncMock(return_value=False)
        self.mock_buyer.add_to_bag = AsyncMock()

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        self.assertFalse(res)
        self.mock_buyer.open_product_page.assert_awaited_once()
        self.mock_buyer.add_to_bag.assert_not_called()


class TestPhase1BFinalTargetCheck(unittest.TestCase):
    """验证加车前终极硬门禁 (FINAL_TARGET_CHECK)"""

    def setUp(self):
        self.config = make_test_config()
        self.mock_page = MagicMock()
        self.mock_page.url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
        self.mock_buyer = MagicMock()
        self.mock_resolver = MagicMock()
        self.mock_notifier = MagicMock()
        self.mock_notifier.notify = AsyncMock()

    def test_final_target_check_blocks_add_to_bag_when_color_invalid(self):
        """在加车前终极核验中，若颜色不符，输出 FINAL_TARGET_CHECK_FAILED 并拒不加车"""
        # 初次 Preservation 核验全部通过
        # 但在步骤 3 final check 时，颜色突然失效为 False
        self.mock_buyer.verify_starwhite_selected = AsyncMock(side_effect=[True, False])
        self.mock_buyer.verify_512gb_selected = AsyncMock(side_effect=[True, True])
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(side_effect=[True, True])
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock()

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        self.assertFalse(res)
        self.mock_buyer.add_to_bag.assert_not_called()

    def test_final_target_check_blocks_add_to_bag_when_storage_invalid(self):
        """在加车前终极核验中，若容量不符，输出 FINAL_TARGET_CHECK_FAILED 并拒不加车"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(side_effect=[True, True])
        self.mock_buyer.verify_512gb_selected = AsyncMock(side_effect=[True, False])
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(side_effect=[True, True])
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock()

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        self.assertFalse(res)
        self.mock_buyer.add_to_bag.assert_not_called()

    def test_final_target_check_blocks_add_to_bag_when_sku_invalid(self):
        """在加车前终极核验中，若 SKU 一致性未通过，输出 FINAL_TARGET_CHECK_FAILED 并拒不加车"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(side_effect=[True, True])
        self.mock_buyer.verify_512gb_selected = AsyncMock(side_effect=[True, True])
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(side_effect=[True, False])
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock()

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        self.assertFalse(res)
        self.mock_buyer.add_to_bag.assert_not_called()

    def test_final_target_check_enforces_require_catalog_true(self):
        """在加车前终极核验中，require_catalog=True 必须被强制传入"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock(return_value=True)
        self.mock_buyer.proceed_to_bag_and_checkout = AsyncMock(return_value=(True, "到达停机点"))

        asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        self.assertEqual(self.mock_buyer.verify_target_sku_consistency.call_count, 2)
        # 验证两次调用（初次 preservation 检查和 final check）均带有 require_catalog=True
        first_call_kwargs = self.mock_buyer.verify_target_sku_consistency.call_args_list[0][1]
        second_call_kwargs = self.mock_buyer.verify_target_sku_consistency.call_args_list[1][1]
        self.assertTrue(first_call_kwargs.get("require_catalog"))
        self.assertTrue(second_call_kwargs.get("require_catalog"))

    def test_add_to_bag_failure_returns_false_and_no_handoff(self):
        """add_to_bag 失败时返回 False，且不触发人工接管通知"""
        self.mock_buyer.verify_starwhite_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_512gb_selected = AsyncMock(return_value=True)
        self.mock_buyer.verify_target_sku_consistency = AsyncMock(return_value=True)
        self.mock_buyer.handle_trade_in = AsyncMock(return_value=True)
        self.mock_buyer.handle_applecare = AsyncMock(return_value=True)
        self.mock_buyer.add_to_bag = AsyncMock(return_value=False)
        self.mock_buyer.proceed_to_bag_and_checkout = AsyncMock()

        res = asyncio.run(execute_formal_online_checkout_flow(
            page=self.mock_page,
            config=self.config,
            buyer=self.mock_buyer,
            resolver=self.mock_resolver,
            notifier=self.mock_notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part="MK2P4CH/A",
        ))

        self.assertFalse(res)
        self.mock_buyer.add_to_bag.assert_awaited_once()
        self.mock_buyer.proceed_to_bag_and_checkout.assert_not_called()
        self.mock_notifier.notify.assert_not_called()


class TestPhase1BRunLaunchModeIntegration(unittest.TestCase):
    """验证 run_launch_mode 对 Target Prepare Gate 及双通道主流程的集成把控"""

    def setUp(self):
        self.config = make_test_config()

    @patch("src.main.evaluate_launch_readiness")
    @patch("src.main.BrowserManager")
    async def _run_with_mocks(self, mock_bm_cls, mock_eval, eval_ret, resolve_ret, prepare_ret=None):
        mock_bm = MagicMock()
        mock_page = MagicMock()
        mock_bm.start = AsyncMock(return_value=mock_page)
        mock_bm.close = AsyncMock()
        mock_bm_cls.return_value = mock_bm
        mock_eval.return_value = eval_ret

        with patch("src.main.AppleCatalogResolver") as mock_res_cls, \
             patch("src.main.execute_formal_target_prepare") as mock_prep:
            mock_resolver = MagicMock()
            mock_resolver.resolve = AsyncMock(return_value=resolve_ret)
            mock_res_cls.return_value = mock_resolver
            if prepare_ret is not None:
                mock_prep.return_value = prepare_ret

            await run_launch_mode(self.config, interval_sec=1, max_rounds=1)
            return mock_prep

    def test_run_launch_mode_blocked_when_readiness_fails(self):
        """前置健康检查不通过时直接阻断退出，不执行 Target Prepare"""
        mock_prep = asyncio.run(self._run_with_mocks(
            eval_ret=(False, [], ["Session missing"]),
            resolve_ret=None,
        ))
        mock_prep.assert_not_called()

    def test_run_launch_mode_blocked_when_target_sku_mismatch(self):
        """官方 Catalog 解析为 TARGET_SKU_MISMATCH 时阻断退出，不执行 Target Prepare"""
        res_result = ResolutionResult(
            status=ResolutionStatus.TARGET_SKU_MISMATCH,
            error_message="Part number mismatch",
        )
        mock_prep = asyncio.run(self._run_with_mocks(
            eval_ret=(True, [], []),
            resolve_ret=res_result,
        ))
        mock_prep.assert_not_called()

    def test_run_launch_mode_blocked_when_target_changed(self):
        """官方 Catalog 解析为 TARGET_CHANGED 时阻断退出，不执行 Target Prepare"""
        res_result = ResolutionResult(
            status=ResolutionStatus.TARGET_CHANGED,
            error_message="Part number changed",
        )
        mock_prep = asyncio.run(self._run_with_mocks(
            eval_ret=(True, [], []),
            resolve_ret=res_result,
        ))
        mock_prep.assert_not_called()

    def test_run_launch_mode_blocks_when_target_prepare_fails(self):
        """Target Prepare Gate 返回 False 时，直接退出，不进入监控轮询"""
        resolved_prod = ResolvedProduct(
            product_name="iPhone Duo",
            color="星光白色",
            storage="512GB",
            part_number="MK2P4CH/A",
            price="RMB 7,999",
            product_url="https://www.apple.com.cn/shop/buy-iphone/iphone-duo",
        )
        res_result = ResolutionResult(
            status=ResolutionStatus.SUCCESS,
            product=resolved_prod,
        )

        with patch("src.main.PickupMonitor") as mock_pm_cls:
            mock_prep = asyncio.run(self._run_with_mocks(
                eval_ret=(True, [], []),
                resolve_ret=res_result,
                prepare_ret=False,
            ))
            mock_prep.assert_awaited_once()
            # PickupMonitor 不应被启动
            mock_pm_cls.assert_not_called()


class TestPhase1BArchitectureIsolation(unittest.TestCase):
    """验证架构隔离：正式首发代码与 src.rehearsal 物理隔离，无顶层 rehearsal 导入"""

    def test_no_top_level_rehearsal_import_in_main(self):
        """解析 src/main.py AST，确保在模块顶层没有任何 rehearsal 导入"""
        main_py_path = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
        with open(main_py_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename="src/main.py")

        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("rehearsal", alias.name, f"Top-level import '{alias.name}' detected in src/main.py")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                self.assertNotIn("rehearsal", mod, f"Top-level from import '{mod}' detected in src/main.py")

    def test_rehearsal_only_imported_under_launch_rehearsal_condition(self):
        """确保 rehearsal 仅在 args.mode == 'launch-rehearsal' 分支内局部懒加载"""
        main_py_path = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
        with open(main_py_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 检查所有出现 'rehearsal' 的行
        rehearsal_imports = [line.strip() for line in content.splitlines() if "import" in line and "rehearsal" in line]
        self.assertEqual(len(rehearsal_imports), 1, f"Expected exactly 1 rehearsal import, found: {rehearsal_imports}")
        self.assertEqual(rehearsal_imports[0], "from .rehearsal import run_launch_rehearsal")

    def test_apple_store_has_no_rehearsal_import(self):
        """解析 src/apple_store.py AST，确保完全没有引用 rehearsal"""
        apple_store_path = os.path.join(os.path.dirname(__file__), "..", "src", "apple_store.py")
        with open(apple_store_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("rehearsal", content, "src/apple_store.py must not reference rehearsal")


if __name__ == "__main__":
    unittest.main()
