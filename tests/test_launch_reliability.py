"""Offline regressions for the launch boundary, fresh data, and manual handoff."""
import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import async_playwright
from src.catalog import AppleCatalogResolver, ResolutionStatus
from src.config import AppConfig
from src.apple_store import AppleStoreBuyer, PurchaseNotReady
from src.launch_timing import parse_launch_timestamp, launch_wait_seconds, acquire_launch_lock
from src.main import (AvailabilityEvent, LaunchCoordinator, OnlineMonitorTask,
                      evaluate_launch_readiness, wait_for_manual_handoff,
                      execute_formal_online_checkout_flow)
from src.notifier import NotificationEvent

SKU = "MK2P4CH/A"
URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
CATALOG = {
    "products": [{"partNumber": SKU, "dimensionColor": "starwhite",
                  "dimensionCapacity": "512gb", "comingSoon": True}],
    "displayValues": {"dimensionColor": {"starwhite": {"value": "星光白色"}},
                      "dimensionCapacity": {"512gb": {"value": "512GB"}}},
}


class TestLaunchTiming(unittest.TestCase):
    def test_second_instance_is_blocked_and_lock_can_be_released(self):
        with tempfile.TemporaryDirectory() as folder:
            path = folder + "/launch.lock"
            first = acquire_launch_lock(path)
            try:
                with self.assertRaises(RuntimeError):
                    acquire_launch_lock(path)
            finally:
                first.close()
            acquire_launch_lock(path).close()

    def test_timezone_is_required(self):
        with self.assertRaises(ValueError):
            parse_launch_timestamp("2026-10-16T20:00:00")

    def test_china_launch_is_1200_utc(self):
        self.assertEqual(parse_launch_timestamp("2026-10-16T20:00:00+08:00"),
                         datetime(2026, 10, 16, 12, tzinfo=timezone.utc).timestamp())
        with patch("src.launch_timing.time.time", return_value=90):
            self.assertEqual(launch_wait_seconds(100), 10)


