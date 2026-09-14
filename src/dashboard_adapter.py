"""
iPhone Duo 首发控制中心 - 观察者适配器 (Dashboard Adapter)
=====================================================
作为生产系统与 Tkinter GUI 之间的最小侵入式单向状态传递桥梁 (Observer / State Sink)。

【核心安全隔离原则】：
1. 彻底与购买核心链路解耦：Dashboard 崩溃、未启动或窗口关闭，绝对不影响 Launch 购买；
2. 线程安全桥接：生产端 (asyncio 协程/后台线程) 通过无锁队列发送事件，GUI 主线程定时拉取；
3. 零阻塞设计：所有提交均为非阻塞 (put_nowait)，队列满时自动丢弃最旧数据，绝不阻塞主购买流程；
4. 异常完全隔离：适配器所有公开方法均带有全局捕获，任何序列化或传参错误绝不向外抛出。
"""

import queue
import logging
import datetime
from typing import Optional, Dict, Any, Callable
from .dashboard_state import DashboardState, mask_webhook_url

logger = logging.getLogger("DashboardAdapter")


class DashboardAdapter:
    """
    单向状态汇聚适配器 (Thread-Safe State Sink)
    """

    def __init__(self, max_queue_size: int = 500):
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self._is_active: bool = True
        self._latest_state: DashboardState = DashboardState()

    @property
    def is_active(self) -> bool:
        return self._is_active

    @is_active.setter
    def is_active(self, val: bool):
        self._is_active = val

    def get_latest_state(self) -> DashboardState:
        return self._latest_state

    def _safe_put(self, action: Callable[[DashboardState], None]):
        """线程安全非阻塞投递状态变更操作"""
        if not self._is_active:
            return
        try:
            # 立即在本地影子状态上执行一次以保持 latest_state 鲜活
            action(self._latest_state)
            self._queue.put_nowait(action)
        except queue.Full:
            # 队列满时丢弃一条最旧消息，防止无界堆积
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(action)
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"DashboardAdapter 状态投递被安全隔离: {e}")

    # ==========================================
    # 状态通知接口 (生产链路中仅需轻量调用以下方法)
    # ==========================================

    def init_from_config(self, config: Any):
        def update(st: DashboardState):
            if hasattr(config, "target") and config.target:
                if getattr(config.target, "product", None):
                    st.target_product = config.target.product
                if getattr(config.target, "color", None):
                    st.target_color = config.target.color
                if getattr(config.target, "storage", None):
                    st.target_storage = config.target.storage
                if getattr(config.target, "part_number", None):
                    st.target_sku = config.target.part_number
            if hasattr(config, "notifications") and config.notifications and hasattr(config.notifications, "feishu"):
                if config.notifications.feishu:
                    url = getattr(config.notifications.feishu, "webhook_url", "") or ""
                    st.feishu_masked_token = mask_webhook_url(url)
                    st.feishu_status = "已验证" if getattr(config.notifications.feishu, "verified", False) else "未配置"
            if hasattr(config, "launch") and config.launch and hasattr(config.launch, "launch_time"):
                st.launch_time_iso = config.launch.launch_time
                st.update_current_time()
        self._safe_put(update)

    def on_mode_changed(self, mode: str):
        def update(st: DashboardState):
            st.dashboard_mode = mode
            st.is_demo = (mode == "DEMO")
        self._safe_put(update)

    def on_browser_started(self, ok: bool = True, desc: str = ""):
        def update(st: DashboardState):
            st.browser_status = "已启动" if ok else "未启动"
            st.checklist["browser_prewarmed"] = ok
            st.add_event("INFO", f"Chromium 浏览器已启动: {desc or '就绪 (Browser=1, Context=1, Page=1)'}")
        self._safe_put(update)

    def on_session_checking(self):
        def update(st: DashboardState):
            st.session_status = "检查中"
            st.add_event("INFO", "正在真实核验 Apple Account 会话登录态...")
        self._safe_put(update)

    def on_session_status(self, is_ok: bool, desc: str = "", verified_at: str = ""):
        def update(st: DashboardState):
            st.session_status = "已就绪" if is_ok else "未就绪"
            st.session_last_verified_at = verified_at or datetime.datetime.now().strftime("%H:%M:%S")
            st.checklist["session_verified"] = is_ok
            if is_ok:
                st.session_strict_blocked = False
                st.pipeline_session = "COMPLETED"
                st.add_event("SUCCESS", f"Apple 登录会话验证通过: {desc or '会话有效'}")
            else:
                st.pipeline_session = "ACTIVE"
                st.add_event("WARNING", f"Apple 登录会话未就绪: {desc or '需要登录'}")
        self._safe_put(update)

    def on_session_strict_blocked(self, reason: str = ""):
        def update(st: DashboardState):
            st.session_status = "未就绪"
            st.session_strict_blocked = True
            st.session_fail_reason = reason
            st.checklist["session_verified"] = False
            st.hero_title = "首发流程已阻断"
            st.hero_subtitle = "Apple 登录已失效，首发流程已阻断。请重新登录：python -m src.main --mode prepare-session"
            st.hero_level = "ERROR"
            st.add_event("CRITICAL", f"🛑 [LAUNCH_BLOCKED_SESSION] Apple 登录会话失效，流程严格阻断 ({reason})")
        self._safe_put(update)

    def on_public_only_warning(self):
        def update(st: DashboardState):
            st.is_public_only = True
            st.hero_title = "受限模式 · 仅公开页面验证"
            st.hero_subtitle = "⚠️ Apple 登录会话未就绪 当前仅进行公开页面验证 禁止进入购买流程"
            st.hero_level = "WARNING"
            st.add_event("WARNING", "⚠️ [PUBLIC_ONLY_MODE] 会话未登录，当前仅验证公开目录与自提快照，购买已禁用")
        self._safe_put(update)

    def on_catalog_checking(self):
        def update(st: DashboardState):
            st.catalog_status = "解析中"
            st.add_event("INFO", "正在获取并解析官方实时 Catalog...")
        self._safe_put(update)

    def on_catalog_verified(self, product: str, sku: str, sku_count: int = 8):
        def update(st: DashboardState):
            st.catalog_status = "已验证"
            st.target_product = product
            st.target_sku = sku
            st.target_locked = True
            st.checklist["catalog_locked"] = True
            st.add_event("SUCCESS", f"官方 Catalog 解析成功锁定: {sku} (共 {sku_count} 个 SKU)")
        self._safe_put(update)

    def on_target_preparing(self):
        def update(st: DashboardState):
            st.target_prepare_status = "预选中"
            st.pipeline_prepare = "ACTIVE"
            st.add_event("INFO", "正在执行首发目标规格正式预热与预选 (星光白色 · 512GB)...")
        self._safe_put(update)

    def on_target_prepared(self, product: str, color: str, storage: str, sku: str, success: bool = True):
        def update(st: DashboardState):
            st.target_product = product
            st.target_color = color
            st.target_storage = storage
            st.target_sku = sku
            st.target_locked = success
            st.target_prepare_status = "已就绪" if success else "异常"
            st.checklist["target_spec_prepared"] = success
            if success:
                st.pipeline_prepare = "COMPLETED"
                st.pipeline_monitor = "ACTIVE"
                if not st.is_public_only:
                    st.hero_title = "首发系统已准备就绪" if st.is_ready_for_launch() else "系统已准备就绪"
                    st.hero_subtitle = f"{product} · {color} · {storage} [{sku}] 预选成功，双通道监控中。"
                    st.hero_level = "READY"
                st.add_event("SUCCESS", f"首发目标预选就绪: {product} {color} {storage} [{sku}]")
            else:
                st.hero_title = "首发目标预选失败"
                st.hero_subtitle = "规格不一致或无法定位选项，请人工排查购买页面。"
                st.hero_level = "ERROR"
                st.add_event("CRITICAL", f"首发目标预选失败: {sku}")
        self._safe_put(update)

    def on_purchase_options_prepared(self, tradein_ok: bool = True, applecare_ok: bool = True):
        def update(st: DashboardState):
            st.purchase_options_prepared = (tradein_ok and applecare_ok)
            st.tradein_status = "已预选 (不折抵)" if tradein_ok else "未预选"
            st.applecare_status = "已预选 (不加服务)" if applecare_ok else "未预选"
            st.checklist["purchase_options_prepared"] = (tradein_ok and applecare_ok)
            st.add_event("SUCCESS", "✅ [FORMAL_PURCHASE_OPTIONS_PREPARED] 购买选项预选就绪 (不折抵 · 不加 AppleCare)")
        self._safe_put(update)

    def on_purchase_options_preserved(self):
        def update(st: DashboardState):
            st.checklist["selection_preservation_verified"] = True
            st.add_event("SUCCESS", "✅ [FORMAL_PURCHASE_OPTIONS_PRESERVED] 折抵与 AppleCare 选项已完全保留，跳过重复点击")
        self._safe_put(update)

    def on_latency_recorded(self, handoff_sec: float, feishu_ack_ms: Optional[float] = None):
        def update(st: DashboardState):
            st.latency_trigger_to_handoff_sec = handoff_sec
            if feishu_ack_ms is not None:
                st.feishu_last_ack_ms = feishu_ack_ms
            st.add_event("INFO", f"⚡ [LATENCY_PERF] 触发至人工接管耗时: {handoff_sec:.2f} 秒 (飞书 ACK: {st.feishu_last_ack_ms or 0:.1f}ms)")
        self._safe_put(update)

    def on_online_status(
        self,
        state_token: str,
        state_cn: str,
        failures: int = 0,
        backoff_sec: float = 0.0,
        next_check_text: str = "约 25 秒后",
    ):
        def update(st: DashboardState):
            st.online_state_token = state_token
            st.online_state_cn = state_cn
            st.online_last_check = datetime.datetime.now().strftime("%H:%M:%S")
            st.online_next_check = next_check_text
            st.online_failures = failures
            st.online_backoff_sec = backoff_sec
            st.checklist["online_monitor_standby"] = True
            if state_token == "AVAILABLE":
                st.hero_title = "线上商城已开放购买！"
                st.hero_subtitle = "检测到 AVAILABLE 信号，购买主通道已触发！"
                st.hero_level = "ONLINE_AVAILABLE"
                st.pipeline_purchase = "ACTIVE"
                st.add_event("SUCCESS", "⚡ 线上商城开放购买！触发自动化购买推进")
            elif backoff_sec > 0:
                st.add_event("WARNING", f"线上请求触发安全退避: 休眠 {backoff_sec:.0f}s (连续错误 {failures})")
        self._safe_put(update)

    def on_pickup_round_completed(
        self,
        duration_sec: float,
        next_check_text: str = "约 30 秒后",
        backoff_sec: float = 0.0,
    ):
        def update(st: DashboardState):
            st.pickup_last_check = datetime.datetime.now().strftime("%H:%M:%S")
            st.pickup_next_check = next_check_text
            st.pickup_round_duration = f"{duration_sec:.2f} 秒"
            st.pickup_backoff_sec = backoff_sec
            st.checklist["pickup_monitor_standby"] = True
        self._safe_put(update)

    def on_store_checked(
        self,
        store_number: str,
        store_name: str,
        status: str,
        status_cn: str,
        quote: str = "",
    ):
        def update(st: DashboardState):
            st.update_store(store_number, status, status_cn, quote)
            if status == "AVAILABLE":
                st.add_event("SUCCESS", f"🎉 直营店发现自提现货: {store_name} ({store_number}) [{quote or '可取货'}]")
        self._safe_put(update)

    def on_feishu_status(self, connected: bool, last_ack_ms: Optional[float] = None, pending_count: int = 0, raw_url: str = ""):
        def update(st: DashboardState):
            st.feishu_status = "已连接" if connected else "未配置"
            if last_ack_ms is not None:
                st.feishu_last_ack_ms = last_ack_ms
            st.pending_notifications = pending_count
            if raw_url:
                st.feishu_masked_token = mask_webhook_url(raw_url)
            st.checklist["feishu_async_ready"] = connected
        self._safe_put(update)

    def on_online_checking(self):
        def update(st: DashboardState):
            st.add_event("INFO", "正在检查 Apple 线上购买通道状态...")
        self._safe_put(update)

    def on_pickup_checking(self):
        def update(st: DashboardState):
            st.add_event("INFO", "正在巡检上海 4 直营店自提库存快照...")
        self._safe_put(update)

    def on_selection_preserved(self, sku: str = "MK2P4CH/A"):
        def update(st: DashboardState):
            st.checklist["selection_preservation_verified"] = True
            st.add_event("SUCCESS", f"✅ [FORMAL_SELECTION_PRESERVED] 目标规格完全保留 (星光白色 · 512GB · {sku})")
        self._safe_put(update)

    def on_final_target_checked(self, ok: bool, sku: str = "MK2P4CH/A"):
        def update(st: DashboardState):
            if ok:
                st.add_event("SUCCESS", f"✅ [FINAL_TARGET_CHECK] 终极规格一致性硬门禁核验通过 ({sku})")
            else:
                st.add_event("CRITICAL", f"🛑 [FINAL_TARGET_CHECK_FAILED] 终极规格一致性核验失败 ({sku})")
        self._safe_put(update)

    def on_runtime_verify_blocked(self, sku: str = "MK2P4CH/A"):
        def update(st: DashboardState):
            st.hero_title = "受控验证完成 · 真实加车已安全阻断"
            st.hero_subtitle = f"星光白色 · 512GB · {sku} 终极一致性核验通过，受控硬门禁生效。"
            st.hero_level = "READY"
            st.pipeline_purchase = "COMPLETED"
            st.add_event("SUCCESS", "🛡️ [RUNTIME_VERIFY_ADD_TO_BAG_BLOCKED] 受控硬门禁生效：安全阻断真实加车")
        self._safe_put(update)

    def on_human_action_required(self, reason: str = ""):
        def update(st: DashboardState):
            st.human_action_required = True
            st.human_action_reason = reason or st.get_handoff_text()
            st.pipeline_handoff = "ACTIVE"
            st.pipeline_purchase = "COMPLETED"
            st.hero_title = "⚠️ 请立即人工接管浏览器"
            st.hero_subtitle = f"{st.get_handoff_text()} (详情: {reason or '已到达安全停机点'})"
            st.hero_level = "HANDOFF"
            st.add_event("CRITICAL", f"🚨 人工接管触发：{reason or '到达安全停机点'}")
        self._safe_put(update)

    def on_custom_event(self, level: str, message: str):
        def update(st: DashboardState):
            st.add_event(level, message)
        self._safe_put(update)

    def add_event(self, level: str, message: str):
        self.on_custom_event(level, message)

    def on_stage_changed(self, stage_id: str, desc: str = ""):
        def update(st: DashboardState):
            if stage_id == "session":
                st.pipeline_session = "ACTIVE"
            elif stage_id == "prepare":
                st.pipeline_session = "COMPLETED"
                st.pipeline_prepare = "ACTIVE"
            elif stage_id == "monitor":
                st.pipeline_session = "COMPLETED"
                st.pipeline_prepare = "COMPLETED"
                st.pipeline_monitor = "ACTIVE"
            elif stage_id == "purchase":
                st.pipeline_session = "COMPLETED"
                st.pipeline_prepare = "COMPLETED"
                st.pipeline_monitor = "COMPLETED"
                st.pipeline_purchase = "ACTIVE"
            elif stage_id == "handoff":
                st.pipeline_session = "COMPLETED"
                st.pipeline_prepare = "COMPLETED"
                st.pipeline_monitor = "COMPLETED"
                st.pipeline_purchase = "COMPLETED"
                st.pipeline_handoff = "ACTIVE"
            if desc:
                st.add_event("INFO", f"流程阶段更新: {desc}")
        self._safe_put(update)

    # ==========================================
    # GUI 主线程拉取方法 (非阻塞批量排空)
    # ==========================================

    def drain_to_state(self, gui_state: DashboardState, max_batch: int = 50) -> int:
        """GUI 主线程轮询调用：将队列中积压的变更函数依次应用到 gui_state"""
        count = 0
        while count < max_batch:
            try:
                action = self._queue.get_nowait()
                action(gui_state)
                count += 1
            except queue.Empty:
                break
            except Exception as e:
                logger.debug(f"GUI 状态消费异常: {e}")
                break
        gui_state.update_current_time()
        return count
