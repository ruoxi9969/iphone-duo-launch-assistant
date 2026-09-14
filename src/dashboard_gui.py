"""
iPhone Duo 首发控制中心 - 桌面交互界面 (Launch Control Center GUI)
============================================================
Apple 纯净设计语言 · 简体中文 · 原生 Tkinter + ttk 实现

【设计规范】：
1. 苹果极简浅色系：浅灰底色 (#F5F5F7) + 纯白卡片 (#FFFFFF) + 柔和边框 (#E5E5EA)；
2. 字体层级：优先调用 macOS 苹方 (PingFang SC) 与 SF Pro，中文清晰锐利；
3. 状态语义色：正常绿 (#34C759 / #1E7E34)、等待蓝 (#0071E3)、警告橙 (#FF9500)、异常红 (#FF3B30)；
4. 布局结构：顶部导航栏 + 三列式核心状态看板 + 底部最近事件日志流；
5. 线程安全：与 DashboardAdapter 无缝桥接，GUI 线程通过 root.after 定时安全拉取；
6. 故障隔离：独立运行，退出或崩溃绝不干扰后台抢购进程。
"""

import os
import sys
import time
import datetime
import tkinter as tk
from tkinter import ttk, font
from typing import Optional, Dict, Any, List

from .dashboard_state import DashboardState, StoreDisplayState, RecentEvent
from .dashboard_adapter import DashboardAdapter


# =====================================================================
# 苹果设计色彩常数表 (Apple Light Theme Palette)
# =====================================================================
BG_WINDOW = "#F5F5F7"         # 窗口主背景色 (Apple Light Grey)
BG_CARD = "#FFFFFF"           # 卡片背景纯白色
BORDER_CARD = "#E5E5EA"       # 卡片极细边框 (1px)
BORDER_ACTIVE = "#0071E3"     # 高亮/激活边框 (Apple Blue)

TEXT_PRIMARY = "#1D1D1F"      # 主标题与主文本 (Near Black)
TEXT_SECONDARY = "#6E6E73"    # 二级说明文字 (Medium Grey)
TEXT_MUTED = "#86868B"        # 辅助/时间戳文字 (Muted Grey)

# 语义胶囊背景与文字色
COLOR_GREEN_BG = "#E8F8EE"
COLOR_GREEN_FG = "#1E7E34"

COLOR_BLUE_BG = "#EBF4FE"
COLOR_BLUE_FG = "#0066CC"

COLOR_ORANGE_BG = "#FFF4E5"
COLOR_ORANGE_FG = "#B25E00"

COLOR_RED_BG = "#FDEBEC"
COLOR_RED_FG = "#C91A25"

COLOR_GREY_BG = "#EFEFF4"
COLOR_GREY_FG = "#636366"


def get_mac_font_family() -> str:
    """获取 macOS 优先的中文字体名称"""
    available = font.families() if font else []
    for candidate in ["PingFang SC", "苹方-简", ".AppleSystemUIFont", "SF Pro Text", "Helvetica Neue", "Arial"]:
        if candidate in available:
            return candidate
    return "Helvetica"


class CardFrame(tk.Frame):
    """标准 Apple 卡片容器 (白色背景 + 柔和细边框 + 内边距)"""

    def __init__(self, parent, bg=BG_CARD, border_color=BORDER_CARD, padx=16, pady=14, **kwargs):
        super().__init__(
            parent,
            bg=bg,
            highlightbackground=border_color,
            highlightcolor=border_color,
            highlightthickness=1,
            padx=padx,
            pady=pady,
            **kwargs
        )


class StatusPill(tk.Label):
    """状态胶囊指示器 (Rounded Pill Capsule)"""

    def __init__(self, parent, text="", level="GREEN", font_family="PingFang SC", font_size=11, **kwargs):
        self.font_family = font_family
        self.font_size = font_size
        super().__init__(
            parent,
            text=f"  {text}  ",
            font=(self.font_family, self.font_size, "bold"),
            padx=4,
            pady=2,
            **kwargs
        )
        self.set_level(level, text)

    def set_level(self, level: str, text: Optional[str] = None):
        if text is not None:
            self.config(text=f"  {text}  ")

        lvl = (level or "").upper()
        if lvl in ("GREEN", "SUCCESS", "READY", "ONLINE_AVAILABLE", "AVAILABLE", "已就绪", "正常", "已验证", "已锁定", "已启用", "可到店取货"):
            self.config(bg=COLOR_GREEN_BG, fg=COLOR_GREEN_FG)
        elif lvl in ("BLUE", "INFO", "MONITORING", "ACTIVE", "监控中", "查询中"):
            self.config(bg=COLOR_BLUE_BG, fg=COLOR_BLUE_FG)
        elif lvl in ("ORANGE", "WARNING", "PREPARING", "COMING_SOON", "即将发售", "退避中", "未启动", "未就绪"):
            self.config(bg=COLOR_ORANGE_BG, fg=COLOR_ORANGE_FG)
        elif lvl in ("RED", "CRITICAL", "ERROR", "HANDOFF", "目标异常", "接口异常", "熔断拦截"):
            self.config(bg=COLOR_RED_BG, fg=COLOR_RED_FG)
        else:
            self.config(bg=COLOR_GREY_BG, fg=COLOR_GREY_FG)


