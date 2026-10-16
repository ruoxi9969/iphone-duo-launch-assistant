"""Offline browser regressions: real DOM transitions, budget and no final submission."""
import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timezone

from playwright.async_api import async_playwright
from src.apple_store import AppleStoreBuyer
from src.checkout import (advance_checkout, amount_cny, official_url,
                          wait_for_storefront, cart_is_single_target)
from src.config import AppConfig, load_config

URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
BAG = "https://www.apple.com.cn/shop/bag"
SHIPPING = "https://secure6.www.apple.com.cn/shop/checkout/shipping"
BILLING = "https://secure6.www.apple.com.cn/shop/checkout/billing"


class MoneyAndOriginTests(unittest.TestCase):
    def test_full_prices_only(self):
        for value in ("RMB 17,999", "17999.00", "￥17999"):
            self.assertEqual(amount_cny(value), 17999)
        for value in (None, "每月749.96", "RMB 17,999起", "-1", "NaN"):
            self.assertIsNone(amount_cny(value))

    def test_origin_lookalikes_rejected(self):
        self.assertTrue(official_url(BILLING))
        for url in ("http://www.apple.com.cn/shop/bag", "https://apple.com.cn.evil.test/", "https://evilapple.com.cn/"):
            self.assertFalse(official_url(url))

    def test_bad_budget_fails_config_loading(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as f:
            f.write("checkout:\n  max_total_cny: 18000\n")
            f.flush()
            with self.assertRaises(ValueError):
                load_config(f.name)


class CheckoutFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.config = AppConfig()
        self.config.checkout.auto_continue = True
        self.buyer = AppleStoreBuyer(self.page, self.config, self.temp.name)
        self.price = "RMB 17,999"
        self.quantity = "1"
        self.extra_item = ""
        self.security = ""
        self.clicks = []
        async def record(route):
            self.clicks.append(route.request.url)
            await route.fulfill(body="ok")
        await self.page.route("**/record/*", record)
        async def respond(route):
            url = route.request.url
            if "/record/" in url:
                await route.fallback()
                return
            if url == URL:
                catalog = {"products": [{"partNumber": "MK2P4CH/A", "dimensionColor": "starwhite", "dimensionCapacity": "512gb", "fullPrice": "p1", "comingSoon": False}],
                           "displayValues": {"dimensionColor": {"starwhite": {"value": "星光白色"}}, "dimensionCapacity": {"512gb": {"value": "512GB"}}, "prices": {"p1": {"currentPrice": {"amount": self.price}}}}}
                body = ('<input name="dimensionColor" value="starwhite" type="radio" checked>'
                        '<input name="dimensionCapacity" value="512gb" type="radio" checked>'
                        '<script>window.PRODUCT_SELECTION_BOOTSTRAP=' + json.dumps({"productSelectionData": catalog}) + '</script>'
                        '<button data-autom="continueButton" onclick="location.href=\'' + BAG + '\'">继续</button>')
            elif url == BAG:
                body = (self.security + '<div data-autom="bag-item">iPhone Duo 星光白色 512GB'
                        '<select data-autom="item-quantity-dropdown"><option selected>' + self.quantity + '</option></select></div>'
                        + self.extra_item + '<span data-autom="bag-total-value">' + self.price + '</span>'
                        '<button data-autom="checkout" onclick="location.href=\'' + SHIPPING + '\'">结账</button>')
            elif url == SHIPPING:
                body = ('<input required value="saved address">'
                        '<button data-autom="shipping-continue-button" onclick="location.href=\'' + BILLING + '\'">继续前往付款</button>')
            else:
                body = ('<label><input type="radio" name="paymentMethod" value="wechatpay">微信支付</label>'
                        '<button data-autom="place-order" onclick="fetch(\'/record/final\')">现在下订单</button>')
            await route.fulfill(content_type="text/html; charset=utf-8", body=body)
        await self.page.route("https://**apple.com.cn/**", respond)
        await self.page.goto(URL)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.temp.cleanup()

    async def test_known_flow_selects_wechat_and_never_submits(self):
        ok, reason = await advance_checkout(self.buyer, initial_continue=True)
        self.assertTrue(ok, reason)
        self.assertEqual(self.page.url, BILLING, reason)
        self.assertTrue(await self.page.locator("input[value=wechatpay]").is_checked())
        self.assertIn("本人核对并提交订单", reason)
        self.assertFalse(self.clicks)

    async def test_cart_positive_evidence(self):
        await self.page.goto(BAG)
        self.assertTrue(await cart_is_single_target(self.page, self.config),
                        await self.page.locator('body').inner_text())

    async def test_over_budget_product_is_not_clicked(self):
        self.price = "RMB 18,000"
        await self.page.reload()
        ok, reason = await advance_checkout(self.buyer, initial_continue=True)
        self.assertFalse(ok)
        self.assertEqual(self.page.url, URL)
        self.assertIn("预算", reason)

    async def test_unknown_price_is_not_clicked(self):
        self.price = "每月 749.96"
        await self.page.reload()
        ok, _ = await advance_checkout(self.buyer, initial_continue=True)
        self.assertFalse(ok)
        self.assertEqual(self.page.url, URL)

    async def test_extra_cart_item_stops_before_checkout(self):
        self.extra_item = '<div data-autom="bag-item">unrelated item</div>'
        ok, reason = await advance_checkout(self.buyer, initial_continue=True)
        self.assertTrue(ok)
        self.assertEqual(self.page.url, BAG)
        self.assertIn("购物袋", reason)

    async def test_quantity_two_stops_before_checkout(self):
        self.quantity = "2"
        await advance_checkout(self.buyer, initial_continue=True)
        self.assertEqual(self.page.url, BAG)

    async def test_authentication_requires_human(self):
        self.security = '<input type="password">'
        _, reason = await advance_checkout(self.buyer, initial_continue=True)
        self.assertEqual(self.page.url, BAG)
        self.assertIn("安全验证", reason)

    async def test_future_launch_does_not_click(self):
        self.config.launch.launch_time = datetime.fromtimestamp(4102444800, timezone.utc).isoformat()
        ok, _ = await advance_checkout(self.buyer, initial_continue=True)
        self.assertFalse(ok)
        self.assertEqual(self.page.url, URL)

    async def test_uncertain_click_is_attempted_only_once(self):
        calls = 0
        async def uncertain_click(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise TimeoutError("response lost")
        with patch("playwright.async_api.Locator.click", uncertain_click):
            ok, reason = await advance_checkout(self.buyer, initial_continue=True)
        self.assertFalse(ok)
        self.assertEqual(calls, 1)
        self.assertIn("不确定", reason)

    async def test_continue_never_falls_through_to_a_second_cart_submit(self):
        await self.page.locator('button').evaluate('''el => {
            el.onclick = () => {
                el.remove();
                const add = document.createElement('button');
                add.dataset.autom = 'add-to-cart';
                add.textContent = '添加到购物袋';
                add.onclick = () => window.duplicateAdd = true;
                document.body.append(add);
            };
        }''')
        await advance_checkout(self.buyer, initial_continue=True)
        self.assertIsNot(await self.page.evaluate('window.duplicateAdd === true'), True)

    async def test_required_address_never_guessed(self):
        await self.page.goto(SHIPPING)
        await self.page.locator("input").fill("")
        _, reason = await advance_checkout(self.buyer)
        self.assertEqual(self.page.url, SHIPPING)
        self.assertIn("必填", reason)

    async def test_terms_are_not_accepted_automatically(self):
        await self.page.goto(SHIPPING)
        await self.page.set_content('<input type="checkbox" name="terms"><button data-autom="shipping-continue-button">继续</button>')
        _, reason = await advance_checkout(self.buyer)
        self.assertIn("协议", reason)
        self.assertFalse(await self.page.locator("input").is_checked())

    async def test_maintenance_waits_until_launch_then_checks_live_form(self):
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = URL
        page.goto = AsyncMock(return_value=MagicMock(status=200))
        page.locator.return_value.count = AsyncMock(side_effect=[0, 1])
        page.locator.return_value.inner_text = AsyncMock(return_value="来得早，来得好。北京时间晚8点接受预购")
        self.config.launch.launch_time = datetime.fromtimestamp(4102444800, timezone.utc).isoformat()
        with patch("src.checkout.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await wait_for_storefront(page, self.config)
        self.assertEqual(sleep.await_count, 1)
        self.assertGreater(sleep.await_args.args[0], 0)
        self.assertEqual(page.goto.await_count, 2)
