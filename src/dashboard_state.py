"""
iPhone Duo 首发控制中心 - 状态模型 (Dashboard State Model)
=====================================================
独立纯 Python 数据模型，用于集中维护 Dashboard 界面所需的全部展示状态。
设计原则：
1. 纯数据类，与 Tkinter GUI / Playwright 浏览器 / 网络完全解耦；
2. 包含严格的敏感信息脱敏 (Secret Masking) 机制，绝不泄漏 Cookie / Webhook / 密码 / 手机号；
3. 支持序列化、深拷贝与事件安全追加。
"""

import time
import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple


def mask_secret(value: str, keep_start: int = 2, keep_end: int = 4) -> str:
    """脱敏字符串：如保留前2后4字符，其余隐藏为 ****"""
    if not value:
        return ""
    val_str = str(value).strip()
    if len(val_str) <= (keep_start + keep_end):
        return "****"
    return f"{val_str[:keep_start]}****{val_str[-keep_end:]}"


def mask_webhook_url(url: str) -> str:
    """脱敏 Webhook 地址：仅保留尾部 token 片段，绝不显示完整路径"""
    if not url:
        return "未配置"
    clean = url.strip()
    if "/" in clean:
        token = clean.split("/")[-1]
        return mask_secret(token, keep_start=2, keep_end=4)
    return mask_secret(clean, keep_start=2, keep_end=4)


@dataclass
class StoreDisplayState:
    """单个 Apple 零售店展示状态"""
    store_number: str                 # 门店代码: R390, R401, R581, R683
    name: str                         # 门店名称: 香港广场, 上海环贸 iapm, 五角场, 环球港
    status: str = "COMING_SOON"       # 内部状态 token: COMING_SOON, AVAILABLE, CHECKING, BACKOFF, ERROR
    status_cn: str = "即将发售"        # 界面中文状态: 即将发售, 可到店取货, 查询中, 退避中, 接口异常
    pickup_quote: str = "暂不提供取货" # 取货说明
    updated_at: str = "--:--:--"      # 最近更新时间
    is_available: bool = False        # 是否有现货 (PICKUP_AVAILABLE)
    has_error: bool = False           # 是否异常


@dataclass
class RecentEvent:
    """控制中心最近事件条目"""
    timestamp: str                    # 时间戳: HH:MM:SS
    level: str                        # INFO, SUCCESS, WARNING, CRITICAL
    level_cn: str                     # 信息, 成功, 提醒, 告警
    message: str                      # 中文事件说明


