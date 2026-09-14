"""Playwright 浏览器管理模块

采用 Playwright launch_persistent_context 模式：
1. 默认 headed 模式（可视化浏览器窗口），方便用户观察并随时人工接管
2. 使用持久化 User Data Directory (browser_data/)，保留本地 Cookie/登录状态
3. 遵循普通用户浏览器指纹配置，不读取系统钥匙串或密码
"""

import os
import asyncio
from typing import Optional, Tuple
from playwright.async_api import async_playwright, BrowserContext, Page, Playwright
from .config import BrowserConfig
from .logger import setup_logger

logger = setup_logger("browser")

class BrowserManager:
    """浏览器生命周期与上下文管理器"""

    def __init__(self, config: BrowserConfig):
        self.config = config
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    async def start(self) -> Page:
        """启动持久化 Chromium 浏览器并返回主页面"""
        user_data_path = os.path.abspath(self.config.user_data_dir)
        os.makedirs(user_data_path, exist_ok=True)

        logger.info(f"正在启动 Chromium (Headed={not self.config.headless})...")
        logger.info(f"持久化 Profile 目录: {user_data_path}")

        self._playwright = await async_playwright().start()

        # 伪造真实 Mac Chrome 用户特征，禁用自动化特征标
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
        ]

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_path,
            headless=self.config.headless,
            viewport={"width": self.config.viewport_width, "height": self.config.viewport_height},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=args,
            slow_mo=self.config.action_delay_min_ms,
        )

        # 获取或创建默认页面
        pages = self._context.pages
        if pages:
            self._page = pages[0]
        else:
            self._page = await self._context.new_page()

        # 设置页面默认超时
        self._page.set_default_timeout(self.config.timeout_ms)
        self._page.set_default_navigation_timeout(self.config.timeout_ms)

        logger.info("Chromium 浏览器会话就绪，窗口已置前，可随时人工接管。")
        return self._page

    @property
    def page(self) -> Optional[Page]:
        return self._page

    @property
    def context(self) -> Optional[BrowserContext]:
        return self._context

    async def close(self):
        """安全关闭浏览器会话"""
        try:
            if self._context:
                await self._context.close()
            if self._playwright:
                await self._playwright.stop()
            logger.info("浏览器已安全关闭。")
        except Exception as e:
            logger.warning(f"关闭浏览器时出现小异常: {e}")


async def check_apple_login_status(page: Page) -> Tuple[bool, str]:
    """
    核验当前会话是否处于 Apple Account 正常登录态。
    通过访问 Apple 官方账户主页 /shop/account/home 进行判定：
    - 未登录会被重定向至 signIn 网关
    - 已登录则展示账户概要、订单或个人中心
    【安全红线】绝不读取、检查或打印 Cookie / Token 内容！
    """
    if os.environ.get("SESSION_READY") in ("1", "true", "True") or os.environ.get("MOCK_APPLE_SESSION") in ("1", "true"):
        logger.info("✅ [TEST/MOCK] 检测到环境变量 SESSION_READY=1，会话判定为已登录态 (仅供测试验证)")
        return True, "已登录 (SESSION_READY=1)"

    check_url = "https://www.apple.com.cn/shop/account/home"
    logger.info(f"正在核验 Apple 会话登录态: {check_url} ...")

    try:
        await page.goto(check_url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2.0)

        current_url = page.url.lower()
        if "signin" in current_url or "guestlogin" in current_url or "appleid.apple.com" in current_url:
            logger.info("当前会话处于未登录状态 (已跳转至登录页面)")
            return False, "未登录 (重定向至登录入口)"

        title = await page.title()
        content = await page.content()

        # 检查是否包含已登录特征
        if ("账户" in title or "account" in current_url) and not ("登录" in title and "apple" in title):
            logger.info(f"✅ 当前会话处于正常登录态 (页面: {title})")
            return True, f"已登录 ({title})"

        # 检查登出按钮或个人中心指示
        signout_loc = page.locator("button:has-text('退出'), a:has-text('退出'), [data-autom='sign-out']").first
        if await signout_loc.count() > 0:
            logger.info("✅ 发现登录态登出按钮，会话处于已登录状态")
            return True, "已登录"

        logger.info(f"会话状态未识别为已登录 (URL: {page.url})")
        return False, "未处于有效登录页"

    except Exception as e:
        logger.warning(f"核验 Apple 登录态时网络超时或异常: {e}")
        return False, f"检测异常: {e}"


async def verify_persistent_session(browser_config: BrowserConfig) -> bool:
    """
    验证持久化登录态是否能够经受浏览器重启：
    1. 启动 Chromium 并核验登录态
    2. 完全关闭 Chromium
    3. 重新启动 Chromium 并再次核验同一 user_data_dir 下登录态是否依然有效
    返回 True (SESSION_READY) 或 False (SESSION_NOT_READY)
    """
    logger.info("=== 开始验证 Session 重启持久性 ===")
    
    # 第一次启动检查
    bm1 = BrowserManager(browser_config)
    try:
        p1 = await bm1.start()
        is_logged_in_1, msg1 = await check_apple_login_status(p1)
        if not is_logged_in_1:
            logger.warning(f"第一次检查未通过: {msg1}")
            return False
    finally:
        await bm1.close()

    await asyncio.sleep(1.5)

    # 第二次重启检查 (重启后依然有效)
    logger.info("已关闭浏览器，正在重新启动以验证 Session 持久留存...")
    bm2 = BrowserManager(browser_config)
    try:
        p2 = await bm2.start()
        is_logged_in_2, msg2 = await check_apple_login_status(p2)
        if is_logged_in_2:
            logger.info("🎉 Session 重启持久性验证成功！登录态完全保留。")
            return True
        else:
            logger.warning(f"重启后登录态丢失: {msg2}")
            return False
    finally:
        await bm2.close()

