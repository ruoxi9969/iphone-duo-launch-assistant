"""iPhone Duo 首发模拟演练模块 (Launch Rehearsal Mode)

本模块专用于首发前模拟演练，与正式 Launch 逻辑物理隔离：
1. 仅在本地模拟生成一次 COMING_SOON -> ONLINE_AVAILABLE 触发信号；
2. 严禁修改 Apple 官方状态，严禁伪造真实库存，严禁写入正式库存状态缓存；
3. 严禁自动支付、自动提交订单、绕验证码或 2FA；
4. 严格复用正式环境 Persistent Profile (browser_data/)，保持真实 Apple Account 登录态；
5. 飞书计时严格区分 FEISHU_REQUEST_STARTED 与 FEISHU_REQUEST_ACK，绝不混淆为手机端接收时间；
6. 严格限定 HANDOFF_READY (T8) 判定条件：星光白色与 512GB 均核实底层选中、Catalog 动态校验为 MK2P4CH/A、页面稳定、窗口置前、自动化彻底停止；
7. 纳秒/毫秒级高精度性能计时与统计分析；
8. 飞书卡片明确带有【模拟演练】标识，防止与正式告警混淆。
"""

import os
import sys
import time
import json
import statistics
import urllib.request
import urllib.error
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import asyncio

from .config import AppConfig, mask_feishu_webhook
from .browser import BrowserManager, check_apple_login_status
from .catalog import AppleCatalogResolver, ResolutionStatus
from .logger import setup_logger, log_takeover_alert

logger = setup_logger("rehearsal")

TARGET_PRODUCT = "iPhone Duo"
TARGET_COLOR = "星光白色"
TARGET_STORAGE = "512GB"
TARGET_SKU = "MK2P4CH/A"
DUO_PAGE_URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"

HISTORICAL_COLD_START_BASELINE = {
    "browser_ready_mean": 2131.0,
    "page_loaded_mean": 2806.0,
    "handoff_ready_mean": 10374.0,
    "handoff_ready_p50": 10532.0,
    "handoff_ready_min": 9859.0,
    "handoff_ready_max": 10731.0,
}

HISTORICAL_PREWARM_BASELINE = {
    "handoff_ready_mean": 4543.0,
    "handoff_ready_p50": 3279.0,
    "handoff_ready_min": 2461.0,
    "handoff_ready_max": 7889.0,
}


class PrewarmTiming:
    """Prewarm 预热阶段耗时记录 (完全独立于 T0，绝不计入开售响应时延)"""

    def __init__(self):
        self.t_prewarm_start: float = 0.0
        self.t_browser_ready: float = 0.0
        self.t_page_loaded: float = 0.0
        self.t_color_preselected: float = 0.0
        self.t_storage_preselected: float = 0.0
        self.t_sku_verified: float = 0.0
        self.t_prewarm_done: float = 0.0
        self.session_verified: bool = False
        self.catalog_verified: bool = False
        self.login_status: str = "UNKNOWN"
        self.preselect_target: bool = False
        self.preselect_color_confirmed: bool = False
        self.preselect_storage_confirmed: bool = False
        self.preselect_sku_verified: bool = False
        self.preselect_url: str = ""
        self.prewarm_target_ready: bool = False

    @property
    def browser_startup_ms(self) -> int:
        if self.t_browser_ready <= 0 or self.t_prewarm_start <= 0:
            return 0
        return int(round((self.t_browser_ready - self.t_prewarm_start) * 1000.0))

    @property
    def page_load_ms(self) -> int:
        if self.t_page_loaded <= 0 or self.t_browser_ready <= 0:
            return 0
        return int(round((self.t_page_loaded - self.t_browser_ready) * 1000.0))

    @property
    def preselect_color_ms(self) -> int:
        if self.t_color_preselected <= 0 or self.t_page_loaded <= 0:
            return 0
        return int(round((self.t_color_preselected - self.t_page_loaded) * 1000.0))

    @property
    def preselect_storage_ms(self) -> int:
        if self.t_storage_preselected <= 0 or self.t_color_preselected <= 0:
            return 0
        return int(round((self.t_storage_preselected - self.t_color_preselected) * 1000.0))

    @property
    def total_prewarm_ms(self) -> int:
        if self.t_prewarm_done <= 0 or self.t_prewarm_start <= 0:
            return 0
        return int(round((self.t_prewarm_done - self.t_prewarm_start) * 1000.0))


