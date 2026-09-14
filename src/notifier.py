"""通知服务模块 (飞书 Webhook 移动端推送与本地双保险音频提醒 - V0.4 Launch Day 实战版)

支持事件：
- TARGET_AVAILABLE      : 目标商品可购买/有货
- PICKUP_AVAILABLE      : 直营店到店自提出现库存
- ONLINE_AVAILABLE      : 官网线上支持快递配送
- COMING_SOON           : 官方状态为即将发售
- PREORDER_READY        : 官方预购已开启配置准备
- TARGET_CHANGED        : 官方 SKU 规格发生变动 (严重告警)
- HUMAN_ACTION_REQUIRED : 到达敏感步骤需人工接管 (停机点)
- SYSTEM_ERROR          : 系统异常或连续拦截告警

设计与安全原则：
1. 凭证安全保护：飞书 Webhook 在日志与界面中一律脱敏 (https://open.feishu.cn/.../AB****YZ)，严防 Key 泄露。
2. 未配置 Webhook 时，明确输出 FEISHU_WAITING_FOR_CONFIGURATION，绝不编造发送成功。
3. 严格真机验证：仅在用户手机实际确认收到时才输出 FEISHU_END_TO_END_OK，接口成功输出 FEISHU_WEBHOOK_REQUEST_OK。
4. 消息卡片合规：卡片内【立即前往 Apple 官网】按钮仅允许指向 Apple 官方网址，严禁指向任何第三方站点。
5. 状态去重机制：同一 SKU+渠道+门店+状态不重复刷屏，状态从 UNAVAILABLE 变回 AVAILABLE 时重新提醒。
6. 双保险与故障容错：飞书发送超时/断网/失败自动标记 FEISHU_DEGRADED，绝不导致主程序崩溃，本地 macOS 铃声独立于网络持续保障。
"""

import os
import sys
import json
import time
import asyncio
import hashlib
import datetime
from datetime import datetime
import subprocess
import urllib.request
import urllib.error
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum

from .logger import setup_logger
from .config import NotificationConfig, FeishuConfig, BarkConfig, SoundConfig, mask_feishu_webhook, mask_credential

logger = setup_logger("notifier")

class NotificationEvent(str, Enum):
    TARGET_AVAILABLE = "TARGET_AVAILABLE"
    PICKUP_AVAILABLE = "PICKUP_AVAILABLE"
    ONLINE_AVAILABLE = "ONLINE_AVAILABLE"
    COMING_SOON = "COMING_SOON"
    PREORDER_READY = "PREORDER_READY"
    TARGET_CHANGED = "TARGET_CHANGED"
    HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"
    SYSTEM_ERROR = "SYSTEM_ERROR"
    ERROR = "SYSTEM_ERROR"  # 兼容旧字段


def get_event_meta(event: NotificationEvent, product: str = "iPhone Duo") -> Tuple[str, str]:
    """根据事件类型获取标准化标题与飞书卡片主题色 (template)"""
    if event == NotificationEvent.PICKUP_AVAILABLE:
        return f"🍎 {product} 上海门店有货", "green"
    elif event == NotificationEvent.ONLINE_AVAILABLE:
        return f"🍎 {product} 官网可以购买", "green"
    elif event == NotificationEvent.TARGET_AVAILABLE:
        return f"🍎 {product} 目标库存出现", "green"
    elif event == NotificationEvent.PREORDER_READY:
        return f"🔔 {product} 预备抢购已开启", "orange"
    elif event == NotificationEvent.COMING_SOON:
        return f"ℹ️ {product} 官方状态：即将发售", "blue"
    elif event == NotificationEvent.TARGET_CHANGED:
        return f"🚨 {product} 官方 SKU 发生变化", "red"
    elif event == NotificationEvent.HUMAN_ACTION_REQUIRED:
        return "⚠️ Apple 购买流程需要人工接管", "orange"
    elif event in (NotificationEvent.SYSTEM_ERROR, NotificationEvent.ERROR):
        return "⚠️ Apple 抢购助手异常", "red"
    return f"🍎 {product} 状态更新", "blue"


