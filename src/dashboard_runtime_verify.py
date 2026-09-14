"""
iPhone Duo 首发控制中心 - 受控真实运行时联调 (Dashboard Runtime Verify)
===================================================================
模式: --mode dashboard-runtime-verify
特点:
1. 真实 Dashboard GUI (Tkinter 主线程, 模式徽章 【受控联调 · 禁止真实加车】)
2. 真实 Persistent Chromium (1 Context, 1 Page, 复用 ./browser_data)
3. 真实核验 Apple Session, 真实 Catalog, 真实目标预选 (星光白色 · 512GB · MK2P4CH/A)
4. 真实 Online Snapshot + 上海四店 Pickup Snapshot (严格单店节流 >= 2.0s)
5. 模拟库存触发 (SIMULATED_RUNTIME_ONLINE_AVAILABLE)
6. 严格硬门禁 RUNTIME_VERIFY_NO_ADD_TO_BAG = True，绝对禁止真实加车与订单
7. 窗口焦点协调：人工接管触发时自动置前 Chromium，Dashboard 坚决不争夺焦点
8. 高清 Retina 截图自动保存至 logs/screenshots/dashboard_v11/
"""

import os
import time
import subprocess
import threading
import asyncio
import logging
import tkinter as tk
from typing import Optional

from .config import AppConfig
from .dashboard_state import DashboardState
from .dashboard_adapter import DashboardAdapter
from .dashboard_gui import LaunchControlCenterGUI
from .main import run_formal_runtime_verify

logger = logging.getLogger("DashboardRuntimeVerify")


def capture_window_screenshot(root: tk.Tk, output_path: str, is_handoff: bool = False):
    """通过 macOS 原生 screencapture 精准捕获控制中心窗口截图"""
    try:
        if not root.winfo_exists():
            return
        if not is_handoff:
            root.lift()
            root.attributes("-topmost", True)
            root.update_idletasks()
            root.update()
            time.sleep(0.3)
            root.attributes("-topmost", False)
        else:
            root.attributes("-topmost", False)
            root.update_idletasks()
            root.update()
        x = root.winfo_rootx()
        y = root.winfo_rooty()
        w = root.winfo_width()
        h = root.winfo_height()
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        subprocess.run(["screencapture", "-R", f"{x},{y},{w},{h}", output_path], check=False)
        if os.path.exists(output_path):
            size_kb = os.path.getsize(output_path) / 1024
            logger.info(f"📸 控制中心 V1.1 截图已保存: {output_path} ({size_kb:.1f} KB)")
            print(f"  📸 截图已保存: {output_path} ({size_kb:.1f} KB)")
    except Exception as e:
        logger.warning(f"截图捕获失败: {e}")


