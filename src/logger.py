"""日志管理模块，提供统一、清晰的控制台与文件日志输出。"""

import logging
import os
import sys
from datetime import datetime

class ColoredFormatter(logging.Formatter):
    """带 ANSI 颜色的终端日志格式化器"""

    COLORS = {
        logging.DEBUG: "\033[36m",     # 青色
        logging.INFO: "\033[32m",      # 绿色
        logging.WARNING: "\033[33m",   # 黄色
        logging.ERROR: "\033[31m",     # 红色
        logging.CRITICAL: "\033[35m",  # 紫色
    }
    RESET = "\033[0m"
    BOLD = "\033[1m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        time_str = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        levelname = f"[{record.levelname:<7}]"
        
        # 特别高亮人工接管提示
        if getattr(record, "is_takeover", False):
            return (
                f"\n{self.BOLD}\033[41m\033[37m"
                f" ===================== [ 🚨 需要人工接管 ] ===================== {self.RESET}\n"
                f"{self.BOLD}\033[93m[{time_str}] {record.getMessage()}{self.RESET}\n"
                f"{self.BOLD}\033[41m\033[37m"
                f" ================================================================ {self.RESET}\n"
            )
            
        return f"{color}[{time_str}] {levelname}{self.RESET} {record.getMessage()}"


def setup_logger(name: str = "apple_buyer", log_dir: str = "./logs") -> logging.Logger:
    """初始化并返回全局 Logger"""
    os.makedirs(log_dir, exist_ok=True)
    
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    
    # 避免重复绑定 handler
    if logger.handlers:
        return logger

    # 控制台 Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ColoredFormatter())
    logger.addHandler(console_handler)

    # 文件 Handler (纯文本)
    today = datetime.now().strftime("%Y-%m-%d")
    file_handler = logging.FileHandler(os.path.join(log_dir, f"buyer_{today}.log"), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)-7s] [%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    return logger

def log_takeover_alert(logger: logging.Logger, message: str):
    """专用人工接管高危报警输出"""
    record = logger.makeRecord(
        logger.name,
        logging.CRITICAL,
        "",
        0,
        message,
        None,
        None
    )
    record.is_takeover = True
    logger.handle(record)
    # 蜂鸣声提示 (在 macOS 终端发出 bell)
    sys.stdout.write("\a")
    sys.stdout.flush()
