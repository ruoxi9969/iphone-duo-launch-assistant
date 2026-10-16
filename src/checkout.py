"""Bounded, normal-click checkout navigation. Never submits an order or a contract."""
import asyncio
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from .selectors import Selectors
from .catalog import AppleCatalogResolver, ResolutionStatus
from .launch_timing import parse_launch_timestamp, launch_wait_seconds
from .logger import setup_logger

logger = setup_logger("checkout")


def official_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and (host == "apple.com.cn" or host.endswith(".apple.com.cn"))


def amount_cny(value):
    text = re.sub(r"^(?:RMB|CNY|人民币|[¥￥])\s*", "", str(value or "").strip(), flags=re.I)
    if not re.fullmatch(r"\d[\d,]*(?:\.\d{1,2})?", text):
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


async def first_visible(page, selectors):
    for selector in selectors:
        loc = page.locator(selector).first
        if await loc.count() and await loc.is_visible():
            return loc
    return None


async def wait_for_storefront(page, config, adapter=None):
    """Maintenance is standby, never readiness. At launch issue one normal navigation."""
    deadline = parse_launch_timestamp(config.launch.launch_time)
    delay = 0
    while not page.is_closed():
        if delay:
            await asyncio.sleep(delay)
        try:
            response = await page.goto(config.product_url, wait_until="domcontentloaded", timeout=25000)
        except Exception:
            if page.is_closed():
                raise
            delay = max(15, min(delay * 2, 60))
            logger.warning("[STORE_NETWORK_WAIT] 商店暂未响应，等待 %ss 后重查", delay)
            continue
        status = response.status if response is not None else 200
        if status in (403, 429, 541):
            delay = max(60, min(delay * 2, 300))
            logger.warning("[STORE_BACKOFF] HTTP %s，等待 %ss", status, delay)
            continue
        if status >= 400 or not official_url(page.url):
            raise RuntimeError(f"商店访问异常 HTTP {status}，请人工核对")
        if await page.locator("input[name='dimensionColor']").count():
            logger.info("[STOREFRONT_OPEN] 实际购买页面已恢复，开始实时核验")
            return
        text = await page.locator("body").inner_text()
        if not ("来得早，来得好" in text and "接受预购" in text):
            raise RuntimeError("官网当前页面无法识别，保留窗口等待人工核对")
        remaining = launch_wait_seconds(deadline)
        delay = remaining if remaining else 15
        logger.info("[STORE_MAINTENANCE_WAIT] 官网维护中，尚未完成就绪核验；%.1fs 后重查", delay)
        if adapter and hasattr(adapter, "on_stage_changed"):
            adapter.on_stage_changed("store_wait", "官网维护中，等待开售恢复后核验")
    raise RuntimeError("等待开售期间浏览器已关闭")


async def product_budget_ok(buyer):
    res = await AppleCatalogResolver().resolve(
        buyer.page, buyer.config.target.product, buyer.config.target.color,
        buyer.config.target.storage,
        expected_part_number=buyer.config.target.part_number or "MK2P4CH/A",
        current_page_only=True,
    )
    if res.status != ResolutionStatus.SUCCESS or not res.product or res.is_stale:
        return False
    price = amount_cny(res.product.price)
    return price is not None and 0 < price <= Decimal(str(buyer.config.checkout.max_total_cny))


async def cart_is_single_target(page, config):
    """Fail closed unless one explicit cart item, quantity and full total are observable."""
    items = page.locator("[data-autom='bag-item'], .rs-bag-item")
    if await items.count() != 1:
        return False
    item = items.first
    text = await item.inner_text()
    if not all(s in text for s in (config.target.product, config.target.color, config.target.storage)):
        return False
    quantities = item.locator("select[data-autom='item-quantity-dropdown'], input[data-autom='item-quantity']")
    if await quantities.count() != 1 or await quantities.input_value() != "1":
        return False
    total = await first_visible(page, ["[data-autom='bag-total-value']", "[data-autom='bag-total']"])
    if total is None:
        return False
    value = amount_cny(await total.inner_text())
    return value is not None and 0 < value <= Decimal(str(config.checkout.max_total_cny))


