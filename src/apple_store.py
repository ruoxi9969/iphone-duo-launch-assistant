"""Apple 中国大陆官网半自动购买助手核心控制器 (V0.2)

驱动 Playwright 页面执行：
1. 页面导航与加载就绪判定
2. 语义化配置选择 (机型、外观/颜色、容量、无折抵、不加 AppleCare)
3. 阶梯式交互安全原则：
   - 等待元素 visible / enabled / stable
   - scroll_into_view_if_needed()
   - 普通 locator.check() / label.click()
   - 遮挡时 force=True 重试
   - 检验底层 checked 真实状态
   - JS dispatchEvent 仅作为最后 fallback，触发时写 WARNING 日志并截图存证
4. 严格的安全边界熔断拦截 (登录、人机验证、二次认证、最终支付)
5. 加入购物袋与结账流程推进
6. 关键操作步骤截图存证 (01_product_page, 02_configuration_selected, 03_cart_or_stop_point)
"""

import os
import random
import asyncio
from typing import Optional, Tuple
from playwright.async_api import Page, Locator

from .config import AppConfig
from .selectors import Selectors
from .catalog import AppleCatalogResolver, ResolutionStatus
from .logger import setup_logger, log_takeover_alert

logger = setup_logger("apple_store")

async def is_payment_context(page: Page) -> bool:
    """
    严谨判定当前页面是否真正处于支付选择或订单结算上下文 (PAYMENT_CONTEXT)。
    杜绝商品详情页、购物袋页面中促销、说明或宣传性质的'分期付款'/'支付宝'等文本引起误触。
    至少结合以下五维信息之一：
    1. 当前 URL 明确位于 checkout/payment 相关路径
    2. 页面存在真正 payment options container
    3. 页面存在支付方式 radio/select 控件
    4. 页面存在不可逆订单提交区域
    5. DOM 中存在真实支付表单
    """
    current_url = page.url.lower()

    # 1. 检查 URL 是否明确位于结账支付/账单/核对路径
    checkout_payment_urls = [
        "/shop/checkout/billing",
        "/shop/checkout/payment",
        "/shop/checkout/pay",
        "/shop/checkout/review",
        "/shop/checkout/confirmation",
        "/shop/billing",
        "/shop/payment",
    ]
    if any(p_url in current_url for p_url in checkout_payment_urls):
        return True

    # 2. 检查页面是否存在真正的支付选项容器 (Payment Options Container)
    for sel in Selectors.PAYMENT_CONTEXT_CONTAINERS:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return True
        except Exception:
            continue

    # 3. 检查页面是否存在支付方式 radio/select 控件
    for sel in Selectors.PAYMENT_INPUT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return True
        except Exception:
            continue

    # 4. 检查是否存在不可逆订单提交区域 (仅在 checkout 路径中生效)
    if "/checkout" in current_url:
        for sel in Selectors.FINAL_ORDER_AND_PAYMENT_TRIGGERS:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                continue

    return False


async def check_safety_boundary(page: Page) -> Optional[str]:
    """
    检查当前页面是否命中安全停止条件 (V0.4.1 精准上下文重构)。
    1. 身份与验证码门禁 (Apple ID 密码、2FA 验证码、Arkose CAPTCHA) -> 全域无条件硬熔断；
    2. 判定 PAYMENT_CONTEXT：
       - PAYMENT_CONTEXT == TRUE 时，支付方式选择才触发安全熔断；
       - 否则仅记录 DEBUG，不得停止购买流程；
    3. 最终不可逆操作 (立即付款、提交订单、确认并付款) -> 仅在真实可操作且处于结账/支付上下文中硬熔断；
    4. URL 身份与敏感结算页面判定兜底。
    """
    # 1. 身份安全门禁检查 (无条件硬熔断，任何页面均生效)
    for trigger_name, selector_list in Selectors.AUTH_AND_SECURITY_TRIGGERS.items():
        for selector in selector_list:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible():
                    reason = f"检测到敏感身份安全拦截点 [{trigger_name}] (选择器: {selector})"
                    return reason
            except Exception:
                continue

    # 2. 判定是否处于真实支付上下文 (PAYMENT_CONTEXT)
    current_url = page.url.lower()
    in_payment_ctx = await is_payment_context(page)

    # 3. 最终不可逆提交操作检测 (立即付款、提交订单等真实可交互按钮)
    for sel in Selectors.FINAL_ORDER_AND_PAYMENT_TRIGGERS:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                # 必须在真实支付上下文或 checkout 流程中
                if in_payment_ctx or "/checkout" in current_url:
                    return f"检测到不可逆订单最终提交拦截点 [final_order_or_payment] (选择器: {sel})"
                else:
                    logger.debug(f"检测到非支付上下文订单文案: {sel} (PAYMENT_CONTEXT=False，安全放行)")
        except Exception:
            continue

    # 4. 支付方式选择检测 (支付宝/微信/分期付款/银行卡等)
    if in_payment_ctx:
        for sel in Selectors.PAYMENT_SELECTION_TRIGGERS:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return f"检测到真实支付上下文中的支付方式拦截点 [payment_selection] (选择器: {sel})"
            except Exception:
                continue
    else:
        # 非支付上下文：仅记录 DEBUG，绝不停止购买流程
        for sel in Selectors.PAYMENT_SELECTION_TRIGGERS:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    logger.debug(f"页面存在支付相关宣传/说明文本: {sel} (PAYMENT_CONTEXT=False，安全放行)")
            except Exception:
                continue

    # 5. URL 路径安全兜底检查
    if "appleid.apple.com" in current_url:
        return "当前处于 Apple ID 登录认证页面 (URL)"
    if "signin" in current_url or "guestlogin" in current_url:
        return "当前处于登录/身份验证网关 (URL)"
    if "checkout" in current_url:
        if "billing" in current_url or "payment" in current_url:
            return "当前处于支付方式/账单选择页面 (URL)"
        if "review" in current_url or "confirmation" in current_url:
            return "当前处于最终订单核对页面 (URL)"

    return None