class HorizontalStepper(tk.Canvas):
    """Launch 5 级状态链水平进度横幅"""

    STAGES = [
        ("session", "登录正常"),
        ("prepare", "目标就绪"),
        ("monitor", "库存监控"),
        ("purchase", "购买推进"),
        ("handoff", "人工接管"),
    ]

    def __init__(self, parent, font_family="PingFang SC", bg=BG_CARD, height=64, **kwargs):
        super().__init__(parent, bg=bg, height=height, highlightthickness=0, **kwargs)
        self.font_family = font_family
        self.stage_states = {
            "session": "COMPLETED",
            "prepare": "COMPLETED",
            "monitor": "ACTIVE",
            "purchase": "PENDING",
            "handoff": "PENDING",
        }
        self.bind("<Configure>", lambda e: self.draw())

    def update_stages(self, session="COMPLETED", prepare="COMPLETED", monitor="ACTIVE", purchase="PENDING", handoff="PENDING"):
        self.stage_states = {
            "session": session,
            "prepare": prepare,
            "monitor": monitor,
            "purchase": purchase,
            "handoff": handoff,
        }
        self.draw()

    def draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 100:
            return

        n = len(self.STAGES)
        margin_x = 45
        step_x = (w - 2 * margin_x) / (n - 1) if n > 1 else 0
        node_y = 22

        # 1. 绘制底层连接横线
        for i in range(n - 1):
            x1 = margin_x + i * step_x
            x2 = margin_x + (i + 1) * step_x
            curr_state = self.stage_states.get(self.STAGES[i][0], "PENDING")
            next_state = self.stage_states.get(self.STAGES[i + 1][0], "PENDING")
            line_color = "#34C759" if curr_state == "COMPLETED" and next_state in ("COMPLETED", "ACTIVE") else "#E5E5EA"
            self.create_line(x1, node_y, x2, node_y, fill=line_color, width=3)

        # 2. 绘制每个阶段的节点与文字
        for i, (key, label) in enumerate(self.STAGES):
            cx = margin_x + i * step_x
            st = self.stage_states.get(key, "PENDING")

            if st == "COMPLETED":
                # 绿色实心圆带白色对勾
                self.create_oval(cx - 12, node_y - 12, cx + 12, node_y + 12, fill="#34C759", outline="#34C759")
                self.create_text(cx, node_y, text="✓", fill="#FFFFFF", font=(self.font_family, 11, "bold"))
                txt_color = "#1D1D1F"
            elif st == "ACTIVE":
                # 蓝色呼吸高亮圆
                self.create_oval(cx - 13, node_y - 13, cx + 13, node_y + 13, fill=COLOR_BLUE_BG, outline="#0071E3", width=2)
                self.create_oval(cx - 5, node_y - 5, cx + 5, node_y + 5, fill="#0071E3", outline="")
                txt_color = "#0071E3"
            else:
                # 灰色未完成圆
                self.create_oval(cx - 10, node_y - 10, cx + 10, node_y + 10, fill="#FFFFFF", outline="#D1D1D6", width=2)
                txt_color = "#86868B"

            # 节点文字
            self.create_text(cx, node_y + 24, text=label, fill=txt_color, font=(self.font_family, 11, "bold" if st in ("COMPLETED", "ACTIVE") else "normal"))


