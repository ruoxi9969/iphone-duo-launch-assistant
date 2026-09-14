"""
iPhone Duo 首发控制中心 - 模拟演示引擎 (Dashboard Demo Runner)
===========================================================
用于在完全离线、不启动 Chromium、不访问真实 Apple、不发送真实飞书的前提下，
完整走通控制中心的生命周期演进，供人工进行视觉自审与界面截屏归档。

【演进时间轴 (Timeline)】:
0s  : 系统初始化 (INITIALIZING)
2s  : Apple 会话登录态通过 (SESSION OK)
4s  : 官方 Catalog 解析成功并锁定 MK2P4CH/A (CATALOG_VERIFIED)
6s  : 首发目标页面预选就绪 (FORMAL_TARGET_PREPARED)
8s  : 启动线上与自提双通道监控 (MONITORING)
12s : 上海四店自提轮询快照 (PICKUP_COMING_SOON)
15s : 线上官网状态核查快照 (ONLINE_COMING_SOON)
18s : 线上购买触发并到达人工安全停机点 (HUMAN_HANDOFF_READY)
22s : 演示完成，自动保存全套截图并安全退出
"""

import os
import time
import subprocess
import tkinter as tk
import logging
from typing import Optional

from .dashboard_state import DashboardState
from .dashboard_adapter import DashboardAdapter
from .dashboard_gui import LaunchControlCenterGUI

logger = logging.getLogger("DashboardDemo")


def capture_window_screenshot(root: tk.Tk, output_path: str):
    """通过 macOS 原生 screencapture 精准捕获控制中心窗口截图"""
    try:
        root.lift()
        root.attributes("-topmost", True)
        root.update_idletasks()
        root.update()
        time.sleep(0.3)
        x = root.winfo_rootx()
        y = root.winfo_rooty()
        w = root.winfo_width()
        h = root.winfo_height()
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        subprocess.run(["screencapture", "-R", f"{x},{y},{w},{h}", output_path], check=False)
        root.attributes("-topmost", False)
        if os.path.exists(output_path):
            size_kb = os.path.getsize(output_path) / 1024
            logger.info(f"📸 控制中心界面截图已保存: {output_path} ({size_kb:.1f} KB)")
            print(f"  📸 截图已保存: {output_path} ({size_kb:.1f} KB)")
    except Exception as e:
        logger.warning(f"截图捕获失败: {e}")