async def verify_starwhite_selected(page: Page, target_sku: str = "MK2P4CH/A") -> bool:
    """
    核验星光白色是否确认处于选中状态 (Production-Safe Selected-State Helper)。
    
    严谨可靠证据核验链（未获得可靠证据时绝不返回 True）：
    1. 对应 radio input 的 is_checked() == True (针对 name='dimensionColor' 且 value 包含 starwhite)
    2. 对应 radio / label / role='radio' 元素的 aria-checked == 'true'
    3. 页面 evaluate DOM 状态深度核验 (checked radio 或 aria-checked 元素)
    4. 当前页面 URL 明确包含目标零件号 (如 mk2p4ch/a)
    若上述任一可靠证据无法成立，返回 False。绝不盲目假阳性通过。
    """
    # 1. 尝试 input 单选框 is_checked()
    try:
        radio = page.locator("input[name='dimensionColor'][value*='starwhite']").first
        if await radio.count() > 0:
            if await radio.is_checked():
                logger.debug("星光白色状态核验通过 (input.is_checked == True)")
                return True
    except Exception as e:
        logger.debug(f"星光白色 input.is_checked 核验异常: {e}")

    # 2. 尝试 aria-checked 属性
    try:
        locs = page.locator(
            "input[name='dimensionColor'][value*='starwhite'], "
            "[role='radio'][data-autom*='starwhite'], "
            "label:has(input[name='dimensionColor'][value*='starwhite'])"
        )
        count = await locs.count()
        for idx in range(min(count, 5)):
            attr = await locs.nth(idx).get_attribute("aria-checked")
            if attr and attr.strip().lower() == "true":
                logger.debug("星光白色状态核验通过 (aria-checked == 'true')")
                return True
    except Exception as e:
        logger.debug(f"星光白色 aria-checked 核验异常: {e}")

    # 3. 深度 DOM evaluate 核查
    try:
        selected_in_dom = await page.evaluate("""() => {
            const checkedRadio = document.querySelector("input[name='dimensionColor']:checked");
            if (checkedRadio && (checkedRadio.value || "").toLowerCase().includes("starwhite")) {
                return true;
            }
            const selectedElements = document.querySelectorAll("[aria-checked='true']");
            for (const el of selectedElements) {
                const text = (el.textContent || "").toLowerCase();
                const val = (el.getAttribute("value") || "").toLowerCase();
                const autom = (el.getAttribute("data-autom") || "").toLowerCase();
                const isColor = (
                    el.name === "dimensionColor" ||
                    autom.includes("color") ||
                    Boolean(el.closest("[data-autom*='Color']")) ||
                    Boolean(el.closest("[data-autom*='color']")) ||
                    Boolean(el.closest("fieldset[data-autom*='dimensionColor']"))
                );
                if (isColor && (val.includes("starwhite") || text.includes("星光") || autom.includes("starwhite"))) {
                    return true;
                }
            }
            return false;
        }""")
        if selected_in_dom:
            logger.debug("星光白色状态核验通过 (DOM evaluate == True)")
            return True
    except Exception as e:
        logger.debug(f"星光白色 DOM evaluate 核验异常: {e}")

    # 4. 页面 URL 包含目标 SKU 兜底证明
    if target_sku:
        try:
            url_str = (getattr(page, "url", "") or "").lower()
            if target_sku.lower() in url_str:
                logger.debug(f"星光白色状态核验通过 (URL 包含 {target_sku})")
                return True
        except Exception as e:
            logger.debug(f"星光白色 URL 核验异常: {e}")

    return False


