"""Apple 中国大陆官网语义化选择器仓库

集中管理所有页面元素定位逻辑，优先使用语义化选择器 (role, aria, text, data-autom)
以便 Apple 前端更新 DOM 时快速维护，避免硬编码物理坐标。
"""

from typing import Dict, List

class Selectors:
    """Apple 中国大陆官网选择器映射表"""

    # --- 1. 页面加载与就绪标识 ---
    PAGE_READY_INDICATORS = [
        "fieldset",
        "h1.rf-bfe-header",
        "h1:has-text('购买')",
        "main#main",
    ]

    # --- 2. 机型选择器 ---
    # 若页面包含多机型切换（例如 6.1 英寸 vs 6.7 英寸）
    MODEL_SELECTORS = [
        "input[name='dimensionScreensize'][value='6_1inch']",
        "input[name='dimensionScreensize']",
        "label:has-text('iPhone')",
    ]

    # --- 3. 颜色/外观映射 (针对中文名与标准 value) ---
    COLOR_VALUE_MAP: Dict[str, str] = {
        "白色": "white",
        "白色钛金属": "white",
        "黑色": "black",
        "黑色钛金属": "black",
        "星光白色": "starwhite",
        "星光白": "starwhite",
        "starwhite": "starwhite",
        "夜空色": "nightsky",
        "夜空": "nightsky",
        "nightsky": "nightsky",
        "沙漠色": "desert",
        "沙漠色钛金属": "desert",
        "原色": "natural",
        "原色钛金属": "natural",
        "青雾蓝色": "mistblue",
        "薰衣草紫色": "lavender",
        "鼠尾草绿色": "sage",
        "深青色": "teal",
        "群青色": "ultramarine",
        "粉色": "pink",
    }

    # --- 4. 存储容量映射 ---
    STORAGE_VALUE_MAP: Dict[str, str] = {
        "128gb": "128gb",
        "128GB": "128gb",
        "256gb": "256gb",
        "256GB": "256gb",
        "512gb": "512gb",
        "512GB": "512gb",
        "1tb": "1tb",
        "1TB": "1tb",
        "2tb": "2tb",
        "2TB": "2tb",
    }

    # --- 5. Apple Trade In 换购服务 (选择“不折抵换购”) ---
    TRADE_IN_NONE = [
        "input[value='noTradeIn']",
        "input[data-autom='choose-noTradeIn']",
        "label[for='noTradeIn']",
        "label:has-text('不折抵换购')",
        "label:has-text('没有折抵换购')",
    ]

    # --- 6. AppleCare+ 服务计划 (选择“不加 AppleCare+”) ---
    APPLECARE_NONE = [
        "label:has-text('不加 AppleCare+')",
        "label:has-text('不加 AppleCare+ 服务计划')",
        "input[value*='no_applecare']",
    ]

    # --- 7. 付款方案 (若存在全款购买选择) ---
    PAYMENT_FULL = [
        "input[value='fullPrice']",
        "label:has-text('全款')",
        "label:has-text('一次性付款')",
    ]

    # --- 8. 添加到购物袋按钮 ---
    ADD_TO_BAG = [
        "button[name='add-to-cart']",
        "button[data-autom='add-to-cart']",
        "button:has-text('添加到购物袋')",
        "button:has-text('预购')",
        "button[data-autom='addToCart']",
    ]

    # --- 9. 购物袋与结账引导 ---
    REVIEW_BAG = [
        "button:has-text('查看购物袋')",
        "a:has-text('查看购物袋')",
        "button[name='proceed']",
        "a[href*='/shop/bag']",
    ]

    CHECKOUT_BUTTONS = [
        "button#shoppingCart\\.actions\\.checkout",
        "button[id='shoppingCart.actions.checkout']",
        "button[data-autom='checkout']",
        "button:has-text('结账')",
    ]

    GUEST_CHECKOUT_BUTTONS = [
        "button#signIn\\.guestLogin\\.guestCheckout",
        "button[id='signIn.guestLogin.guestCheckout']",
        "button:has-text('以访客身份继续')",
        "button:has-text('访客结账')",
    ]

    # --- 10. 允许自动推进的普通结账步骤 (配送方式 / 地址继续) ---
    SAFE_CHECKOUT_CONTINUE_BUTTONS = [
        "button[id*='shippingOptions.addressSelector']",
        "button[id*='shippingOptions.continue']",
        "button:has-text('继续前往送货地址')",
        "button:has-text('继续前往送货选项')",
        "button[data-autom='shipping-continue-button']",
    ]

    # --- 11. 强制安全熔断分类定义 (V0.4.1 精准上下文重构) ---
    # A. 身份安全门禁 (全域无条件生效，任何页面遇到必须停机交接人工)
    AUTH_AND_SECURITY_TRIGGERS: Dict[str, List[str]] = {
        "apple_id_login_or_password": [
            "iframe#aid-auth-widget",
            "input#account_name_text_field",
            "input#password_text_field",
            "button:has-text('登录并继续')",
            "h1:has-text('登录以更快结账')",
            "h2:has-text('登录以更快结账')",
            "input[type='password']",
        ],
        "2fa_verification": [
            "div.security-code-input",
            "input[id*='char-digit']",
            "input[id*='security-code']",
            "h1:has-text('输入双重认证验证码')",
            "h2:has-text('输入双重认证验证码')",
        ],
        "captcha_challenge": [
            "div#arkose",
            "iframe[src*='arkoselabs']",
            "iframe[src*='captcha']",
            "div[class*='challenge-container']",
            "h2:has-text('人机验证')",
        ],
    }

    # B. 真实支付上下文判断容器与控件 (用于判定 PAYMENT_CONTEXT == True)
    PAYMENT_CONTEXT_CONTAINERS: List[str] = [
        "[data-autom='payment-options']",
        "div#payment-options-container",
        "section[data-autom='payment-section']",
        "fieldset[data-autom='billing-payment-options']",
        "div[data-autom='checkout-payment']",
        "div.rs-payment-options",
        "div.rs-billing-payment",
        "form#payment-form",
        "form[name='paymentForm']",
        "fieldset:has(legend:has-text('付款方式'))",
        "fieldset:has(legend:has-text('支付方式'))",
    ]

    PAYMENT_INPUT_SELECTORS: List[str] = [
        "input[name='paymentMethod']",
        "input[name='billingOption']",
        "input[name='payment-type']",
        "input[value='alipay']",
        "input[value='wechatpay']",
        "input[value='chinaUnionPay']",
        "input[value='installment']",
        "input[value='creditCard']",
        "input[name='installmentPlan']",
        "select[name='paymentMethod']",
    ]

    # C. 支付方式选择触发器 (仅在 PAYMENT_CONTEXT == True 时才触发安全熔断)
    PAYMENT_SELECTION_TRIGGERS: List[str] = [
        "input[name='paymentMethod']",
        "button:has-text('前往支付宝付款')",
        "button:has-text('前往微信付款')",
        "button:has-text('前往银联付款')",
        "button:has-text('Apple Pay')",
        "button:has-text('微信支付')",
        "button:has-text('支付宝')",
        "button:has-text('分期付款')",
        "button:has-text('银行卡')",
        "section[data-autom='payment-section']",
        "div#payment-options-container",
    ]

    # D. 最终不可逆订单提交/付款动作 (必须位于真实操作控件)
    FINAL_ORDER_AND_PAYMENT_TRIGGERS: List[str] = [
        "button:has-text('立即付款')",
        "button:has-text('支付')",
        "button:has-text('确认并付款')",
        "button:has-text('提交订单')",
        "button:has-text('下订单')",
        "button:has-text('完成订单')",
        "button[data-autom='place-order']",
        "button[id*='placeOrder']",
        "button[name='place-order']",
        "button[id*='continueToReview']",
    ]

    # 保持向后兼容的汇总映射
    SAFETY_TRIGGERS: Dict[str, List[str]] = {
        **AUTH_AND_SECURITY_TRIGGERS,
        "payment_selection": PAYMENT_SELECTION_TRIGGERS,
        "final_order_or_payment": FINAL_ORDER_AND_PAYMENT_TRIGGERS,
    }

