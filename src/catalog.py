"""动态 Apple 官方 Catalog / Part Number 解析模块 (V0.3)

核心能力：
1. 黄金数据源：从 Apple 官方购买页 window.PRODUCT_SELECTION_BOOTSTRAP 提取动态规格矩阵
2. 智能归一化：支持中文显示名、英文 slug、容量大小归一，拒绝对未知颜色胡乱猜测
3. 严格断言与歧义防护：
   - 精准单命中 -> ResolvedProduct
   - 歧义多重命中 -> MULTIPLE_MATCHES (要求人工确认，绝不随机猜)
   - 无有效匹配 -> NO_MATCH (严禁模糊乱选)
   - 手工配置与官方解析冲突 -> TARGET_SKU_MISMATCH (强校验熔断)
4. 本地持久化缓存 (cache/catalog_cn.json)：
   - 网络故障或离线时降级读取，但必须明确标记 CATALOG_STALE，不能冒充实时数据
5. 目录树形格式化打印与新品自动发现 (--mode catalog / --mode watch-product)
"""

import os
import re
import json
import time
import asyncio
import datetime
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from playwright.async_api import Page

from .logger import setup_logger

logger = setup_logger("catalog")

class LaunchReadinessState(str, Enum):
    """iPhone Duo / 新品首发全链路 Launch Readiness 统一状态机"""
    CATALOG_FOUND          = "CATALOG_FOUND"          # 目录中发现商品
    COMING_SOON            = "COMING_SOON"            # 官方标识即将发售 (尚未开售)
    PREORDER_READY         = "PREORDER_READY"         # 预备抢购/预购配置准备已开启
    ORDERABLE              = "ORDERABLE"              # 线上已开放下订
    PICKUP_AVAILABLE       = "PICKUP_AVAILABLE"       # 直营店已出现自提库存
    ONLINE_AVAILABLE       = "ONLINE_AVAILABLE"       # 官网线上快递支持订购
    HUMAN_ACTION_REQUIRED  = "HUMAN_ACTION_REQUIRED"  # 流程推进至人工接管点
    SOLD_OUT               = "SOLD_OUT"               # 官方明确显示已售罄/缺货
    UNKNOWN                = "UNKNOWN"                # 未知/未明确状态 (严禁解释为 SOLD_OUT)


class ResolutionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    NO_MATCH = "NO_MATCH"
    MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    CATALOG_STALE = "CATALOG_STALE"
    TARGET_SKU_MISMATCH = "TARGET_SKU_MISMATCH"
    TARGET_CHANGED = "TARGET_CHANGED"
    ERROR = "ERROR"


@dataclass
class ResolvedProduct:
    product_name: str
    color: str
    storage: str
    part_number: str
    product_url: str
    price: Optional[str] = None
    coming_soon: bool = False
    region: str = "CN"
    is_stale: bool = False
    dimension_color_slug: str = ""
    dimension_capacity_slug: str = ""
    readiness_state: LaunchReadinessState = LaunchReadinessState.UNKNOWN

    def summary(self) -> str:
        s = f"{self.product_name} · {self.color} · {self.storage} -> {self.part_number}"
        if self.price:
            s += f" ({self.price})"
        if self.coming_soon:
            s += " [即将发售]"
        if self.is_stale:
            s += " [CATALOG_STALE - 缓存数据]"
        return s


@dataclass
class ResolutionResult:
    status: ResolutionStatus
    product: Optional[ResolvedProduct] = None
    matches: List[Dict[str, Any]] = field(default_factory=list)
    error_message: Optional[str] = None
    is_stale: bool = False


# 颜色与名称映射词典
COLOR_MAP = {
    # 常用基础色
    "白色": "white",
    "白": "white",
    "white": "white",
    "黑色": "black",
    "黑": "black",
    "black": "black",
    "星光白色": "starwhite",
    "星光白": "starwhite",
    "星光色": "starwhite",
    "starwhite": "starwhite",
    "starlight": "starwhite",
    "夜空色": "nightsky",
    "夜空": "nightsky",
    "nightsky": "nightsky",
    "midnight": "nightsky",
    "薰衣草紫色": "lavender",
    "薰衣草紫": "lavender",
    "lavender": "lavender",
    "青雾蓝色": "mistblue",
    "青雾蓝": "mistblue",
    "mistblue": "mistblue",
    "鼠尾草绿色": "sage",
    "鼠尾草绿": "sage",
    "sage": "sage",
    # Pro 系列钛金属
    "沙漠色钛金属": "deserttitanium",
    "沙漠钛": "deserttitanium",
    "原色钛金属": "naturaltitanium",
    "原色钛": "naturaltitanium",
    "白色钛金属": "whitetitanium",
    "白钛": "whitetitanium",
    "黑色钛金属": "blacktitanium",
    "黑钛": "blacktitanium",
}


