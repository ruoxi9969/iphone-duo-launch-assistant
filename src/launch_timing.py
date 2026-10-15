"""Timezone-aware launch timing without increasing the normal request budget."""

from datetime import datetime
import time
from typing import Optional
from pathlib import Path
import fcntl


def parse_launch_timestamp(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("launch.launch_time 必须包含时区，例如 2026-10-16T20:00:00+08:00")
    return parsed.timestamp()


def launch_wait_seconds(timestamp: Optional[float]) -> float:
    return max(0.0, timestamp - time.time()) if timestamp is not None else 0.0


def acquire_launch_lock(path: str = "state/launch.lock"):
    """The returned handle owns the lock until the process/main function exits."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("抢购助手已经在运行，请使用已打开的窗口，避免重复启动。") from None
    return handle