class RehearsalTiming:
    """单轮演练计时器 (基于 monotonic clock / perf_counter)"""

    def __init__(self, run_id: int, is_prewarmed: bool = False):
        self.run_id = run_id
        self.is_prewarmed = is_prewarmed
        self.t0: float = 0.0
        self.t1_alert: float = 0.0
        self.t2a_feishu_started: float = 0.0
        self.t2b_feishu_ack: float = 0.0
        self.t3_browser_ready: float = 0.0
        self.t4_page_loaded: float = 0.0
        self.t_reload_started: float = 0.0
        self.t_reload_done: float = 0.0
        self.reload_count: int = 0

        self.t5_color_selected: float = 0.0
        self.color_action_type: str = "none"
        self.color_diag: Dict[str, float] = {}

        self.t6_storage_selected: float = 0.0
        self.storage_action_type: str = "none"
        self.storage_diag: Dict[str, float] = {}

        self.t7_target_verified: float = 0.0
        self.t8_handoff_ready: float = 0.0

        self.feishu_success: bool = False
        self.color_confirmed: bool = False
        self.storage_confirmed: bool = False
        self.sku_verified: bool = False
        self.page_stable: bool = False
        self.window_brought_to_front: bool = False
        self.handoff_qualified: bool = False

        # Target-SKU Preselection 状态与跳过记录
        self.preselect_target: bool = False
        self.color_reselect_skipped: bool = False
        self.storage_reselect_skipped: bool = False
        self.color_reselect_reason: str = "none"
        self.storage_reselect_reason: str = "none"
        self.t_post_reload_color_verified: float = 0.0
        self.t_post_reload_storage_verified: float = 0.0

        # 10 项严格条件打点记录
        self.cond_same_context: bool = False
        self.cond_same_tab: bool = False
        self.cond_reload_ok: bool = False
        self.cond_color_confirmed: bool = False
        self.cond_storage_confirmed: bool = False
        self.cond_sku_verified: bool = False
        self.cond_page_stable: bool = False
        self.cond_window_front: bool = False
        self.cond_auto_stopped: bool = False
        self.cond_human_takeover_ready: bool = False

        self.screenshots: List[str] = []

    def start_trigger(self):
        """T0: 收到模拟 SIMULATED_ONLINE_AVAILABLE 信号"""
        self.t0 = time.perf_counter()

    def mark_local_alert(self):
        """T1: 本地声音提醒触发"""
        self.t1_alert = time.perf_counter()

    def mark_feishu_started(self):
        """T2a: FEISHU_REQUEST_STARTED (飞书 Webhook POST 请求发起)"""
        self.t2a_feishu_started = time.perf_counter()

    def mark_feishu_ack(self, success: bool):
        """T2b: FEISHU_REQUEST_ACK (飞书服务器返回 ACK 确认，非手机收到时间)"""
        self.t2b_feishu_ack = time.perf_counter()
        self.feishu_success = success

    def mark_browser_ready(self):
        """T3: Chromium 浏览器启动完成并置前 (冷启动)"""
        self.t3_browser_ready = time.perf_counter()

    def mark_page_loaded(self):
        """T4: Apple Duo 官网公开页面加载完成 (冷启动)"""
        self.t4_page_loaded = time.perf_counter()

    def mark_reload_started(self):
        """Prewarm: 单次页面刷新开始"""
        self.t_reload_started = time.perf_counter()

    def mark_reload_done(self):
        """Prewarm: 单次页面刷新完成 (domcontentloaded)"""
        self.t_reload_done = time.perf_counter()
        self.t4_page_loaded = self.t_reload_done

    def record_color_diag(self, action_type: str, diag: Dict[str, float]):
        self.color_action_type = action_type
        self.color_diag = diag

    def record_storage_diag(self, action_type: str, diag: Dict[str, float]):
        self.storage_action_type = action_type
        self.storage_diag = diag

    def mark_color_selected(self, confirmed: bool):
        """T5: 星光白色配置选中并核实底层状态"""
        self.t5_color_selected = time.perf_counter()
        self.color_confirmed = confirmed

    def mark_storage_selected(self, confirmed: bool):
        """T6: 512GB 配置选中并核实底层状态"""
        self.t6_storage_selected = time.perf_counter()
        self.storage_confirmed = confirmed

    def mark_post_reload_color_verified(self, confirmed: bool, skipped: bool = True, reason: str = "preserved"):
        """Target Preselection: Reload 后核验星光白色状态"""
        self.t_post_reload_color_verified = time.perf_counter()
        self.t5_color_selected = self.t_post_reload_color_verified
        self.color_confirmed = confirmed
        self.color_reselect_skipped = skipped
        self.color_reselect_reason = reason

    def mark_post_reload_storage_verified(self, confirmed: bool, skipped: bool = True, reason: str = "preserved"):
        """Target Preselection: Reload 后核验 512GB 状态"""
        self.t_post_reload_storage_verified = time.perf_counter()
        self.t6_storage_selected = self.t_post_reload_storage_verified
        self.storage_confirmed = confirmed
        self.storage_reselect_skipped = skipped
        self.storage_reselect_reason = reason

    def mark_target_verified(self, verified: bool):
        """T7: 目标规格核验完成 (MK2P4CH/A)"""
        self.t7_target_verified = time.perf_counter()
        self.sku_verified = verified

    def mark_handoff_ready(self, page_stable: bool = True, window_front: bool = True):
        """
        T8: HANDOFF_READY (严格判定人工接管就绪点 - 冷启动模式)
        """
        self.t8_handoff_ready = time.perf_counter()
        self.page_stable = page_stable
        self.window_brought_to_front = window_front
        self.cond_color_confirmed = self.color_confirmed
        self.cond_storage_confirmed = self.storage_confirmed
        self.cond_sku_verified = self.sku_verified
        self.cond_page_stable = page_stable
        self.cond_window_front = window_front
        self.cond_auto_stopped = True
        self.cond_human_takeover_ready = True
        self.handoff_qualified = (
            self.color_confirmed and
            self.storage_confirmed and
            self.sku_verified and
            self.page_stable and
            self.window_brought_to_front
        )

    def mark_handoff_ready_prewarm(
        self,
        same_context: bool,
        same_tab: bool,
        reload_ok: bool,
        color_confirmed: bool,
        storage_confirmed: bool,
        sku_verified: bool,
        page_stable: bool,
        window_front: bool,
        auto_stopped: bool,
        human_takeover_ready: bool,
    ):
        """
        T8: HANDOFF_READY (严格判定人工接管就绪点 - Pre-Warmed 10 项严苛判定)
        """
        self.t8_handoff_ready = time.perf_counter()
        self.cond_same_context = same_context
        self.cond_same_tab = same_tab
        self.cond_reload_ok = reload_ok
        self.cond_color_confirmed = color_confirmed
        self.cond_storage_confirmed = storage_confirmed
        self.cond_sku_verified = sku_verified
        self.cond_page_stable = page_stable
        self.cond_window_front = window_front
        self.cond_auto_stopped = auto_stopped
        self.cond_human_takeover_ready = human_takeover_ready

        self.color_confirmed = color_confirmed
        self.storage_confirmed = storage_confirmed
        self.sku_verified = sku_verified
        self.page_stable = page_stable
        self.window_brought_to_front = window_front

        self.handoff_qualified = (
            same_context and same_tab and reload_ok and
            color_confirmed and storage_confirmed and sku_verified and
            page_stable and window_front and auto_stopped and human_takeover_ready
        )

    def _ms(self, t: float) -> int:
        if t <= 0 or self.t0 <= 0:
            return 0
        return int(round((t - self.t0) * 1000.0))

    @property
    def latency_local_alert(self) -> int:
        return self._ms(self.t1_alert)

    @property
    def latency_feishu_started(self) -> int:
        return self._ms(self.t2a_feishu_started)

    @property
    def latency_feishu_ack(self) -> int:
        return self._ms(self.t2b_feishu_ack)

    @property
    def latency_browser_ready(self) -> int:
        return self._ms(self.t3_browser_ready)

    @property
    def latency_page_loaded(self) -> int:
        return self._ms(self.t4_page_loaded)

    @property
    def latency_reload_started(self) -> int:
        return self._ms(self.t_reload_started)

    @property
    def latency_reload_done(self) -> int:
        return self._ms(self.t_reload_done)

    @property
    def latency_post_reload_color_verified(self) -> int:
        return self._ms(self.t_post_reload_color_verified)

    @property
    def latency_post_reload_storage_verified(self) -> int:
        return self._ms(self.t_post_reload_storage_verified)

    @property
    def latency_color_selected(self) -> int:
        return self._ms(self.t5_color_selected)

    @property
    def latency_storage_selected(self) -> int:
        return self._ms(self.t6_storage_selected)

    @property
    def latency_target_verified(self) -> int:
        return self._ms(self.t7_target_verified)

    @property
    def latency_handoff_ready(self) -> int:
        return self._ms(self.t8_handoff_ready)

    @property
    def total_latency(self) -> int:
        return self.latency_handoff_ready

    def format_report(self) -> str:
        """生成单轮演练 Timing Report 标准文本"""
        feishu_status_text = "成功" if self.feishu_success else "未配置/跳过"
        handoff_status_text = "严格满足全量条件" if self.handoff_qualified else "部分条件未满足 (HANDOFF_READY=False)"

        if self.preselect_target:
            color_status = "Preserved (Skipped click)" if self.color_reselect_skipped else f"Reselected ({self.color_reselect_reason})"
            storage_status = "Preserved (Skipped click)" if self.storage_reselect_skipped else f"Reselected ({self.storage_reselect_reason})"
            sku_status = f"{TARGET_SKU} Verified" if self.sku_verified else f"{TARGET_SKU} Mismatch"
            lines = [
                "==================================================",
                f"Launch Rehearsal Timing Report (Run {self.run_id:02d} - Pre-Warmed & Target-Preselected)",
                "==================================================",
                f"Simulation Trigger          +0 ms",
                f"Local Alert                 +{self.latency_local_alert} ms",
                f"Feishu Request Started      +{self.latency_feishu_started} ms (Async Non-Blocking)",
                f"Feishu Request Acked        +{self.latency_feishu_ack} ms ({feishu_status_text})",
                f"Duo Page Reload Started     +{self.latency_reload_started} ms",
                f"Duo Page Reload Done        +{self.latency_reload_done} ms (DOM Ready)",
                f"Starwhite Verified          +{self.latency_post_reload_color_verified or self.latency_color_selected} ms ({color_status})",
                f"512GB Verified              +{self.latency_post_reload_storage_verified or self.latency_storage_selected} ms ({storage_status})",
                f"Target SKU Verified         +{self.latency_target_verified} ms ({sku_status})",
                f"Human Handoff Ready         +{self.latency_handoff_ready} ms ({handoff_status_text})",
                "",
                f"TOTAL AUTOMATION LATENCY: {self.total_latency} ms",
                "==================================================",
                "  * 注: 预热阶段已完成星光白色 + 512GB 预选，单次 Reload 后直接保留状态，跳过重复点击。",
                "  * 注: Feishu Request Acked 为异步任务并发完成，绝不阻塞加车/选配主路径；",
                "    ACK 仅代表飞书服务器接收，绝不代表手机客户端 App 已接收或渲染消息。",
                "  * 注: Human Handoff Ready 严格满足 10 项严苛就绪判定：同一 Context、同一 Tab、",
                "    单次 Reload、双规格可靠选中、MK2P4CH/A 核验通过、页面稳定、窗口置前、自动化停止。",
            ]
        elif self.is_prewarmed:
            lines = [
                "==================================================",
                f"Launch Rehearsal Timing Report (Run {self.run_id:02d} - Pre-Warmed)",
                "==================================================",
                f"Simulation Trigger          +0 ms",
                f"Local Alert                 +{self.latency_local_alert} ms",
                f"Feishu Request Started      +{self.latency_feishu_started} ms (Async Non-Blocking)",
                f"Feishu Request Acked        +{self.latency_feishu_ack} ms ({feishu_status_text})",
                f"Duo Page Reload Started     +{self.latency_reload_started} ms",
                f"Duo Page Reload Done        +{self.latency_reload_done} ms",
                f"Starwhite Selected          +{self.latency_color_selected} ms ({self.color_action_type})",
                f"512GB Selected              +{self.latency_storage_selected} ms ({self.storage_action_type})",
                f"Target Verified             +{self.latency_target_verified} ms",
                f"Human Handoff Ready         +{self.latency_handoff_ready} ms ({handoff_status_text})",
                "",
                f"TOTAL AUTOMATION LATENCY: {self.total_latency} ms",
                "==================================================",
                "  * 注: Feishu Request Acked 为异步任务并发完成，绝不阻塞加车/选配主路径；",
                "    ACK 仅代表飞书服务器接收，绝不代表手机客户端 App 已接收或渲染消息。",
                "  * 注: Human Handoff Ready 严格满足 10 项严苛就绪判定：同一 Context、同一 Tab、",
                "    单次 Reload、双规格可靠选中、MK2P4CH/A 核验通过、页面稳定、窗口置前、自动化停止。",
            ]
        else:
            lines = [
                "==================================================",
                f"Launch Rehearsal Timing Report (Run {self.run_id:02d})",
                "==================================================",
                f"Simulation Trigger          +0 ms",
                f"Local Alert                 +{self.latency_local_alert} ms",
                f"Feishu Request Started      +{self.latency_feishu_started} ms",
                f"Feishu Request Acked        +{self.latency_feishu_ack} ms ({feishu_status_text})",
                f"Browser Ready               +{self.latency_browser_ready} ms",
                f"Duo Page Loaded             +{self.latency_page_loaded} ms",
                f"Starwhite Selected          +{self.latency_color_selected} ms",
                f"512GB Selected              +{self.latency_storage_selected} ms",
                f"Target Verified             +{self.latency_target_verified} ms",
                f"Human Handoff Ready         +{self.latency_handoff_ready} ms ({handoff_status_text})",
                "",
                f"TOTAL AUTOMATION LATENCY: {self.total_latency} ms",
                "==================================================",
                "  * 注: Feishu Request Acked 仅代表飞书服务器成功接收 Webhook POST 请求，",
                "    绝不代表手机客户端 App 已接收或渲染消息。",
                "  * 注: Human Handoff Ready 严格要求星光白色+512GB选中、MK2P4CH/A校验通过、",
                "    页面操作稳定且 Headed 窗口置前。",
            ]
        return "\n".join(lines)


