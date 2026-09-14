"""上海 Apple Store 直营店自提 (Pickup) 库存监控模块 (V0.2)

核心功能：
1. 监控上海 4 家核心 Apple 直营店 (香港广场 R390, 环贸 iapm R401, 五角场 R581, 环球港 R683)
2. 内部 7 态严格模型 (AVAILABLE, UNAVAILABLE, COMING_SOON, NOT_FOR_PICKUP, NOT_YET_RELEASED, UNKNOWN, ERROR)
   严禁将 HTTP 541 / 网络异常 / 结构解析失败当作 UNAVAILABLE！
3. 会话管理与反 541 机制：在真实 Chromium 页面内完成 Akamai Shield (shld_bt_ck, as_atb) 握手
4. 防刷频控与指数退避：
   - 单店串行间隔 >= 2s
   - 基础轮询 30s + 10s 随机 Jitter
   - 遇到 403 / 429 / 541 时指数退避 (60s -> 120s -> 240s) 并重置会话
   - 连续失败超限自动暂停，严禁轰炸
5. 到货触发链：
   - 发现 AVAILABLE -> 二次确认 -> Bark 强提醒 + 本地声音 -> 自动打开官方购买/取货页面 -> 保持浏览器等待人工下单
"""

import os
import json
import time
import random
import asyncio
import datetime
from enum import Enum
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from playwright.async_api import Page, BrowserContext

from .logger import setup_logger, log_takeover_alert
from .config import AppConfig, StoreConfig
from .notifier import Notifier, NotificationEvent
from .browser import BrowserManager

logger = setup_logger("pickup_monitor")

class InventoryStatus(str, Enum):
    AVAILABLE = "AVAILABLE"                 # 明确有货，支持到店自提
    UNAVAILABLE = "UNAVAILABLE"             # 明确无货
    COMING_SOON = "COMING_SOON"             # 即将发售
    NOT_FOR_PICKUP = "NOT_FOR_PICKUP"       # 该配置不支持到店取货 (如 ineligible)
    NOT_YET_RELEASED = "NOT_YET_RELEASED"   # 尚未正式开售 (如 NOT_FOR_SALE)
    UNKNOWN = "UNKNOWN"                     # 拦截/限流/暂无数据/结构漂移 (必须附带具体原因)
    ERROR = "ERROR"                         # 底层网络超时或会话断开

    @property
    def label(self) -> str:
        labels = {
            self.AVAILABLE: "有货 (可取货)",
            self.UNAVAILABLE: "无货",
            self.COMING_SOON: "即将发售",
            self.NOT_FOR_PICKUP: "不支持取货",
            self.NOT_YET_RELEASED: "暂未开售",
            self.UNKNOWN: "未确认 (未知)",
            self.ERROR: "异常失败",
        }
        return labels.get(self, "未知")


@dataclass
class StoreCheckResult:
    store_number: str
    store_name: str
    part_number: str
    status: InventoryStatus
    pickup_display: str = ""
    pickup_quote: Optional[str] = None
    product_title: Optional[str] = None
    reason: Optional[str] = None
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def summary(self) -> str:
        s = f"[{self.store_name} ({self.store_number})] {self.part_number}: {self.status.label}"
        if self.pickup_quote:
            s += f" - {self.pickup_quote}"
        if self.reason:
            s += f" (原因: {self.reason})"
        return s