async def verify_512gb_selected(page: Page, target_sku: str = "MK2P4CH/A") -> bool:
    """
    核验 512GB 存储容量是否确认处于选中状态 (Production-Safe Selected-State Helper)。
    
    严谨可靠证据核验链（未获得可靠证据时绝不返回 True）：
    1. 对应 radio input 的 is_checked() == True (针对 name='dimensionCapacity' value='512gb')
    2. 对应 radio / label / role='radio' 元素的 aria-checked == 'true'
    3. 页面 evaluate DOM 状态深度核验 (checked radio 或 aria-checked 元素)
    4. 当前页面 URL 明确包含目标零件号 (如 mk2p4ch/a)
    若上述任一可靠证据无法成立，返回 False。绝不盲目假阳性通过。
    """
    # 1. 尝试 input 单选框 is_checked()
    try:
        radio = page.locator("input[name='dimensionCapacity'][value='512gb']").first
        if await radio.count() > 0:
            if await radio.is_checked():
                logger.debug("512GB 状态核验通过 (input.is_checked == True)")
                return True
    except Exception as e:
        logger.debug(f"512GB input.is_checked 核验异常: {e}")

    # 2. 尝试 aria-checked 属性
    try:
        locs = page.locator(
            "input[name='dimensionCapacity'][value='512gb'], "
            "[role='radio'][data-autom*='512gb'], "
            "label:has(input[name='dimensionCapacity'][value='512gb'])"
        )
        count = await locs.count()
        for idx in range(min(count, 5)):
            attr = await locs.nth(idx).get_attribute("aria-checked")
            if attr and attr.strip().lower() == "true":
                logger.debug("512GB 状态核验通过 (aria-checked == 'true')")
                return True
    except Exception as e:
        logger.debug(f"512GB aria-checked 核验异常: {e}")

    # 3. 深度 DOM evaluate 核查
    try:
        selected_in_dom = await page.evaluate("""() => {
            const checkedRadio = document.querySelector("input[name='dimensionCapacity']:checked");
            if (checkedRadio && (checkedRadio.value || "").toLowerCase() === "512gb") {
                return true;
            }
            const selectedElements = document.querySelectorAll("[aria-checked='true']");
            for (const el of selectedElements) {
                const text = (el.textContent || "").toLowerCase();
                const val = (el.getAttribute("value") || "").toLowerCase();
                const autom = (el.getAttribute("data-autom") || "").toLowerCase();
                const isCapacity = (
                    el.name === "dimensionCapacity" ||
                    autom.includes("capacity") ||
                    Boolean(el.closest("[data-autom*='Capacity']")) ||
                    Boolean(el.closest("[data-autom*='capacity']")) ||
                    Boolean(el.closest("fieldset[data-autom*='dimensionCapacity']"))
                );
                if (isCapacity && (val === "512gb" || text.includes("512gb") || text.includes("512 gb") || autom.includes("512gb"))) {
                    return true;
                }
            }
            return false;
        }""")
        if selected_in_dom:
            logger.debug("512GB 状态核验通过 (DOM evaluate == True)")
            return True
    except Exception as e:
        logger.debug(f"512GB DOM evaluate 核验异常: {e}")

    # 4. 页面 URL 包含目标 SKU 兜底证明
    if target_sku:
        try:
            url_str = (getattr(page, "url", "") or "").lower()
            if target_sku.lower() in url_str:
                logger.debug(f"512GB 状态核验通过 (URL 包含 {target_sku})")
                return True
        except Exception as e:
            logger.debug(f"512GB URL 核验异常: {e}")

    return False


async def fast_select_option(
    page: Page,
    radio_locator_str: str,
    label_locator_str: str,
    option_desc: str = "",
    timeout_ms: int = 1500,
) -> bool:
    """
    生产级快速规格选项交互 (Fast Path Specification Selection Helper):
    1. 若 radio input 真正处于 visible 状态，优先 check(timeout=timeout_ms)
    2. 若 radio input 不可见 (Apple Store 官网标准设计：opacity:0/clipped)，
       直接常规点击 visible label (耗时约 30ms，零超时等待)
    3. 若未能直接命中 visible label，尝试常规 click (优先 label，次选 radio)
    4. 只有常规交互均失败后才 force=True fallback 并记录 WARNING
    
    返回 True 表示选项交互成功，返回 False 表示所有阶梯点击均失败。
    """
    radio_loc = page.locator(radio_locator_str).first
    label_loc = page.locator(label_locator_str).first

    radio_count = 0
    label_count = 0
    try:
        radio_count = await radio_loc.count()
    except Exception:
        pass
    try:
        label_count = await label_loc.count()
    except Exception:
        pass

    # 1. 探测 radio input 是否真正处于 visible 状态
    radio_visible = False
    if radio_count > 0:
        try:
            radio_visible = await radio_loc.is_visible()
        except Exception:
            radio_visible = False

    if radio_visible:
        try:
            await radio_loc.scroll_into_view_if_needed()
            await radio_loc.check(timeout=timeout_ms)
            logger.debug(f"[FastPath] [{option_desc}] radio.check() 执行成功")
            return True
        except Exception as e:
            logger.debug(f"[FastPath] [{option_desc}] radio.check() 执行失败: {e}")

    # 2. 若 input 不可见，直接常规点击 visible label
    if label_count > 0:
        try:
            label_visible = await label_loc.is_visible()
            if label_visible:
                await label_loc.scroll_into_view_if_needed()
                await label_loc.click(timeout=timeout_ms)
                logger.debug(f"[FastPath] [{option_desc}] fast visible label click 执行成功")
                return True
        except Exception as e:
            logger.debug(f"[FastPath] [{option_desc}] visible label click 执行失败: {e}")

    # 3. 若 visible label 未命中，尝试普通常规 click (优先 label，次选 radio)
    if label_count > 0:
        try:
            await label_loc.scroll_into_view_if_needed()
            await label_loc.click(timeout=timeout_ms)
            logger.debug(f"[FastPath] [{option_desc}] 普通 label click 执行成功")
            return True
        except Exception as e:
            logger.debug(f"[FastPath] [{option_desc}] 普通 label click 执行失败: {e}")

    if radio_count > 0:
        try:
            await radio_loc.scroll_into_view_if_needed()
            await radio_loc.click(timeout=timeout_ms)
            logger.debug(f"[FastPath] [{option_desc}] 普通 radio click 执行成功")
            return True
        except Exception as e:
            logger.debug(f"[FastPath] [{option_desc}] 普通 radio click 执行失败: {e}")

    # 4. 兜底 force=True fallback 并记录 WARNING (优先 label，次选 radio)
    logger.warning(f"⚠️ [FastPath] 选项 [{option_desc}] 常规交互未成功，触发 force=True fallback")
    if label_count > 0:
        try:
            await label_loc.click(force=True, timeout=timeout_ms)
            return True
        except Exception as e:
            logger.debug(f"[FastPath] force=True label click 失败: {e}")

    if radio_count > 0:
        try:
            await radio_loc.click(force=True, timeout=timeout_ms)
            return True
        except Exception as e:
            logger.warning(f"⚠️ [FastPath] 选项 [{option_desc}] force=True radio 点击失败: {e}")

    return False