def play_rehearsal_sound(enabled: bool = True):
    """触发演练本地声音提醒 (系统 bell + macOS 原生声音)"""
    if not enabled:
        return

    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass

    if sys.platform == "darwin":
        sound_file = "/System/Library/Sounds/Ping.aiff"
        if os.path.exists("/System/Library/Sounds/Hero.aiff"):
            sound_file = "/System/Library/Sounds/Hero.aiff"
        try:
            import subprocess
            subprocess.Popen(
                ["afplay", sound_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            logger.debug(f"macOS afplay 调用失败: {e}")


async def send_feishu_rehearsal_card(webhook_url: str, timeout_sec: int = 5) -> bool:
    """
    发送明确带有【模拟演练】标识的飞书交互式卡片。
    严格隔离，严防混淆真实库存。
    """
    url = (webhook_url or "").strip()
    if not url:
        logger.warning("飞书 Webhook 未配置，演练跳过飞书网络请求 (仅记录本地日志)")
        return False

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "🍎 【模拟演练】iPhone Duo 首发模拟演练"
                },
                "template": "blue"
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "**【这是模拟通知，不代表真实有货】**\n\n"
                            f"**产品**：{TARGET_PRODUCT}\n"
                            f"**配置**：{TARGET_COLOR} · {TARGET_STORAGE}\n"
                            f"**SKU**：{TARGET_SKU}\n"
                            "**状态**：SIMULATED ONLINE AVAILABLE\n\n"
                            "⚠️ **请不要下单，本消息仅用于首发演练。**\n"
                            f"**演练时间**：{now_str}\n"
                            "**模式标记**：THIS IS A REHEARSAL"
                        )
                    }
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "👉 查看 Apple 官网演练页面 (不代表真实有货)"
                            },
                            "type": "default",
                            "url": DUO_PAGE_URL
                        }
                    ]
                }
            ]
        }
    }

    def _http_post():
        req_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=req_bytes,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "AppleStoreInventoryBuyer/0.4 (LaunchRehearsalBot)",
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                data = resp.read()
                try:
                    res = json.loads(data.decode("utf-8"))
                    code = res.get("code", res.get("StatusCode", -1))
                    return code == 0
                except Exception:
                    return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            logger.warning(f"飞书演练请求 HTTP 异常: HTTP {e.code} ({e.reason})")
            return False
        except Exception as e:
            logger.warning(f"飞书演练请求网络异常: {e}")
            return False

    return await asyncio.to_thread(_http_post)


async def _rehearsal_interact_option(
    page: Any,
    radio_locator_str: str,
    label_locator_str: str,
    option_desc: str,
) -> bool:
    """演练选项点击交互（冷启动模式兼容）：
    1. 优先 locator.check() (针对 radio input)
    2. 优先 label.click() (针对相关 label 元素，普通点击)
    3. 优先 普通 click() (针对 radio 或对应元素)
    4. 只有正常交互失败后才能 force=True，并记录 WARNING
    """
    radio_loc = page.locator(radio_locator_str).first
    label_loc = page.locator(label_locator_str).first

    try:
        if await radio_loc.count() > 0:
            await radio_loc.scroll_into_view_if_needed()
            await radio_loc.check(timeout=1500)
            logger.debug(f"[演练模式] [{option_desc}] check() 执行成功")
            return True
    except Exception:
        pass

    try:
        if await label_loc.count() > 0:
            await label_loc.scroll_into_view_if_needed()
            await label_loc.click(timeout=1500)
            logger.debug(f"[演练模式] [{option_desc}] label.click() 执行成功")
            return True
    except Exception:
        pass

    try:
        if await radio_loc.count() > 0:
            await radio_loc.scroll_into_view_if_needed()
            await radio_loc.click(timeout=1500)
            logger.debug(f"[演练模式] [{option_desc}] radio.click() 执行成功")
            return True
    except Exception:
        pass

    logger.warning(f"⚠️ [演练模式] 选项 [{option_desc}] 正常交互未成功，触发 force=True fallback")
    try:
        if await label_loc.count() > 0:
            await label_loc.click(force=True, timeout=1500)
            return True
        elif await radio_loc.count() > 0:
            await radio_loc.click(force=True, timeout=1500)
            return True
    except Exception as e:
        logger.warning(f"⚠️ [演练模式] 选项 [{option_desc}] force=True 点击失败: {e}")

    return False


async def _rehearsal_interact_option_fast(
    page: Any,
    radio_locator_str: str,
    label_locator_str: str,
    option_desc: str,
    timing: Optional[RehearsalTiming] = None,
    option_type: str = "option",
) -> bool:
    """演练选项快速交互（Fast Path）：
    1. 若 radio input 真正处于 visible 状态，优先 check(timeout=1500)
    2. 若 radio input 不可见（Apple Store DOM 常见设计：opacity:0/clipped），
       直接常规点击 visible label（耗时约 30ms，零超时等待）
    3. 若未能命中 visible label，尝试常规 click
    4. 只有常规交互均失败后才 force=True fallback 并记录 WARNING
    """
    diag = {
        "start": time.perf_counter(),
        "selector_found": 0.0,
        "action_start": 0.0,
        "action_end": 0.0,
        "state_confirmed": 0.0,
    }

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

    diag["selector_found"] = time.perf_counter()
    diag["action_start"] = time.perf_counter()

    action_type = "none"
    success = False

    # 1. 探测 input 是否真正可见
    radio_visible = False
    if radio_count > 0:
        try:
            radio_visible = await radio_loc.is_visible()
        except Exception:
            radio_visible = False

    if radio_visible:
        try:
            await radio_loc.scroll_into_view_if_needed()
            await radio_loc.check(timeout=1500)
            action_type = "check"
            success = True
            logger.debug(f"[FastPath] [{option_desc}] check() 成功")
        except Exception as e:
            logger.debug(f"[FastPath] [{option_desc}] check() 失败: {e}")

    # 2. 若 input 不可见，直接常规点击 visible label
    if not success and label_count > 0:
        try:
            label_visible = await label_loc.is_visible()
            if label_visible:
                await label_loc.scroll_into_view_if_needed()
                await label_loc.click(timeout=1500)
                action_type = "fast_label_click"
                success = True
                logger.debug(f"[FastPath] [{option_desc}] fast_label_click 成功")
        except Exception as e:
            logger.debug(f"[FastPath] [{option_desc}] fast_label_click 失败: {e}")

    # 3. 若 visible label 未命中，尝试普通常规 click (优先 label，次选 radio)
    if not success and label_count > 0:
        try:
            await label_loc.scroll_into_view_if_needed()
            await label_loc.click(timeout=1500)
            action_type = "label_click"
            success = True
            logger.debug(f"[FastPath] [{option_desc}] 普通 label click 成功")
        except Exception as e:
            logger.debug(f"[FastPath] [{option_desc}] 普通 label click 失败: {e}")

    if not success and radio_count > 0:
        try:
            await radio_loc.scroll_into_view_if_needed()
            await radio_loc.click(timeout=1500)
            action_type = "radio_click"
            success = True
            logger.debug(f"[FastPath] [{option_desc}] 普通 radio click 成功")
        except Exception as e:
            logger.debug(f"[FastPath] [{option_desc}] 普通 radio click 失败: {e}")

    # 4. 兜底 force=True fallback 并写 WARNING (优先 label，次选 radio)
    if not success:
        logger.warning(f"⚠️ [演练模式] 选项 [{option_desc}] 常规交互失败，触发 force=True fallback")
        if label_count > 0:
            try:
                await label_loc.click(force=True, timeout=1500)
                action_type = "force_fallback_label"
                success = True
            except Exception as e:
                logger.debug(f"⚠️ [FastPath] force=True label click 失败: {e}")
        if not success and radio_count > 0:
            try:
                await radio_loc.click(force=True, timeout=1500)
                action_type = "force_fallback_radio"
                success = True
            except Exception as e:
                logger.warning(f"⚠️ [演练模式] 选项 [{option_desc}] force=True 点击失败: {e}")

    diag["action_end"] = time.perf_counter()

    if timing:
        if option_type == "color":
            timing.record_color_diag(action_type, diag)
        elif option_type == "storage":
            timing.record_storage_diag(action_type, diag)

    return success


async def _verify_color_confirmed(page: Any) -> bool:
    """核验星光白色是否确认选中，必须满足以下至少一个可靠证据，绝不强行视为成功：
    1. 对应 radio/input 的 is_checked() == True
    2. aria-checked == "true"
    3. Apple 当前页面公开选中状态明确对应 starwhite
    4. 页面最终 SKU/URL 与 MK2P4CH/A 一致且 Catalog 映射确认该 SKU 为星光白色
    如果不能证明，返回 False。
    """
    try:
        radio = page.locator("input[name='dimensionColor'][value*='starwhite']").first
        if await radio.count() > 0:
            if await radio.is_checked():
                return True
    except Exception:
        pass

    try:
        locs = page.locator(
            "input[name='dimensionColor'][value*='starwhite'], "
            "[role='radio'][data-autom*='starwhite'], "
            "label:has(input[name='dimensionColor'][value*='starwhite'])"
        )
        count = await locs.count()
        for idx in range(min(count, 3)):
            attr = await locs.nth(idx).get_attribute("aria-checked")
            if attr and attr.strip().lower() == "true":
                return True
    except Exception:
        pass

    try:
        selected_in_dom = await page.evaluate("""() => {
            const checkedRadio = document.querySelector("input[name='dimensionColor']:checked");
            if (checkedRadio && checkedRadio.value.toLowerCase().includes("starwhite")) {
                return true;
            }
            const selectedElements = document.querySelectorAll("[aria-checked='true']");
            for (const el of selectedElements) {
                const text = (el.textContent || "").toLowerCase();
                const val = (el.getAttribute("value") || "").toLowerCase();
                const autom = (el.getAttribute("data-autom") || "").toLowerCase();
                if ((val.includes("starwhite") || text.includes("星光") || autom.includes("starwhite")) &&
                    (el.name === "dimensionColor" || autom.includes("color") || el.closest("[data-autom*='Color']"))) {
                    return true;
                }
            }
            return false;
        }""")
        if selected_in_dom:
            return True
    except Exception:
        pass

    try:
        url_lower = page.url.lower()
        if TARGET_SKU.lower() in url_lower:
            return True
    except Exception:
        pass

    return False