class LaunchControlCenterGUI:
    """
    iPhone Duo 首发控制中心主窗口
    """

    def __init__(self, root: tk.Tk, adapter: Optional[DashboardAdapter] = None, is_demo: bool = False):
        self.root = root
        self.adapter = adapter or DashboardAdapter()
        self.state = self.adapter.get_latest_state()
        self.state.is_demo = is_demo
        self.font_family = get_mac_font_family()

        self._setup_window()
        self._build_top_bar()
        self._build_bottom_events()
        self._build_main_body()

        # 启动主线程定时刷新轮询 (每 100ms 拉取一次状态队列)
        self.root.after(100, self._poll_adapter_queue)
        # 启动时钟与时间更新 (每 1000ms)
        self.root.after(1000, self._tick_clock)

    def _setup_window(self):
        self.root.title("iPhone Duo 首发控制中心")
        self.root.geometry("1440x900")
        self.root.minsize(1280, 760)
        self.root.configure(bg=BG_WINDOW)

    # -----------------------------------------------------------------
    # PART F: 顶部导航栏 (Header Bar)
    # -----------------------------------------------------------------
    def _build_top_bar(self):
        header_frame = tk.Frame(self.root, bg=BG_CARD, height=78, highlightbackground=BORDER_CARD, highlightthickness=1)
        header_frame.pack(side="top", fill="x", padx=0, pady=0)
        header_frame.pack_propagate(False)

        inner_header = tk.Frame(header_frame, bg=BG_CARD)
        inner_header.pack(fill="both", expand=True, padx=24, pady=8)

        # 左侧标题区
        left_box = tk.Frame(inner_header, bg=BG_CARD)
        left_box.pack(side="left", fill="y")

        title_row = tk.Frame(left_box, bg=BG_CARD)
        title_row.pack(side="top", anchor="w")

        apple_logo = tk.Label(title_row, text="🍎", font=(self.font_family, 20), bg=BG_CARD)
        apple_logo.pack(side="left", padx=(0, 6))

        app_title = tk.Label(
            title_row,
            text=self.state.app_title,
            font=(self.font_family, 22, "bold"),
            fg=TEXT_PRIMARY,
            bg=BG_CARD
        )
        app_title.pack(side="left", padx=(0, 10))

        b_text, b_level = self.state.get_mode_badge_info()
        self.mode_badge = StatusPill(title_row, text=b_text, level=b_level, font_family=self.font_family, font_size=11)
        self.mode_badge.pack(side="left")

        # Apple 会话状态指示器 (Header Session Pill)
        session_box = tk.Frame(title_row, bg=BG_CARD)
        session_box.pack(side="left", padx=(10, 0))
        tk.Label(session_box, text="Apple 会话:", font=(self.font_family, 11), fg=TEXT_SECONDARY, bg=BG_CARD).pack(side="left", padx=(0, 4))
        p_text, p_lvl, p_time = self.state.get_session_pill_info()
        self.header_session_pill = StatusPill(session_box, text=p_text, level=p_lvl, font_family=self.font_family, font_size=10)
        self.header_session_pill.pack(side="left")
        self.header_session_time_lbl = tk.Label(session_box, text=p_time, font=(self.font_family, 10), fg=TEXT_MUTED, bg=BG_CARD)
        self.header_session_time_lbl.pack(side="left", padx=(4, 0))

        app_sub = tk.Label(
            left_box,
            text=self.state.app_subtitle,
            font=(self.font_family, 12),
            fg=TEXT_MUTED,
            bg=BG_CARD
        )
        app_sub.pack(side="top", anchor="w", pady=(3, 0))

        # 右侧时间与开售倒计时区
        right_box = tk.Frame(inner_header, bg=BG_CARD)
        right_box.pack(side="right", fill="y")

        self.clock_label = tk.Label(
            right_box,
            text="00:00:00",
            font=(self.font_family, 20, "bold"),
            fg=TEXT_PRIMARY,
            bg=BG_CARD
        )
        self.clock_label.pack(side="top", anchor="e")

        clock_sub_box = tk.Frame(right_box, bg=BG_CARD)
        clock_sub_box.pack(side="top", anchor="e", pady=(3, 0))

        self.date_label = tk.Label(
            clock_sub_box,
            text="2026年9月14日",
            font=(self.font_family, 12),
            fg=TEXT_MUTED,
            bg=BG_CARD
        )
        self.date_label.pack(side="left", padx=(0, 8))

        self.countdown_pill = StatusPill(
            clock_sub_box,
            text="开售时间未设置",
            level="GREY",
            font_family=self.font_family,
            font_size=10
        )
        self.countdown_pill.pack(side="left")

        self.advice_pill = StatusPill(
            clock_sub_box,
            text="",
            level="GREY",
            font_family=self.font_family,
            font_size=10
        )
        self.advice_pill.pack(side="left", padx=(6, 0))

    # -----------------------------------------------------------------
    # 主体三列布局 (Three Columns Body)
    # -----------------------------------------------------------------
    def _build_main_body(self):
        body_container = tk.Frame(self.root, bg=BG_WINDOW)
        body_container.pack(side="top", fill="both", expand=True, padx=20, pady=(16, 8))

        # 配置网格列权重
        body_container.grid_columnconfigure(0, weight=0, minsize=320)  # 左侧状态区
        body_container.grid_columnconfigure(1, weight=1, minsize=540)  # 中央核心主状态
        body_container.grid_columnconfigure(2, weight=0, minsize=340)  # 右侧上海门店区
        body_container.grid_rowconfigure(0, weight=1)

        # 1. 左列容器
        left_col = tk.Frame(body_container, bg=BG_WINDOW)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self._build_left_column(left_col)

        # 2. 中列容器
        center_col = tk.Frame(body_container, bg=BG_WINDOW)
        center_col.grid(row=0, column=1, sticky="nsew", padx=10)
        self._build_center_column(center_col)

        # 3. 右列容器
        right_col = tk.Frame(body_container, bg=BG_WINDOW)
        right_col.grid(row=0, column=2, sticky="nsew", padx=(10, 0))
        self._build_right_column(right_col)

    # -----------------------------------------------------------------
    # PART G, H, M: 左侧列组件 (Target, Health, Notifications)
    # -----------------------------------------------------------------
    def _build_left_column(self, parent: tk.Frame):
        # 1. 目标商品卡 (PART G)
        self.target_card = CardFrame(parent)
        self.target_card.pack(fill="x", pady=(0, 12))

        t_head = tk.Frame(self.target_card, bg=BG_CARD)
        t_head.pack(fill="x", pady=(0, 8))
        tk.Label(t_head, text="🎯 目标商品", font=(self.font_family, 15, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD).pack(side="left")
        self.target_pill = StatusPill(t_head, text="目标已锁定", level="GREEN", font_family=self.font_family, font_size=10)
        self.target_pill.pack(side="right")

        self.target_name_lbl = tk.Label(self.target_card, text=self.state.target_product, font=(self.font_family, 18, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD)
        self.target_name_lbl.pack(anchor="w")

        spec_box = tk.Frame(self.target_card, bg=BG_CARD)
        spec_box.pack(fill="x", pady=(8, 4))

        tk.Label(spec_box, text="外观颜色：", font=(self.font_family, 12), fg=TEXT_SECONDARY, bg=BG_CARD).grid(row=0, column=0, sticky="w", pady=2)
        self.target_color_lbl = tk.Label(spec_box, text=self.state.target_color, font=(self.font_family, 12, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD)
        self.target_color_lbl.grid(row=0, column=1, sticky="w", pady=2)

        tk.Label(spec_box, text="存储容量：", font=(self.font_family, 12), fg=TEXT_SECONDARY, bg=BG_CARD).grid(row=1, column=0, sticky="w", pady=2)
        self.target_storage_lbl = tk.Label(spec_box, text=self.state.target_storage, font=(self.font_family, 12, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD)
        self.target_storage_lbl.grid(row=1, column=1, sticky="w", pady=2)

        tk.Label(spec_box, text="官方零件号：", font=(self.font_family, 12), fg=TEXT_SECONDARY, bg=BG_CARD).grid(row=2, column=0, sticky="w", pady=2)
        self.target_sku_lbl = tk.Label(spec_box, text=self.state.target_sku, font=(self.font_family, 12, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD)
        self.target_sku_lbl.grid(row=2, column=1, sticky="w", pady=2)

        tk.Label(spec_box, text="购买配置：", font=(self.font_family, 12), fg=TEXT_SECONDARY, bg=BG_CARD).grid(row=3, column=0, sticky="w", pady=2)
        self.target_options_lbl = tk.Label(spec_box, text="不折抵 · 不加 AppleCare", font=(self.font_family, 12, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD)
        self.target_options_lbl.grid(row=3, column=1, sticky="w", pady=2)

        tk.Label(spec_box, text="配置状态：", font=(self.font_family, 12), fg=TEXT_SECONDARY, bg=BG_CARD).grid(row=4, column=0, sticky="w", pady=2)
        self.target_options_pill = StatusPill(spec_box, text="待准备", level="ORANGE", font_family=self.font_family, font_size=10)
        self.target_options_pill.grid(row=4, column=1, sticky="w", pady=2)

        # 2. 系统健康卡 (PART H)
        self.health_card = CardFrame(parent)
        self.health_card.pack(fill="x", pady=(0, 10))

        tk.Label(self.health_card, text="🩺 系统健康检测", font=(self.font_family, 15, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD).pack(anchor="w", pady=(0, 6))

        health_items = [
            ("Apple 登录会话", "session_status", "已就绪"),
            ("官方 Catalog", "catalog_status", "已验证"),
            ("Chromium 浏览器", "browser_status", "已预热"),
            ("目标规格预选", "target_prepare_status", "已就绪"),
            ("飞书网络提醒", "feishu_status", "正常"),
            ("本地提示音", "local_alert_status", "正常"),
            ("频率保护护盾", "rate_guard_status", "已启用"),
            ("加车安全门禁", "security_gate_status", "已启用"),
        ]
        self.health_pills = {}
        h_grid = tk.Frame(self.health_card, bg=BG_CARD)
        h_grid.pack(fill="x")

        for idx, (title, key, default_val) in enumerate(health_items):
            tk.Label(h_grid, text=title, font=(self.font_family, 12), fg=TEXT_SECONDARY, bg=BG_CARD).grid(row=idx, column=0, sticky="w", pady=2)
            p = StatusPill(h_grid, text=default_val, level="GREEN", font_family=self.font_family, font_size=10)
            p.grid(row=idx, column=1, sticky="e", pady=2, padx=(10, 0))
            h_grid.grid_columnconfigure(1, weight=1)
            self.health_pills[key] = p

        # 3. 通知状态卡 (PART M)
        self.notice_card = CardFrame(parent)
        self.notice_card.pack(fill="x", pady=(0, 10))

        tk.Label(self.notice_card, text="📲 外部通知通道", font=(self.font_family, 15, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD).pack(anchor="w", pady=(0, 6))

        n_grid = tk.Frame(self.notice_card, bg=BG_CARD)
        n_grid.pack(fill="x")

        tk.Label(n_grid, text="飞书 Webhook：", font=(self.font_family, 12), fg=TEXT_SECONDARY, bg=BG_CARD).grid(row=0, column=0, sticky="w", pady=2)
        self.feishu_lbl = tk.Label(n_grid, text="已连接 (f1****4f)", font=(self.font_family, 12, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD)
        self.feishu_lbl.grid(row=0, column=1, sticky="w", pady=2)

        tk.Label(n_grid, text="最后响应 ACK：", font=(self.font_family, 12), fg=TEXT_SECONDARY, bg=BG_CARD).grid(row=1, column=0, sticky="w", pady=2)
        self.ack_lbl = tk.Label(n_grid, text="552.1 ms (非阻塞)", font=(self.font_family, 12), fg=TEXT_PRIMARY, bg=BG_CARD)
        self.ack_lbl.grid(row=1, column=1, sticky="w", pady=2)

        tk.Label(n_grid, text="本地提示音：", font=(self.font_family, 12), fg=TEXT_SECONDARY, bg=BG_CARD).grid(row=2, column=0, sticky="w", pady=2)
        self.alert_lbl = tk.Label(n_grid, text="正常 (afplay 双保险)", font=(self.font_family, 12), fg=TEXT_PRIMARY, bg=BG_CARD)
        self.alert_lbl.grid(row=2, column=1, sticky="w", pady=2)

        tk.Label(n_grid, text="后台待发送队列：", font=(self.font_family, 12), fg=TEXT_SECONDARY, bg=BG_CARD).grid(row=3, column=0, sticky="w", pady=2)
        self.pending_notice_lbl = tk.Label(n_grid, text="0 个待发送", font=(self.font_family, 12), fg=TEXT_MUTED, bg=BG_CARD)
        self.pending_notice_lbl.grid(row=3, column=1, sticky="w", pady=2)

        # 4. 响应性能面板 (PART P: Latency Performance)
        self.latency_card = CardFrame(parent)
        self.latency_card.pack(fill="x")

        lat_head = tk.Frame(self.latency_card, bg=BG_CARD)
        lat_head.pack(fill="x", pady=(0, 4))
        tk.Label(lat_head, text="⚡ 响应性能", font=(self.font_family, 14, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD).pack(side="left")
        self.latency_pill = StatusPill(lat_head, text="极佳 < 2.5s", level="GREEN", font_family=self.font_family, font_size=10)
        self.latency_pill.pack(side="right")

        lat_grid = tk.Frame(self.latency_card, bg=BG_CARD)
        lat_grid.pack(fill="x")

        tk.Label(lat_grid, text="最近触发 → 接管：", font=(self.font_family, 11), fg=TEXT_SECONDARY, bg=BG_CARD).grid(row=0, column=0, sticky="w", pady=2)
        self.lat_val_lbl = tk.Label(lat_grid, text="--", font=(self.font_family, 11, "bold"), fg=COLOR_GREEN_FG, bg=BG_CARD)
        self.lat_val_lbl.grid(row=0, column=1, sticky="w", pady=2)

        tk.Label(lat_grid, text="飞书通知耗时：", font=(self.font_family, 11), fg=TEXT_SECONDARY, bg=BG_CARD).grid(row=1, column=0, sticky="w", pady=2)
        self.feishu_ack_val_lbl = tk.Label(lat_grid, text="--", font=(self.font_family, 11), fg=TEXT_PRIMARY, bg=BG_CARD)
        self.feishu_ack_val_lbl.grid(row=1, column=1, sticky="w", pady=2)

    # -----------------------------------------------------------------
    # PART I, J, K: 中央列组件 (Hero, Stepper, Monitors)
    # -----------------------------------------------------------------
    def _build_center_column(self, parent: tk.Frame):
        # 1. 核心主状态区 (PART I)
        self.hero_card = CardFrame(parent, pady=18)
        self.hero_card.pack(fill="x", pady=(0, 12))

        self.hero_top = tk.Frame(self.hero_card, bg=BG_CARD)
        self.hero_top.pack(fill="x")

        self.hero_pill = StatusPill(self.hero_top, text="● READY", level="GREEN", font_family=self.font_family, font_size=10)
        self.hero_pill.pack(side="left", padx=(0, 8))

        self.hero_title_lbl = tk.Label(
            self.hero_top,
            text=self.state.hero_title,
            font=(self.font_family, 22, "bold"),
            fg=TEXT_PRIMARY,
            bg=BG_CARD
        )
        self.hero_title_lbl.pack(side="left")

        self.hero_sub_lbl = tk.Label(
            self.hero_card,
            text=self.state.hero_subtitle,
            font=(self.font_family, 13),
            fg=TEXT_SECONDARY,
            bg=BG_CARD,
            wraplength=480,
            justify="left"
        )
        self.hero_sub_lbl.pack(anchor="w", pady=(8, 0))

        # 会话失效紧急恢复指令栏
        self.hero_cmd_box = tk.Frame(self.hero_card, bg="#FDEBEC", highlightbackground="#C91A25", highlightthickness=1, padx=10, pady=6)
        self.hero_cmd_lbl = tk.Label(
            self.hero_cmd_box,
            text="👉 会话失效阻断。请在终端执行恢复命令：\n   python -m src.main --mode prepare-session",
            font=(self.font_family, 11, "bold"),
            fg="#C91A25",
            bg="#FDEBEC",
            justify="left"
        )
        self.hero_cmd_lbl.pack(side="left", anchor="w")
        # 默认根据当前状态决定是否显示
        if self.state.session_strict_blocked:
            self.hero_cmd_box.pack(fill="x", pady=(8, 0))

        # 2. Launch 5 级状态链 (PART J)
        self.stepper_card = CardFrame(parent, pady=10)
        self.stepper_card.pack(fill="x", pady=(0, 12))

        tk.Label(self.stepper_card, text="🚀 首发推进状态链", font=(self.font_family, 14, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD).pack(anchor="w", pady=(0, 4))
        self.stepper = HorizontalStepper(self.stepper_card, font_family=self.font_family, height=64)
        self.stepper.pack(fill="x", expand=True)

        # 3. 监控卡 (PART K: 线上购买 + 直营店自提)
        self.monitor_card = CardFrame(parent)
        self.monitor_card.pack(fill="both", expand=True)

        tk.Label(self.monitor_card, text="📊 双通道解耦监控状态", font=(self.font_family, 15, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD).pack(anchor="w", pady=(0, 10))

        m_container = tk.Frame(self.monitor_card, bg=BG_CARD)
        m_container.pack(fill="both", expand=True)
        m_container.grid_columnconfigure(0, weight=1)
        m_container.grid_columnconfigure(1, weight=1)

        # 3.1 线上购买主通道小卡
        online_subcard = tk.Frame(m_container, bg=COLOR_BLUE_BG, highlightbackground="#D0E3F8", highlightthickness=1, padx=12, pady=12)
        online_subcard.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        o_header = tk.Frame(online_subcard, bg=COLOR_BLUE_BG)
        o_header.pack(fill="x")
        tk.Label(o_header, text="🌐 线上主通道 (PRIMARY)", font=(self.font_family, 13, "bold"), fg=COLOR_BLUE_FG, bg=COLOR_BLUE_BG).pack(side="left")
        self.online_pill = StatusPill(o_header, text="即将发售", level="ORANGE", font_family=self.font_family, font_size=10)
        self.online_pill.pack(side="right")

        o_body = tk.Frame(online_subcard, bg=COLOR_BLUE_BG)
        o_body.pack(fill="x", pady=(8, 0))

        tk.Label(o_body, text="最近检查：", font=(self.font_family, 11), fg=TEXT_SECONDARY, bg=COLOR_BLUE_BG).grid(row=0, column=0, sticky="w", pady=2)
        self.online_last_lbl = tk.Label(o_body, text="--:--:--", font=(self.font_family, 11, "bold"), fg=TEXT_PRIMARY, bg=COLOR_BLUE_BG)
        self.online_last_lbl.grid(row=0, column=1, sticky="w", pady=2)

        tk.Label(o_body, text="下次检查：", font=(self.font_family, 11), fg=TEXT_SECONDARY, bg=COLOR_BLUE_BG).grid(row=1, column=0, sticky="w", pady=2)
        self.online_next_lbl = tk.Label(o_body, text="约 25 秒后", font=(self.font_family, 11), fg=TEXT_PRIMARY, bg=COLOR_BLUE_BG)
        self.online_next_lbl.grid(row=1, column=1, sticky="w", pady=2)

        tk.Label(o_body, text="轮询节奏：", font=(self.font_family, 11), fg=TEXT_SECONDARY, bg=COLOR_BLUE_BG).grid(row=2, column=0, sticky="w", pady=2)
        tk.Label(o_body, text="20~30 秒 (防频控)", font=(self.font_family, 11), fg=TEXT_PRIMARY, bg=COLOR_BLUE_BG).grid(row=2, column=1, sticky="w", pady=2)

        tk.Label(o_body, text="退避状态：", font=(self.font_family, 11), fg=TEXT_SECONDARY, bg=COLOR_BLUE_BG).grid(row=3, column=0, sticky="w", pady=2)
        self.online_backoff_lbl = tk.Label(o_body, text="正常无退避", font=(self.font_family, 11), fg=TEXT_PRIMARY, bg=COLOR_BLUE_BG)
        self.online_backoff_lbl.grid(row=3, column=1, sticky="w", pady=2)

        # 3.2 直营店自提副通道小卡
        pickup_subcard = tk.Frame(m_container, bg=COLOR_GREY_BG, highlightbackground="#E2E2E8", highlightthickness=1, padx=12, pady=12)
        pickup_subcard.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        p_header = tk.Frame(pickup_subcard, bg=COLOR_GREY_BG)
        p_header.pack(fill="x")
        tk.Label(p_header, text="🏬 直营店提醒通道 (ALERT)", font=(self.font_family, 13, "bold"), fg=TEXT_PRIMARY, bg=COLOR_GREY_BG).pack(side="left")
        self.pickup_pill = StatusPill(p_header, text="监控中", level="BLUE", font_family=self.font_family, font_size=10)
        self.pickup_pill.pack(side="right")

        p_body = tk.Frame(pickup_subcard, bg=COLOR_GREY_BG)
        p_body.pack(fill="x", pady=(8, 0))

        tk.Label(p_body, text="轮询耗时：", font=(self.font_family, 11), fg=TEXT_SECONDARY, bg=COLOR_GREY_BG).grid(row=0, column=0, sticky="w", pady=2)
        self.pickup_dur_lbl = tk.Label(p_body, text="9.35 秒 (4 店)", font=(self.font_family, 11, "bold"), fg=TEXT_PRIMARY, bg=COLOR_GREY_BG)
        self.pickup_dur_lbl.grid(row=0, column=1, sticky="w", pady=2)

        tk.Label(p_body, text="下次巡检：", font=(self.font_family, 11), fg=TEXT_SECONDARY, bg=COLOR_GREY_BG).grid(row=1, column=0, sticky="w", pady=2)
        self.pickup_next_lbl = tk.Label(p_body, text="约 30 秒后", font=(self.font_family, 11), fg=TEXT_PRIMARY, bg=COLOR_GREY_BG)
        self.pickup_next_lbl.grid(row=1, column=1, sticky="w", pady=2)

        tk.Label(p_body, text="单店节流：", font=(self.font_family, 11), fg=TEXT_SECONDARY, bg=COLOR_GREY_BG).grid(row=2, column=0, sticky="w", pady=2)
        tk.Label(p_body, text=">= 2.0 秒强制休眠", font=(self.font_family, 11), fg=TEXT_PRIMARY, bg=COLOR_GREY_BG).grid(row=2, column=1, sticky="w", pady=2)

        tk.Label(p_body, text="退避状态：", font=(self.font_family, 11), fg=TEXT_SECONDARY, bg=COLOR_GREY_BG).grid(row=3, column=0, sticky="w", pady=2)
        self.pickup_backoff_lbl = tk.Label(p_body, text="正常无退避", font=(self.font_family, 11), fg=TEXT_PRIMARY, bg=COLOR_GREY_BG)
        self.pickup_backoff_lbl.grid(row=3, column=1, sticky="w", pady=2)

    # -----------------------------------------------------------------
    # PART L, N: 右侧列组件 (Shanghai Stores, Permanent Safety)
    # -----------------------------------------------------------------
    def _build_right_column(self, parent: tk.Frame):
        # 1. 上海四店自提状态卡 (PART L)
        self.stores_card = CardFrame(parent)
        self.stores_card.pack(fill="x", pady=(0, 12))

        tk.Label(self.stores_card, text="🏬 上海直营店库存监控", font=(self.font_family, 15, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD).pack(anchor="w", pady=(0, 8))

        self.store_widgets = {}
        for s_id in ["R390", "R401", "R581", "R683"]:
            st_data = self.state.stores.get(s_id)
            s_name = st_data.name if st_data else s_id

            row_frame = tk.Frame(self.stores_card, bg="#FAFAFA", highlightbackground=BORDER_CARD, highlightthickness=1, padx=10, pady=8)
            row_frame.pack(fill="x", pady=4)

            head_line = tk.Frame(row_frame, bg="#FAFAFA")
            head_line.pack(fill="x")

            name_lbl = tk.Label(head_line, text=f"{s_name} [{s_id}]", font=(self.font_family, 12, "bold"), fg=TEXT_PRIMARY, bg="#FAFAFA")
            name_lbl.pack(side="left")

            pill = StatusPill(head_line, text="即将发售", level="ORANGE", font_family=self.font_family, font_size=10)
            pill.pack(side="right")

            bottom_line = tk.Frame(row_frame, bg="#FAFAFA")
            bottom_line.pack(fill="x", pady=(4, 0))

            quote_lbl = tk.Label(bottom_line, text="目前暂不提供 Apple Store 取货服务", font=(self.font_family, 11), fg=TEXT_MUTED, bg="#FAFAFA")
            quote_lbl.pack(side="left")

            time_lbl = tk.Label(bottom_line, text="核查: --:--:--", font=(self.font_family, 10), fg=TEXT_MUTED, bg="#FAFAFA")
            time_lbl.pack(side="right")

            self.store_widgets[s_id] = {
                "container": row_frame,
                "name_lbl": name_lbl,
                "pill": pill,
                "quote_lbl": quote_lbl,
                "time_lbl": time_lbl,
                "bottom_line": bottom_line,
            }

        # 2. 首发 10 项核验清单卡 (PART Q: 10-Item Ready Checklist)
        self.checklist_card = CardFrame(parent)
        self.checklist_card.pack(fill="x", pady=(0, 10))

        c_head = tk.Frame(self.checklist_card, bg=BG_CARD)
        c_head.pack(fill="x", pady=(0, 6))
        tk.Label(c_head, text="📋 首发 10 项核验清单", font=(self.font_family, 14, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD).pack(side="left")
        self.checklist_summary_pill = StatusPill(c_head, text="待核验 (0/10)", level="GREY", font_family=self.font_family, font_size=10)
        self.checklist_summary_pill.pack(side="right")

        c_grid = tk.Frame(self.checklist_card, bg=BG_CARD)
        c_grid.pack(fill="x")
        c_grid.grid_columnconfigure(0, weight=1)
        c_grid.grid_columnconfigure(1, weight=1)

        CHECKLIST_DEF = [
            ("browser_prewarmed", "1. 浏览器预热", 0, 0),
            ("session_verified", "2. Apple 登录会话", 1, 0),
            ("catalog_locked", "3. 官方目录锁定", 2, 0),
            ("target_spec_prepared", "4. 目标规格预选", 3, 0),
            ("purchase_options_prepared", "5. 购买选项预选", 4, 0),
            ("selection_preservation_verified", "6. 规格保持校验", 0, 1),
            ("online_monitor_standby", "7. 线上通道待命", 1, 1),
            ("pickup_monitor_standby", "8. 自提通道待命", 2, 1),
            ("feishu_async_ready", "9. 飞书异步通道", 3, 1),
            ("local_alert_active", "10. 本地蜂鸣警报", 4, 1),
        ]

        self.checklist_widgets = {}
        for key, name, r, c in CHECKLIST_DEF:
            row_box = tk.Frame(c_grid, bg=BG_CARD)
            row_box.grid(row=r, column=c, sticky="ew", padx=3, pady=1)
            lbl = tk.Label(row_box, text=name, font=(self.font_family, 10), fg=TEXT_SECONDARY, bg=BG_CARD)
            lbl.pack(side="left")
            pill = StatusPill(row_box, text="○ 待检", level="GREY", font_family=self.font_family, font_size=9)
            pill.pack(side="right")
            self.checklist_widgets[key] = {"lbl": lbl, "pill": pill}

        # 3. 永久安全隔离区 (PART N)
        self.safety_card = CardFrame(parent, border_color="#D1D1D6")
        self.safety_card.pack(fill="both", expand=True)

        self.safety_head = tk.Frame(self.safety_card, bg=BG_CARD)
        self.safety_head.pack(fill="x", pady=(0, 6))

        self.safety_title_lbl = tk.Label(
            self.safety_head,
            text="🔒 资金与安全模式",
            font=(self.font_family, 14, "bold"),
            fg=TEXT_PRIMARY,
            bg=BG_CARD
        )
        self.safety_title_lbl.pack(side="left")

        self.safety_pill = StatusPill(self.safety_head, text="已隔离保护", level="GREEN", font_family=self.font_family, font_size=10)
        self.safety_pill.pack(side="right")

        safety_text = (
            "自动化流程严守安全底线，绝对不触碰：\n"
            "• Apple ID 账户密码\n"
            "• 双重认证 (2FA) 验证码\n"
            "• 复杂图形人机验证 (CAPTCHA)\n"
            "• 支付方式与扣款网关\n"
            "• 不可逆最终订单提交\n\n"
            "💡 当需要人工操作时，系统会自动停止自动化、鸣响报警并将浏览器置顶前台。"
        )
        self.safety_desc_lbl = tk.Label(
            self.safety_card,
            text=safety_text,
            font=(self.font_family, 11),
            fg=TEXT_SECONDARY,
            bg=BG_CARD,
            justify="left",
            wraplength=300
        )
        self.safety_desc_lbl.pack(anchor="w", pady=(4, 0))

    # -----------------------------------------------------------------
    # PART O: 底部事件流 (Recent Events)
    # -----------------------------------------------------------------
    def _build_bottom_events(self):
        bottom_card = CardFrame(self.root, padx=16, pady=10)
        bottom_card.pack(side="bottom", fill="x", padx=20, pady=(0, 16))

        head_line = tk.Frame(bottom_card, bg=BG_CARD)
        head_line.pack(fill="x", pady=(0, 6))

        tk.Label(head_line, text="📋 最近事件与日志流 (实时摘要)", font=(self.font_family, 14, "bold"), fg=TEXT_PRIMARY, bg=BG_CARD).pack(side="left")
        tk.Label(head_line, text="最多展示 20 条 · 详细日志请见 logs/buyer.log", font=(self.font_family, 11), fg=TEXT_MUTED, bg=BG_CARD).pack(side="right")

        # 采用 Treeview 清爽展示
        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "Events.Treeview",
            background=BG_CARD,
            foreground=TEXT_PRIMARY,
            rowheight=24,
            fieldbackground=BG_CARD,
            font=(self.font_family, 11)
        )
        style.configure(
            "Events.Treeview.Heading",
            background="#F2F2F7",
            foreground=TEXT_SECONDARY,
            font=(self.font_family, 11, "bold")
        )
        style.map("Events.Treeview", background=[("selected", COLOR_BLUE_BG)], foreground=[("selected", COLOR_BLUE_FG)])

        tree_frame = tk.Frame(bottom_card, bg=BG_CARD)
        tree_frame.pack(fill="x", expand=True)

        columns = ("time", "level", "message")
        self.event_tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            height=4,
            style="Events.Treeview"
        )
        self.event_tree.heading("time", text="时间")
        self.event_tree.heading("level", text="级别")
        self.event_tree.heading("message", text="中文事件详情")

        self.event_tree.column("time", width=90, minwidth=80, anchor="center")
        self.event_tree.column("level", width=80, minwidth=70, anchor="center")
        self.event_tree.column("message", width=1100, minwidth=600, anchor="w")

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.event_tree.yview)
        self.event_tree.configure(yscrollcommand=scrollbar.set)

        self.event_tree.pack(side="left", fill="x", expand=True)
        scrollbar.pack(side="right", fill="y")

    # -----------------------------------------------------------------
    # 定时器与状态刷新引擎
    # -----------------------------------------------------------------
    def _tick_clock(self):
        """每秒更新时间戳与开售倒计时"""
        self.state.update_current_time()
        self.clock_label.config(text=self.state.current_time_str)
        self.date_label.config(text=self.state.current_date_str)
        c_text, c_lvl = self.state.get_countdown_pill_info()
        self.countdown_pill.set_level(c_lvl, c_text)
        if hasattr(self, "advice_pill"):
            adv = self.state.launch_advice_str
            if adv:
                a_lvl = "GREEN" if "线上可购" in adv else ("ORANGE" if ("准备阶段" in adv or "等待 Apple" in adv or "检查" in adv) else "BLUE")
                self.advice_pill.set_level(a_lvl, adv)
                if not self.advice_pill.winfo_ismapped():
                    self.advice_pill.pack(side="left", padx=(6, 0))
            else:
                if self.advice_pill.winfo_ismapped():
                    self.advice_pill.pack_forget()
        self.root.after(1000, self._tick_clock)

    def _poll_adapter_queue(self):
        """排空适配器中的状态变更并更新界面"""
        if self.adapter:
            count = self.adapter.drain_to_state(self.state)
            if count > 0:
                self.apply_state_to_widgets()
        self.root.after(100, self._poll_adapter_queue)

    def apply_state_to_widgets(self):
        """将 self.state 中的最新属性映射并渲染到 Tkinter 界面组件上"""
        st = self.state

        # 0. 顶部 Header 动态更新
        b_text, b_level = st.get_mode_badge_info()
        self.mode_badge.set_level(b_level, b_text)
        c_text, c_lvl = st.get_countdown_pill_info()
        self.countdown_pill.set_level(c_lvl, c_text)
        if hasattr(self, "advice_pill"):
            adv = st.launch_advice_str
            if adv:
                a_lvl = "GREEN" if "线上可购" in adv else ("ORANGE" if ("准备阶段" in adv or "等待 Apple" in adv or "检查" in adv) else "BLUE")
                self.advice_pill.set_level(a_lvl, adv)
                if not self.advice_pill.winfo_ismapped():
                    self.advice_pill.pack(side="left", padx=(6, 0))
            else:
                if self.advice_pill.winfo_ismapped():
                    self.advice_pill.pack_forget()
        p_text, p_lvl, p_time = st.get_session_pill_info()
        self.header_session_pill.set_level(p_lvl, p_text)
        self.header_session_time_lbl.config(text=p_time)

        # 1. 目标卡片
        self.target_name_lbl.config(text=st.target_product)
        self.target_color_lbl.config(text=st.target_color)
        self.target_storage_lbl.config(text=st.target_storage)
        self.target_sku_lbl.config(text=st.target_sku)
        if st.target_mismatch:
            self.target_pill.set_level("RED", "目标异常")
        elif st.target_locked:
            self.target_pill.set_level("GREEN", "目标已锁定")
        else:
            self.target_pill.set_level("GREY", "未锁定")

        opt_status = "已预选" if st.purchase_options_prepared else "待准备"
        opt_level = "GREEN" if st.purchase_options_prepared else "ORANGE"
        self.target_options_pill.set_level(opt_level, opt_status)

        # 2. 系统健康
        for key, pill in self.health_pills.items():
            val = getattr(st, key, "未知")
            pill.set_level(val, val)

        # 3. 通知状态
        self.feishu_lbl.config(text=f"{st.feishu_status} ({st.feishu_masked_token})")
        if st.feishu_last_ack_ms is not None:
            self.ack_lbl.config(text=f"{st.feishu_last_ack_ms:.1f} ms (非阻塞)")
        self.alert_lbl.config(text=f"{st.local_alert_status} (afplay 双保险)")
        self.pending_notice_lbl.config(text=f"{st.pending_notifications} 个待发送")

        # 3.5 响应性能指标
        if st.latency_trigger_to_handoff_sec is not None:
            lat_sec = st.latency_trigger_to_handoff_sec
            self.lat_val_lbl.config(text=f"{lat_sec:.2f} 秒")
            if lat_sec <= 2.5:
                self.latency_pill.set_level("GREEN", f"极佳 {lat_sec:.2f}s")
            elif lat_sec <= 4.0:
                self.latency_pill.set_level("GREEN", f"良好 {lat_sec:.2f}s")
            else:
                self.latency_pill.set_level("ORANGE", f"一般 {lat_sec:.2f}s")
        else:
            self.lat_val_lbl.config(text="-- (待触发)")
            self.latency_pill.set_level("GREY", "待触发")

        if st.feishu_last_ack_ms is not None:
            self.feishu_ack_val_lbl.config(text=f"{st.feishu_last_ack_ms:.1f} ms")
        else:
            self.feishu_ack_val_lbl.config(text="--")

        # 4. 中央大状态
        lvl = st.hero_level.upper()
        if lvl in ("READY", "ONLINE_AVAILABLE", "AVAILABLE"):
            h_bg = COLOR_GREEN_BG
            h_border = "#C7EED4" if lvl == "READY" else "#34C759"
        elif lvl in ("MONITORING", "ACTIVE", "INFO"):
            h_bg = COLOR_BLUE_BG
            h_border = "#C5DFF9"
        elif lvl in ("HANDOFF", "WARNING"):
            h_bg = "#FFF4E5"
            h_border = "#FF9500"
        elif lvl in ("ERROR", "CRITICAL"):
            h_bg = COLOR_RED_BG
            h_border = COLOR_RED_FG
        else:
            h_bg = BG_CARD
            h_border = BORDER_CARD

        self.hero_card.config(bg=h_bg, highlightbackground=h_border)
        self.hero_top.config(bg=h_bg)
        self.hero_title_lbl.config(text=st.hero_title, bg=h_bg)
        self.hero_sub_lbl.config(text=st.hero_subtitle, bg=h_bg)
        self.hero_pill.set_level(st.hero_level, f"● {st.hero_level}")

        if st.session_strict_blocked:
            self.hero_cmd_box.pack(fill="x", pady=(8, 0))
            fail_hint = f" ({st.session_fail_reason})" if st.session_fail_reason else ""
            self.hero_cmd_lbl.config(
                text=f"👉 会话失效阻断{fail_hint}。请在终端执行恢复命令：\n   python -m src.main --mode prepare-session",
                justify="left"
            )
        else:
            self.hero_cmd_box.pack_forget()

        # 4.5 首发 10 项核验清单
        passed, total, all_ok = st.get_checklist_status()
        is_ready = st.is_ready_for_launch()
        if all_ok and is_ready:
            self.checklist_summary_pill.set_level("GREEN", f"首发准备完成 ({passed}/{total})")
        elif st.session_strict_blocked:
            self.checklist_summary_pill.set_level("RED", f"流程已阻断 ({passed}/{total})")
        else:
            self.checklist_summary_pill.set_level("ORANGE" if passed > 0 else "GREY", f"首发暂不可启动 ({passed}/{total})")

        for key, w in self.checklist_widgets.items():
            is_pass = st.checklist.get(key, False)
            if is_pass:
                w["pill"].set_level("GREEN", "✓ 就绪")
                w["lbl"].config(fg=TEXT_PRIMARY)
            else:
                w["pill"].set_level("GREY", "○ 待检")
                w["lbl"].config(fg=TEXT_SECONDARY)

        # 5. 5 级状态链
        self.stepper.update_stages(
            session=st.pipeline_session,
            prepare=st.pipeline_prepare,
            monitor=st.pipeline_monitor,
            purchase=st.pipeline_purchase,
            handoff=st.pipeline_handoff,
        )

        # 6. 线上通道
        self.online_pill.set_level(st.online_state_token, st.online_state_cn)
        self.online_last_lbl.config(text=st.online_last_check)
        self.online_next_lbl.config(text=st.online_next_check)
        if st.online_backoff_sec > 0:
            self.online_backoff_lbl.config(text=f"退避中 ({st.online_backoff_sec:.0f}s)", fg=COLOR_RED_FG)
        else:
            self.online_backoff_lbl.config(text="正常无退避", fg=TEXT_PRIMARY)

        # 7. 自提通道
        self.pickup_pill.set_level(st.pickup_state_token, st.pickup_state_cn)
        self.pickup_dur_lbl.config(text=st.pickup_round_duration)
        self.pickup_next_lbl.config(text=st.pickup_next_check)
        if st.pickup_backoff_sec > 0:
            self.pickup_backoff_lbl.config(text=f"退避中 ({st.pickup_backoff_sec:.0f}s)", fg=COLOR_RED_FG)
        else:
            self.pickup_backoff_lbl.config(text="正常无退避", fg=TEXT_PRIMARY)

        # 8. 上海四店
        for s_id, w in self.store_widgets.items():
            st_data = st.stores.get(s_id)
            if not st_data:
                continue
            w["pill"].set_level(st_data.status, st_data.status_cn)
            w["quote_lbl"].config(text=st_data.pickup_quote)
            w["time_lbl"].config(text=f"核查: {st_data.updated_at}")
            if st_data.is_available:
                w["container"].config(bg=COLOR_GREEN_BG, highlightbackground="#34C759", highlightthickness=2)
                w["name_lbl"].config(bg=COLOR_GREEN_BG, fg=COLOR_GREEN_FG)
                w["quote_lbl"].config(bg=COLOR_GREEN_BG, fg=COLOR_GREEN_FG)
                w["time_lbl"].config(bg=COLOR_GREEN_BG, fg=COLOR_GREEN_FG)
                w["bottom_line"].config(bg=COLOR_GREEN_BG)
            else:
                w["container"].config(bg="#FAFAFA", highlightbackground=BORDER_CARD, highlightthickness=1)
                w["name_lbl"].config(bg="#FAFAFA", fg=TEXT_PRIMARY)
                w["quote_lbl"].config(bg="#FAFAFA", fg=TEXT_MUTED)
                w["time_lbl"].config(bg="#FAFAFA", fg=TEXT_MUTED)
                w["bottom_line"].config(bg="#FAFAFA")

        # 9. 安全模式 / 人工接管卡片
        s_pill_text, s_pill_lvl, s_title, s_desc = st.get_safety_info()
        is_alert = (s_pill_lvl == "RED")
        s_bg = COLOR_RED_BG if is_alert else BG_CARD
        s_fg = COLOR_RED_FG if is_alert else TEXT_PRIMARY
        s_border = COLOR_RED_FG if is_alert else BORDER_CARD

        self.safety_card.config(bg=s_bg, highlightbackground=s_border, highlightthickness=2 if is_alert else 1)
        self.safety_head.config(bg=s_bg)
        self.safety_title_lbl.config(text=s_title, fg=s_fg, bg=s_bg)
        self.safety_pill.set_level(s_pill_lvl, s_pill_text)
        self.safety_desc_lbl.config(text=s_desc, fg=s_fg if is_alert else TEXT_SECONDARY, bg=s_bg)

        # 焦点协调策略：当需要人工接管时，Dashboard 坚决不争抢焦点，解除任何置顶
        if st.human_action_required:
            try:
                self.root.attributes("-topmost", False)
            except Exception:
                pass

        # 10. 最近事件流 (只刷新有新增或更新的内容)
        current_rows = self.event_tree.get_children()
        new_events = st.recent_events
        # 简单比对刷新
        if len(current_rows) != len(new_events):
            for row in current_rows:
                self.event_tree.delete(row)
            for ev in new_events:
                self.event_tree.insert("", "end", values=(ev.timestamp, ev.level_cn, ev.message))
            # 滚动到最底部
            if self.event_tree.get_children():
                self.event_tree.yview_moveto(1.0)