class TestLaunchLifecycle(unittest.IsolatedAsyncioTestCase):
    def make_monitor(self, data, stale=False, launch_time=None):
        resolver = MagicMock()
        resolver.fetch_product_catalog = AsyncMock(return_value=(data, stale))
        stop = asyncio.Event()
        queue = asyncio.Queue()
        task = OnlineMonitorTask(MagicMock(), resolver, queue, asyncio.Lock(),
                                 interval_sec=0.01, min_interval_sec=0.01,
                                 _allow_test_override=True, launch_time=launch_time)
        return task, resolver, stop, queue

    async def run_one_round(self, data, stale=False):
        task, resolver, stop, queue = self.make_monitor(data, stale)
        async def fetch(*args, **kwargs):
            stop.set()
            return data, stale
        resolver.fetch_product_catalog.side_effect = fetch
        await task.run(stop)
        return task, queue

    async def test_get_ready_does_not_trigger_a_purchase(self):
        _, queue = await self.run_one_round({"products": [
            {"partNumber": SKU, "comingSoon": True, "getReady": True}]})
        self.assertTrue(queue.empty())

    async def test_stale_available_data_cannot_trigger(self):
        task, queue = await self.run_one_round({"products": [
            {"partNumber": SKU, "comingSoon": False}]}, stale=True)
        self.assertTrue(queue.empty())
        self.assertEqual(task.consecutive_errors, 1)

    async def test_missing_availability_is_not_available(self):
        _, queue = await self.run_one_round({"products": [{"partNumber": SKU}]})
        self.assertTrue(queue.empty())

    async def test_false_coming_soon_before_launch_cannot_trigger(self):
        data = {"products": [{"partNumber": SKU, "comingSoon": False}]}
        iso = datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()
        task, resolver, stop, queue = self.make_monitor(data, launch_time=iso)
        async def fetch(*args, **kwargs):
            stop.set()
            return data, False
        resolver.fetch_product_catalog.side_effect = fetch
        await task.run(stop)
        self.assertTrue(queue.empty())

    async def test_launch_boundary_is_checked_before_normal_cadence(self):
        target = time.time() + 0.12
        iso = datetime.fromtimestamp(target, timezone.utc).isoformat()
        data = {"products": [{"partNumber": SKU, "comingSoon": False}]}
        task, resolver, stop, queue = self.make_monitor(data, launch_time=iso)
        task.min_interval_sec = 0.15
        task.interval_sec = 10
        observed = []
        async def fetch(*args, **kwargs):
            observed.append(time.time())
            stop.set()
            return data, False
        resolver.fetch_product_catalog.side_effect = fetch
        await asyncio.wait_for(task.run(stop), 1)
        self.assertEqual(len(observed), 1)
        self.assertGreaterEqual(observed[0], target)
        self.assertLess(observed[0] - target, 0.3)
        self.assertEqual((await queue.get()).state, "AVAILABLE")

    async def test_edge_error_from_fetch_reaches_monitor_backoff(self):
        task, resolver, stop, queue = self.make_monitor(None)
        async def fetch(*args, **kwargs):
            stop.set()
            raise RuntimeError("Catalog HTTP 429")
        resolver.fetch_product_catalog.side_effect = fetch
        await task.run(stop)
        self.assertEqual(task.last_status_code, 429)
        self.assertTrue(task.backoff_tracker.is_in_backoff())
        self.assertTrue(queue.empty())

    async def test_handoff_waits_for_user_to_close_the_page(self):
        page = MagicMock()
        page.is_closed.return_value = False
        page.bring_to_front = AsyncMock()
        running = asyncio.create_task(wait_for_manual_handoff(page))
        await asyncio.sleep(0)
        self.assertFalse(running.done())
        page.close.assert_not_called()
        callback = page.on.call_args.args[1]
        callback()
        await asyncio.wait_for(running, 1)
        page.remove_listener.assert_called_once()

    async def test_ready_event_is_ignored_without_stopping_monitors(self):
        cfg = AppConfig()
        coord = LaunchCoordinator(MagicMock(), cfg, MagicMock(), MagicMock(),
                                  MagicMock(), MagicMock())
        self.assertFalse(await coord.handle_online_event(
            AvailabilityEvent("online", "PREORDER_READY", SKU)))
        self.assertFalse(coord.stop_event.is_set())
        self.assertFalse(coord.purchase_flow_started)

    async def test_live_signal_refreshes_page_before_checkout(self):
        page = MagicMock()
        page.reload = AsyncMock(return_value=None)
        coord = LaunchCoordinator(page, AppConfig(), MagicMock(), MagicMock(),
                                  MagicMock(), MagicMock(), refresh_on_available=True)
        async def flow(**kwargs):
            page.reload.assert_awaited_once()
            return True
        with patch("src.main.execute_formal_online_checkout_flow", side_effect=flow):
            self.assertTrue(await coord.handle_online_event(AvailabilityEvent("online", "AVAILABLE", SKU)))

    async def test_disabled_purchase_button_can_recover_without_double_order(self):
        coord = LaunchCoordinator(MagicMock(), AppConfig(), MagicMock(), MagicMock(),
                                  MagicMock(), MagicMock())
        with patch("src.main.execute_formal_online_checkout_flow", new_callable=AsyncMock) as flow:
            flow.side_effect = [PurchaseNotReady("not enabled"), True]
            event = AvailabilityEvent("online", "AVAILABLE", SKU)
            self.assertFalse(await coord.handle_online_event(event))
            self.assertFalse(coord.purchase_flow_started)
            self.assertFalse(coord.stop_event.is_set())
            self.assertTrue(await coord.handle_online_event(event))
            self.assertTrue(coord.stop_event.is_set())
            self.assertEqual(flow.await_count, 2)

    async def test_uncertain_add_to_bag_is_not_clicked_again(self):
        page = MagicMock()
        button = page.locator.return_value.first
        button.count = AsyncMock(return_value=1)
        button.is_visible = AsyncMock(return_value=True)
        button.is_enabled = AsyncMock(return_value=True)
        button.wait_for = AsyncMock()
        button.scroll_into_view_if_needed = AsyncMock()
        button.click = AsyncMock(side_effect=TimeoutError("response uncertain"))
        with tempfile.TemporaryDirectory() as folder:
            buyer = AppleStoreBuyer(page, AppConfig(), screenshots_dir=folder)
            buyer.save_screenshot = AsyncMock()
            buyer._human_delay = AsyncMock()
            with patch("src.apple_store.asyncio.sleep", new_callable=AsyncMock):
                self.assertFalse(await buyer.add_to_bag())
        button.click.assert_awaited_once()
        button.evaluate.assert_not_called()

    async def test_new_official_continue_hands_off_without_guessing_cart_action(self):
        buyer = MagicMock()
        for name in ("verify_starwhite_selected", "verify_512gb_selected",
                     "verify_target_sku_consistency", "is_trade_in_none_selected",
                     "is_applecare_none_selected", "is_official_continue_ready"):
            setattr(buyer, name, AsyncMock(return_value=True))
        buyer.is_add_to_bag_ready = AsyncMock(return_value=False)
        notifier = MagicMock()
        result = await execute_formal_online_checkout_flow(
            MagicMock(), AppConfig(), buyer, MagicMock(), notifier,
            NotificationEvent.ONLINE_AVAILABLE)
        self.assertTrue(result)
        buyer.add_to_bag.assert_not_called()
        buyer.proceed_to_bag_and_checkout.assert_not_called()
        notifier.notify_background.assert_called_once()

    async def test_explicitly_disabled_feishu_does_not_block_local_alerts(self):
        cfg = AppConfig()
        cfg.notifications.feishu.enabled = False
        page = MagicMock(goto=AsyncMock())
        resolver = MagicMock()
        resolver.fetch_product_catalog = AsyncMock(return_value=(CATALOG, False))
        product = MagicMock(part_number=SKU)
        resolver.resolve = AsyncMock(return_value=MagicMock(
            status=ResolutionStatus.SUCCESS, product=product, is_stale=False))
        with patch("src.main.check_apple_login_status", AsyncMock(return_value=(True, "ready"))), \
             patch("src.main.PickupMonitor") as pickup:
            pickup.return_value.run_single_round = AsyncMock(return_value=[None] * 4)
            ready, _, blocking = await evaluate_launch_readiness(page, cfg, resolver, MagicMock())
        self.assertTrue(ready, blocking)