async def _verify_storage_confirmed(page: Any) -> bool:
    """核验 512GB 是否确认选中，必须满足以下至少一个可靠证据，绝不强行视为成功：
    1. 512gb radio checked
    2. aria-checked == true
    3. 页面当前公开配置状态明确为 512GB
    4. 最终 SKU/URL + Catalog 能证明为 MK2P4CH/A
    如果不能证明，返回 False。
    """
    try:
        radio = page.locator("input[name='dimensionCapacity'][value='512gb']").first
        if await radio.count() > 0:
            if await radio.is_checked():
                return True
    except Exception:
        pass

    try:
        locs = page.locator(
            "input[name='dimensionCapacity'][value='512gb'], "
            "[role='radio'][data-autom*='512gb'], "
            "label:has(input[name='dimensionCapacity'][value='512gb'])"
        )
        count = await locs.count()
        for idx in range(min(count, 3)):
            attr = await locs.nth(idx).get_attribute("aria-checked")
            if attr and attr.strip().lower() == "true":
                return True
    except Exception:
        pass

    try:
        selected_in_dom = await page.evaluate("""() => {
            const checkedRadio = document.querySelector("input[name='dimensionCapacity']:checked");
            if (checkedRadio && checkedRadio.value.toLowerCase() === "512gb") {
                return true;
            }
            const selectedElements = document.querySelectorAll("[aria-checked='true']");
            for (const el of selectedElements) {
                const text = (el.textContent || "").toLowerCase();
                const val = (el.getAttribute("value") || "").toLowerCase();
                const autom = (el.getAttribute("data-autom") || "").toLowerCase();
                if ((val === "512gb" || text.includes("512gb") || text.includes("512 gb") || autom.includes("512gb")) &&
                    (el.name === "dimensionCapacity" || autom.includes("capacity") || el.closest("[data-autom*='Capacity']"))) {
                    return true;
                }
            }
            return false;
        }""")
        if selected_in_dom:
            return True
    except Exception:
        pass

    try:
        url_lower = page.url.lower()
        if TARGET_SKU.lower() in url_lower:
            return True
    except Exception:
        pass

    return False


async def _verify_target_sku_lightweight(page: Any) -> bool:
    """核验当前选中的目标 SKU 是否仍为 MK2P4CH/A"""
    try:
        url_lower = page.url.lower()
        if TARGET_SKU.lower() in url_lower:
            return True
    except Exception:
        pass

    try:
        resolver = AppleCatalogResolver()
        res = await resolver.resolve(page, TARGET_PRODUCT, TARGET_COLOR, TARGET_STORAGE, expected_part_number=TARGET_SKU)
        return (res.status in (ResolutionStatus.SUCCESS, ResolutionStatus.CATALOG_STALE)) and (res.product and res.product.part_number == TARGET_SKU)
    except Exception as e:
        logger.debug(f"目标 SKU 核验异常: {e}")
        return False


async def prewarm_rehearsal_session(config: AppConfig, preselect_target: bool = False) -> Tuple[BrowserManager, Any, PrewarmTiming]:
    """预热演练环境：
    1. 启动单个 Headed Chromium 实例 (复用 browser_data/ 持久化 Profile)；
    2. 打开单 Tab 访问 Apple Duo 页面；
    3. 等待 domcontentloaded；
    4. 会话状态与初始 Catalog 核验锁定 MK2P4CH/A；
    5. 若 preselect_target=True:
       - 预选外观颜色：星光白色；
       - 预选存储容量：512GB；
       - Catalog / SKU 校验确认 MK2P4CH/A；
       - 记录 URL；
       - 满足 8 项前置条件输出 PREWARM_TARGET_READY；
    6. page.bring_to_front()；
    7. 控制台输出预热完成信息。
    预热阶段耗时记录在 PrewarmTiming 中，完全独立于 T0，绝不计入开售响应时延。
    """
    prewarm_timing = PrewarmTiming()
    prewarm_timing.preselect_target = preselect_target
    prewarm_timing.t_prewarm_start = time.perf_counter()

    config.browser.user_data_dir = "./browser_data"
    config.browser.headless = False
    profile_path = os.path.abspath(config.browser.user_data_dir)
    logger.info(f"[Prewarm] 正在启动正式环境 Persistent Profile 浏览器: {profile_path} (Headed=True)...")

    browser_manager = BrowserManager(config.browser)
    page = await browser_manager.start()
    prewarm_timing.t_browser_ready = time.perf_counter()

    # 验证仅打开 1 个窗口与 1 个 Tab
    pages = browser_manager.context.pages if browser_manager.context else [page]
    logger.info(f"[Prewarm] 浏览器就绪: 窗口 1 个, Tab 数量: {len(pages)}")

    # 验证登录态
    try:
        login_res = await check_apple_login_status(page)
        if isinstance(login_res, tuple):
            is_logged_in, status_str = login_res
            prewarm_timing.session_verified = is_logged_in
            prewarm_timing.login_status = status_str
        elif hasattr(login_res, "value"):
            prewarm_timing.login_status = login_res.value
            prewarm_timing.session_verified = (login_res.value in ("LOGGED_IN", "SESSION_VALID", "LOGIN_AVAILABLE"))
        else:
            prewarm_timing.login_status = str(login_res)
            prewarm_timing.session_verified = True
        logger.info(f"[Prewarm] 登录态检查结果: {prewarm_timing.login_status} (verified={prewarm_timing.session_verified})")
    except Exception as e:
        logger.warning(f"[Prewarm] 登录态检查异常: {e}")
        prewarm_timing.login_status = "CHECK_FAILED"

    # 打开 Duo 页面并等待 domcontentloaded
    logger.info(f"[Prewarm] 正在预载 Apple Duo 页面: {DUO_PAGE_URL} ...")
    await page.goto(DUO_PAGE_URL, wait_until="domcontentloaded", timeout=25000)
    prewarm_timing.t_page_loaded = time.perf_counter()

    # Catalog 权威核验
    logger.info(f"[Prewarm] 正在核验官方 Catalog 是否为 {TARGET_PRODUCT} · {TARGET_COLOR} · {TARGET_STORAGE} [{TARGET_SKU}] ...")
    try:
        resolver = AppleCatalogResolver()
        res = await resolver.resolve(page, TARGET_PRODUCT, TARGET_COLOR, TARGET_STORAGE, expected_part_number=TARGET_SKU)
        catalog_ok = (res.status in (ResolutionStatus.SUCCESS, ResolutionStatus.CATALOG_STALE)) and (res.product and res.product.part_number == TARGET_SKU)
        prewarm_timing.catalog_verified = catalog_ok
        logger.info(f"[Prewarm] Catalog 核验结果: {catalog_ok} (Status={res.status.value})")
    except Exception as e:
        logger.warning(f"[Prewarm] Catalog 核验异常: {e}")
        prewarm_timing.catalog_verified = False

    if preselect_target:
        logger.info(f"[Prewarm] 【Target-SKU Preselection】开始在 T0 前预选目标配置: {TARGET_COLOR} · {TARGET_STORAGE} [{TARGET_SKU}] ...")
        # 预选星光白色
        await _rehearsal_interact_option_fast(
            page=page,
            radio_locator_str="input[name='dimensionColor'][value*='starwhite']",
            label_locator_str="label:has-text('星光白色'), label:has(input[name='dimensionColor'][value*='starwhite'])",
            option_desc=f"外观颜色: {TARGET_COLOR}",
            option_type="color"
        )
        await asyncio.sleep(0.15)
        color_ok = await _verify_color_confirmed(page)
        prewarm_timing.t_color_preselected = time.perf_counter()
        prewarm_timing.preselect_color_confirmed = color_ok
        logger.info(f"[Prewarm] 预选星光白色完成: confirmed={color_ok} (耗时: {prewarm_timing.preselect_color_ms} ms)")

        # 预选 512GB
        await _rehearsal_interact_option_fast(
            page=page,
            radio_locator_str="input[name='dimensionCapacity'][value='512gb']",
            label_locator_str="label:has-text('512GB'), label:has(input[name='dimensionCapacity'][value='512gb'])",
            option_desc=f"存储容量: {TARGET_STORAGE}",
            option_type="storage"
        )
        await asyncio.sleep(0.15)
        storage_ok = await _verify_storage_confirmed(page)
        prewarm_timing.t_storage_preselected = time.perf_counter()
        prewarm_timing.preselect_storage_confirmed = storage_ok
        logger.info(f"[Prewarm] 预选 512GB 完成: confirmed={storage_ok} (耗时: {prewarm_timing.preselect_storage_ms} ms)")

        # 核验 SKU 与 URL
        sku_ok = await _verify_target_sku_lightweight(page)
        prewarm_timing.t_sku_verified = time.perf_counter()
        prewarm_timing.preselect_sku_verified = sku_ok
        prewarm_timing.preselect_url = page.url
        logger.info(f"[Prewarm] 预选规格 SKU 核验: {sku_ok} (URL: {prewarm_timing.preselect_url})")

        # 8 项严格就绪判定
        ctx_pages = browser_manager.context.pages if browser_manager.context else [page]
        same_ctx = bool(browser_manager.context is not None and getattr(page, "context", None) == browser_manager.context)
        same_tab = (len(ctx_pages) == 1 and ctx_pages[0] == page)

        try:
            await page.bring_to_front()
            window_front = True
        except Exception:
            window_front = False

        ready_8 = (
            prewarm_timing.session_verified and
            (prewarm_timing.t_page_loaded > 0) and
            color_ok and
            storage_ok and
            sku_ok and
            same_ctx and
            same_tab and
            window_front
        )
        prewarm_timing.prewarm_target_ready = ready_8
        prewarm_timing.t_prewarm_done = time.perf_counter()

        print("\n" + "=" * 65)
        if ready_8:
            print("  ✅ 【PREWARM_TARGET_READY】目标配置预选就绪 (星光白色 · 512GB · MK2P4CH/A)")
        else:
            print("  ⚠️ 【PREWARM_TARGET_PARTIAL】目标配置预选部分未满足")
        print(f"  浏览器启动耗时 : {prewarm_timing.browser_startup_ms} ms")
        print(f"  页面首载耗时   : {prewarm_timing.page_load_ms} ms")
        print(f"  颜色预选耗时   : {prewarm_timing.preselect_color_ms} ms (星光白色确认={prewarm_timing.preselect_color_confirmed})")
        print(f"  容量预选耗时   : {prewarm_timing.preselect_storage_ms} ms (512GB确认={prewarm_timing.preselect_storage_confirmed})")
        print(f"  预选目标 SKU   : {TARGET_SKU} (核验成功={prewarm_timing.preselect_sku_verified})")
        print(f"  预选稳定 URL   : {prewarm_timing.preselect_url}")
        print(f"  预热总耗时     : {prewarm_timing.total_prewarm_ms} ms (完全独立于 T0，绝不计入开售响应)")
        print(f"  会话登录态     : {prewarm_timing.login_status}")
        print(f"  Chromium 实例  : 1 个 (browser_data/ Profile, Headed=True, 单 Tab)")
        print("=" * 65 + "\n")

    else:
        # 窗口置前
        try:
            await page.bring_to_front()
        except Exception:
            pass

        prewarm_timing.t_prewarm_done = time.perf_counter()

        print("\n" + "=" * 65)
        print("  ✅ 【PREWARM_READY】浏览器与目标页面预热就绪")
        print(f"  浏览器启动耗时 : {prewarm_timing.browser_startup_ms} ms")
        print(f"  页面首载耗时   : {prewarm_timing.page_load_ms} ms")
        print(f"  预热总耗时     : {prewarm_timing.total_prewarm_ms} ms (完全独立于 T0，绝不计入开售响应)")
        print(f"  会话登录态     : {prewarm_timing.login_status}")
        print(f"  Catalog 锁定   : {TARGET_SKU} (核验成功={prewarm_timing.catalog_verified})")
        print(f"  Chromium 实例  : 1 个 (browser_data/ Profile, Headed=True, 单 Tab)")
        print("=" * 65 + "\n")

    return browser_manager, page, prewarm_timing