def run_dashboard_runtime_verify(
    config: AppConfig,
    auto_exit_seconds: Optional[int] = None,
    test_close_isolation: bool = False,
    submode: str = "strict",
    ignore_session_check: bool = False,
) -> bool:
    """
    运行受控运行时联调验证 (--mode dashboard-runtime-verify)
    """
    if submode == "public-only":
        ignore_session_check = True

    print("\n" + "=" * 65)
    print("  【🍎 iPhone Duo 控制中心受控运行时联调】Dashboard Runtime Verify (V1.2)")
    print(f"  子模式: {submode} (Strict Fail-Closed={not ignore_session_check})")
    print("  模式标识: 【受控联调 · 禁止真实加车】(RUNTIME_VERIFY)")
    print("  窗口规划: 1 Dashboard GUI + 1 Persistent Chromium")
    print("  安全红线: RUNTIME_VERIFY_NO_ADD_TO_BAG = True 绝对生效")
    print("=" * 65 + "\n")

    adapter = DashboardAdapter()
    if config:
        adapter.init_from_config(config)
    st = adapter.get_latest_state()
    st.dashboard_mode = "RUNTIME_VERIFY"
    if submode == "public-only":
        st.is_public_only = True
        st.hero_title = "受限模式 · 仅公开页面验证"
        st.hero_subtitle = "⚠️ Apple 登录会话未就绪 当前仅进行公开页面验证 禁止进入购买流程"
        st.hero_level = "WARNING"
    else:
        st.hero_title = "正在初始化受控联调环境..."
        st.hero_subtitle = "启动持久化 Chromium 浏览器并连接 Apple 官方环境。"
        st.hero_level = "PREPARING"
    st.browser_status = "未启动"
    st.session_status = "未就绪"
    st.catalog_status = "解析中"
    st.target_prepare_status = "未就绪"
    st.pipeline_session = "ACTIVE"
    st.pipeline_prepare = "PENDING"
    st.pipeline_monitor = "PENDING"
    adapter.add_event("INFO", f"控制中心进入受控运行时联调模式 ({submode})...")

    screenshot_dir = os.path.abspath("logs/screenshots/dashboard_v12")
    os.makedirs(screenshot_dir, exist_ok=True)

    root = tk.Tk()
    gui = LaunchControlCenterGUI(root, adapter=adapter, is_demo=False)
    gui.apply_state_to_widgets()

    # 捕获初始态截图 (01_initial.png)
    root.after(800, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "01_initial.png")))

    verification_result = {"success": False, "finished": False}

    def run_worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # 执行受控真实运行时验证
            ok = loop.run_until_complete(
                run_formal_runtime_verify(
                    config=config,
                    ignore_session_check=ignore_session_check,
                    submode=submode,
                    dashboard_adapter=adapter,
                )
            )
            verification_result["success"] = ok
        except Exception as e:
            logger.error(f"受控运行时联调异常: {e}")
            adapter.on_custom_event("CRITICAL", f"运行时联调异常: {e}")
        finally:
            verification_result["finished"] = True
            loop.close()

    worker_thread = threading.Thread(target=run_worker, daemon=True)
    worker_thread.start()

    captured_stages = set()

    def check_stage_captures():
        if not root.winfo_exists():
            return
        st = adapter.get_latest_state()

        # 02 Session OK
        if "02" not in captured_stages and st.pipeline_session == "COMPLETED":
            captured_stages.add("02")
            root.after(300, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "02_session_ok.png")))

        # 02b Session Blocked
        if "02b" not in captured_stages and st.session_strict_blocked:
            captured_stages.add("02b")
            root.after(300, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "02_session_blocked.png")))

        # 03 Target Prepared
        if "03" not in captured_stages and st.pipeline_prepare == "COMPLETED":
            captured_stages.add("03")
            root.after(300, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "03_target_prepared.png")))

        # 04 Monitoring Snapshot
        if "04" not in captured_stages and st.pipeline_monitor == "ACTIVE" and any(s.status == "COMING_SOON" for s in st.stores.values()):
            captured_stages.add("04")
            root.after(300, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "04_monitoring_real_snapshot.png")))

        # 05 Runtime Trigger
        if "05" not in captured_stages and (st.online_state_token == "AVAILABLE" or "线上通道现货已就绪" in st.hero_title):
            captured_stages.add("05")
            root.after(300, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "05_runtime_trigger.png")))

        # 06 Final Verified
        if "06" not in captured_stages and ("受控验证完成" in st.hero_title or any("RUNTIME_VERIFY_ADD_TO_BAG_BLOCKED" in e.message for e in st.recent_events)):
            captured_stages.add("06")
            root.after(300, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "06_final_verified.png")))

        # 07 Handoff Simulation
        if "07" not in captured_stages and st.human_action_required:
            captured_stages.add("07")
            root.after(300, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "07_handoff_simulation.png"), is_handoff=True))

        root.after(500, check_stage_captures)

    root.after(1000, check_stage_captures)

    # 兜底定时抓取
    root.after(4500, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "02_session_ok.png")))
    root.after(10000, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "03_target_prepared.png")))
    root.after(22000, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "04_monitoring_real_snapshot.png")))
    root.after(26000, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "05_runtime_trigger.png")))
    root.after(29000, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "06_final_verified.png")))
    root.after(32000, lambda: capture_window_screenshot(root, os.path.join(screenshot_dir, "07_handoff_simulation.png"), is_handoff=True))

    # 如果启用了窗口关闭隔离测试 (PART I)
    if test_close_isolation:
        def trigger_close_isolation():
            logger.info("🧪 [PART I 隔离测试] 模拟关闭 Dashboard 窗口，验证后台购买任务不受影响...")
            root.destroy()
        # 在真实 Snapshot 完成后模拟关闭窗口
        root.after(23000, trigger_close_isolation)

    if auto_exit_seconds is not None and auto_exit_seconds > 0:
        def auto_quit():
            if root.winfo_exists():
                print(f"\n✅ 受控联调演示计时完成 ({auto_exit_seconds}s)，正在退出 GUI...")
                root.destroy()
        root.after(auto_exit_seconds * 1000, auto_quit)

    try:
        root.mainloop()
    except Exception as e:
        logger.warning(f"GUI mainloop 退出: {e}")

    # 等待后台任务平稳结束
    worker_thread.join(timeout=10.0)
    logger.info(f"Dashboard Runtime Verify 完成 (Success={verification_result['success']})")
    return verification_result["success"]
