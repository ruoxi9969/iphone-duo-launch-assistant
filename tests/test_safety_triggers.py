"""
Unit tests for V0.4.1 Safety Triggers and Payment Context.

Tests required by prompt:
- Test A: 商品页存在“分期付款”营销文本 -> NO SAFETY STOP (None)
- Test B: 购物袋页面存在付款方式说明文字 -> NO SAFETY STOP (None)
- Test C: 真实 checkout payment context 中出现支付方式按钮 -> HUMAN_ACTION_REQUIRED
- Test D: 真实“提交订单/立即付款”按钮出现 -> HUMAN_ACTION_REQUIRED
"""

import unittest
from playwright.async_api import async_playwright
from src.apple_store import is_payment_context, check_safety_boundary


class TestSafetyTriggers(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_a_product_page_installment_marketing_no_safety_stop(self):
        """
        Test A: 商品页存在“分期付款”营销文本。
        预期: NO SAFETY STOP
        """
        target_url = "https://www.apple.com.cn/shop/buy-iphone/iphone-17"
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>购买 iPhone 17 - Apple (中国大陆)</title></head>
        <body>
            <h1>购买 iPhone 17</h1>
            <div class="marketing-banner">
                <p>最高可享 24 期免息分期付款，折合每月仅需 RMB 299 起。</p>
                <button type="button" class="btn-learn-more">分期付款方案详情</button>
            </div>
            <div class="color-selection">
                <input type="radio" name="dimensionColor" value="white" id="color-white" checked />
                <label for="color-white">白色</label>
            </div>
        </body>
        </html>
        """
        await self.page.route(target_url, lambda r: r.fulfill(body=html, content_type="text/html"))
        await self.page.goto(target_url)

        # 1. 核验上下文为 False
        in_ctx = await is_payment_context(self.page)
        self.assertFalse(in_ctx, "商品页绝不应被判定为 PAYMENT_CONTEXT")

        # 2. 核验安全边界不触发停机
        safety_reason = await check_safety_boundary(self.page)
        self.assertIsNone(safety_reason, "商品页中的分期付款营销文本绝不应触发安全熔断停机 (NO SAFETY STOP)")

    async def test_b_shopping_bag_payment_hints_no_safety_stop(self):
        """
        Test B: 购物袋页面存在付款方式说明文字。
        预期: NO SAFETY STOP
        """
        target_url = "https://www.apple.com.cn/shop/bag"
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>购物袋 - Apple (中国大陆)</title></head>
        <body>
            <h1>你的购物袋总计 RMB 8,799</h1>
            <div class="bag-payment-summary">
                <p>我们支持多种付款方式：微信支付、支付宝、银联信用卡或借记卡以及花呗分期付款。</p>
                <button type="button" class="help-btn">了解分期付款</button>
            </div>
            <button id="shoppingCart.actions.checkout">结账</button>
        </body>
        </html>
        """
        await self.page.route(target_url, lambda r: r.fulfill(body=html, content_type="text/html"))
        await self.page.goto(target_url)

        # 1. 核验上下文为 False
        in_ctx = await is_payment_context(self.page)
        self.assertFalse(in_ctx, "单纯购物袋页面绝不应被判定为 PAYMENT_CONTEXT")

        # 2. 核验安全边界不触发停机
        safety_reason = await check_safety_boundary(self.page)
        self.assertIsNone(safety_reason, "购物袋页面的支付说明文字绝不应误触发停机 (NO SAFETY STOP)")

    async def test_c_checkout_payment_context_with_payment_buttons(self):
        """
        Test C: 真实 checkout payment context 中出现支付方式按钮。
        预期: HUMAN_ACTION_REQUIRED
        """
        target_url = "https://www.apple.com.cn/shop/checkout/billing"
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>结账 - 付款方式 - Apple (中国大陆)</title></head>
        <body>
            <h1>选择付款方式</h1>
            <div id="payment-options-container" class="rs-payment-options">
                <fieldset>
                    <legend>付款方式</legend>
                    <button type="button" class="payment-method-btn">支付宝</button>
                    <button type="button" class="payment-method-btn">微信支付</button>
                    <button type="button" class="payment-method-btn">分期付款</button>
                </fieldset>
            </div>
        </body>
        </html>
        """
        await self.page.route(target_url, lambda r: r.fulfill(body=html, content_type="text/html"))
        await self.page.goto(target_url)

        # 1. 核验上下文为 True
        in_ctx = await is_payment_context(self.page)
        self.assertTrue(in_ctx, "checkout/billing 路径与容器必须被判定为 PAYMENT_CONTEXT=True")

        # 2. 核验安全边界立即触发熔断停机
        safety_reason = await check_safety_boundary(self.page)
        self.assertIsNotNone(safety_reason, "真实支付上下文中的支付按钮必须触发熔断 (HUMAN_ACTION_REQUIRED)")
        self.assertIn("payment_selection", safety_reason)

    async def test_d_checkout_irreversible_place_order_button(self):
        """
        Test D: 真实“提交订单/立即付款”按钮出现。
        预期: HUMAN_ACTION_REQUIRED
        """
        target_url = "https://www.apple.com.cn/shop/checkout/review"
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>结账 - 核对订单 - Apple (中国大陆)</title></head>
        <body>
            <h1>核对并提交订单</h1>
            <div class="order-final-actions">
                <p>点击下方按钮即表示您同意 Apple 销售条款并立即扣款。</p>
                <button type="button" data-autom="place-order" id="placeOrderBtn" class="btn-primary">立即付款</button>
            </div>
        </body>
        </html>
        """
        await self.page.route(target_url, lambda r: r.fulfill(body=html, content_type="text/html"))
        await self.page.goto(target_url)

        # 1. 核验安全边界立即触发不可逆订单熔断
        safety_reason = await check_safety_boundary(self.page)
        self.assertIsNotNone(safety_reason, "真实立即付款/提交订单按钮必须硬熔断 (HUMAN_ACTION_REQUIRED)")
        self.assertIn("final_order_or_payment", safety_reason)


if __name__ == "__main__":
    unittest.main()