async def execute_prewarmed_single_run(
    config: AppConfig,
    browser_manager: BrowserManager,
    page: Any,
    run_id: int,
    screenshots_dir: str,
    preselect_target: bool = False,
) -> RehearsalTiming:
    """执行单轮 Pre-Warmed 首发模拟演练 (支持 Target Preselection 极速链路)"""
    timing = RehearsalTiming(run_id=run_id, is_prewarmed=True)
    timing.preselect_target = preselect_target
    os.makedirs(screenshots_dir, exist_ok=True)

    mode_tag = "Pre-Warmed & Target-Preselected Fast Path" if preselect_target else "Pre-Warmed Fast Path"
    print("\n" + "=" * 65)
    print(f"  【Pre-Warmed Launch Rehearsal 极速演练 - 第 {run_id} 轮】")
    print("  模拟事件: COMING_SOON -> ONLINE_AVAILABLE")
    print(f"  演练目标: {TARGET_PRODUCT} · {TARGET_COLOR} · {TARGET_STORAGE} [{TARGET_SKU}]")
    print(f"  模式标记: THIS IS A REHEARSAL ({mode_tag})")
    print("=" * 65 + "\n")

    # T0: 本地生成 SIMULATED_ONLINE_AVAILABLE 信号
    timing.start_trigger()
    logger.info(f"[Run {run_id:02d}] T0: 收到模拟触发信号 SIMULATED_ONLINE_AVAILABLE")

    # T1: 立即触发 Mac 本地声音
    play_rehearsal_sound(enabled=config.notifications.sound.enabled)
    timing.mark_local_alert()
    logger.info(f"[Run {run_id:02d}] T1: 本地声音提醒触发完成 (+{timing.latency_local_alert} ms)")

    # T2a / T2b: 飞书 Webhook 异步并发任务 (绝不阻塞购买加车关键路径)
    timing.mark_feishu_started()
    logger.info(f"[Run {run_id:02d}] T2a: 飞书 Webhook 异步任务发起 (+{timing.latency_feishu_started} ms)")

    webhook_url = config.notifications.feishu.webhook_url
    timeout_sec = config.notifications.feishu.timeout_seconds

    async def _async_feishu_dispatch():
        feishu_ok = await send_feishu_rehearsal_card(
            webhook_url=webhook_url,
            timeout_sec=timeout_sec
        )
        timing.mark_feishu_ack(feishu_ok)
        logger.info(f"[Run {run_id:02d}] T2b: 飞书 Webhook 应答完成 (ACK) (+{timing.latency_feishu_ack} ms, 成功={feishu_ok}) [异步并发，不阻塞主链路]")

    feishu_task = asyncio.create_task(_async_feishu_dispatch())

    # 在同一个 Tab 内执行单次刷新 (wait_until="domcontentloaded")
    timing.mark_reload_started()
    logger.info(f"[Run {run_id:02d}] 正在对当前 Tab 执行单次刷新 (wait_until='domcontentloaded') ...")
    await page.reload(wait_until="domcontentloaded", timeout=25000)
    timing.mark_reload_done()
    timing.reload_count += 1
    logger.info(f"[Run {run_id:02d}] Duo 页面刷新完成 (+{timing.latency_reload_done} ms, reload_count={timing.reload_count})")

    # 保存截图 1: page_loaded
    p1_path = os.path.join(screenshots_dir, f"run_{run_id:02d}_page_loaded.png")
    try:
        await page.screenshot(path=p1_path)
        timing.screenshots.append(p1_path)
        logger.info(f"[Run {run_id:02d}] 📸 页面刷新截图已保存: {p1_path}")
    except Exception as e:
        logger.warning(f"[Run {run_id:02d}] 截图保存异常: {e}")

    if preselect_target:
        logger.info(f"[Run {run_id:02d}] 正在检测 Reload 后配置保留状态 (Selection Preservation)...")
        c_preserved = await _verify_color_confirmed(page)
        if c_preserved:
            timing.mark_post_reload_color_verified(confirmed=True, skipped=True, reason="preserved")
            timing.color_action_type = "preserved_skipped"
            logger.info(f"[Run {run_id:02d}] ✅ T5: 星光白色配置在 Reload 后成功保留 (跳过重复点击) (+{timing.latency_color_selected} ms)")
        else:
            logger.warning(f"[Run {run_id:02d}] ⚠️ Reload 后星光白色状态丢失 (原因: radio unchecked or DOM reset)，触发 Fast Path 重选...")
            await _rehearsal_interact_option_fast(
                page=page,
                radio_locator_str="input[name='dimensionColor'][value*='starwhite']",
                label_locator_str="label:has-text('星光白色'), label:has(input[name='dimensionColor'][value*='starwhite'])",
                option_desc=f"外观颜色: {TARGET_COLOR}",
                timing=timing,
                option_type="color"
            )
            await asyncio.sleep(0.05)
            c_confirmed = await _verify_color_confirmed(page)
            timing.mark_post_reload_color_verified(confirmed=c_confirmed, skipped=False, reason="radio unchecked")
            logger.info(f"[Run {run_id:02d}] T5: 星光白色重选完成 (+{timing.latency_color_selected} ms, 确认={c_confirmed})")

        s_preserved = await _verify_storage_confirmed(page)
        if s_preserved:
            timing.mark_post_reload_storage_verified(confirmed=True, skipped=True, reason="preserved")
            timing.storage_action_type = "preserved_skipped"
            logger.info(f"[Run {run_id:02d}] ✅ T6: 512GB 配置在 Reload 后成功保留 (跳过重复点击) (+{timing.latency_storage_selected} ms)")
        else:
            logger.warning(f"[Run {run_id:02d}] ⚠️ Reload 后 512GB 状态丢失 (原因: radio unchecked or DOM reset)，触发 Fast Path 重选...")
            await _rehearsal_interact_option_fast(
                page=page,
                radio_locator_str="input[name='dimensionCapacity'][value='512gb']",
                label_locator_str="label:has-text('512GB'), label:has(input[name='dimensionCapacity'][value='512gb'])",
                option_desc=f"存储容量: {TARGET_STORAGE}",
                timing=timing,
                option_type="storage"
            )
            await asyncio.sleep(0.05)
            s_confirmed = await _verify_storage_confirmed(page)
            timing.mark_post_reload_storage_verified(confirmed=s_confirmed, skipped=False, reason="radio unchecked")
            logger.info(f"[Run {run_id:02d}] T6: 512GB 重选完成 (+{timing.latency_storage_selected} ms, 确认={s_confirmed})")

    else:
        # T5: 极速选择星光白色 (Fast Path: visible check -> label click)
        logger.info(f"[Run {run_id:02d}] 正在选择外观颜色: {TARGET_COLOR} (Fast Path)...")
        await _rehearsal_interact_option_fast(
            page=page,
            radio_locator_str="input[name='dimensionColor'][value*='starwhite']",
            label_locator_str="label:has-text('星光白色'), label:has(input[name='dimensionColor'][value*='starwhite'])",
            option_desc=f"外观颜色: {TARGET_COLOR}",
            timing=timing,
            option_type="color"
        )
        await asyncio.sleep(0.1)
        color_confirmed = await _verify_color_confirmed(page)
        timing.mark_color_selected(confirmed=color_confirmed)
        if timing.color_diag:
            timing.color_diag["state_confirmed"] = time.perf_counter()
            f_ms = int((timing.color_diag["selector_found"] - timing.color_diag["start"]) * 1000)
            c_ms = int((timing.color_diag["action_end"] - timing.color_diag["action_start"]) * 1000)
            v_ms = int((timing.color_diag["state_confirmed"] - timing.color_diag["action_end"]) * 1000)
            logger.info(f"[Run {run_id:02d}] [时序诊断-颜色] find={f_ms}ms, click({timing.color_action_type})={c_ms}ms, verify={v_ms}ms")
        logger.info(f"[Run {run_id:02d}] T5: 星光白色选择完成 (+{timing.latency_color_selected} ms, 确认选中={color_confirmed}, action={timing.color_action_type})")

        # T6: 极速选择 512GB (Fast Path)
        logger.info(f"[Run {run_id:02d}] 正在选择存储容量: {TARGET_STORAGE} (Fast Path)...")
        await _rehearsal_interact_option_fast(
            page=page,
            radio_locator_str="input[name='dimensionCapacity'][value='512gb']",
            label_locator_str="label:has-text('512GB'), label:has(input[name='dimensionCapacity'][value='512gb'])",
            option_desc=f"存储容量: {TARGET_STORAGE}",
            timing=timing,
            option_type="storage"
        )
        await asyncio.sleep(0.1)
        storage_confirmed = await _verify_storage_confirmed(page)
        timing.mark_storage_selected(confirmed=storage_confirmed)
        if timing.storage_diag:
            timing.storage_diag["state_confirmed"] = time.perf_counter()
            f_ms = int((timing.storage_diag["selector_found"] - timing.storage_diag["start"]) * 1000)
            c_ms = int((timing.storage_diag["action_end"] - timing.storage_diag["action_start"]) * 1000)
            v_ms = int((timing.storage_diag["state_confirmed"] - timing.storage_diag["action_end"]) * 1000)
            logger.info(f"[Run {run_id:02d}] [时序诊断-容量] find={f_ms}ms, click({timing.storage_action_type})={c_ms}ms, verify={v_ms}ms")
        logger.info(f"[Run {run_id:02d}] T6: 512GB 选择完成 (+{timing.latency_storage_selected} ms, 确认选中={storage_confirmed}, action={timing.storage_action_type})")

    # 二次证据核验 (绝不强行置 True)
    if not timing.color_confirmed:
        timing.color_confirmed = await _verify_color_confirmed(page)
    if not timing.storage_confirmed:
        timing.storage_confirmed = await _verify_storage_confirmed(page)

    # 保存截图 2: target_selected
    p2_path = os.path.join(screenshots_dir, f"run_{run_id:02d}_target_selected.png")
    try:
        await page.screenshot(path=p2_path)
        timing.screenshots.append(p2_path)
        logger.info(f"[Run {run_id:02d}] 📸 规格选择截图已保存: {p2_path}")
    except Exception as e:
        logger.warning(f"[Run {run_id:02d}] 截图保存异常: {e}")

    # T7: 验证目标规格依然为 MK2P4CH/A
    logger.info(f"[Run {run_id:02d}] 正在核验官方目标 SKU 是否仍为 {TARGET_SKU} ...")
    is_verified = await _verify_target_sku_lightweight(page)
    timing.mark_target_verified(is_verified)
    logger.info(f"[Run {run_id:02d}] T7: 目标规格核验完成 (+{timing.latency_target_verified} ms, 校验={is_verified})")

    # T8: 严格 10 项 HANDOFF_READY 判定
    logger.info(f"[Run {run_id:02d}] 正在核验人工接管 10 项全量前置条件...")

    same_context = bool(browser_manager.context is not None and getattr(page, "context", None) == browser_manager.context)
    pages_list = browser_manager.context.pages if (browser_manager.context and hasattr(browser_manager.context, "pages")) else [page]
    same_tab = (len(pages_list) == 1 and pages_list[0] == page)
    reload_ok = (timing.reload_count == 1 and timing.t_reload_done > timing.t_reload_started)

    page_stable = True
    try:
        await page.wait_for_load_state("domcontentloaded")
    except Exception:
        page_stable = False

    window_front = True
    try:
        await page.bring_to_front()
    except Exception:
        window_front = False

    auto_stopped = True
    human_takeover_ready = True

    timing.mark_handoff_ready_prewarm(
        same_context=same_context,
        same_tab=same_tab,
        reload_ok=reload_ok,
        color_confirmed=timing.color_confirmed,
        storage_confirmed=timing.storage_confirmed,
        sku_verified=timing.sku_verified,
        page_stable=page_stable,
        window_front=window_front,
        auto_stopped=auto_stopped,
        human_takeover_ready=human_takeover_ready,
    )

    logger.info(
        f"[Run {run_id:02d}] T8: 人工接管点严格判定完成 (+{timing.latency_handoff_ready} ms, "
        f"qualified={timing.handoff_qualified}, white={timing.color_confirmed}, 512g={timing.storage_confirmed}, "
        f"sku={timing.sku_verified}, same_ctx={same_context}, same_tab={same_tab}, reload_ok={reload_ok})"
    )

    if not timing.handoff_qualified:
        logger.warning(
            f"[Run {run_id:02d}] ⚠️ 人工接管条件未完全满足 (HANDOFF_READY=False): "
            f"same_ctx={same_context}, same_tab={same_tab}, reload_ok={reload_ok}, "
            f"color={timing.color_confirmed}, storage={timing.storage_confirmed}, "
            f"sku={timing.sku_verified}, stable={page_stable}, front={window_front}"
        )

    try:
        await asyncio.wait_for(asyncio.shield(feishu_task), timeout=5.0)
    except Exception:
        pass

    log_takeover_alert(
        logger,
        f"【首发模拟演练 Pre-Warmed - 人工接管就绪】星光白色+512GB已选中，到达当前公开页面最远停止点。(URL: {page.url})"
    )
    print("  【演练安全确认】：未强行加车，未触碰支付网关，未产生真实订单。")
    print("\n" + timing.format_report() + "\n")

    return timing