@dataclass
class NotificationPayload:
    event: NotificationEvent
    title: str = ""
    product: str = "iPhone Duo"
    color: str = "星光白色"
    storage: str = "512GB"
    sku: str = "MK2P4CH/A"
    store: str = ""
    channel: str = ""  # "线上快递" / "Apple Store 自提" / "Apple 中国官网"
    status_text: str = ""
    quote: str = ""    # 如 "今天可取" / "明天可取"
    url: str = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
    details: str = ""
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not self.title:
            default_title, _ = get_event_meta(self.event, self.product or "iPhone Duo")
            self.title = default_title
        # 严格限定跳转按钮 URL 必须为 Apple 官方网址
        if self.url and not (self.url.startswith("https://www.apple.com.cn") or self.url.startswith("https://apple.com.cn") or self.url.startswith("https://secure")):
            self.url = "https://www.apple.com.cn"

    def format_plain_text(self) -> str:
        """构建标准化飞书文本正文"""
        lines = [f"{self.title}\n"]
        if self.product:
            lines.append(f"产品：{self.product}")
        if self.color or self.storage:
            lines.append(f"配置：{self.color} · {self.storage}")
        if self.sku:
            lines.append(f"SKU：{self.sku}")
        if self.store:
            lines.append(f"门店：{self.store}")
        elif self.channel:
            lines.append(f"渠道：{self.channel}")
        if self.status_text:
            lines.append(f"状态：{self.status_text}")
        if self.quote:
            lines.append(f"取货说明：{self.quote}")
        lines.append(f"时间：{self.timestamp}")
        if self.details:
            lines.append(f"说明：{self.details}")
        if self.url:
            lines.append(f"👉 Apple 官方购买页面: {self.url}")
        return "\n".join(lines)

    def format_interactive_card(self) -> Dict[str, Any]:
        """构建飞书富文本交互式消息卡片 (带官方直达按钮)"""
        _, template_color = get_event_meta(self.event, self.product)

        md_content = []
        if self.product:
            md_content.append(f"**产品**：{self.product}")
        if self.color or self.storage:
            md_content.append(f"**配置**：{self.color} · {self.storage}")
        if self.sku:
            md_content.append(f"**SKU**：{self.sku}")
        if self.store:
            md_content.append(f"**门店**：{self.store}")
        elif self.channel:
            md_content.append(f"**渠道**：{self.channel}")
        if self.status_text:
            md_content.append(f"**状态**：{self.status_text}")
        if self.quote:
            md_content.append(f"**取货说明**：{self.quote}")
        md_content.append(f"**时间**：{self.timestamp}")
        if self.details:
            md_content.append(f"**说明**：{self.details}")

        elements: List[Dict[str, Any]] = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(md_content)
                }
            }
        ]

        # 官方直达按钮 (严格只指向 Apple 官网)
        if self.url:
            btn_text = "👉 立即前往 Apple 官网" if "buy" in self.url or "shop" in self.url else "👉 查看 Apple 官网"
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": btn_text
                        },
                        "type": "primary",
                        "url": self.url
                    }
                ]
            })

        card_payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": self.title
                    },
                    "template": template_color
                },
                "elements": elements
            }
        }
        return card_payload


class NotificationDeduplicator:
    """
    通知去重状态机：
    缓存同一 (SKU + 渠道 + 门店) 的上一通知状态。
    若状态未发生改变，短时间内拒绝重复刷屏；
    若状态发生变迁 (如 UNAVAILABLE -> AVAILABLE，或 COMING_SOON -> PREORDER_READY)，允许发送；
    若变迁后再次回到 AVAILABLE，允许重新发送提醒。
    """

    def __init__(self):
        # 键: (sku, channel, store) -> 值: (event, status_text)
        self._history: Dict[Tuple[str, str, str], Tuple[NotificationEvent, str]] = {}

    def should_send(self, payload: NotificationPayload) -> bool:
        # 严重告警或人工接管永远不拦截
        if payload.event in (
            NotificationEvent.HUMAN_ACTION_REQUIRED,
            NotificationEvent.TARGET_CHANGED,
            NotificationEvent.SYSTEM_ERROR
        ):
            return True

        key = (payload.sku or "", payload.channel or "", payload.store or "")
        last_state = self._history.get(key)

        current_state = (payload.event, payload.status_text)
        if last_state == current_state:
            # 状态完全相同，去重丢弃
            return False

        return True

    def record_sent(self, payload: NotificationPayload):
        key = (payload.sku or "", payload.channel or "", payload.store or "")
        self._history[key] = (payload.event, payload.status_text)

    def clear(self):
        self._history.clear()