def parse_fulfillment_response(data: Dict[str, Any], store_number: str, part_number: str) -> StoreCheckResult:
    """
    解析 Apple 官方 fulfillment-messages 响应 JSON 并映射为 7 态模型。
    绝不将异常、结构缺失或拦截折叠为 UNAVAILABLE。
    """
    # 提取 stores 节点 (兼容 body.stores 与 body.content.pickupMessage.stores)
    body = data.get("body", {})
    content = body.get("content", {})
    pickup_msg = content.get("pickupMessage", {})
    stores = pickup_msg.get("stores", []) or body.get("stores", [])

    if not stores:
        return StoreCheckResult(
            store_number=store_number,
            store_name=store_number,
            part_number=part_number,
            status=InventoryStatus.UNKNOWN,
            reason="Apple 响应缺少门店数据 (NoPickupData)",
        )

    matched_store = None
    for s in stores:
        if s.get("storeNumber") == store_number:
            matched_store = s
            break

    if not matched_store:
        return StoreCheckResult(
            store_number=store_number,
            store_name=store_number,
            part_number=part_number,
            status=InventoryStatus.UNKNOWN,
            reason=f"Apple 响应中未包含目标门店 {store_number} (StoreNotReturned)",
        )

    store_name = matched_store.get("storeName") or store_number
    parts_av = matched_store.get("partsAvailability", {})

    if not parts_av:
        return StoreCheckResult(
            store_number=store_number,
            store_name=store_name,
            part_number=part_number,
            status=InventoryStatus.UNKNOWN,
            reason=f"门店 {store_number} 没有返回任何型号可用性 (EmptyParts)",
        )

    part_info = parts_av.get(part_number)
    if not part_info:
        return StoreCheckResult(
            store_number=store_number,
            store_name=store_name,
            part_number=part_number,
            status=InventoryStatus.UNKNOWN,
            reason=f"响应中未包含零件号 {part_number} (PartNotReturned)",
        )

    # 提取关键字段
    raw_display = (part_info.get("pickupDisplay") or "").strip().lower()
    msg_types = part_info.get("messageTypes", {})
    regular = msg_types.get("regular", {})
    pickup_quote = regular.get("storePickupQuote") or part_info.get("pickupSearchQuote")
    product_title = regular.get("storePickupProductTitle")

    # 检查 deliveryMessage 中的销售状态
    delivery_msg = content.get("deliveryMessage", {}).get(part_number, {})
    sale_reason = ""
    try:
        sale_reason = delivery_msg.get("regular", {}).get("buyability", {}).get("reason", "")
    except Exception:
        pass

    # 状态映射：业务销售限制 (未发售 / 即将发售) 优先于通用 unavailable
    if raw_display == "available":
        status = InventoryStatus.AVAILABLE
    elif sale_reason == "NOT_FOR_SALE":
        status = InventoryStatus.NOT_YET_RELEASED
    elif sale_reason == "COMING_SOON" or raw_display == "coming_soon":
        status = InventoryStatus.COMING_SOON
    elif raw_display == "ineligible":
        status = InventoryStatus.NOT_FOR_PICKUP
    elif raw_display == "unavailable":
        status = InventoryStatus.UNAVAILABLE
    else:
        status = InventoryStatus.UNKNOWN
        reason = f"未知 pickupDisplay 字段取值: '{raw_display}'"
        return StoreCheckResult(
            store_number=store_number,
            store_name=store_name,
            part_number=part_number,
            status=status,
            pickup_display=raw_display,
            pickup_quote=pickup_quote,
            product_title=product_title,
            reason=reason,
        )

    return StoreCheckResult(
        store_number=store_number,
        store_name=store_name,
        part_number=part_number,
        status=status,
        pickup_display=raw_display,
        pickup_quote=pickup_quote,
        product_title=product_title,
    )