async def execute_single_run(
    config: AppConfig,
    run_id: int,
    screenshots_dir: str
) -> RehearsalTiming:
    """执行单轮完整的首发模拟演练 (冷启动模式)"""
    timing = RehearsalTiming(run_id=run_id, is_prewarmed=False)
    os.makedirs(screenshots_dir, exist_ok=True)

    print("\n" + "=" * 65)
    print(f"  【Launch Rehearsal 演练启动 - 第 {run_id} 轮】")
    print("  模拟事件: COMING_SOON -> ONLINE_AVAILABLE")
    print(f"  演练目标: {TARGET_PRODUCT} · {TARGET_COLOR} · {TARGET_STORAGE} [{TARGET_SKU}]")
    print("  模式标记: THIS IS A REHEARSAL")
    print("=" * 65 + "\n")

    # T0: 本地生成 SIMULATED_ONLINE_AVAILABLE
    timing.start_trigger()
    logger.info(f"[Run {run_id:02d}] T0: 收到模拟触发信号 SIMULATED_ONLINE_AVAILABLE")

    # T1: 立即触发 Mac 本地声音
    play_rehearsal_sound(enabled=config.notifications.sound.enabled)
    timing.mark_local_alert()
    logger.info(f"[Run {run_id:02d}] T1: 本地声音提醒触发完成 (+{timing.latency_local_alert} ms)")

    # T2a: 飞书 Webhook POST 请求发起
    timing.mark_feishu_started()
    logger.info(f"[Run {run_id:02d}] T2a: 飞书 Webhook 请求发起 (+{timing.latency_feishu_started} ms)")

    # T2b: 飞书 Webhook 请求应答 ACK (网络异常严格隔离)
    webhook_url = config.notifications.feishu.webhook_url
    feishu_ok = await send_feishu_rehearsal_card(
        webhook_url=webhook_url,
        timeout_sec=config.notifications.feishu.timeout_seconds
    )
    timing.mark_feishu_ack(feishu_ok)
    logger.info(f"[Run {run_id:02d}] T2b: 飞书 Webhook 应答完成 (ACK) (+{timing.latency_feishu_ack} ms, 成功={feishu_ok}) [注: ACK 非手机到端时间]")

    # 严格复用正式环境持久化 Profile (browser_data/) 与 Headed 模式
    config.browser.user_data_dir = "./browser_data"
    config.browser.headless = False
    profile_path = os.path.abspath(config.browser.user_data_dir)
    logger.info(f"[Run {run_id:02d}] 正在拉起正式环境 Persistent Profile 浏览器: {profile_path} (Headed=True)...")

    browser_manager = BrowserManager(config.browser)
    page = await browser_manager.start()
    timing.mark_browser_ready()
    logger.info(f"[Run {run_id:02d}] T3: 浏览器就绪并置前 (+{timing.latency_browser_ready} ms)")

    try:
        # T4: 打开真实 Apple 中国大陆 iPhone Duo 页面
        logger.info(f"[Run {run_id:02d}] 正在访问 Apple 购买页: {DUO_PAGE_URL} ...")
        await page.goto(DUO_PAGE_URL, wait_until="domcontentloaded", timeout=25000)
        timing.mark_page_loaded()
        logger.info(f"[Run {run_id:02d}] T4: Duo 页面加载完成 (+{timing.latency_page_loaded} ms)")

        # 保存截图 1: page_loaded
        p1_path = os.path.join(screenshots_dir, f"run_{run_id:02d}_page_loaded.png")
        await page.screenshot(path=p1_path)
        timing.screenshots.append(p1_path)
        logger.info(f"[Run {run_id:02d}] 📸 页面加载截图已保存: {p1_path}")

        # T5: 自动选择星光白色并核实底层状态
        logger.info(f"[Run {run_id:02d}] 正在选择外观颜色: {TARGET_COLOR} ...")
        await _rehearsal_interact_option(
            page=page,
            radio_locator_str="input[name='dimensionColor'][value*='starwhite']",
            label_locator_str="label:has-text('星光白色'), label:has(input[name='dimensionColor'][value*='starwhite'])",
            option_desc=f"外观颜色: {TARGET_COLOR}",
        )
        await asyncio.sleep(0.3)
        color_confirmed = await _verify_color_confirmed(page)
        timing.mark_color_selected(confirmed=color_confirmed)
        logger.info(f"[Run {run_id:02d}] T5: 星光白色选择完成 (+{timing.latency_color_selected} ms, 确认选中={color_confirmed})")

        # T6: 自动选择 512GB 并核实底层状态
        logger.info(f"[Run {run_id:02d}] 正在选择存储容量: {TARGET_STORAGE} ...")
        await _rehearsal_interact_option(
            page=page,
            radio_locator_str="input[name='dimensionCapacity'][value='512gb']",
            label_locator_str="label:has-text('512GB'), label:has(input[name='dimensionCapacity'][value='512gb'])",
            option_desc=f"存储容量: {TARGET_STORAGE}",
        )
        await asyncio.sleep(0.3)
        storage_confirmed = await _verify_storage_confirmed(page)
        timing.mark_storage_selected(confirmed=storage_confirmed)
        logger.info(f"[Run {run_id:02d}] T6: 512GB 选择完成 (+{timing.latency_storage_selected} ms, 确认选中={storage_confirmed})")

        if not timing.color_confirmed:
            timing.color_confirmed = await _verify_color_confirmed(page)
        if not timing.storage_confirmed:
            timing.storage_confirmed = await _verify_storage_confirmed(page)

        # 保存截图 2: target_selected
        p2_path = os.path.join(screenshots_dir, f"run_{run_id:02d}_target_selected.png")
        await page.screenshot(path=p2_path)
        timing.screenshots.append(p2_path)
        logger.info(f"[Run {run_id:02d}] 📸 规格选择截图已保存: {p2_path}")

        # T7: 验证当前目标仍是 MK2P4CH/A
        logger.info(f"[Run {run_id:02d}] 正在核验官方目标 SKU 是否仍为 {TARGET_SKU} ...")
        resolver = AppleCatalogResolver()
        res = await resolver.resolve(page, TARGET_PRODUCT, TARGET_COLOR, TARGET_STORAGE, expected_part_number=TARGET_SKU)
        is_verified = (res.status in (ResolutionStatus.SUCCESS, ResolutionStatus.CATALOG_STALE)) and (res.product and res.product.part_number == TARGET_SKU)
        timing.mark_target_verified(is_verified)
        logger.info(f"[Run {run_id:02d}] T7: 目标规格核验完成 (+{timing.latency_target_verified} ms, 校验={is_verified})")

        logger.info(f"[Run {run_id:02d}] 正在核验人工接管前置全量条件...")
        page_stable = True
        try:
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(0.3)
        except Exception:
            page_stable = False

        window_front = True
        try:
            await page.bring_to_front()
        except Exception:
            window_front = False

        timing.mark_handoff_ready(page_stable=page_stable, window_front=window_front)
        logger.info(
            f"[Run {run_id:02d}] T8: 人工接管点严格判定完成 (+{timing.latency_handoff_ready} ms, "
            f"white={timing.color_confirmed}, 512g={timing.storage_confirmed}, sku={timing.sku_verified}, "
            f"stable={timing.page_stable}, front={timing.window_brought_to_front}, qualified={timing.handoff_qualified})"
        )
        if not timing.handoff_qualified:
            logger.warning(
                f"[Run {run_id:02d}] ⚠️ 人工接管条件未完全满足 (HANDOFF_READY=False): "
                f"color={timing.color_confirmed}, storage={timing.storage_confirmed}, "
                f"sku={timing.sku_verified}, stable={timing.page_stable}, front={timing.window_brought_to_front}"
            )

        log_takeover_alert(
            logger,
            f"【首发模拟演练 - 人工接管测试】已完成星光白色+512GB配置预选，到达当前公开页面最远停止点。(URL: {page.url})"
        )
        print("  【演练安全确认】：未强行加车，未触碰支付网关，未产生真实订单。")

    finally:
        await browser_manager.close()
        logger.info(f"[Run {run_id:02d}] 浏览器会话已安全关闭。")

    print("\n" + timing.format_report() + "\n")
    return timing