def run_dashboard_demo(auto_exit_seconds: int = 24, auto_capture: bool = True):
    """
    运行控制中心演示模式 (--mode dashboard-demo)
    """
    print("\n" + "=" * 65)
    print("  【🍎 iPhone Duo 首发控制中心】演示模式 (Dashboard Demo)")
    print("  特点: 纯本地渲染 · 绝不启动 Chromium · 绝不请求 Apple 官网 · 绝无真实下单")
    print("=" * 65 + "\n")

    root = tk.Tk()
    adapter = DashboardAdapter()
    gui = LaunchControlCenterGUI(root, adapter=adapter, is_demo=True)

    screenshot_dir = os.path.abspath("logs/screenshots/dashboard")
    os.makedirs(screenshot_dir, exist_ok=True)

    # 阶段 0: 初始状态 (0s)
    def stage_0():
        adapter.get_latest_state().hero_title = "系统正在初始化..."
        adapter.get_latest_state().hero_subtitle = "正在加载配置文件与首发监控组件，请稍候。"
        adapter.get_latest_state().hero_level = "PREPARING"
        adapter.get_latest_state().session_status = "检查中"
        adapter.get_latest_state().catalog_status = "解析中"
        adapter.get_latest_state().browser_status = "启动中"
        adapter.get_latest_state().target_prepare_status = "未就绪"
        adapter.get_latest_state().pipeline_session = "ACTIVE"
        adapter.get_latest_state().pipeline_prepare = "PENDING"
        adapter.get_latest_state().pipeline_monitor = "PENDING"
        adapter.add_event("INFO", "控制中心系统初始化完成，开始演练...")
        gui.apply_state_to_widgets()
        if auto_capture:
            root.after(1000, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "dashboard_initial.png")))

    # 阶段 1: 会话验证通过 (2s)
    def stage_1():
        adapter.on_session_status(True, "Apple Account 登录凭证有效")
        adapter.get_latest_state().browser_status = "已预热"
        adapter.get_latest_state().pipeline_prepare = "ACTIVE"
        gui.apply_state_to_widgets()

    # 阶段 2: 官方 Catalog 解析通过 (4s)
    def stage_2():
        adapter.on_catalog_verified("iPhone Duo", "MK2P4CH/A", sku_count=8)
        gui.apply_state_to_widgets()

    # 阶段 3: 首发目标预热完成 (6s)
    def stage_3():
        adapter.on_target_prepared("iPhone Duo", "星光白色", "512GB", "MK2P4CH/A", success=True)
        gui.apply_state_to_widgets()
        if auto_capture:
            root.after(800, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "dashboard_ready.png")))

    # 阶段 4: 双通道监控启动 (8s)
    def stage_4():
        adapter.on_online_status("COMING_SOON", "即将发售", failures=0, backoff_sec=0.0)
        adapter.on_custom_event("INFO", "🌐 线上商品监控任务启动 (基准间隔 25s, 最小保护 15s)")
        adapter.on_custom_event("INFO", "🏬 上海 4 店自提监控任务启动 (基准间隔 30s, 单店节流 >= 2.0s)")
        gui.apply_state_to_widgets()
        if auto_capture:
            root.after(1500, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "dashboard_monitoring.png")))

    # 阶段 5: 自提快照 (12s)
    def stage_5():
        adapter.on_pickup_round_completed(9.35, next_check_text="约 28 秒后")
        adapter.on_store_checked("R390", "香港广场", "COMING_SOON", "即将发售", "目前暂不提供取货服务")
        adapter.on_store_checked("R401", "上海环贸 iapm", "COMING_SOON", "即将发售", "目前暂不提供取货服务")
        adapter.on_store_checked("R581", "五角场", "COMING_SOON", "即将发售", "目前暂不提供取货服务")
        adapter.on_store_checked("R683", "环球港", "COMING_SOON", "即将发售", "目前暂不提供取货服务")
        adapter.on_custom_event("INFO", "上海 4 家直营店第 1 轮巡检完成 (耗时 9.35s，均未放货)")
        gui.apply_state_to_widgets()

    # 阶段 6: 线上状态核验 (15s)
    def stage_6():
        adapter.on_online_status("COMING_SOON", "即将发售", next_check_text="约 18 秒后")
        adapter.on_custom_event("INFO", "线上商品状态确认: COMING_SOON (静默等待开售整点)")
        gui.apply_state_to_widgets()

    # 阶段 7: 触发购买并到达人工停机点 (18s)
    def stage_7():
        adapter.on_online_status("AVAILABLE", "可购买")
        adapter.on_human_action_required("【界面演示】模拟购买流程已推进至人工接管阶段。本模式未访问 Apple，也未执行真实加车。")
        gui.apply_state_to_widgets()
        if auto_capture:
            root.after(1000, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "dashboard_handoff.png")))
            root.after(1500, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "dashboard_demo.png")))

    # 阶段 8: 自动退出 (auto_exit_seconds)
    def stage_exit():
        print("\n✅ 控制中心演示演练完成，正在安全退出...")
        root.destroy()

    # 注册时间轴调度
    root.after(100, stage_0)
    root.after(2000, stage_1)
    root.after(4000, stage_2)
    root.after(6000, stage_3)
    root.after(8000, stage_4)
    root.after(12000, stage_5)
    root.after(15000, stage_6)
    root.after(18000, stage_7)

    if auto_exit_seconds > 0:
        root.after(auto_exit_seconds * 1000, stage_exit)

    # 启动 GUI 主事件循环
    root.mainloop()