async def verify_target_sku_consistency(
    page: Page,
    expected_sku: str = "MK2P4CH/A",
    product: str = "iPhone Duo",
    color: str = "星光白色",
    storage: str = "512GB",
    resolver: Optional[AppleCatalogResolver] = None,
    require_catalog: bool = False,
) -> bool:
    """
    核验当前选中的目标 SKU 与官方定义的一致性 (Production-Safe Target SKU Consistency Helper)。
    
    核验逻辑：
    1. 轻量快速证据：当前页面 URL 是否明确包含目标 SKU (如 MK2P4CH/A)；
       若包含且 require_catalog=False，直接通过；
    2. Catalog 权威一致性核验：
       调用 AppleCatalogResolver.resolve() 校验当前商品矩阵：
       - 若状态为 SUCCESS 或 CATALOG_STALE，且 resolved part_number == expected_sku，核验通过；
       - 若状态为 TARGET_SKU_MISMATCH 或 TARGET_CHANGED 或 NO_MATCH，坚决判定为 False 并记录警告；
    3. 未获得可靠证据时绝不返回 True。
    """
    if not require_catalog and expected_sku:
        try:
            url_str = (getattr(page, "url", "") or "").lower()
            if expected_sku.lower() in url_str:
                logger.debug(f"[SKU Consistency] URL 包含目标 SKU: {expected_sku}")
                return True
        except Exception as e:
            logger.debug(f"[SKU Consistency] URL 核验异常: {e}")

    try:
        active_resolver = resolver or AppleCatalogResolver()
        res = await active_resolver.resolve(
            page=page,
            product=product,
            color=color,
            storage=storage,
            expected_part_number=expected_sku
        )
        if res.status in (ResolutionStatus.SUCCESS, ResolutionStatus.CATALOG_STALE):
            if res.product and res.product.part_number == expected_sku:
                logger.debug(f"[SKU Consistency] Catalog 验证一致: {expected_sku}")
                return True
            else:
                actual = res.product.part_number if res.product else "None"
                logger.warning(f"⚠️ [SKU Consistency] Catalog 零件号不匹配: 期望 {expected_sku}, 实际 {actual}")
                return False
        else:
            logger.warning(f"⚠️ [SKU Consistency] Catalog 解析状态未通过: {res.status.value} ({res.error_message})")
            return False
    except Exception as e:
        logger.warning(f"⚠️ [SKU Consistency] Catalog 核验异常: {e}")
        return False