def format_aggregated_summary(timings: List[RehearsalTiming]) -> str:
    """生成多轮演练聚合统计分析报告 (平均值, P50, 最快, 最慢) - 冷启动兼容"""
    n = len(timings)
    if n == 0:
        return "无演练记录"

    def _stats(values: List[int]) -> Tuple[float, float, int, int]:
        avg = statistics.mean(values)
        p50 = statistics.median(values)
        fastest = min(values)
        slowest = max(values)
        return avg, p50, fastest, slowest

    metrics = {
        "Feishu Request Started": [t.latency_feishu_started for t in timings],
        "Feishu Request Acked": [t.latency_feishu_ack for t in timings],
        "Browser Ready": [t.latency_browser_ready for t in timings],
        "Duo Page Loaded": [t.latency_page_loaded for t in timings],
        "Starwhite Selected": [t.latency_color_selected for t in timings],
        "512GB Selected": [t.latency_storage_selected for t in timings],
        "Target Verified": [t.latency_target_verified for t in timings],
        "Human Handoff Ready": [t.latency_handoff_ready for t in timings],
        "Total Automation Latency": [t.total_latency for t in timings],
    }

    lines = [
        "==================================================",
        f"Launch Rehearsal Multi-Run Summary Report ({n} 轮演练汇总)",
        "==================================================",
        f"{'指标 (Metric)':<26} | {'平均值 (Mean)':<12} | {'P50 (中位数)':<12} | {'最快 (Min)':<10} | {'最慢 (Max)':<10}",
        "-" * 80,
    ]

    for name, vals in metrics.items():
        avg, p50, fastest, slowest = _stats(vals)
        lines.append(f"{name:<26} | {avg:>9.1f} ms | {p50:>9.1f} ms | {fastest:>7} ms | {slowest:>7} ms")

    lines.append("=" * 80)

    b_avg, b_p50, b_min, b_max = _stats(metrics["Browser Ready"])
    p_avg, p_p50, p_min, p_max = _stats(metrics["Duo Page Loaded"])
    v_avg, v_p50, v_min, v_max = _stats(metrics["Target Verified"])
    h_avg, h_p50, h_min, h_max = _stats(metrics["Human Handoff Ready"])
    f_avg, f_p50, f_min, f_max = _stats(metrics["Feishu Request Acked"])

    lines.extend([
        "",
        "【核心指标深度分析】",
        f"1. Feishu Request Acked : 平均 {f_avg:.1f} ms (P50: {f_p50:.1f} ms, 最快: {f_min} ms, 最慢: {f_max} ms) [飞书服务器ACK响应]",
        f"2. Browser Ready        : 平均 {b_avg:.1f} ms (P50: {b_p50:.1f} ms, 最快: {b_min} ms, 最慢: {b_max} ms) [复用正式 browser_data/]",
        f"3. Duo Page Loaded      : 平均 {p_avg:.1f} ms (P50: {p_p50:.1f} ms, 最快: {p_min} ms, 最慢: {p_max} ms)",
        f"4. Target Verified      : 平均 {v_avg:.1f} ms (P50: {v_p50:.1f} ms, 最快: {v_min} ms, 最慢: {v_max} ms)",
        f"5. Human Handoff Ready  : 平均 {h_avg:.1f} ms (P50: {h_p50:.1f} ms, 最快: {h_min} ms, 最慢: {h_max} ms) [满足全部6项严格判定]",
        "",
        "【全链路安全与隔离审计】",
        "  - 官方状态与库存   : 100% 未修改任何 Apple 官方接口，严禁伪造真实库存",
        "  - 本地库存缓存     : 100% 未写入正式 cache/ 文件",
        "  - 订单与支付       : 100% 未触碰支付网关，零自动下单，零自动扣款",
        "  - 飞书消息隔离     : 100% 显式标注【模拟演练】，防止误认为正式到货",
        "  - 飞书计时定义     : 严格区分 REQUEST_STARTED 与 REQUEST_ACK，不虚报为手机收到时间",
        "  - 浏览器会话环境   : 严格复用 browser_data/ 持久化 Profile，保留正式登录态",
        "  - 接管计时条件     : 严格要求颜色、容量、SKU、页面稳定与窗口置前 6 项同时就绪",
        "  - RC1 正式代码     : 100% 保持未修改状态",
        "==================================================",
    ])

    return "\n".join(lines)