async def advance_checkout(buyer, initial_continue=False):
    """Each known stage clicked once; DOM changes replace unconditional multi-second sleeps."""
    page, config = buyer.page, buyer.config
    deadline = parse_launch_timestamp(config.launch.launch_time)
    if launch_wait_seconds(deadline):
        return False, "尚未到开售时间，未点击购买按钮"
    clicked = set()
    expires = time.monotonic() + 30
    pending_until = 0
    payment_selected = False
    for _ in range(160):
        if page.is_closed() or time.monotonic() >= expires:
            break
        if not official_url(page.url):
            return False, "已离开 Apple 官方结账域名，请人工核对"
        for name, selectors in Selectors.AUTH_AND_SECURITY_TRIGGERS.items():
            if await first_visible(page, selectors) is not None:
                return True, f"需要本人完成登录或安全验证：{name}"

        path = urlsplit(page.url).path
        # Select only the user's explicit WeChat preference; never save new credentials.
        if "/checkout" in path and not payment_selected and config.checkout.preferred_payment == "wechatpay":
            wechat = await first_visible(page, ["input[type='radio'][value='wechatpay']"])
            if wechat is not None and await wechat.is_enabled():
                await wechat.check(timeout=2000)
                payment_selected = True

        reason = await buyer.check_safety_boundary()
        if reason:
            suffix = "（已预选微信一次付清）" if payment_selected else ""
            return True, reason + suffix + "；请本人核对并提交订单，随后扫码付款。"
        if "/checkout" in path and await first_visible(page, [
            "input[type='checkbox'][name*='terms']", "input[type='checkbox'][id*='terms']",
            "input[type='checkbox'][name*='agreement']",
        ]) is not None:
            return True, "结账需要本人确认协议，请核对条款并继续，随后扫码付款。"

        stage, loc = None, None
        if "/shop/buy-iphone/iphone-duo" in path:
            if initial_continue and "product_continue" not in clicked:
                if not await product_budget_ok(buyer):
                    return False, "目标实时价格无法核实或超过预算，未点击继续"
                stage = "product_continue"
                loc = await first_visible(page, ["button[data-autom='continueButton']"])
            elif not initial_continue and "add" not in clicked:
                if not await product_budget_ok(buyer):
                    return False, "目标实时价格无法核实或超过预算，未尝试加车"
                if not (await buyer.verify_starwhite_selected() and await buyer.verify_512gb_selected()):
                    return False, "继续后目标规格未能确认，未尝试加车"
                stage = "add"
                loc = await first_visible(page, Selectors.ADD_TO_BAG)
        elif "/shop/bag" in path:
            if "checkout" not in clicked:
                if not await cart_is_single_target(page, config):
                    if time.monotonic() < pending_until:
                        await asyncio.sleep(0.1)
                        continue
                    return True, "购物袋型号、数量或总价未能完整自动核实，请本人核对后结账。"
                stage = "checkout"
                loc = await first_visible(page, Selectors.CHECKOUT_BUTTONS)
        elif "/checkout" in path:
            loc = await first_visible(page, Selectors.SAFE_CHECKOUT_CONTINUE_BUTTONS)
            if loc is not None:
                stage = "shipping:" + str(await loc.get_attribute("id") or await loc.get_attribute("data-autom") or await loc.inner_text())
                # An address form must be completed by the user; no guessing/filling personal data.
                required = page.locator("input[required]:visible")
                for idx in range(await required.count()):
                    if not await required.nth(idx).input_value():
                        return True, "配送信息尚有必填项，请本人核对后继续。"

        if loc is None:
            # A known post-add link can lead to the bag, but never navigate away from checkout.
            review = await first_visible(page, Selectors.REVIEW_BAG)
            if review is not None and "review_bag" not in clicked and "/checkout" not in path:
                stage, loc = "review_bag", review
        if loc is not None and stage not in clicked and await loc.is_enabled():
            clicked.add(stage)  # Mark BEFORE clicking: uncertain responses never permit a retry.
            try:
                await loc.click(timeout=5000)
                await page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                return False, "页面响应不确定，已停止自动点击，请核对当前页面，避免重复加车。"
            logger.info("[AUTO_CHECKOUT_STAGE] %s", stage)
            pending_until = time.monotonic() + 8
            continue
        if not pending_until or time.monotonic() >= pending_until:
            break
        await asyncio.sleep(0.1)
    return True, "已到当前可识别的结账步骤，请本人继续核对并扫码付款。"