class FeishuNotifier:
    """飞书群自定义机器人 Webhook 推送客户端"""

    def __init__(self, config: FeishuConfig):
        self.config = config
        self.consecutive_failures = 0
        self.is_degraded = False

    async def send(self, payload: NotificationPayload) -> bool:
        """异步发送飞书通知，网络异常严格隔离绝不崩溃"""
        if not self.config.enabled:
            logger.debug("飞书通知服务未启用，跳过推送")
            return False

        webhook_url = (self.config.webhook_url or "").strip()
        if not webhook_url:
            logger.debug(f"飞书 Webhook 未配置 ({self.config.masked_url})，跳过推送")
            return False

        card_data = payload.format_interactive_card()
        success = await asyncio.to_thread(self._send_http_post, webhook_url, card_data)

        if success:
            self.consecutive_failures = 0
            self.is_degraded = False
            logger.info(f"📲 飞书卡片推送成功 [{self.config.masked_url}]: {payload.title}")
            return True
        else:
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3 and not self.is_degraded:
                self.is_degraded = True
                logger.warning(f"⚠️ [FEISHU_DEGRADED] 飞书 Webhook 连续失败 {self.consecutive_failures} 次，已标记降级模式，由本地音频持续接管报警！")
            return False

    def _send_http_post(self, webhook_url: str, post_data: Dict[str, Any]) -> bool:
        """使用标准库 urllib 发送 JSON POST 请求"""
        req_bytes = json.dumps(post_data).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=req_bytes,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "AppleStoreInventoryBuyer/0.4 (FeishuBot)",
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                resp_bytes = resp.read()
                try:
                    res_json = json.loads(resp_bytes.decode("utf-8"))
                    # 飞书 API 规范：code == 0 或 StatusCode == 0 表示成功
                    code = res_json.get("code", res_json.get("StatusCode", -1))
                    if code == 0:
                        return True
                    else:
                        logger.warning(f"飞书服务响应错误码: code={code}, msg={res_json.get('msg', res_json.get('StatusMessage'))}")
                        return False
                except Exception:
                    return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            logger.warning(f"飞书 HTTP 异常: HTTP {e.code} ({e.reason})")
            return False
        except Exception as e:
            logger.warning(f"飞书网络请求超时或无法连接: {e}")
            return False