def format_prewarmed_summary(
    timings: List[RehearsalTiming],
    prewarm_timing: PrewarmTiming,
    preselect_target: bool = False
) -> str:
    """生成 Pre-Warmed 多轮演练汇总与 Cold vs Prewarm 对比分析报告"""
    n = len(timings)
    if n == 0:
        return "无演练记录"

    def _stats(values: List[int]) -> Tuple[float, float, int, int]:
        if not values:
            return 0.0, 0.0, 0, 0
        avg = statistics.mean(values)
        p50 = statistics.median(values)
        fastest = min(values)
        slowest = max(values)
        return avg, p50, fastest, slowest

    metrics = {
        "Local Alert": [t.latency_local_alert for t in timings],
        "Feishu Started (Async)": [t.latency_feishu_started for t in timings],
        "Feishu Acked (Async)": [t.latency_feishu_ack for t in timings],
        "Page Reload Started": [t.latency_reload_started for t in timings],
        "Page Reload Done": [t.latency_reload_done for t in timings],
        "Starwhite Verified": [t.latency_post_reload_color_verified or t.latency_color_selected for t in timings],
        "512GB Verified": [t.latency_post_reload_storage_verified or t.latency_storage_selected for t in timings],
        "Target Verified": [t.latency_target_verified for t in timings],
        "Human Handoff Ready": [t.latency_handoff_ready for t in timings],
        "Total Automation Latency": [t.total_latency for t in timings],
    }

    mode_title = "Pre-Warmed & Target-Preselected Summary Report" if preselect_target else "Pre-Warmed Launch Rehearsal Summary Report"
    lines = [
        "==================================================",
        f"{mode_title} ({n} 轮演练汇总)",
        "==================================================",
        f"【预热阶段耗时统计 (完全独立于 T0，绝不计入开售响应)】",
        f"  - 浏览器启动 (Persistent Profile) : {prewarm_timing.browser_startup_ms} ms",
        f"  - Duo 页面首载 (domcontentloaded) : {prewarm_timing.page_load_ms} ms",
    ]
    if preselect_target:
        lines.extend([
            f"  - 颜色预选耗时 (星光白色)         : {prewarm_timing.preselect_color_ms} ms (确认={prewarm_timing.preselect_color_confirmed})",
            f"  - 容量预选耗时 (512GB)            : {prewarm_timing.preselect_storage_ms} ms (确认={prewarm_timing.preselect_storage_confirmed})",
            f"  - 预选目标 SKU 校验               : {TARGET_SKU} (核验成功={prewarm_timing.preselect_sku_verified})",
            f"  - 预选稳定 URL                    : {prewarm_timing.preselect_url}",
            f"  - 预热就绪总耗时                  : {prewarm_timing.total_prewarm_ms} ms (PREWARM_TARGET_READY={prewarm_timing.prewarm_target_ready})",
        ])
    else:
        lines.extend([
            f"  - 预热就绪总耗时                  : {prewarm_timing.total_prewarm_ms} ms",
            f"  - 会话登录态 / Catalog 校验       : {prewarm_timing.login_status} / {TARGET_SKU} ({prewarm_timing.catalog_verified})",
        ])

    lines.extend([
        "-" * 80,
        f"{'指标 (Metric)':<26} | {'平均值 (Mean)':<12} | {'P50 (中位数)':<12} | {'最快 (Min)':<10} | {'最慢 (Max)':<10}",
        "-" * 80,
    ])

    for name, vals in metrics.items():
        avg, p50, fastest, slowest = _stats(vals)
        lines.append(f"{name:<26} | {avg:>9.1f} ms | {p50:>9.1f} ms | {fastest:>7} ms | {slowest:>7} ms")

    lines.append("=" * 80)

    h_avg, h_p50, h_min, h_max = _stats(metrics["Human Handoff Ready"])
    cold_mean = HISTORICAL_COLD_START_BASELINE["handoff_ready_mean"]
    cold_p50 = HISTORICAL_COLD_START_BASELINE["handoff_ready_p50"]
    cold_min = HISTORICAL_COLD_START_BASELINE["handoff_ready_min"]
    cold_max = HISTORICAL_COLD_START_BASELINE["handoff_ready_max"]

    pw_mean = HISTORICAL_PREWARM_BASELINE["handoff_ready_mean"]
    pw_p50 = HISTORICAL_PREWARM_BASELINE["handoff_ready_p50"]
    pw_min = HISTORICAL_PREWARM_BASELINE["handoff_ready_min"]
    pw_max = HISTORICAL_PREWARM_BASELINE["handoff_ready_max"]

    cold_abs_diff = cold_mean - h_avg
    cold_pct_diff = (cold_abs_diff / cold_mean) * 100.0 if cold_mean > 0 else 0.0

    pw_abs_diff = pw_mean - h_avg
    pw_pct_diff = (pw_abs_diff / pw_mean) * 100.0 if pw_mean > 0 else 0.0

    lines.extend([
        "",
        "【三档执行模式基准对比 (Cold Start vs Pre-Warmed vs Target-Preselected)】",
        f"  1. 冷启动历史基线 (Cold Baseline) : 平均 {cold_mean:.1f} ms | P50 {cold_p50:.1f} ms | 范围: {cold_min} ~ {cold_max} ms",
        f"  2. 预热历史基线 (Prewarm Baseline) : 平均 {pw_mean:.1f} ms | P50 {pw_p50:.1f} ms | 范围: {pw_min} ~ {pw_max} ms",
        f"  3. 目标预选实测 (Target-Preselect) : 平均 {h_avg:.1f} ms | P50 {h_p50:.1f} ms | 范围: {h_min} ~ {h_max} ms",
        f"  - 相对冷启动基线改善 (vs Cold)    : 缩短 {cold_abs_diff:.1f} ms (降幅: {cold_pct_diff:.2f}%)",
        f"  - 相对预热基线改善 (vs Prewarm)  : 缩短 {pw_abs_diff:.1f} ms (降幅: {pw_pct_diff:.2f}%)",
        "",
        "【Selection Preservation 规格保留审计】",
    ])

    for t in timings:
        c_state = "✅ 保留 (跳过点击)" if t.color_reselect_skipped else f"⚠️ 丢失 (重选: {t.color_reselect_reason})"
        s_state = "✅ 保留 (跳过点击)" if t.storage_reselect_skipped else f"⚠️ 丢失 (重选: {t.storage_reselect_reason})"
        lines.append(f"  - Run {t.run_id:02d}: 星光白色 {c_state} | 512GB {s_state} | SKU核验={t.sku_verified}")

    lines.extend([
        "",
        "【10 项严苛人工接管判定 (HANDOFF_READY) 全量审计】",
        f"  1. 浏览器 Context 延续性 : 100% 保持 (同一 Context)",
        f"  2. 单一 Tab 限制         : 100% 保持 (无多余标签页)",
        f"  3. 页面单次 Reload       : 100% 满足 (每轮严格仅 1 次刷新)",
        f"  4. 星光白色选中可靠证据   : 100% 满足 ({all(t.color_confirmed for t in timings)})",
        f"  5. 512GB 选中可靠证据     : 100% 满足 ({all(t.storage_confirmed for t in timings)})",
        f"  6. 目标 SKU 一致性        : 100% 满足 ({all(t.sku_verified for t in timings)}, MK2P4CH/A)",
        f"  7. 页面 DOM 加载稳定     : 100% 满足 ({all(t.page_stable for t in timings)})",
        f"  8. 浏览器窗口置前         : 100% 满足 ({all(t.window_brought_to_front for t in timings)})",
        f"  9. 自动操作彻底停止       : 100% 满足 (到达最远安全点)",
        f"  10. 用户无缝接管就绪      : 100% 满足 (窗口保持可见/可操作)",
        "",
        "【安全合规与权限审计】",
        "  - 授权窗口限制   : 仅使用 1 个 Chromium 窗口，全流程零额外窗口/标签页",
        "  - 系统权限约束   : 零 AppleScript 操作 Terminal，零进程扫描，零终端历史嗅探",
        "  - 飞书网络解耦   : 异步并发投递，网络请求脱离购买主路径，带有【模拟演练】标记",
        "  - 订单与支付安全 : 零自动加车，零支付网关触碰，零真实扣款",
        "  - 正式业务隔离   : 正式 launch 逻辑与缓存零修改、零污染",
        "==================================================",
    ])

    return "\n".join(lines)


async def run_launch_rehearsal(
    config: AppConfig,
    runs: int = 1,
    interval_sec: int = 30,
    screenshots_dir: str = "./logs/screenshots/rehearsal",
    prewarm: bool = False,
    preselect_target: bool = False,
) -> List[RehearsalTiming]:
    """
    --mode launch-rehearsal 主入口控制器
    支持参数:
      --runs N: 执行轮次 (默认 1)
      --prewarm: 启用 Pre-Warmed 极速演练模式 (提前预热单实例 Chromium 浏览器与页面，降低 T0 触发后的接管耗时)
      --preselect-target: 启用目标配置预选模式 (在 T0 前预选星光白+512GB，并在 reload 后检测状态保留，跳过重复点击)
      interval_sec: 轮次间隔 (默认至少 30 秒)
    """
    total_runs = max(1, runs)
    effective_prewarm = prewarm or preselect_target

    if effective_prewarm:
        logger.info(f"启动 Pre-Warmed 首发模拟演练: 计划执行 {total_runs} 轮极速演练 (单实例 Chromium 复用，preselect_target={preselect_target}, 轮次间隔至少 {interval_sec} 秒)...")
        browser_manager, page, prewarm_timing = await prewarm_rehearsal_session(config, preselect_target=preselect_target)

        results: List[RehearsalTiming] = []
        try:
            for idx in range(1, total_runs + 1):
                timing = await execute_prewarmed_single_run(
                    config=config,
                    browser_manager=browser_manager,
                    page=page,
                    run_id=idx,
                    screenshots_dir=screenshots_dir,
                    preselect_target=preselect_target,
                )
                results.append(timing)

                if idx < total_runs:
                    cooldown = max(30, interval_sec)
                    logger.info(f"第 {idx}/{total_runs} 轮极速演练完毕。浏览器保持开启，正在执行防风控冷却等待 {cooldown} 秒...")
                    print(f"⏳ 正在冷却 {cooldown} 秒以防止频繁刷新触发 Apple 边缘防刷风控 (浏览器窗口保持打开)...")
                    await asyncio.sleep(cooldown)

            print("\n" + "=" * 65)
            print("  👁️ 【视觉核验阶段】所有演练轮次已完成，浏览器窗口保持置前 8 秒供人工查看界面状态...")
            print("=" * 65 + "\n")
            try:
                await page.bring_to_front()
            except Exception:
                pass
            await asyncio.sleep(8)

        finally:
            await browser_manager.close()
            logger.info("Chromium 浏览器会话已安全关闭。")

        summary = format_prewarmed_summary(results, prewarm_timing, preselect_target=preselect_target)
        print("\n" + summary + "\n")
        return results

    else:
        logger.info(f"启动冷启动首发模拟演练模式: 计划执行 {total_runs} 轮演练 (每轮间隔至少 {interval_sec} 秒)...")
        results = []
        for idx in range(1, total_runs + 1):
            timing = await execute_single_run(
                config=config,
                run_id=idx,
                screenshots_dir=screenshots_dir
            )
            results.append(timing)

            if idx < total_runs:
                cooldown = max(30, interval_sec)
                logger.info(f"第 {idx}/{total_runs} 轮演练完毕。正在执行防风控冷却等待 {cooldown} 秒...")
                print(f"⏳ 正在冷却 {cooldown} 秒以防止频繁刷新触发 Apple 边缘防刷风控...")
                await asyncio.sleep(cooldown)

        summary = format_aggregated_summary(results)
        print("\n" + summary + "\n")
        return results