@dataclass
class DashboardState:
    """
    Dashboard 完整状态聚合模型
    """
    version: str = "V0.4.2 RC2"
    app_title: str = "iPhone Duo 首发控制中心"
    app_subtitle: str = "V0.4.2 RC2 · 首发日控制面板"
    dashboard_mode: str = "DEMO"           # "DEMO", "RUNTIME_VERIFY", "FORMAL_LAUNCH"
    is_demo: bool = False
    current_time_str: str = ""
    launch_time_iso: Optional[str] = None  # 如 "2026-10-16T20:00:00+08:00"
    launch_countdown_str: str = "开售时间未设置"
    launch_advice_str: str = ""

    # 目标商品 (Target)
    target_product: str = "iPhone Duo"
    target_color: str = "星光白色"
    target_storage: str = "512GB"
    target_sku: str = "MK2P4CH/A"
    target_locked: bool = True
    target_mismatch: bool = False
    target_status_text: str = "目标已锁定"
    purchase_options_prepared: bool = False
    tradein_status: str = "待准备"
    applecare_status: str = "待准备"

    # 系统健康检查 (System Health)
    session_status: str = "已就绪"         # "已就绪", "未就绪", "检查中"
    session_last_verified_at: str = "--:--:--"
    session_strict_blocked: bool = False
    session_fail_reason: str = ""
    is_public_only: bool = False
    catalog_status: str = "已验证"         # "已验证", "解析中", "异常"
    browser_status: str = "已预热"         # "已预热", "未启动", "已启动", "运行中"
    target_prepare_status: str = "已就绪"  # "已就绪", "预选中", "未就绪"
    feishu_status: str = "已连接"          # "已连接", "未配置", "重试中"
    feishu_masked_token: str = "f1****4f"
    feishu_last_ack_ms: Optional[float] = 552.1
    local_alert_status: str = "正常"       # "正常", "静音", "异常"
    pending_notifications: int = 0
    rate_guard_status: str = "已启用"      # "已启用", "退避中"
    security_gate_status: str = "已启用"   # "已启用", "熔断拦截"

    # 响应性能指标 (Latency Metrics)
    latency_trigger_to_handoff_sec: Optional[float] = None

    # 中央核心大状态 (Hero Status)
    hero_title: str = "系统已准备就绪"
    hero_subtitle: str = "目标规格已预选，监控已启动，等待 Apple 开售。"
    hero_level: str = "READY"              # "INITIAL", "PREPARING", "READY", "ONLINE_AVAILABLE", "PURCHASE", "HANDOFF", "WARNING", "ERROR"

    # 首发 10 项检查清单 (10-Item Ready Checklist)
    checklist: Dict[str, bool] = field(default_factory=lambda: {
        "browser_prewarmed": False,
        "session_verified": False,
        "catalog_locked": False,
        "target_spec_prepared": False,
        "purchase_options_prepared": False,
        "selection_preservation_verified": False,
        "online_monitor_standby": False,
        "pickup_monitor_standby": False,
        "feishu_async_ready": False,
        "local_alert_active": False,
    })

    # Launch 5 级状态链 (Horizontal Pipeline Stepper)
    # 各阶段状态: "PENDING" (○), "ACTIVE" (◉), "COMPLETED" (✓)
    pipeline_session: str = "COMPLETED"     # 登录会话
    pipeline_prepare: str = "COMPLETED"     # 目标预选
    pipeline_monitor: str = "ACTIVE"        # 库存监控
    pipeline_purchase: str = "PENDING"      # 购买推进
    pipeline_handoff: str = "PENDING"       # 人工接管

    # 线上主通道 (Online Monitor)
    online_state_token: str = "COMING_SOON"
    online_state_cn: str = "即将发售"
    online_last_check: str = "--:--:--"
    online_next_check: str = "约 25 秒后"
    online_cadence: str = "20~30 秒 (基准 25s)"
    online_failures: int = 0
    online_backoff_sec: float = 0.0

    # 直营店自提通道 (Pickup Monitor)
    pickup_state_token: str = "MONITORING"
    pickup_state_cn: str = "监控中"
    pickup_last_check: str = "--:--:--"
    pickup_next_check: str = "约 30 秒后"
    pickup_cadence: str = "30~40 秒 (基准 30s)"
    pickup_round_duration: str = "9.35 秒"
    pickup_store_throttle: str = ">= 2.0 秒"
    pickup_backoff_sec: float = 0.0

    # 上海 4 直营店 (Shanghai 4 Stores)
    stores: Dict[str, StoreDisplayState] = field(default_factory=dict)

    # 安全隔离区 (Safety Gateway)
    human_action_required: bool = False
    human_action_reason: str = ""

    # 最近事件流 (Recent Events, 保持最新 20 条)
    recent_events: List[RecentEvent] = field(default_factory=list)

    def __post_init__(self):
        # 默认初始化上海 4 店权威数据
        if not self.stores:
            self.stores = {
                "R390": StoreDisplayState(store_number="R390", name="香港广场", status="COMING_SOON", status_cn="即将发售", pickup_quote="目前暂不提供零售店取货"),
                "R401": StoreDisplayState(store_number="R401", name="上海环贸 iapm", status="COMING_SOON", status_cn="即将发售", pickup_quote="目前暂不提供零售店取货"),
                "R581": StoreDisplayState(store_number="R581", name="五角场", status="COMING_SOON", status_cn="即将发售", pickup_quote="目前暂不提供零售店取货"),
                "R683": StoreDisplayState(store_number="R683", name="环球港", status="COMING_SOON", status_cn="即将发售", pickup_quote="目前暂不提供零售店取货"),
            }
        self.update_current_time()

    def get_launch_advice(self, now: Optional[datetime.datetime] = None) -> str:
        """
        根据当前时间与开售状态提供中文提示 (纯 UI 提示，不改变购买核心状态机):
        - 距离预购 > 60分钟: “距离预购尚早”
        - T-60 ~ T-15: “建议检查 Apple 登录会话”
        - T-15 ~ T0: “首发准备阶段”
        - T0 后但 Apple 仍 COMING_SOON: “已到官方预购时间，等待 Apple 放货信号”
        - AVAILABLE: “检测到线上可购”
        """
        if self.online_state_token == "AVAILABLE":
            return "检测到线上可购"

        if not self.launch_time_iso:
            return ""

        try:
            target = datetime.datetime.fromisoformat(self.launch_time_iso)
            if now is None:
                now = datetime.datetime.now()
            now_cmp = datetime.datetime.now(target.tzinfo) if (target.tzinfo is not None and now.tzinfo is None) else (now.astimezone(target.tzinfo) if (target.tzinfo is not None and now.tzinfo is not None) else now)
            diff = target - now_cmp
            total_secs = int(diff.total_seconds())

            if total_secs <= 0:
                return "已到官方预购时间，等待 Apple 放货信号"
            elif total_secs <= 15 * 60:
                return "首发准备阶段"
            elif total_secs <= 60 * 60:
                return "建议检查 Apple 登录会话"
            else:
                return "距离预购尚早"
        except Exception:
            return ""

    def update_current_time(self, now: Optional[datetime.datetime] = None):
        if now is None:
            now = datetime.datetime.now()
        self.current_time_str = now.strftime("%H:%M:%S")
        self.current_date_str = now.strftime("%Y年%m月%d日")

        if self.launch_time_iso:
            try:
                target = datetime.datetime.fromisoformat(self.launch_time_iso)
                if target.tzinfo is not None and now.tzinfo is None:
                    now_cmp = datetime.datetime.now(target.tzinfo)
                elif target.tzinfo is not None and now.tzinfo is not None:
                    now_cmp = now.astimezone(target.tzinfo)
                else:
                    now_cmp = now
                diff = target - now_cmp
                total_secs = int(diff.total_seconds())
                if total_secs > 0:
                    days, rem_h = divmod(total_secs, 86400)
                    hours, rem_m = divmod(rem_h, 3600)
                    mins, secs = divmod(rem_m, 60)
                    if days > 0:
                        self.launch_countdown_str = f"距离预购 {days}天 {hours:02d}:{mins:02d}:{secs:02d}"
                    else:
                        self.launch_countdown_str = f"T-{hours:02d}:{mins:02d}:{secs:02d}"
                else:
                    past = abs(total_secs)
                    hours, rem = divmod(past, 3600)
                    mins, secs = divmod(rem, 60)
                    self.launch_countdown_str = f"预购已开始 +{hours:02d}:{mins:02d}:{secs:02d}"
            except Exception:
                self.launch_countdown_str = "开售时间未设置"
        else:
            self.launch_countdown_str = "开售时间未设置"

        self.launch_advice_str = self.get_launch_advice(now=now)

    def get_checklist_status(self) -> Tuple[int, int, bool]:
        """返回检查清单通过数、总数及是否全部通过 (passed, total, all_passed)"""
        total = len(self.checklist)
        passed = sum(1 for v in self.checklist.values() if v)
        return passed, total, (passed == total and total > 0)

    def is_ready_for_launch(self) -> bool:
        """
        首发严格就绪判定 (Strict Launch Readiness Gate):
        必须同时满足全部硬性前置指标，且非公开降级模式：
        1. 会话健康: session_status == '已就绪' 且 not session_strict_blocked
        2. Catalog: catalog_status == '已验证'
        3. 目标规格: target_prepare_status == '已就绪'
        4. 选项预选: purchase_options_prepared == True
        5. 飞书通道: feishu_status in ('已连接', '已验证')
        6. 本地报警: local_alert_status == '正常'
        7. 频率保护: rate_guard_status == '已启用'
        8. 绝对排他: not is_public_only
        """
        if self.is_public_only or self.session_strict_blocked:
            return False
        if self.session_status != "已就绪":
            return False
        if self.catalog_status != "已验证":
            return False
        if self.target_prepare_status != "已就绪":
            return False
        if not self.purchase_options_prepared:
            return False
        if self.feishu_status not in ("已连接", "已验证"):
            return False
        if self.local_alert_status != "正常":
            return False
        if self.rate_guard_status != "已启用":
            return False
        return True

    def get_session_pill_info(self) -> Tuple[str, str, str]:
        """返回 (状态文本, 状态级别, 核验时间文本) 用于 Header 会话胶囊"""
        time_suffix = f"核验: {self.session_last_verified_at}" if self.session_last_verified_at != "--:--:--" else ""
        if self.session_status == "检查中":
            return ("● 检查中", "ORANGE", time_suffix)
        elif self.session_status == "已就绪" and not self.session_strict_blocked:
            return ("● 已登录", "GREEN", time_suffix)
        elif self.session_strict_blocked:
            return ("● 已阻断", "RED", time_suffix)
        else:
            return ("● 已失效", "RED", time_suffix)

    def get_countdown_text(self, now: Optional[datetime.datetime] = None) -> str:
        """获取并刷新当前开售倒计时文案"""
        self.update_current_time(now=now)
        return self.launch_countdown_str

    def get_countdown_pill_info(self, now: Optional[datetime.datetime] = None) -> Tuple[str, str]:
        """返回 (倒计时文案, 状态级别) 用于顶部 Header 胶囊显示"""
        text = self.get_countdown_text(now=now)
        if text == "开售时间未设置":
            return (text, "GREY")
        if text.startswith("预购已开始"):
            return (text, "GREEN")
        if text.startswith("T-"):
            return (text, "ORANGE")
        # 距离预购 X天
        return (text, "BLUE")

    def get_handoff_text(self) -> str:
        """根据当前运行模式返回标准人机接管指引文案"""
        if self.dashboard_mode == "FORMAL_LAUNCH":
            return "商品已加入购物袋，购买流程已推进至人工安全接管点，请立即在 Apple 官方页面中继续操作。"
        elif self.dashboard_mode == "RUNTIME_VERIFY":
            return "受控验证已完成最终规格核验，并在真实加车前安全阻断。当前页面已准备好供人工核验。"
        elif self.dashboard_mode == "DEMO":
            return "【界面演示】模拟购买流程已推进至人工接管阶段。本模式未访问 Apple，也未执行真实加车。"
        else:
            return "商品已进入人工安全接管点，请立即在 Apple 官方页面中继续操作。"

    def get_mode_badge_info(self) -> Tuple[str, str]:
        """返回 (徽章文案, 状态级别) 用于顶部 Header 模式显示"""
        if self.dashboard_mode == "FORMAL_LAUNCH":
            if self.session_strict_blocked or self.hero_level == "ERROR":
                return ("【首发实战 · 流程阻断】", "RED")
            return ("【首发实战 · 生产状态】", "GREEN")
        elif self.dashboard_mode == "RUNTIME_VERIFY":
            if self.is_public_only:
                return ("【受控联调 · 公开页面验证】", "ORANGE")
            return ("【受控联调 · 禁止真实加车】", "BLUE")
        elif self.dashboard_mode == "DEMO":
            return ("【界面演示 · 全部状态模拟】", "ORANGE")
        else:
            return (f"【{self.dashboard_mode}】", "GREY")

    def get_safety_info(self) -> Tuple[str, str, str, str]:
        """返回 (药丸文本, 药丸级别, 卡片标题, 卡片说明正文)"""
        if self.human_action_required:
            handoff_desc = (
                f"{self.get_handoff_text()}\n\n"
                "💡 自动化已主动停止并交由人工接管。为确保资金绝对安全，机器人坚决不代替您点击最终付款与订单提交。"
            )
            return ("请立即操作", "RED", "⚠️ 需要人工接管！", handoff_desc)

        if self.session_strict_blocked:
            block_desc = (
                f"Apple 登录会话未就绪或已失效 ({self.session_fail_reason or '需要登录'})。\n\n"
                "🔒 首发流程已严格阻断 (Fail-Closed)，坚决禁止非登录状态下盲目运行。\n\n"
                "👉 请立即在终端中执行登录会话准备：\n"
                "   python -m src.main --mode prepare-session\n\n"
                "登录成功并验证持久化后，重新启动控制中心。"
            )
            return ("流程已阻断", "RED", "🛑 Apple 登录已失效", block_desc)

        if self.is_public_only:
            public_desc = (
                "⚠️ 当前处于公开页面验证模式 (--public-only)：\n\n"
                "• Apple 登录态未就绪，仅验证公开 Catalog 与门店自提快照；\n"
                "• 坚决禁止进入加车与购买推进主流程；\n"
                "• 首发状态标记为暂不可启动，绝不虚假显示已就绪。\n\n"
                "💡 如需完整联调，请先运行: python -m src.main --mode prepare-session"
            )
            return ("公开页面验证", "ORANGE", "⚠️ 公开页面受限模式", public_desc)

        if self.dashboard_mode == "FORMAL_LAUNCH":
            return (
                "已隔离保护",
                "GREEN",
                "🔒 资金安全保护已启用",
                "自动化流程严守安全底线，绝对不触碰：\n"
                "• Apple ID 账户密码\n"
                "• 双重认证 (2FA) 验证码\n"
                "• 复杂图形人机验证 (CAPTCHA)\n"
                "• 支付方式与扣款网关\n"
                "• 不可逆最终订单提交\n\n"
                "💡 当需要人工操作时，系统会自动停止自动化、鸣响报警并将浏览器置顶前台。"
            )
        elif self.dashboard_mode == "RUNTIME_VERIFY":
            return (
                "真实加车已禁用",
                "BLUE",
                "🧪 受控验证保护模式",
                "当前处于受控联调验证模式：\n"
                "• 硬门禁 RUNTIME_VERIFY_NO_ADD_TO_BAG 强制锁定为 True\n"
                "• 严格执行规格核验与终极 FINAL_TARGET_CHECK\n"
                "• 绝不发起真实 add_to_bag() 网络请求\n"
                "• 绝不生成真实订单与资金扣款\n\n"
                "💡 演练完成后可安全关闭窗口，无需担心误购。"
            )
        else:  # DEMO
            return (
                "全部状态模拟",
                "ORANGE",
                "🎨 界面演示 · 安全沙箱",
                "当前处于纯本地界面演练模式：\n"
                "• 全部事件与状态均为内存生成\n"
                "• 绝不启动真实 Chromium 浏览器\n"
                "• 绝不访问 Apple 任何网络接口\n"
                "• 纯粹用于展示首发控制中心排版与交互\n\n"
                "💡 可随意体验，进程退出后不留存任何副作用。"
            )

    def add_event(self, level: str, message: str):
        """向事件流安全追加事件 (最多保留 20 条)"""
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        level_clean = level.upper()
        level_map = {
            "INFO": "信息",
            "SUCCESS": "成功",
            "WARNING": "提醒",
            "CRITICAL": "告警",
            "ERROR": "错误",
        }
        level_cn = level_map.get(level_clean, "信息")
        event = RecentEvent(timestamp=now_str, level=level_clean, level_cn=level_cn, message=message)
        self.recent_events.append(event)
        if len(self.recent_events) > 20:
            self.recent_events = self.recent_events[-20:]

    def set_target(self, product: str, color: str, storage: str, sku: str, locked: bool = True, mismatch: bool = False):
        self.target_product = product
        self.target_color = color
        self.target_storage = storage
        self.target_sku = sku
        self.target_locked = locked
        self.target_mismatch = mismatch
        self.target_status_text = "目标异常" if mismatch else ("目标已锁定" if locked else "未锁定")

    def update_store(self, store_number: str, status: str, status_cn: str, quote: str = ""):
        if store_number in self.stores:
            s = self.stores[store_number]
            s.status = status
            s.status_cn = status_cn
            if quote:
                s.pickup_quote = quote
            s.updated_at = datetime.datetime.now().strftime("%H:%M:%S")
            s.is_available = (status == "AVAILABLE")
            s.has_error = (status in ("ERROR", "BACKOFF", "403", "541", "429"))