def normalize_product_name(product: str) -> Tuple[str, str]:
    """标准化产品名称并输出 (Canonical Name, URL slug)"""
    p = product.strip()
    slug = p.lower().replace(" ", "-")
    # 特例规整
    if slug.startswith("buy-"):
        slug = slug[4:]
    return p, slug


def normalize_color(color_input: str) -> str:
    """标准化颜色输入，匹配已知的 slug 别名；未知颜色保留原始文本，绝不猜测"""
    c = color_input.strip()
    c_lower = c.lower()
    return COLOR_MAP.get(c, COLOR_MAP.get(c_lower, c_lower))


def normalize_storage(storage_input: str) -> str:
    """标准化存储容量输入 (如 512GB, 512gb, 512G -> 512gb)"""
    s = storage_input.strip().lower()
    s = s.replace(" ", "")
    # 匹配数字+单位
    match = re.search(r"(\d+)(gb|tb|g|t)?", s)
    if match:
        num = match.group(1)
        unit = match.group(2) or "gb"
        if unit in ("g", "gb"):
            return f"{num}gb"
        elif unit in ("t", "tb"):
            return f"{num}tb"
    return s


class AppleCatalogResolver:
    """Apple 官网商品目录与 Part Number 自动解析器"""

    def __init__(self, cache_dir: str = "./cache"):
        self.cache_dir = os.path.abspath(cache_dir)
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_path = os.path.join(self.cache_dir, "catalog_cn.json")

    def _load_cache(self) -> Dict[str, Any]:
        """从本地磁盘读取缓存的 Catalog 数据"""
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"读取 Catalog 本地缓存失败: {e}")
        return {}

    def _save_cache(self, product_key: str, data: Dict[str, Any]):
        """将成功抓取的商品 Catalog 更新到本地持久化缓存"""
        current_cache = self._load_cache()
        if "products" not in current_cache:
            current_cache = {
                "updated_at": datetime.datetime.now().isoformat(),
                "region": "CN",
                "products": {}
            }

        current_cache["updated_at"] = datetime.datetime.now().isoformat()
        current_cache["products"][product_key] = data

        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(current_cache, f, ensure_ascii=False, indent=2)
            logger.debug(f"Catalog 缓存已更新: {self.cache_path} ({product_key})")
        except Exception as e:
            logger.warning(f"写入 Catalog 本地缓存失败: {e}")

    async def fetch_product_catalog(self, page: Page, product_name: str, force_refresh: bool = False, no_navigate: bool = False) -> Tuple[Optional[Dict[str, Any]], bool]:
        """
        从 Apple 官网实时提取指定产品的 Catalog 数据；
        若网络或页面异常，自动降级至本地缓存并返回 (data, is_stale=True)
        no_navigate: 若为 True，严禁调用 page.goto，仅通过页面同源 evaluate/fetch 提取，保护 Purchase Page 热态状态
        """
        canonical_name, slug = normalize_product_name(product_name)
        product_url = f"https://www.apple.com.cn/shop/buy-iphone/{slug}"

        # 1. 尝试从实时页面抓取
        if not no_navigate:
            logger.info(f"正在从 Apple 官方购买页提取 Catalog: {product_url} ...")
        try:
            if not no_navigate:
                await page.goto(product_url, wait_until="domcontentloaded", timeout=25000)
            
            # 提取 window.PRODUCT_SELECTION_BOOTSTRAP (若 no_navigate，在后台通过 fetch 异步获取最新响应)
            if no_navigate:
                ps_data = await page.evaluate("""async () => {
                    try {
                        const resp = await fetch(window.location.href, { cache: "no-store", credentials: "same-origin" });
                        const html = await resp.text();
                        const match = html.match(/PRODUCT_SELECTION_BOOTSTRAP\\s*=\\s*(\\{.+?\\});/s);
                        if (match) {
                            const parsed = JSON.parse(match[1]);
                            if (parsed && parsed.productSelectionData) {
                                return parsed.productSelectionData;
                            }
                        }
                    } catch (e) {}
                    if (typeof window.PRODUCT_SELECTION_BOOTSTRAP !== "undefined") {
                        return window.PRODUCT_SELECTION_BOOTSTRAP.productSelectionData || null;
                    }
                    return null;
                }""")
            else:
                ps_data = await page.evaluate("""() => {
                    if (typeof window.PRODUCT_SELECTION_BOOTSTRAP !== "undefined") {
                        return window.PRODUCT_SELECTION_BOOTSTRAP.productSelectionData || null;
                    }
                    return null;
                }""")

            if ps_data and "products" in ps_data:
                if not no_navigate:
                    logger.info(f"✅ 成功提取官方实时 Catalog ({len(ps_data.get('products', []))} 个 SKU)")
                self._save_cache(canonical_name, {
                    "url": product_url,
                    "fetched_at": datetime.datetime.now().isoformat(),
                    "productSelectionData": ps_data
                })
                return ps_data, False

            if not no_navigate:
                logger.warning(f"⚠️ 页面已加载但未发现有效 PRODUCT_SELECTION_BOOTSTRAP: {product_url}")

        except Exception as e:
            if not no_navigate:
                logger.warning(f"⚠️ 实时拉取 Catalog 遭遇异常: {e}")
            else:
                logger.debug(f"no_navigate 提取 Catalog 异常: {e}")

        # 2. 降级读取本地缓存
        logger.info(f"正在尝试从本地缓存读取 {canonical_name} Catalog 兜底数据...")
        cache_data = self._load_cache()
        cached_prod = cache_data.get("products", {}).get(canonical_name)
        if cached_prod and "productSelectionData" in cached_prod:
            logger.info(f"⚠️ 使用本地缓存 Catalog (获取于 {cached_prod.get('fetched_at')}) [CATALOG_STALE]")
            return cached_prod["productSelectionData"], True

        logger.error(f"❌ 无法从实时页面或本地缓存获取 {canonical_name} 的 Catalog 数据！")
        return None, False

    async def resolve(
        self,
        page: Page,
        product: str,
        color: str,
        storage: str,
        expected_part_number: Optional[str] = None,
        force_refresh: bool = False
    ) -> ResolutionResult:
        """
        核心解析方法：根据输入的产品名称、颜色、存储容量，动态解析出唯一的官方 Part Number
        """
        canonical_name, slug = normalize_product_name(product)
        product_url = f"https://www.apple.com.cn/shop/buy-iphone/{slug}"

        ps_data, is_stale = await self.fetch_product_catalog(page, product, force_refresh)
        if not ps_data:
            return ResolutionResult(
                status=ResolutionStatus.PRODUCT_NOT_FOUND,
                error_message=f"无法获取产品 [{product}] 的官方 Catalog，请核对产品名称是否正确或官网是否已上线购买页。",
                is_stale=is_stale
            )

        products_list: List[Dict[str, Any]] = ps_data.get("products", [])
        display_values: Dict[str, Any] = ps_data.get("displayValues", {})
        color_display_map: Dict[str, Any] = display_values.get("dimensionColor", {})
        capacity_display_map: Dict[str, Any] = display_values.get("dimensionCapacity", {})
        prices_map: Dict[str, Any] = display_values.get("prices", {})

        target_color_norm = normalize_color(color)
        target_storage_norm = normalize_storage(storage)

        # 建立反向查找：中文颜色文本 -> slug
        chinese_color_to_slug = {}
        for c_slug, c_info in color_display_map.items():
            if isinstance(c_info, dict) and "value" in c_info:
                # 过滤 HTML
                val_text = re.sub(r"<[^>]+>", "", c_info["value"]).strip()
                chinese_color_to_slug[val_text] = c_slug
                chinese_color_to_slug[c_slug] = c_slug

        # 确定匹配的 color slug
        resolved_color_slug = None
        if color in chinese_color_to_slug:
            resolved_color_slug = chinese_color_to_slug[color]
        elif target_color_norm in chinese_color_to_slug:
            resolved_color_slug = chinese_color_to_slug[target_color_norm]
        elif target_color_norm in color_display_map:
            resolved_color_slug = target_color_norm

        if not resolved_color_slug:
            # 严格检查：颜色完全不匹配，绝不瞎猜！
            valid_colors = [re.sub(r"<[^>]+>", "", v.get("value", "")) for k, v in color_display_map.items() if isinstance(v, dict) and "value" in v]
            return ResolutionResult(
                status=ResolutionStatus.NO_MATCH,
                error_message=f"颜色 [{color}] 不在 [{product}] 的可选规格中！官方可选颜色: {valid_colors}。系统已拒绝猜测最接近颜色。",
                is_stale=is_stale
            )

        # 匹配候选 SKU
        candidate_skus = []
        for p_item in products_list:
            p_color = (p_item.get("dimensionColor") or "").strip().lower()
            p_cap = (p_item.get("dimensionCapacity") or "").strip().lower()

            # 颜色匹配
            if p_color == resolved_color_slug:
                # 容量匹配
                if p_cap == target_storage_norm or p_cap.replace(" ", "") == target_storage_norm:
                    candidate_skus.append(p_item)

        # 1. 0 个匹配 -> NO_MATCH
        if len(candidate_skus) == 0:
            valid_caps = list(capacity_display_map.keys()) if capacity_display_map else []
            return ResolutionResult(
                status=ResolutionStatus.NO_MATCH,
                error_message=f"未找到匹配的规格组合: {color} ({resolved_color_slug}) + {storage} ({target_storage_norm})。该颜色可能不提供此容量。",
                is_stale=is_stale
            )

        # 2. 多个匹配 -> MULTIPLE_MATCHES (歧义防护)
        if len(candidate_skus) > 1:
            match_parts = [p.get("partNumber") for p in candidate_skus]
            return ResolutionResult(
                status=ResolutionStatus.MULTIPLE_MATCHES,
                matches=candidate_skus,
                error_message=f"规格解析命中多个候选 SKU: {match_parts}，存在歧义（可能包含不同屏幕尺寸或网络制式），请人工明确配置。",
                is_stale=is_stale
            )

        # 3. 唯一定位匹配
        matched_sku = candidate_skus[0]
        actual_part_number = matched_sku.get("partNumber", "")

        # 提取价格
        full_price_key = matched_sku.get("fullPrice")
        price_str = None
        if full_price_key and full_price_key in prices_map:
            price_info = prices_map[full_price_key].get("currentPrice", {})
            price_str = price_info.get("amount")

        # 检查发售状态与 Launch Readiness
        is_coming_soon = matched_sku.get("comingSoon", False)
        is_get_ready = matched_sku.get("getReady", False)
        if is_coming_soon:
            if is_get_ready:
                readiness = LaunchReadinessState.PREORDER_READY
            else:
                readiness = LaunchReadinessState.COMING_SOON
        else:
            readiness = LaunchReadinessState.ORDERABLE

        # 提取规范中文颜色名
        canonical_color_name = color
        if resolved_color_slug in color_display_map and isinstance(color_display_map[resolved_color_slug], dict):
            raw_v = color_display_map[resolved_color_slug].get("value", "")
            canonical_color_name = re.sub(r"<[^>]+>", "", raw_v).strip()

        resolved_obj = ResolvedProduct(
            product_name=canonical_name,
            color=canonical_color_name,
            storage=target_storage_norm.upper(),
            part_number=actual_part_number,
            product_url=product_url,
            price=price_str,
            coming_soon=is_coming_soon,
            region="CN",
            is_stale=is_stale,
            dimension_color_slug=resolved_color_slug,
            dimension_capacity_slug=target_storage_norm,
            readiness_state=readiness,
        )

        # 4. 手工配置与官方解析强一致性核验 (TARGET_SKU_MISMATCH 与 TARGET_CHANGED 防护)
        if expected_part_number and expected_part_number.strip():
            exp_clean = expected_part_number.strip().upper()
            act_clean = actual_part_number.strip().upper()
            if exp_clean != act_clean:
                # 若目标机型为已锁定的正式 SKU (例如 MK2P4CH/A)，判定为官方 SKU 变更
                if exp_clean == "MK2P4CH/A" or "DUO" in canonical_name.upper():
                    logger.critical(f"🚨 [TARGET_CHANGED] 官方目标发生变化！锁定 SKU [{exp_clean}] 已不再对应 [{canonical_name} {canonical_color_name} {target_storage_norm.upper()}] (实际: [{act_clean}])！")
                    return ResolutionResult(
                        status=ResolutionStatus.TARGET_CHANGED,
                        product=resolved_obj,
                        error_message=f"官方 SKU 发生重大变更：锁定零件号 [{exp_clean}] 与官方最新零件号 [{act_clean}] 不一致！为防抢错机型已紧急停止！",
                        is_stale=is_stale
                    )
                else:
                    logger.error(f"🛑 [TARGET_SKU_MISMATCH] 手工填写零件号 [{exp_clean}] 与官方 Catalog 解析结果 [{act_clean}] 不一致！")
                    return ResolutionResult(
                        status=ResolutionStatus.TARGET_SKU_MISMATCH,
                        product=resolved_obj,
                        error_message=f"配置冲突：您手工填写的 Part Number [{exp_clean}] 与官方实际规格 [{act_clean}] 不符，已禁止启动以防下单错误商品！",
                        is_stale=is_stale
                    )

        status = ResolutionStatus.CATALOG_STALE if is_stale else ResolutionStatus.SUCCESS
        return ResolutionResult(
            status=status,
            product=resolved_obj,
            is_stale=is_stale
        )

    async def check_duo_dry_run(self, page: Page, screenshots_dir: str = "./logs/screenshots/duo") -> Dict[str, Any]:
        """
        iPhone Duo 专属演练 (Duo Dry Run):
        真实目标固定为: iPhone Duo · 星光白色 · 512GB · MK2P4CH/A
        执行公开购买页合法步骤，验证 Catalog、规格匹配、价格、comingSoon 状态与购买按钮。
        保存截图并返回全景信息字典。
        """
        os.makedirs(screenshots_dir, exist_ok=True)
        target_product = "iPhone Duo"
        target_color = "星光白色"
        target_storage = "512GB"
        expected_sku = "MK2P4CH/A"
        duo_url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"

        logger.info("==================================================")
        logger.info("  【Duo Dry Run】iPhone Duo 官方预演与就绪核验")
        logger.info("==================================================")
        logger.info(f"正在访问 Apple 购买页: {duo_url} ...")

        await page.goto(duo_url, wait_until="domcontentloaded", timeout=25000)
        await asyncio.sleep(1.5)

        # 保存 01_duo_page.png
        p1_path = os.path.join(screenshots_dir, "01_duo_page.png")
        await page.screenshot(path=p1_path)
        logger.info(f"📸 Duo 页面截图已保存: {p1_path}")

        # 解析 Catalog
        res = await self.resolve(page, target_product, target_color, target_storage, expected_part_number=expected_sku)
        
        # 尝试在页面上点击颜色与容量 (合法公开浏览行为)
        try:
            c_loc = page.locator("input[name='dimensionColor'][value*='starwhite'], label:has-text('星光白色')").first
            if await c_loc.count() > 0:
                await c_loc.click(force=True)
                await asyncio.sleep(0.5)

            cap_loc = page.locator("input[name='dimensionCapacity'][value='512gb'], label:has-text('512GB')").first
            if await cap_loc.count() > 0:
                await cap_loc.click(force=True)
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.debug(f"Dry Run 界面预选非致命跳过: {e}")

        # 保存 02_duo_selected.png
        p2_path = os.path.join(screenshots_dir, "02_duo_selected.png")
        await page.screenshot(path=p2_path)
        logger.info(f"📸 Duo 预选截图已保存: {p2_path}")

        # 检查购买/预购按钮状态
        bag_loc = page.locator("button[name='add-to-cart'], button[data-autom='add-to-cart'], button:has-text('添加到购物袋'), button:has-text('预购')").first
        has_buy_button = False
        buy_button_text = ""
        if await bag_loc.count() > 0 and await bag_loc.is_visible():
            has_buy_button = True
            buy_button_text = (await bag_loc.inner_text()).strip()

        prod = res.product
        status_label = "COMING_SOON"
        if prod and not prod.coming_soon:
            status_label = "ORDERABLE" if has_buy_button else "ONLINE_AVAILABLE"

        return {
            "success": res.status in (ResolutionStatus.SUCCESS, ResolutionStatus.CATALOG_STALE),
            "status": status_label,
            "product": target_product,
            "color": target_color,
            "storage": target_storage,
            "sku": prod.part_number if prod else expected_sku,
            "price": prod.price if prod else "RMB 17,999",
            "coming_soon": prod.coming_soon if prod else True,
            "is_stale": res.is_stale,
            "has_buy_button": has_buy_button,
            "buy_button_text": buy_button_text,
            "screenshots": [p1_path, p2_path],
        }

    async def get_all_product_slugs(self, page: Page) -> List[str]:
        """获取 Apple 中国官网所有支持购买的 iPhone 机型 slug 列表"""
        logger.info("正在查询 Apple 官网在售机型目录...")
        try:
            await page.goto("https://www.apple.com.cn/shop/buy-iphone", wait_until="domcontentloaded", timeout=20000)
            slugs = await page.evaluate("""() => {
                const anchors = Array.from(document.querySelectorAll('a[href*="/shop/buy-iphone/"]'));
                const list = [];
                for (const a of anchors) {
                    const match = a.href.match(/\\/shop\\/buy-iphone\\/([a-zA-Z0-9_-]+)/);
                    if (match && match[1] && !list.includes(match[1])) {
                        list.push(match[1]);
                    }
                }
                return list;
            }""")
            return slugs
        except Exception as e:
            logger.warning(f"获取在售机型列表出现异常: {e}")
            return ["iphone-17", "iphone-duo", "iphone-16"]

    async def render_catalog_tree(self, page: Page, product_slugs: Optional[List[str]] = None) -> str:
        """
        拉取并生成 Apple CN Catalog 的树状全景视图 (对应 --mode catalog)
        """
        if not product_slugs:
            product_slugs = await self.get_all_product_slugs(page)

        lines = ["\n==================================================", "  Apple CN 官方产品规格与 SKU 目录 (实时解析)", "=================================================="]

        for slug in product_slugs:
            title = slug.replace("-", " ").title()
            # iPhone 保持大小写习惯
            title = re.sub(r"^Iphone", "iPhone", title)

            ps_data, is_stale = await self.fetch_product_catalog(page, slug)
            if not ps_data or "products" not in ps_data:
                continue

            products = ps_data.get("products", [])
            color_map = ps_data.get("displayValues", {}).get("dimensionColor", {})
            prices_map = ps_data.get("displayValues", {}).get("prices", {})

            stale_tag = " [缓存数据]" if is_stale else ""
            lines.append(f"\n{title}{stale_tag}")

            # 按颜色分组
            color_groups: Dict[str, List[Dict[str, Any]]] = {}
            for p in products:
                c_slug = p.get("dimensionColor", "other")
                color_groups.setdefault(c_slug, []).append(p)

            colors_list = list(color_groups.keys())
            for c_idx, c_slug in enumerate(colors_list):
                is_last_color = (c_idx == len(colors_list) - 1)
                color_prefix = "└── " if is_last_color else "├── "
                sub_indent = "    " if is_last_color else "│   "

                # 颜色名称
                c_name = c_slug
                if c_slug in color_map and isinstance(color_map[c_slug], dict):
                    raw_val = color_map[c_slug].get("value", "")
                    c_name = re.sub(r"<[^>]+>", "", raw_val).strip() or c_slug

                lines.append(f"{color_prefix}{c_name} ({c_slug})")

                # 容量规格
                skus = color_groups[c_slug]
                for s_idx, sku in enumerate(skus):
                    is_last_sku = (s_idx == len(skus) - 1)
                    sku_prefix = "└── " if is_last_sku else "├── "

                    cap = (sku.get("dimensionCapacity") or "").upper()
                    part = sku.get("partNumber", "")
                    full_p_key = sku.get("fullPrice")
                    price_str = ""
                    if full_p_key and full_p_key in prices_map:
                        price_str = f" ({prices_map[full_p_key].get('currentPrice', {}).get('amount', '')})"
                    
                    status_note = ""
                    if sku.get("comingSoon"):
                        status_note = " [即将发售]"

                    lines.append(f"{sub_indent}{sku_prefix}{cap:<6} → {part:<12}{price_str}{status_note}")

        lines.append("\n==================================================\n")
        return "\n".join(lines)