class PickupMonitor:
    """上海 Apple Store 直营店自提监控调度器"""

    def __init__(self, config: AppConfig, notifier: Notifier):
        self.config = config
        self.notifier = notifier
        self.pickup_cfg = config.pickup
        self.target_cfg = config.target
        self.consecutive_failures = 0
        self.backoff_delay = 0
        self.last_request_time = 0.0
        self._is_paused = False

    async def _throttle(self, min_seconds: float = 2.0):
        """严格限速：任意两次出站请求间隔不少于 min_seconds，严防突发请求触发风控"""
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < min_seconds:
            await asyncio.sleep(min_seconds - elapsed)
        self.last_request_time = time.time()

    async def ensure_session(self, page: Page) -> bool:
        """
        确保 Playwright 页面会话已完成 Akamai Shield (shld_bt_ck, as_atb) 握手
        """
        warm_url = self.config.product_url
        logger.info(f"正在建立/验证 Apple 官网会话环境: {warm_url} ...")

        try:
            # 若当前 URL 不是购买页，先导航过去
            if "buy-iphone" not in page.url:
                await page.goto(warm_url, wait_until="domcontentloaded", timeout=25000)
            
            # 等待风控校验 Cookie
            deadline = time.time() + 25.0
            while time.time() < deadline:
                cookies = await page.context.cookies()
                names = {c["name"] for c in cookies}
                if "shld_bt_ck" in names and "as_atb" in names:
                    logger.info("✅ Akamai Shield 会话握手完成 (shld_bt_ck, as_atb 就绪)")
                    return True
                await asyncio.sleep(0.5)

            logger.warning("⚠️ 等待风控 Cookie 超时，尝试继续执行...")
            return True
        except Exception as e:
            logger.error(f"建立 Apple 会话失败: {e}")
            return False

    async def query_store(self, page: Page, store_number: str, part_number: str) -> StoreCheckResult:
        """
        在当前会话的 Playwright 页面内执行同源 fetch 查询单家门店库存
        """
        await self._throttle(2.0)

        try:
            res = await page.evaluate("""async ({ storeId, partNo }) => {
                const url = new URL("https://www.apple.com.cn/shop/fulfillment-messages");
                url.searchParams.append("fae", "true");
                url.searchParams.append("pl", "true");
                url.searchParams.append("mts.0", "regular");
                url.searchParams.append("parts.0", partNo);
                url.searchParams.append("store", storeId);
                
                try {
                    const response = await fetch(url.toString(), {
                        credentials: "same-origin",
                        headers: {
                            "Accept": "application/json, text/javascript, */*; q=0.01",
                            "X-Requested-With": "XMLHttpRequest"
                        }
                    });
                    const status = response.status;
                    const text = await response.text();
                    return { status, text };
                } catch (e) {
                    return { status: 0, text: e.toString() };
                }
            }""", {"storeId": store_number, "partNo": part_number})

            status_code = res.get("status", 0)
            raw_text = res.get("text", "")

            # 分类处理 HTTP 响应
            if status_code == 200:
                try:
                    data = json.loads(raw_text)
                    return parse_fulfillment_response(data, store_number, part_number)
                except Exception as e:
                    return StoreCheckResult(
                        store_number=store_number,
                        store_name=store_number,
                        part_number=part_number,
                        status=InventoryStatus.UNKNOWN,
                        reason=f"响应不是合法 JSON (SchemaDrift: {e})",
                    )

            elif status_code in (403, 541):
                # 边缘节点拦截
                return StoreCheckResult(
                    store_number=store_number,
                    store_name=store_number,
                    part_number=part_number,
                    status=InventoryStatus.UNKNOWN,
                    reason=f"Apple 边缘节点拦截 (HTTP {status_code})，会话将被重置",
                )

            elif status_code == 429:
                return StoreCheckResult(
                    store_number=store_number,
                    store_name=store_number,
                    part_number=part_number,
                    status=InventoryStatus.UNKNOWN,
                    reason="请求过于频繁 (HTTP 429)，触发限流退避",
                )

            else:
                return StoreCheckResult(
                    store_number=store_number,
                    store_name=store_number,
                    part_number=part_number,
                    status=InventoryStatus.ERROR,
                    reason=f"网络请求失败 HTTP {status_code}: {raw_text[:80]}",
                )

        except Exception as e:
            return StoreCheckResult(
                store_number=store_number,
                store_name=store_number,
                part_number=part_number,
                status=InventoryStatus.ERROR,
                reason=f"Playwright 执行异常: {e}",
            )

    async def run_single_round(self, page: Page) -> List[StoreCheckResult]:
        """执行单轮全量门店查询"""
        part_no = self.target_cfg.part_number
        stores = self.pickup_cfg.stores
        results: List[StoreCheckResult] = []

        logger.info(f"=== 开始本轮自提监控查询 (目标型号: {self.target_cfg.product} {self.target_cfg.color} {self.target_cfg.storage} [{part_no}]) ===")

        for store in stores:
            res = await self.query_store(page, store.store_number, part_no)
            # 如果解析结果未包含官方门店名称，优先使用权威映射，其次配置名称
            if res.store_name == res.store_number:
                from .config import get_authoritative_store_name
                auth_name = get_authoritative_store_name(store.store_number)
                res.store_name = auth_name or store.name or store.store_number
            results.append(res)
            logger.info(f"  {res.summary()}")

        return results

    async def handle_in_stock(self, page: Page, result: StoreCheckResult):
        """
        发现上海直营店有货后的完整动作链：
        1. 记录日志 (门店 + SKU + 时间)
        2. Bark 强提醒推送
        3. 本地声音报警 (macOS 原生音效 + 终端蜂鸣)
        4. 自动打开官方购买/自提页面
        5. 浏览器保持打开，严格等待用户人工下单
        """
        logger.info("\n" + "!" * 62)
        logger.info(f"🎉 发现上海直营店目标 SKU 有货！")
        logger.info(f"门店: {result.store_name} [{result.store_number}]")
        logger.info(f"型号: {result.product_title or self.target_cfg.product} ({result.part_number})")
        logger.info(f"取货说明: {result.pickup_quote or '可到店取货'}")
        logger.info(f"时间: {result.timestamp}")
        logger.info("!" * 62 + "\n")

        # 1. 发送飞书强提醒与本地声音报警 (自动去重)
        await self.notifier.notify(
            event=NotificationEvent.PICKUP_AVAILABLE,
            title="🍎 iPhone Duo 自提库存出现",
            product=self.target_cfg.product,
            color=self.target_cfg.color,
            storage=self.target_cfg.storage,
            sku=result.part_number,
            store=f"{result.store_name} [{result.store_number}]",
            channel="Apple Store 自提",
            status_text="✅ 可取货",
            quote=result.pickup_quote or "可到店取货",
            url=self.config.product_url,
            details=f"门店 {result.store_name} 出现可预约自提库存。请立即在打开的浏览器中完成人工结账。",
        )

        # 2. 自动在浏览器中打开对应购买/自提页
        logger.info(f"正在调出官方购买页面供人工下单: {self.config.product_url} ...")
        try:
            await page.goto(self.config.product_url, wait_until="domcontentloaded")
            await page.bring_to_front()
        except Exception as e:
            logger.warning(f"调出商品页面出现轻微延迟: {e}")

        # 3. 终端醒目警报提示
        print("\n" + "=" * 65)
        print("  【人工接管抢购提示】")
        print(f"  目标机型在 [{result.store_name}] 发现自提库存！")
        print("  浏览器窗口已自动就绪，请立即手动完成身份登录与支付结算。")
        print("=" * 65 + "\n")

    async def start_monitoring(self, browser_manager: BrowserManager, page: Page, max_rounds: Optional[int] = None):
        """
        自提监控主循环
        支持 Jitter、指数退避、连续失败自停与会话重建
        """
        logger.info("启动上海 Apple Store 直营店自提监控引擎...")
        round_idx = 0

        # 初始化会话
        await self.ensure_session(page)

        while True:
            round_idx += 1
            if max_rounds and round_idx > max_rounds:
                logger.info(f"已达到最大轮次 ({max_rounds})，监控结束。")
                break

            round_start = time.time()
            results = await self.run_single_round(page)

            # 统计本轮结果
            in_stock_items = [r for r in results if r.status == InventoryStatus.AVAILABLE]
            blocked_or_failure_items = [r for r in results if r.status in (InventoryStatus.UNKNOWN, InventoryStatus.ERROR)]

            # 1. 发现有货
            if in_stock_items:
                self.consecutive_failures = 0
                for item in in_stock_items:
                    await self.handle_in_stock(page, item)
                
                logger.info("本轮发现可用库存，浏览器窗口将保持打开等待人工接管。")
                logger.info("如需继续监控请按回车或保持运行；退出请按 Ctrl+C。")
                # 挂起等待人工操作，不自动退出
                try:
                    while True:
                        await asyncio.sleep(60)
                except asyncio.CancelledError:
                    break

            # 2. 发生拦截或异常退避检测
            if len(blocked_or_failure_items) == len(results):
                self.consecutive_failures += 1
                logger.warning(f"⚠️ 本轮所有门店查询均返回异常或拦截 (连续第 {self.consecutive_failures} 轮)")
                
                # 检查是否超过安全熔断上限
                if self.consecutive_failures >= self.pickup_cfg.max_consecutive_failures:
                    self._is_paused = True
                    err_msg = f"连续异常已达上限 ({self.consecutive_failures} 次)！为避免对 Apple 接口形成轰炸，监控已自动安全暂停。"
                    logger.critical(f"🛑 {err_msg}")
                    await self.notifier.notify(
                        event=NotificationEvent.ERROR,
                        title="🛑 Apple 监控安全熔断暂停",
                        product=self.target_cfg.product,
                        status_text="连续查询异常，已自动暂停",
                        details=err_msg,
                    )
                    print("\n" + "=" * 65)
                    print("  【监控安全暂停提示】")
                    print("  " + err_msg)
                    print("  请检查本地网络环境或稍后再试。退出请按 Ctrl+C。")
                    print("=" * 65 + "\n")
                    break

                # 计算指数退避时间: min(300, 60 * 2^(failures - 1)) + jitter
                backoff_base = min(300, 60 * (2 ** (self.consecutive_failures - 1)))
                jitter = random.uniform(0, 15)
                wait_sec = backoff_base + jitter
                logger.warning(f"⏳ 触发风控指数退避，本轮等待 {wait_sec:.1f} 秒后将重新建立会话...")
                await asyncio.sleep(wait_sec)
                
                # 重新建立会话
                await self.ensure_session(page)
                continue

            else:
                # 正常完成本轮查询 (无货或不支持取货均为正常业务响应)
                self.consecutive_failures = 0
                round_duration = time.time() - round_start
                base_interval = self.pickup_cfg.interval_seconds
                jitter = random.uniform(-self.pickup_cfg.jitter_seconds, self.pickup_cfg.jitter_seconds)
                next_wait = max(10.0, base_interval + jitter)
                
                logger.info(f"第 {round_idx} 轮查询完成 (耗时 {round_duration:.2f}s)。约 {next_wait:.1f} 秒后进行下一轮查询...\n")
                await asyncio.sleep(next_wait)