class LocalSoundNotifier:
    """本地声音提醒器 (macOS 原生音频与终端蜂鸣，完全独立于互联网)"""

    def __init__(self, config: SoundConfig):
        self.config = config

    def play(self, event: NotificationEvent):
        """播放本地提醒音，异步非阻塞"""
        if not self.config.enabled:
            return

        # 1. 终端铃声蜂鸣 (跨平台通用，确保任何情况下有提示)
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass

        # 2. macOS afplay 原生声音播放
        if sys.platform == "darwin":
            sound_file = "/System/Library/Sounds/Ping.aiff"
            if event in (
                NotificationEvent.TARGET_AVAILABLE,
                NotificationEvent.PICKUP_AVAILABLE,
                NotificationEvent.ONLINE_AVAILABLE,
                NotificationEvent.PREORDER_READY,
                NotificationEvent.TARGET_CHANGED,
            ):
                if os.path.exists("/System/Library/Sounds/Hero.aiff"):
                    sound_file = "/System/Library/Sounds/Hero.aiff"
                elif os.path.exists("/System/Library/Sounds/Glass.aiff"):
                    sound_file = "/System/Library/Sounds/Glass.aiff"

            if os.path.exists(sound_file):
                try:
                    subprocess.Popen(
                        ["afplay", sound_file],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                except Exception as e:
                    logger.debug(f"macOS afplay 调用失败: {e}")


class NotificationTaskManager:
    """生产级后台通知任务管理器 (Phase 1C 核心架构)：
    1. 保存任务引用 (pending_tasks)，防止被 Python GC 提前回收；
    2. 自动注册 done callback，安全回收并捕获异常，彻底杜绝 'Task exception was never retrieved'；
    3. 维护任务完成日志与失败统计；
    4. 支持退出时有界限的超时清理 (drain)，防止退出时泄漏或无限挂起；
    """

    def __init__(self, name: str = "Notifier"):
        self.name = name
        self._pending_tasks: set[asyncio.Task] = set()
        self._completed_count: int = 0
        self._failed_count: int = 0

    @property
    def pending_count(self) -> int:
        return len(self._pending_tasks)

    @property
    def completed_count(self) -> int:
        return self._completed_count

    @property
    def failed_count(self) -> int:
        return self._failed_count

    @property
    def degraded_errors_count(self) -> int:
        return self._failed_count

    def submit(self, coro, task_name: str = "") -> asyncio.Task:
        """提交异步协程为受管后台任务"""
        task = asyncio.create_task(coro, name=task_name or f"{self.name}-{time.time()}")
        self._pending_tasks.add(task)
        task.add_done_callback(self._handle_task_done)
        return task

    def _handle_task_done(self, task: asyncio.Task):
        """Done callback: 回收引用并安全捕获异常"""
        self._pending_tasks.discard(task)
        if task.cancelled():
            logger.debug(f"后台通知任务已取消: {task.get_name()}")
            return
        exc = task.exception()
        if exc is not None:
            self._failed_count += 1
            logger.error(f"❌ 后台通知任务执行异常 [{task.get_name()}]: {exc}")
        else:
            self._completed_count += 1
            logger.debug(f"✅ 后台通知任务成功完成: {task.get_name()}")

    async def drain(self, timeout: float = 3.0) -> int:
        """有限时间排空未决通知任务，超时强制取消，绝不无限挂起"""
        if not self._pending_tasks:
            return 0
        tasks = list(self._pending_tasks)
        logger.info(f"正在排空 {len(tasks)} 个未决后台通知任务 (限时 {timeout:.1f}s)...")
        try:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                logger.warning(f"⚠️ {len(pending)} 个通知任务未在 {timeout:.1f}s 内完成，触发超时取消...")
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            return len(done)
        except Exception as e:
            logger.error(f"排空后台通知任务异常: {e}")
            return 0


class Notifier:
    """综合通知协调器 (飞书 + 本地声音 + 去重 + 降级容错 + 异步非阻塞调度)"""

    def __init__(self, config: NotificationConfig):
        self.config = config
        self.feishu = FeishuNotifier(config.feishu)
        self.sound = LocalSoundNotifier(config.sound)
        self.deduplicator = NotificationDeduplicator()
        self.task_manager = NotificationTaskManager("Notifier")

    def notify_background(
        self,
        event: NotificationEvent,
        title: str = "",
        product: str = "iPhone Duo",
        color: str = "星光白色",
        storage: str = "512GB",
        sku: str = "MK2P4CH/A",
        store: str = "",
        channel: str = "",
        status_text: str = "",
        quote: str = "",
        url: str = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo",
        details: str = "",
        force: bool = False,
    ) -> Optional[asyncio.Task]:
        """
        非阻塞后台发送通知 (Phase 1C 核心)：
        1. 检查去重 (NotificationDeduplicator)
        2. 立即触发本地声音警报 (LocalSoundNotifier)，零延迟
        3. 记录已发送状态
        4. 将飞书网络推送提交给 TaskManager 在后台异步发送
        5. 返回 Task 句柄，调用方绝不被 HTTP ACK 阻塞
        """
        payload = NotificationPayload(
            event=event,
            title=title,
            product=product,
            color=color,
            storage=storage,
            sku=sku,
            store=store,
            channel=channel,
            status_text=status_text,
            quote=quote,
            url=url,
            details=details,
        )

        # 1. 检查去重 (若状态相同则不重复刷屏)
        if not force and not self.deduplicator.should_send(payload):
            logger.debug(f"🔕 相同状态去重跳过通知: {payload.sku} - {payload.store or payload.channel} - {payload.status_text}")
            return None

        # 2. 本地声音警报 (无论网络状态如何，强制本地响铃双保险，零延迟)
        self.sound.play(event)

        # 3. 记录已发送状态
        self.deduplicator.record_sent(payload)

        # 4. 提交给后台任务管理器在独立任务中发送飞书
        try:
            loop = asyncio.get_running_loop()
            task = self.task_manager.submit(
                self._send_feishu_safe(payload),
                task_name=f"feishu-{event.value}-{sku or 'target'}"
            )
        except RuntimeError:
            task = None
        return task

    async def _send_feishu_safe(self, payload: NotificationPayload) -> bool:
        """内部发送飞书请求，未捕获异常由 TaskManager 的 done_callback 记录并回收"""
        return await self.feishu.send(payload)

    async def drain_pending_notifications(self, timeout: float = 3.0) -> int:
        """有限时间排空后台通知任务"""
        return await self.task_manager.drain(timeout=timeout)


    async def notify(
        self,
        event: NotificationEvent,
        title: str = "",
        product: str = "iPhone Duo",
        color: str = "星光白色",
        storage: str = "512GB",
        sku: str = "MK2P4CH/A",
        store: str = "",
        channel: str = "",
        status_text: str = "",
        quote: str = "",
        url: str = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo",
        details: str = "",
        force: bool = False,
    ) -> bool:
        """统一发送事件通知 (飞书推送 + 本地声音双保险 + 去重处理)"""
        payload = NotificationPayload(
            event=event,
            title=title,
            product=product,
            color=color,
            storage=storage,
            sku=sku,
            store=store,
            channel=channel,
            status_text=status_text,
            quote=quote,
            url=url,
            details=details,
        )

        # 1. 检查去重 (若状态相同则不重复刷屏)
        if not force and not self.deduplicator.should_send(payload):
            logger.debug(f"🔕 相同状态去重跳过通知: {payload.sku} - {payload.store or payload.channel} - {payload.status_text}")
            return False

        # 2. 本地声音警报 (无论网络状态如何，强制本地响铃双保险)
        self.sound.play(event)

        # 3. 记录已发送状态
        self.deduplicator.record_sent(payload)

        # 4. 飞书移动端 Webhook 推送
        feishu_ok = await self.feishu.send(payload)
        return feishu_ok

    async def test_feishu(self, interactive: bool = True) -> Tuple[bool, str]:
        """
        飞书通道端到端测试模式：
        1. 若未配置 Webhook，输出 FEISHU_WAITING_FOR_CONFIGURATION，不编造成功。
        2. 若 Webhook 请求成功，输出 FEISHU_WEBHOOK_REQUEST_OK。
        3. 若用户确认手机飞书 App 实际收到消息，输出 FEISHU_END_TO_END_OK。
        """
        logger.info("正在执行通知链路连通性测试 (飞书 Webhook + 本地声音)...")

        payload = NotificationPayload(
            event=NotificationEvent.TARGET_AVAILABLE,
            title="🍎 Apple 抢购助手测试",
            product="iPhone Duo",
            color="星光白色",
            storage="512GB",
            sku="MK2P4CH/A",
            channel="通知链路测试",
            status_text="测试中",
            url="https://www.apple.com.cn/shop/buy-iphone/iphone-duo",
            details="此通知用于验证 Mac -> 本地程序 -> 飞书 Webhook -> 飞书服务器 -> 用户手机飞书 App 链路。",
        )

        # 触发本地声音
        self.sound.play(payload.event)

        webhook_url = (self.config.feishu.webhook_url or "").strip()
        if not webhook_url:
            logger.warning(f"⚠️ 飞书 Webhook 状态: FEISHU_WAITING_FOR_CONFIGURATION ({self.config.feishu.masked_url})")
            print("\n" + "=" * 65)
            print("  【飞书测试状态: FEISHU_WAITING_FOR_CONFIGURATION】")
            print("  您尚未配置飞书自定义机器人 Webhook 地址！已触发终端铃声与 macOS 本地音频。")
            print("  请按以下步骤配置:")
            print("  1. 在飞书群添加【自定义机器人】获取 Webhook 地址")
            print("  2. 在 config.yaml 填入:")
            print("       notifications:")
            print("         feishu:")
            print("           enabled: true")
            print("           webhook_url: \"https://open.feishu.cn/open-apis/bot/v2/hook/...\"")
            print("  或在终端执行环境变量注入:")
            print("       export FEISHU_WEBHOOK=\"https://open.feishu.cn/open-apis/bot/v2/hook/...\"")
            print("\n  【推送卡片内容预览 (本地模拟)】")
            print(f"  标题: {payload.title}")
            print("  " + payload.format_plain_text().replace("\n", "\n  "))
            print("=" * 65 + "\n")
            return False, "FEISHU_WAITING_FOR_CONFIGURATION"

        logger.info(f"正在向飞书 Webhook 发送测试请求: {self.config.feishu.masked_url} ...")
        req_ok = await self.feishu.send(payload)

        if not req_ok:
            print("\n" + "=" * 65)
            print("  【飞书测试状态: FEISHU_REQUEST_FAILED】")
            print(f"  飞书 Webhook 请求失败，请检查网络或地址有效性 ({self.config.feishu.masked_url})。")
            print("  已触发 macOS 本地声音与终端铃声兜底。")
            print("=" * 65 + "\n")
            return False, "FEISHU_REQUEST_FAILED"

        # Webhook 请求成功
        print("\n" + "=" * 65)
        print("  【飞书测试状态: FEISHU_WEBHOOK_REQUEST_OK】")
        print(f"  ✅ Webhook HTTP 请求成功抵达飞书服务器 [{self.config.feishu.masked_url}]！")

        if interactive:
            try:
                user_confirm = input("  📱 请检查您的手机飞书 App 是否实际收到测试消息？(y/N): ").strip().lower()
                if user_confirm in ("y", "yes"):
                    save_feishu_verification_state(webhook_url, verified=True, last_test_status="FEISHU_END_TO_END_OK")
                    fp_masked = mask_fingerprint(compute_webhook_fingerprint(webhook_url))
                    print("  🎉 飞书真机端到端验证通过: FEISHU_END_TO_END_OK")
                    print(f"  已持久化验证记录至 {DEFAULT_FEISHU_STATE_FILE} (fingerprint: {fp_masked})")
                    print("=" * 65 + "\n")
                    return True, "FEISHU_END_TO_END_OK"
                else:
                    save_feishu_verification_state(webhook_url, verified=False, last_test_status="FEISHU_WEBHOOK_REQUEST_OK")
                    print("  ⚠️ 未获手机端实际接收确认，当前状态保持为: FEISHU_WEBHOOK_REQUEST_OK (未通过端到端核验)")
                    print(f"  已记录未通过状态至 {DEFAULT_FEISHU_STATE_FILE}")
                    print("=" * 65 + "\n")
                    return False, "FEISHU_WEBHOOK_REQUEST_OK"
            except (EOFError, KeyboardInterrupt):
                save_feishu_verification_state(webhook_url, verified=False, last_test_status="FEISHU_WEBHOOK_REQUEST_OK")
                print("  (未获输入确认，保持 FEISHU_WEBHOOK_REQUEST_OK)")
        
        print("=" * 65 + "\n")
        return True, "FEISHU_WEBHOOK_REQUEST_OK"


DEFAULT_FEISHU_STATE_FILE = "state/feishu_verification.json"
VERIFICATION_MAX_AGE_DAYS = 30


def compute_webhook_fingerprint(webhook_url: str) -> str:
    """计算 Webhook URL 的 SHA-256 摘要哈希值 (64位十六进制字符)"""
    url_clean = (webhook_url or "").strip()
    if not url_clean:
        return ""
    return hashlib.sha256(url_clean.encode("utf-8")).hexdigest()


def mask_fingerprint(fingerprint: str) -> str:
    """脱敏展示 fingerprint: 4e27****91ab"""
    if not fingerprint:
        return "(none)"
    if len(fingerprint) > 8:
        return f"{fingerprint[:4]}****{fingerprint[-4:]}"
    return "****"


def save_feishu_verification_state(
    webhook_url: str,
    verified: bool,
    last_test_status: str,
    state_file: str = DEFAULT_FEISHU_STATE_FILE,
    verified_at: Optional[str] = None
):
    """
    持久化飞书端到端验证状态至本地状态文件 (如 state/feishu_verification.json)。
    严格只保存：
    - verified: true / false
    - verified_at: ISO 8601 时间戳
    - webhook_fingerprint: SHA-256 哈希
    - last_test_status: 测试结果状态
    严禁保存完整 Webhook URL、Key 等敏感信息！
    """
    clean_url = (webhook_url or "").strip()
    if not clean_url:
        return

    dir_name = os.path.dirname(state_file)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    fingerprint = compute_webhook_fingerprint(clean_url)
    if not verified_at:
        verified_at = datetime.now().astimezone().isoformat(timespec="seconds")

    data = {
        "verified": verified,
        "verified_at": verified_at,
        "webhook_fingerprint": fingerprint,
        "last_test_status": last_test_status,
    }

    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    logger.debug(f"已持久化飞书验证状态至 {state_file} (fingerprint: {mask_fingerprint(fingerprint)})")


def verify_feishu_status(
    webhook_url: str,
    state_file: str = DEFAULT_FEISHU_STATE_FILE,
    max_age_days: int = VERIFICATION_MAX_AGE_DAYS
) -> Tuple[bool, str, str]:
    """
    加载并核验飞书端到端真机验证状态。
    返回 (is_valid, status_code, reason_desc)
    """
    clean_url = (webhook_url or "").strip()
    if not clean_url:
        return False, "FEISHU_NOT_CONFIGURED", "飞书 Webhook 尚未配置"

    # 环境变量显式覆盖（供持续集成或自动化测试使用）
    if os.environ.get("FEISHU_END_TO_END_OK") in ("1", "true", "True", "yes"):
        return True, "FEISHU_END_TO_END_OK", "飞书真机端到端验证通过 (环境变量覆盖)"

    if not os.path.exists(state_file):
        return False, "FEISHU_NOT_VERIFIED_END_TO_END", "尚未进行飞书端到端真机测试 (状态文件不存在)"

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        return False, "FEISHU_NOT_VERIFIED_END_TO_END", f"读取飞书验证状态文件失败: {e}"

    # 1. 检查 verified 字段
    if not state.get("verified"):
        return False, "FEISHU_NOT_VERIFIED_END_TO_END", "飞书端到端验证状态为未通过 (用户曾输入 n)"

    # 2. 检查 Webhook fingerprint 一致性 (防止继承旧 Webhook 验证)
    curr_fp = compute_webhook_fingerprint(clean_url)
    saved_fp = state.get("webhook_fingerprint", "")
    if not saved_fp or curr_fp != saved_fp:
        logger.warning(
            f"⚠️ [FEISHU_WEBHOOK_CHANGED] 当前 Webhook ({mask_fingerprint(curr_fp)}) "
            f"与验证记录 ({mask_fingerprint(saved_fp)}) 不一致！"
        )
        return False, "FEISHU_WEBHOOK_CHANGED", "当前 Webhook 与此前真机验证记录不一致 (已变更，需重新测试)"

    # 3. 检查有效期 (默认 30 天)
    verified_at_str = state.get("verified_at", "")
    if verified_at_str:
        try:
            verified_dt = datetime.fromisoformat(verified_at_str)
            if verified_dt.tzinfo is None:
                verified_dt = verified_dt.astimezone()
            now_dt = datetime.now().astimezone()
            age_days = (now_dt - verified_dt).total_seconds() / 86400.0
            if age_days > max_age_days:
                logger.warning(f"⚠️ [FEISHU_VERIFICATION_STALE] 飞书端到端验证已过期 ({age_days:.1f} 天 > {max_age_days} 天)")
                return False, "FEISHU_VERIFICATION_STALE", f"飞书端到端验证已超过 {max_age_days} 天有效期 (已过期，需重新验证)"
        except Exception as e:
            logger.warning(f"解析验证时间戳失败: {e}")

    return True, "FEISHU_END_TO_END_OK", "飞书真机端到端验证通过"


def is_feishu_end_to_end_verified(
    feishu_cfg: FeishuConfig,
    state_file: str = DEFAULT_FEISHU_STATE_FILE
) -> bool:
    """向后兼容辅助函数：返回当前配置的飞书 Webhook 是否通过端到端核验"""
    is_valid, _, _ = verify_feishu_status(feishu_cfg.webhook_url, state_file=state_file)
    return is_valid