class TestFreshCatalogBrowser(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.resolver = AppleCatalogResolver(self.temp.name)
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.status = 200
        self.apple_wrapper = False
        self.data = json.loads(json.dumps(CATALOG))
        self.requests = 0
        async def respond(route):
            self.requests += 1
            bootstrap = json.dumps({"productSelectionData": self.data})
            if self.apple_wrapper:
                bootstrap = '{ productSelectionData: ' + json.dumps(self.data) + ', otherMetadata: true }'
            html = ('<input name="dimensionColor" value="starwhite" type="radio" checked>'
                    '<input name="dimensionCapacity" value="512gb" type="radio" checked>'
                    '<script>window.PRODUCT_SELECTION_BOOTSTRAP = ' +
                    bootstrap + ';</script>')
            await route.fulfill(status=self.status, content_type="text/html",
                                body=html if self.status == 200 else "blocked")
        await self.page.route(URL, respond)
        await self.page.goto(URL)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.temp.cleanup()

    async def test_http_block_cannot_fall_back_to_old_page_or_cache(self):
        self.resolver._save_cache("iPhone Duo", {"productSelectionData": CATALOG})
        for status in (403, 429, 541):
            self.status = status
            with self.assertRaisesRegex(Exception, f"Catalog HTTP {status}"):
                await self.resolver.fetch_product_catalog(self.page, "iPhone Duo", no_navigate=True)

    async def test_monitor_reads_new_response_while_preserving_selected_dom(self):
        self.data["products"][0]["comingSoon"] = False
        data, stale = await self.resolver.fetch_product_catalog(self.page, "iPhone Duo", no_navigate=True)
        self.assertFalse(stale)
        self.assertIs(data["products"][0]["comingSoon"], False)
        self.assertTrue(await self.page.locator("input[name=dimensionColor]").is_checked())

    async def test_spec_check_does_not_navigate_or_fetch(self):
        result = await self.resolver.resolve(self.page, "iPhone Duo", "星光白色", "512GB",
                                             expected_part_number=SKU, current_page_only=True)
        self.assertEqual(result.status, ResolutionStatus.SUCCESS)
        self.assertEqual(self.requests, 1)
        self.assertTrue(await self.page.locator("input[name=dimensionCapacity]").is_checked())

    async def test_apples_unquoted_wrapper_and_escaped_nested_json(self):
        self.apple_wrapper = True
        self.data["message"] = 'nested { braces } and quote " and slash \\'
        data, stale = await self.resolver.fetch_product_catalog(self.page, "iPhone Duo", no_navigate=True)
        self.assertFalse(stale)
        self.assertEqual(data, self.data)

    async def test_invalid_fresh_schema_does_not_use_old_dom(self):
        self.data = {"message": "maintenance"}
        with self.assertRaisesRegex(Exception, "Fresh catalog"):
            await self.resolver.fetch_product_catalog(self.page, "iPhone Duo", no_navigate=True)

    async def test_only_enabled_purchase_buttons_are_orderable(self):
        await self.page.set_content('<button data-autom="add-to-cart" disabled>继续</button>')
        buyer = AppleStoreBuyer(self.page, AppConfig(), screenshots_dir=self.temp.name)
        self.assertFalse(await buyer.is_add_to_bag_ready())
        await self.page.locator("button").evaluate("el => el.disabled = false")
        self.assertTrue(await buyer.is_add_to_bag_ready())

    async def test_new_official_continue_is_recognized_only_when_enabled(self):
        await self.page.set_content('<button data-autom="continueButton" disabled>继续</button>')
        buyer = AppleStoreBuyer(self.page, AppConfig(), screenshots_dir=self.temp.name)
        self.assertFalse(await buyer.is_official_continue_ready())
        await self.page.locator("button").evaluate("el => el.disabled = false")
        self.assertTrue(await buyer.is_official_continue_ready())