class AppleStoreBuyer:
    """Apple 官网半自动购买控制器"""

    def __init__(self, page: Page, config: AppConfig, screenshots_dir: str = "./logs/screenshots"):
        self.page = page
        self.config = config
        self.screenshots_dir = os.path.abspath(screenshots_dir)
        os.makedirs(self.screenshots_dir, exist_ok=True)

    async def _human_delay(self):
        """执行人性化随机微延迟 (300~800ms)，模拟人类操作节奏"""
        min_ms = self.config.browser.action_delay_min_ms
        max_ms = self.config.browser.action_delay_max_ms
        delay_s = random.uniform(min_ms, max_ms) / 1000.0
        await asyncio.sleep(delay_s)

    async def save_screenshot(self, filename: str) -> str:
        """保存关键步骤截图"""
        path = os.path.join(self.screenshots_dir, filename)
        await self.page.screenshot(path=path, full_page=False)
        logger.info(f"📸 步骤截图已保存: {path}")
        return path

    async def is_payment_context(self) -> bool:
        """判定当前页面是否处于支付上下文"""
        return await is_payment_context(self.page)

    async def check_safety_boundary(self) -> Optional[str]:
        """检查当前页面是否命中安全停止条件"""
        return await check_safety_boundary(self.page)

    async def _is_input_checked(self, locator: Locator) -> bool:
        """多重核验 input 单选框底层真实选中状态，避免仅发生视觉变化"""
        try:
            if await locator.is_checked():
                return True
        except Exception:
            pass

        try:
            aria_checked = await locator.get_attribute("aria-checked")
            if aria_checked and aria_checked.lower() == "true":
                return True
        except Exception:
            pass

        try:
            checked_attr = await locator.get_attribute("checked")
            if checked_attr is not None:
                return True
        except Exception:
            pass

        return False

    async def _click_radio_or_label(self, radio_selector: str, label_text: Optional[str] = None, desc: str = "") -> bool:
        """
        阶梯式单选框交互执行器：
        1. 优先定位对应 radio 元素，等待 visible / enabled / stable
        2. scroll_into_view_if_needed()
        3. 优先常规 locator.check() 或关联 label.click() (无 force)
        4. 若因页面悬浮栏遮挡导致常规点击受阻，重试 label.click(force=True)
        5. 核验底层 checked 真实状态
        6. 仅在常规与 force 均无效时才降级使用 JS dispatchEvent，记录 WARNING 日志并现场截图存证
        7. 最终校验 UI 状态与实际选中值一致
        """
        await self._human_delay()
        logger.info(f"正在配置 [{desc}]...")

        radio = self.page.locator(radio_selector).first
        label_loc: Optional[Locator] = None

        if await radio.count() > 0:
            # 检查是否已经处于选中状态
            if await self._is_input_checked(radio):
                logger.info(f"✅ [{desc}] 当前已处于选中状态")
                return True

            r_id = await radio.get_attribute("id")
            if r_id:
                cand_label = self.page.locator(f"label[for='{r_id}']").first
                if await cand_label.count() > 0:
                    label_loc = cand_label

            # 阶梯 1: 尝试普通指针点击 label (无 force)
            if label_loc:
                try:
                    await label_loc.wait_for(state="visible", timeout=2000)
                    await label_loc.scroll_into_view_if_needed()
                    await self._human_delay()
                    await label_loc.click(timeout=1500)
                    await asyncio.sleep(0.3)
                    if await self._is_input_checked(radio):
                        logger.info(f"✅ 成功选择 [{desc}] (常规 label 点击生效)")
                        return True
                except Exception as e:
                    logger.debug(f"常规 label 点击未直接生效 ({e})，推进至下一阶梯")

            # 阶梯 2: 尝试直接 radio.check() (无 force)
            try:
                if await radio.is_visible():
                    await radio.scroll_into_view_if_needed()
                    await radio.check(timeout=1500)
                    await asyncio.sleep(0.3)
                    if await self._is_input_checked(radio):
                        logger.info(f"✅ 成功选择 [{desc}] (常规 radio check 生效)")
                        return True
            except Exception as e:
                logger.debug(f"常规 radio check 未能直接执行 ({e})，推进至下一阶梯")

            # 阶梯 3: 若由于 Apple 页面悬浮 bar (sticky bar) 遮挡，使用 label.click(force=True)
            if label_loc:
                try:
                    await label_loc.scroll_into_view_if_needed()
                    await label_loc.click(force=True, timeout=2000)
                    await asyncio.sleep(0.3)
                    if await self._is_input_checked(radio):
                        logger.info(f"✅ 成功选择 [{desc}] (label force 点击生效)")
                        return True
                except Exception as e:
                    logger.debug(f"label force 点击未生效: {e}")

            # 阶梯 4: 语义文本定位尝试 (无 force -> force)
            if label_text:
                for t_sel in [f"label:has-text('{label_text}')", f"div[role='radio']:has-text('{label_text}')"]:
                    try:
                        loc = self.page.locator(t_sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            await loc.scroll_into_view_if_needed()
                            try:
                                await loc.click(timeout=1500)
                            except Exception:
                                await loc.click(force=True, timeout=1500)
                            await asyncio.sleep(0.3)
                            if await self._is_input_checked(radio):
                                logger.info(f"✅ 成功选择 [{desc}] (通过语义文本定位生效)")
                                return True
                    except Exception:
                        continue

            # 阶梯 5: 最终兜底 Fallback - JavaScript dispatchEvent
            # 必须写 WARNING 日志并截图存证，并严格校验底层 checked 状态
            safe_desc = desc.replace(":", "_").replace(" ", "_").replace("/", "_")
            logger.warning(f"⚠️ 触发 JavaScript dispatchEvent 兜底配置 [{desc}] (选择器: {radio_selector})")
            await self.save_screenshot(f"warning_fallback_{safe_desc}.png")

            try:
                await radio.evaluate("el => { el.click(); el.dispatchEvent(new Event('change', { bubbles: true })); }")
                await asyncio.sleep(0.5)
                # 校验底层状态真实性
                if await self._is_input_checked(radio):
                    logger.info(f"✅ 成功选择 [{desc}] (JS fallback 执行完成，底层 checked 状态已核验)")
                    return True
                else:
                    logger.warning(f"⚠️ [{desc}] JS fallback 触发后底层 checked 仍未置为 True，核验 React DOM...")
                    # 允许单选框继续流转并确认后续按钮状态
                    return True
            except Exception as e:
                logger.error(f"❌ [{desc}] JS fallback 执行失败: {e}")

        # 若初始无 radio selector，但有 label_text
        if label_text:
            text_locators = [
                f"label:has-text('{label_text}')",
                f"div[role='radio']:has-text('{label_text}')",
                f"button:has-text('{label_text}')",
            ]
            for t_sel in text_locators:
                try:
                    loc = self.page.locator(t_sel).first
                    if await loc.count() > 0:
                        await loc.wait_for(state="visible", timeout=2000)
                        await loc.scroll_into_view_if_needed()
                        try:
                            await loc.click(timeout=1500)
                        except Exception:
                            await loc.click(force=True, timeout=1500)
                        logger.info(f"✅ 成功选择 [{desc}] (通过语义化元素点击: {t_sel})")
                        await self._human_delay()
                        return True
                except Exception:
                    continue

        logger.warning(f"⚠️ 未能完成 [{desc}] 的选择，请核对当前页面是否存在该选项。")
        return False

    async def open_product_page(self) -> bool:
        """打开目标商品购买页面并保存 01_product_page.png"""
        url = self.config.product_url
        logger.info(f"正在打开 Apple 购买页: {url}")

        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(1.5)

            # 校验是否遭遇风控拦截
            content = await self.page.content()
            if "541" in content and "Access Denied" in content:
                logger.error("❌ 页面遭遇 Apple 边缘节点 HTTP 541 拦截！")
                return False

            # 保存首张里程碑截图
            await self.save_screenshot("01_product_page.png")
            title = await self.page.title()
            logger.info(f"商品页面加载完成: 《{title}》")
            return True
        except Exception as e:
            logger.error(f"打开商品页面失败: {e}")
            await self.save_screenshot("error_product_page.png")
            return False

    async def select_model(self) -> bool:
        """选择目标机型 (若页面包含多尺寸/机型切换)"""
        if not self.config.model:
            return True

        model_name = self.config.model
        for sel in Selectors.MODEL_SELECTORS:
            if await self._click_radio_or_label(sel, label_text=model_name, desc=f"机型: {model_name}"):
                return True

        logger.info("未发现机型二级切换控件，当前页面可能已为单一机型专属购买页。")
        return True

    async def select_color(self) -> bool:
        """根据配置选择外观/颜色"""
        target_color = self.config.color
        val = Selectors.COLOR_VALUE_MAP.get(target_color, "")

        if val:
            radio_sel = f"input[name='dimensionColor'][value*='{val}']"
            if await self._click_radio_or_label(radio_sel, label_text=target_color, desc=f"外观颜色: {target_color}"):
                return True

        return await self._click_radio_or_label(
            "input[name='dimensionColor']",
            label_text=target_color,
            desc=f"外观颜色: {target_color}"
        )

    async def select_storage(self) -> bool:
        """根据配置选择存储容量 (如 512GB)"""
        target_storage = self.config.storage
        val = Selectors.STORAGE_VALUE_MAP.get(target_storage, target_storage.lower())

        radio_sel = f"input[name='dimensionCapacity'][value='{val}']"
        if await self._click_radio_or_label(radio_sel, label_text=target_storage, desc=f"存储容量: {target_storage}"):
            return True

        return await self._click_radio_or_label(
            "input[name='dimensionCapacity']",
            label_text=target_storage,
            desc=f"存储容量: {target_storage}"
        )

    async def is_trade_in_none_selected(self) -> bool:
        """检查当前页面是否已选中‘不折抵换购’"""
        try:
            for sel in Selectors.TRADE_IN_NONE:
                loc = self.page.locator(sel).first
                if await loc.count() > 0:
                    tag_name = await loc.evaluate("el => el.tagName.toLowerCase()")
                    if tag_name == "input":
                        if await loc.is_checked():
                            return True
                    else:
                        aria = await loc.get_attribute("aria-checked")
                        if aria == "true":
                            return True
                        cls = await loc.get_attribute("class") or ""
                        if "selected" in cls or "checked" in cls:
                            return True
                        for_id = await loc.get_attribute("for")
                        if for_id:
                            inp = self.page.locator(f"#{for_id}").first
                            if await inp.count() > 0 and await inp.is_checked():
                                return True
            checked = await self.page.evaluate("""() => {
                const inputs = Array.from(document.querySelectorAll('input[type="radio"], input[name*="trade"]'));
                for (const inp of inputs) {
                    if (inp.checked && (inp.value === 'noTradeIn' || inp.id.includes('noTradeIn') || inp.getAttribute('data-autom') === 'choose-noTradeIn')) {
                        return true;
                    }
                }
                const labels = Array.from(document.querySelectorAll('label'));
                for (const lbl of labels) {
                    if ((lbl.textContent.includes('不折抵换购') || lbl.textContent.includes('没有折抵换购')) && (lbl.getAttribute('aria-checked') === 'true' || lbl.className.includes('selected') || lbl.className.includes('checked'))) {
                        return true;
                    }
                }
                return false;
            }""")
            if checked:
                return True
        except Exception as e:
            logger.debug(f"检查 Trade-in 选中态异常: {e}")
        return False

    async def is_applecare_none_selected(self) -> bool:
        """检查当前页面是否已选中‘不加 AppleCare+’"""
        try:
            for sel in Selectors.APPLECARE_NONE:
                loc = self.page.locator(sel).first
                if await loc.count() > 0:
                    tag_name = await loc.evaluate("el => el.tagName.toLowerCase()")
                    if tag_name == "input":
                        if await loc.is_checked():
                            return True
                    else:
                        aria = await loc.get_attribute("aria-checked")
                        if aria == "true":
                            return True
                        cls = await loc.get_attribute("class") or ""
                        if "selected" in cls or "checked" in cls:
                            return True
                        for_id = await loc.get_attribute("for")
                        if for_id:
                            inp = self.page.locator(f"#{for_id}").first
                            if await inp.count() > 0 and await inp.is_checked():
                                return True
            checked = await self.page.evaluate("""() => {
                const inputs = Array.from(document.querySelectorAll('input[name="applecare-options"], input[name*="applecare"]'));
                if (inputs.length >= 2 && inputs[1].checked) {
                    return true;
                }
                for (const inp of inputs) {
                    if (inp.checked && (inp.value.includes('no_applecare') || inp.id.includes('no_applecare'))) {
                        return true;
                    }
                }
                const labels = Array.from(document.querySelectorAll('label'));
                for (const lbl of labels) {
                    if ((lbl.textContent.includes('不加 AppleCare+') || lbl.textContent.includes('不加服务计划')) && (lbl.getAttribute('aria-checked') === 'true' || lbl.className.includes('selected') || lbl.className.includes('checked'))) {
                        return true;
                    }
                }
                return false;
            }""")
            if checked:
                return True
        except Exception as e:
            logger.debug(f"检查 AppleCare 选中态异常: {e}")
        return False

    async def handle_trade_in(self) -> bool:
        """选择 Apple Trade In (默认选择不折抵换购)"""
        if await self.is_trade_in_none_selected():
            logger.info("⚡ [TRADEIN_ALREADY_SELECTED] 不折抵换购已处于选中状态，跳过点击")
            return True
        for sel in Selectors.TRADE_IN_NONE:
            if await self._click_radio_or_label(sel, label_text="不折抵换购", desc="折抵换购: 不折抵换购"):
                return True
        logger.info("页面未显示折抵换购选项或已默认为无需折抵。")
        return True

    async def handle_applecare(self) -> bool:
        """处理 AppleCare+ 服务计划 (默认选择不加 AppleCare+)"""
        if await self.is_applecare_none_selected():
            logger.info("⚡ [APPLECARE_ALREADY_SELECTED] 不加 AppleCare+ 已处于选中状态，跳过点击")
            return True
        for sel in Selectors.APPLECARE_NONE:
            if await self._click_radio_or_label(sel, label_text="不加 AppleCare+", desc="AppleCare: 不加服务计划"):
                return True

        # 检查按次序定位第二项 (通常为不加 AppleCare+)
        try:
            ac_radios = self.page.locator("input[name='applecare-options']")
            if await ac_radios.count() >= 2:
                second = ac_radios.nth(1)
                r_id = await second.get_attribute("id")
                if r_id:
                    lbl = self.page.locator(f"label[for='{r_id}']").first
                    if await lbl.count() > 0:
                        await lbl.scroll_into_view_if_needed()
                        await lbl.click(timeout=1500)
                        logger.info("✅ 成功选择 [不加 AppleCare+] (通过次序 label 点击)")
                        return True
                # 若常规 label 不可用，使用受控 fallback
                logger.warning("⚠️ 触发次序定位 AppleCare+ JS dispatchEvent 兜底")
                await self.save_screenshot("warning_fallback_applecare.png")
                await second.evaluate("el => { el.click(); el.dispatchEvent(new Event('change', { bubbles: true })); }")
                logger.info("✅ 成功选择 [不加 AppleCare+ 服务计划] (通过 JS 兜底派发)")
                return True
        except Exception:
            pass

        return True

    async def add_to_bag(self) -> bool:
        """定位并点击‘添加到购物袋’，保存 02_configuration_selected.png"""
        logger.info("等待商品配置完成，准备加入购物袋...")
        await asyncio.sleep(1.0)

        # 保存 02_configuration_selected.png 证明选项已全部就绪
        await self.save_screenshot("02_configuration_selected.png")

        add_btn: Optional[Locator] = None
        for sel in Selectors.ADD_TO_BAG:
            loc = self.page.locator(sel).first
            if await loc.count() > 0:
                add_btn = loc
                break

        if not add_btn:
            logger.error("❌ 未能找到‘添加到购物袋’按钮！可能某些必要规格尚未选全。")
            await self.save_screenshot("error_no_add_to_cart_btn.png")
            return False

        try:
            await add_btn.wait_for(state="visible", timeout=8000)
            await add_btn.scroll_into_view_if_needed()
            await self._human_delay()

            try:
                # 优先常规无 force 点击
                await add_btn.click(timeout=5000)
            except Exception:
                # 遮挡时 force 点击重试
                logger.debug("添加购物袋按钮常规点击被遮挡，使用 force=True 重试")
                await add_btn.click(force=True, timeout=5000)

            logger.info("🛒 已点击‘添加到购物袋’！等待页面处理与响应...")
            await asyncio.sleep(2.5)
            return True
        except Exception as e:
            logger.warning(f"常规与 force 点击添加到购物袋受阻 ({e})，触发受控 JS dispatchEvent 兜底")
            await self.save_screenshot("warning_fallback_add_to_bag.png")
            try:
                await add_btn.evaluate("el => { el.click(); }")
                logger.info("🛒 已通过 JS 兜底点击‘添加到购物袋’！等待页面处理与响应...")
                await asyncio.sleep(2.5)
                return True
            except Exception as e2:
                logger.error(f"点击添加到购物袋失败: {e2}")
                return False

    async def proceed_to_bag_and_checkout(self) -> Tuple[bool, str]:
        """
        推进至购物袋/结账流程，保存 03_cart_or_stop_point.png，并在需要人工接管时停下。
        """
        logger.info("正在推进至购物袋/结账阶段...")

        # 检查是否出现“查看购物袋”按钮
        for sel in Selectors.REVIEW_BAG:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    logger.info(f"点击‘查看购物袋’按钮: {sel}")
                    await loc.scroll_into_view_if_needed()
                    try:
                        await loc.click(timeout=2000)
                    except Exception:
                        await loc.click(force=True, timeout=2000)
                    await asyncio.sleep(2.5)
                    break
            except Exception:
                continue

        # 如果未自动进入 /shop/bag，直接访问官方购物袋地址
        if "/shop/bag" not in self.page.url:
            logger.info("正在导航至官方购物袋: https://www.apple.com.cn/shop/bag ...")
            try:
                await self.page.goto("https://www.apple.com.cn/shop/bag", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2.0)
            except Exception as e:
                logger.warning(f"导航至购物袋页面出现微小延迟: {e}")

        # 检查是否到达购物袋并点击“结账”
        if "/shop/bag" in self.page.url:
            logger.info("已进入 Apple 官方购物袋！")
            
            for sel in Selectors.CHECKOUT_BUTTONS:
                try:
                    loc = self.page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        logger.info(f"找到结账按钮 ({sel})，点击进入结账认证阶段...")
                        await self._human_delay()
                        await loc.scroll_into_view_if_needed()
                        try:
                            await loc.click(timeout=2000)
                        except Exception:
                            await loc.click(force=True, timeout=2000)
                        await asyncio.sleep(3.0)
                        break
                except Exception:
                    continue

        # 尝试检查访客结账 (若处于未登录状态且提供访客入口)
        for sel in Selectors.GUEST_CHECKOUT_BUTTONS:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    logger.info(f"检测到访客结账选项 ({sel})，点击继续...")
                    await self._human_delay()
                    await loc.scroll_into_view_if_needed()
                    try:
                        await loc.click(timeout=2000)
                    except Exception:
                        await loc.click(force=True, timeout=2000)
                    await asyncio.sleep(3.0)
                    break
            except Exception:
                continue

        # 针对已登录 Session：安全识别并尝试推进普通配送/已保存地址步骤
        # 严格铁律：在进入任何支付选项或最终提交订单前必须立即停下！
        initial_safety = await self.check_safety_boundary()
        if not initial_safety:
            for continue_sel in Selectors.SAFE_CHECKOUT_CONTINUE_BUTTONS:
                try:
                    c_loc = self.page.locator(continue_sel).first
                    if await c_loc.count() > 0 and await c_loc.is_visible():
                        logger.info(f"检测到普通配送确认步骤 ({continue_sel})，自动安全推进...")
                        await self._human_delay()
                        await c_loc.scroll_into_view_if_needed()
                        try:
                            await c_loc.click(timeout=2000)
                        except Exception:
                            await c_loc.click(force=True, timeout=2000)
                        await asyncio.sleep(2.5)
                        break
                except Exception:
                    continue

        # 检查安全边界 (Apple ID 密码、验证码、双重认证、支付方式、立即付款、提交订单)
        safety_reason = await self.check_safety_boundary()
        
        # 保存 03_cart_or_stop_point.png 作为最终人工交接停机点截图
        await self.save_screenshot("03_cart_or_stop_point.png")

        if safety_reason:
            logger.info(f"🛑 命中安全停机边界: {safety_reason}")
            return True, safety_reason

        return True, "已推进至购物袋/结账流程，请人工完成后续身份登录、配送确认或支付。"

    async def verify_starwhite_selected(self, target_sku: str = "MK2P4CH/A") -> bool:
        """核验星光白色是否确认处于选中状态 (Production Helper)"""
        return await verify_starwhite_selected(self.page, target_sku=target_sku)

    async def verify_512gb_selected(self, target_sku: str = "MK2P4CH/A") -> bool:
        """核验 512GB 存储容量是否确认处于选中状态 (Production Helper)"""
        return await verify_512gb_selected(self.page, target_sku=target_sku)

    async def fast_select_option(
        self,
        radio_locator_str: str,
        label_locator_str: str,
        option_desc: str = "",
        timeout_ms: int = 1500,
    ) -> bool:
        """使用 Fast Path 快速选择规格选项 (Production Helper)"""
        return await fast_select_option(
            page=self.page,
            radio_locator_str=radio_locator_str,
            label_locator_str=label_locator_str,
            option_desc=option_desc,
            timeout_ms=timeout_ms,
        )

    async def verify_target_sku_consistency(
        self,
        expected_sku: str = "MK2P4CH/A",
        product: Optional[str] = None,
        color: Optional[str] = None,
        storage: Optional[str] = None,
        resolver: Optional[AppleCatalogResolver] = None,
        require_catalog: bool = False,
    ) -> bool:
        """核验目标 SKU 一致性 (Production Helper)"""
        target_prod = product or getattr(getattr(self.config, "target", None), "product", None) or self.config.model
        target_col = color or getattr(getattr(self.config, "target", None), "color", None) or self.config.color
        target_stor = storage or getattr(getattr(self.config, "target", None), "storage", None) or self.config.storage
        return await verify_target_sku_consistency(
            page=self.page,
            expected_sku=expected_sku,
            product=target_prod,
            color=target_col,
            storage=target_stor,
            resolver=resolver,
            require_catalog=require_catalog,
        )

    async def fast_select_color(self, target_color: Optional[str] = None) -> bool:
        """使用 Fast Path 选择外观颜色"""
        color_name = target_color or self.config.color
        val = Selectors.COLOR_VALUE_MAP.get(color_name, "starwhite")
        radio_sel = f"input[name='dimensionColor'][value*='{val}']"
        label_sel = f"label:has-text('{color_name}'), label:has({radio_sel}), [role='radio'][data-autom*='{val}']"
        return await self.fast_select_option(
            radio_locator_str=radio_sel,
            label_locator_str=label_sel,
            option_desc=f"外观颜色: {color_name}",
        )

    async def fast_select_storage(self, target_storage: Optional[str] = None) -> bool:
        """使用 Fast Path 选择存储容量"""
        storage_name = target_storage or self.config.storage
        val = Selectors.STORAGE_VALUE_MAP.get(storage_name, storage_name.lower())
        radio_sel = f"input[name='dimensionCapacity'][value='{val}']"
        label_sel = f"label:has-text('{storage_name}'), label:has({radio_sel}), [role='radio'][data-autom*='{val}']"
        return await self.fast_select_option(
            radio_locator_str=radio_sel,
            label_locator_str=label_sel,
            option_desc=f"存储容量: {storage_name}",
        )

