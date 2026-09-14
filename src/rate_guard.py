"""请求预算与速率守卫模块 (Phase 1E Rate Guard & Error Backoff Tracker)

核心职责：
1. RateGuard:
   - 显式 Request Budget 保护，跟踪 last_request_monotonic；
   - 强制两次请求之间间隔绝不低于 min_interval；
   - 防止异常重试、任务重启、事件循环重入导致的请求自旋或 1 秒高频轮询。
2. ErrorBackoffTracker:
   - 针对 403 / 429 / 541 边缘拦截执行严格逐级指数退避 (60s -> 120s -> 240s，上限 300s)；
   - 追踪 consecutive_failures 与 backoff_until；
   - 处于退避期内严格禁止发送任何 HTTP 或页面请求；
   - 成功后平滑恢复 (逐级回降)，禁止瞬间跳变回高频。
3. 架构解耦:
   - Online 与 Pickup 各自持有独立的 RateGuard 与 ErrorBackoffTracker 实例；
   - 彼此退避状态与错误计数器完全隔离。
"""

import asyncio
import logging
import random
import time
from typing import List, Optional, Tuple

logger = logging.getLogger("duo_buyer.rate_guard")


class RateGuard:
    """显式请求频率与预算守卫 (Phase 1E Rate Safety Guard)：
    防止任何异常、任务重启、事件循环重入导致的过密请求或请求自旋。
    跟踪单调时间戳 last_request_monotonic，强制保证两次请求之间时间差不低于 min_interval。
    """

    def __init__(self, min_interval: float = 15.0, name: str = "RateGuard"):
        self.min_interval = float(min_interval)
        self.name = name
        self.last_request_monotonic: float = 0.0
        self.total_requests: int = 0

    def can_request(self) -> bool:
        """检查当前时刻是否已经满足最小安全间隔"""
        if self.last_request_monotonic <= 0.0:
            return True
        return (time.monotonic() - self.last_request_monotonic) >= self.min_interval

    def remaining_wait(self) -> float:
        """计算距离满足下一次请求还需要等待的剩余秒数"""
        if self.last_request_monotonic <= 0.0:
            return 0.0
        elapsed = time.monotonic() - self.last_request_monotonic
        return max(0.0, self.min_interval - elapsed)

    async def throttle(self, stop_event: Optional[asyncio.Event] = None) -> float:
        """检查自上次请求以来的耗时，若不足 min_interval 则异步休眠剩余时间。
        休眠完毕后自动更新 last_request_monotonic 并增加计数。
        返回实际等待的秒数。
        """
        now = time.monotonic()
        wait_time = 0.0

        if self.last_request_monotonic > 0.0:
            elapsed = now - self.last_request_monotonic
            if elapsed < self.min_interval:
                wait_time = self.min_interval - elapsed
                logger.debug(
                    f"⏳ [{self.name}] RateGuard 保护触发：距离上次请求 {elapsed:.2f}s < "
                    f"最小安全间隔 {self.min_interval:.2f}s，强制节流等待 {wait_time:.2f}s..."
                )
                if stop_event is not None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=wait_time)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(wait_time)

        self.last_request_monotonic = time.monotonic()
        self.total_requests += 1
        return wait_time

    def mark_request(self):
        """显式记录请求发出时刻 (用于外部节流或同步场景)"""
        self.last_request_monotonic = time.monotonic()
        self.total_requests += 1

    def reset(self):
        """重置内部计时器"""
        self.last_request_monotonic = 0.0
        self.total_requests = 0


class ErrorBackoffTracker:
    """错误退避追踪器 (Phase 1E 核心)：
    针对 403 / 429 / 541 边缘拦截与网络异常执行逐级指数退避。
    维护 consecutive_failures, consecutive_edge_blocks, backoff_until。
    """

    def __init__(
        self,
        name: str = "BackoffTracker",
        edge_tiers: Optional[List[float]] = None,
        max_edge_backoff: float = 300.0,
        network_backoff_base: float = 5.0,
        max_network_backoff: float = 60.0,
    ):
        self.name = name
        # 默认阶梯: 60s -> 120s -> 240s
        self.edge_tiers: List[float] = edge_tiers if edge_tiers is not None else [60.0, 120.0, 240.0]
        self.max_edge_backoff = float(max_edge_backoff)
        self.network_backoff_base = float(network_backoff_base)
        self.max_network_backoff = float(max_network_backoff)

        self.consecutive_failures: int = 0
        self.consecutive_edge_blocks: int = 0
        self.backoff_until: float = 0.0
        self.last_status_code: int = 200

    def record_edge_block(self, status_code: int = 403) -> float:
        """记录 403 / 429 / 541 拦截，设置并返回需要退避的时长 (秒)"""
        self.last_status_code = status_code
        self.consecutive_failures += 1

        idx = min(self.consecutive_edge_blocks, len(self.edge_tiers) - 1)
        backoff_sec = self.edge_tiers[idx]
        if self.consecutive_edge_blocks >= len(self.edge_tiers):
            # 超过预设阶梯上限，按顶阶翻倍但严格受限于 max_edge_backoff
            multiplier = 2 ** (self.consecutive_edge_blocks - len(self.edge_tiers) + 1)
            backoff_sec = min(self.max_edge_backoff, self.edge_tiers[-1] * multiplier)
        backoff_sec = min(backoff_sec, self.max_edge_backoff)

        self.consecutive_edge_blocks += 1
        now = time.monotonic()
        self.backoff_until = max(self.backoff_until, now + backoff_sec)

        logger.warning(
            f"⚠️ [{self.name}] 命中边缘节点拦截 (HTTP {status_code})！"
            f"执行第 {self.consecutive_edge_blocks} 级退避，休眠 {backoff_sec:.1f}s "
            f"(backoff_until={self.backoff_until:.2f})"
        )
        return backoff_sec

    def record_network_error(self, err_msg: str = "") -> float:
        """记录普通网络异常 (DNS 超时、TCP 连接重置等)"""
        self.consecutive_failures += 1
        backoff_sec = min(self.max_network_backoff, self.consecutive_failures * self.network_backoff_base)
        now = time.monotonic()
        self.backoff_until = max(self.backoff_until, now + backoff_sec)

        logger.warning(
            f"⚠️ [{self.name}] 网络检测异常 (第 {self.consecutive_failures} 次): {err_msg}。"
            f"退避 {backoff_sec:.1f}s (backoff_until={self.backoff_until:.2f})"
        )
        return backoff_sec

    def record_success(self):
        """请求成功：逐步平滑恢复正常节奏，不立即突变"""
        self.last_status_code = 200
        if self.consecutive_edge_blocks > 0:
            self.consecutive_edge_blocks -= 1
        if self.consecutive_failures > 0:
            self.consecutive_failures -= 1
        now = time.monotonic()
        if self.backoff_until <= now:
            self.backoff_until = 0.0

    def is_in_backoff(self) -> bool:
        """检查当前是否处于退避期"""
        return time.monotonic() < self.backoff_until

    def remaining_backoff(self) -> float:
        """获取当前剩余退避时间"""
        return max(0.0, self.backoff_until - time.monotonic())


def calculate_cadence_sleep(
    base_interval: float,
    jitter_range: Tuple[float, float],
    min_interval: float,
    backoff_remaining: float = 0.0,
) -> float:
    """综合基准间隔、Jitter 抖动、最小安全间隔以及剩余退避时间，计算本轮休眠时间"""
    jitter = random.uniform(jitter_range[0], jitter_range[1])
    normal_sleep = max(min_interval, base_interval + jitter)
    return max(normal_sleep, backoff_remaining)
