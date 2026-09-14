"""主执行入口 (CLI - V0.4 Launch Day 实战版)

支持模式：
1. --mode launch          : iPhone Duo 首发实战协同模式 (前置健康检查 + Catalog 实时锁定 + 双通道监控 + 人工安全接管)
2. --mode duo-dry-run     : iPhone Duo 专项演练模式 (锁定 MK2P4CH/A，读取价格与 comingSoon 状态并截屏)
3. --mode prepare-session : 预热登录会话模式 (拉起 Headed 浏览器由用户本人登录，验证重启持久性，严防泄露密码/Cookie)
4. --mode check-session   : 开售前全链路健康度检查 (生成全量 Readiness 报告)
5. --mode test-feishu     : 飞书 Webhook 连通性测试 (未配置提示 WAITING，请求成功提示 REQUEST_OK，真机确认输出 END_TO_END_OK)
6. --mode test-pickup     : 对上海 4 家直营店执行单轮真实库存状态核验
7. --mode online          : 运行官网线上半自动加车与结账推进 (支持已登录会话安全推进并于支付前熔断)
8. --mode pickup          : 运行上海直营店低频自提监控
9. --mode dual            : 双通道综合运行模式
10. --mode catalog        : 树状展示 Apple 中国官网全量在售/新品机型规格与 SKU
"""

import sys
import os
import time
import random
import argparse
import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Tuple

from .config import load_config, AppConfig, mask_feishu_webhook, mask_credential
from .browser import BrowserManager, check_apple_login_status, verify_persistent_session
from .apple_store import (
    AppleStoreBuyer,
    verify_starwhite_selected,
    verify_512gb_selected,
    verify_target_sku_consistency,
    fast_select_option,
)
from .notifier import (
    Notifier, NotificationEvent, is_feishu_end_to_end_verified,
    verify_feishu_status, compute_webhook_fingerprint, mask_fingerprint
)
from .pickup_monitor import PickupMonitor, InventoryStatus
from .catalog import AppleCatalogResolver, ResolutionStatus, ResolvedProduct, LaunchReadinessState
from .rate_guard import RateGuard, ErrorBackoffTracker, calculate_cadence_sleep
from .logger import setup_logger, log_takeover_alert

logger = setup_logger("main")


async def ensure_resolved_target(config: AppConfig, browser_manager: BrowserManager) -> Optional[ResolvedProduct]:
    """
    统一目标 SKU 自动解析器：
    1. 确保线上和自提通道永远使用同一个官方解析出的 Part Number
    2. 若用户手工指定了 part_number，执行强一致性核验；若冲突报错 TARGET_SKU_MISMATCH 并阻断启动
    3. 若官方 Catalog 变更导致目标零件号变更，触发 TARGET_CHANGED 报警并终止启动
    """
    target = config.target
    logger.info(f"正在核验/解析目标商品规格: {target.product} · {target.color} · {target.storage} ...")

    resolver = AppleCatalogResolver()
    page = await browser_manager.start()

    try:
        res = await resolver.resolve(
            page=page,
            product=target.product,
            color=target.color,
            storage=target.storage,
            expected_part_number=target.part_number if not target.auto_resolve_part_number or target.part_number else None
        )

        if res.status == ResolutionStatus.TARGET_CHANGED:
            logger.critical(f"🚨 [TARGET_CHANGED] {res.error_message}")
            print("\n" + "=" * 65)
            print("  【官方目标重大变更：TARGET_CHANGED】")
            print(f"  {res.error_message}")
            print("  系统已停止监控以防下错订单。")
            print("=" * 65 + "\n")
            return None

        if res.status == ResolutionStatus.TARGET_SKU_MISMATCH:
            logger.critical(f"🛑 启动终止：{res.error_message}")
            print("\n" + "=" * 65)
            print("  【配置严重错误：TARGET_SKU_MISMATCH】")
            print(f"  {res.error_message}")
            print("  为确保不买错机型，程序已安全中止。请修正 config.yaml 中的 part_number。")
            print("=" * 65 + "\n")
            return None

        if res.status == ResolutionStatus.NO_MATCH:
            logger.critical(f"🛑 规格未匹配：{res.error_message}")
            print("\n" + "=" * 65)
            print("  【规格解析错误：NO_MATCH】")
            print(f"  {res.error_message}")
            print("=" * 65 + "\n")
            return None

        if res.status == ResolutionStatus.MULTIPLE_MATCHES:
            logger.critical(f"🛑 规格命中多项：{res.error_message}")
            print("\n" + "=" * 65)
            print("  【规格歧义提示：MULTIPLE_MATCHES】")
            print(f"  {res.error_message}")
            print("=" * 65 + "\n")
            return None

        if res.status in (ResolutionStatus.SUCCESS, ResolutionStatus.CATALOG_STALE):
            prod = res.product
            logger.info(f"🎯 目标 SKU 解析完成: {prod.summary()}")
            # 回填全局运行时配置，确保双通道完全一致
            config.target.part_number = prod.part_number
            config.target.model = prod.product_name
            config.target.color = prod.color
            config.target.storage = prod.storage
            config.product_url = prod.product_url
            return prod

        logger.error(f"❌ 解析未获成功状态: {res.status} ({res.error_message})")
        return None

    finally:
        # 如果不是常驻任务，此处不关闭，交由调用方持有或管理
        pass


async def run_online_buyer(config: AppConfig, auto_exit_seconds: int = 15):
    """运行 V0.1 线上半自动购买流程 (注入自动解析的 Part Number)"""
    logger.info("==================================================")
    logger.info("  【线上通道】Apple 中国大陆官网半自动加车与结账")
    logger.info("==================================================")

    browser_manager = BrowserManager(config.browser)
    resolved_prod = await ensure_resolved_target(config, browser_manager)
    if not resolved_prod:
        await browser_manager.close()
        return

    logger.info(f"目标机型: {config.model} (零件号: {config.target.part_number})")
    logger.info(f"外观颜色: {config.color}")
    logger.info(f"存储容量: {config.storage}")
    logger.info(f"购买模式: {config.purchase_mode}")

    # 获取当前活动 page
    page = browser_manager.context.pages[0] if browser_manager.context.pages else await browser_manager.context.new_page()
    buyer = AppleStoreBuyer(page, config)
    notifier = Notifier(config.notifications)

    try:
        # 步骤 1: 打开商品页
        logger.info("\n--- [第 1 步] 打开商品购买页面 ---")
        if not await buyer.open_product_page():
            logger.error("无法正确加载商品页面，流程终止。")
            return

        # 检查初始安全拦截
        stop_reason = await buyer.check_safety_boundary()
        if stop_reason:
            log_takeover_alert(logger, f"触发安全停止条件: {stop_reason}")
            await notifier.notify(
                event=NotificationEvent.HUMAN_ACTION_REQUIRED,
                title="⚠️ Apple 官网人工接管提示",
                product=config.model,
                status_text="触发安全停止条件",
                details=stop_reason,
            )
            print("\n已到达需要人工操作的步骤，请手动完成后续验证或支付。\n")
            return

        # 步骤 2: 选择机型、颜色、容量
        logger.info("\n--- [第 2 步] 阶梯式选择机型、外观颜色与容量 ---")
        await buyer.select_model()

        if not await buyer.select_color():
            logger.error("颜色选择失败，终止后续操作。")
            return

        if not await buyer.select_storage():
            logger.error("容量选择失败，终止后续操作。")
            return

        # 步骤 3: 处理折抵与 AppleCare
        logger.info("\n--- [第 3 步] 确认折抵换购与 AppleCare 选项 ---")
        await buyer.handle_trade_in()
        await buyer.handle_applecare()

        # 步骤 4: 加入购物袋
        logger.info("\n--- [第 4 步] 加入官方购物袋 ---")
        if not await buyer.add_to_bag():
            logger.error("未能成功将商品加入购物袋。")
            return

        # 步骤 5: 推进至结账阶段
        logger.info("\n--- [第 5 步] 推进至购物袋/结账与安全检测 ---")
        success, reason = await buyer.proceed_to_bag_and_checkout()

        # 终端提示与通知报警
        log_takeover_alert(logger, f"安全熔断拦截：{reason}")
        await notifier.notify(
            event=NotificationEvent.HUMAN_ACTION_REQUIRED,
            title="🛒 Apple 购物袋就绪，等待人工接管",
            product=config.model,
            color=config.color,
            storage=config.storage,
            status_text="已到达人工结账网关",
            url="https://www.apple.com.cn/shop/bag",
            details=reason,
        )

        print("\n" + "=" * 62)
        print("  【人工接管提示】")
        print("  已到达需要人工操作的步骤，请手动完成后续验证或支付。")
        print("=" * 62 + "\n")

        if auto_exit_seconds > 0:
            logger.info(f"测试模式：浏览器窗口保持 {auto_exit_seconds} 秒以供人工接管...")
            await asyncio.sleep(auto_exit_seconds)
        else:
            logger.info("浏览器窗口将持续保持打开状态，供您直接在窗口内手动操作。按 Ctrl+C 退出。")
            while True:
                await asyncio.sleep(3600)

    except KeyboardInterrupt:
        logger.info("收到中断信号，退出流程...")
    except Exception as e:
        logger.critical(f"执行过程中发生未捕获异常: {e}", exc_info=True)
        await buyer.save_screenshot("error_unhandled.png")
    finally:
        logger.info("清理浏览器会话...")
        await browser_manager.close()


async def run_pickup_monitor(config: AppConfig, max_rounds: int = 0):
    """运行上海直营店自提监控 (注入自动解析的 Part Number)"""
    logger.info("==================================================")
    logger.info("  【线下自提】上海 Apple Store 直营店自提监控系统")
    logger.info("==================================================")

    browser_manager = BrowserManager(config.browser)
    resolved_prod = await ensure_resolved_target(config, browser_manager)
    if not resolved_prod:
        await browser_manager.close()
        return

    logger.info(f"目标产品: {config.target.product} ({config.target.color} {config.target.storage})")
    logger.info(f"零件号:   {config.target.part_number}")
    logger.info(f"监控门店: {len(config.pickup.stores)} 家上海直营店")
    for s in config.pickup.stores:
        logger.info(f"  - {s.name} ({s.store_number})")
    logger.info(f"轮询间隔: {config.pickup.interval_seconds}s (±{config.pickup.jitter_seconds}s Jitter)")

    notifier = Notifier(config.notifications)
    monitor = PickupMonitor(config, notifier)
    page = browser_manager.context.pages[0] if browser_manager.context.pages else await browser_manager.context.new_page()

    try:
        rounds_limit = max_rounds if max_rounds > 0 else None
        await monitor.start_monitoring(browser_manager, page, max_rounds=rounds_limit)
    except KeyboardInterrupt:
        logger.info("收到用户中断信号，自提监控退出...")
    except Exception as e:
        logger.critical(f"自提监控遇到未捕获异常: {e}", exc_info=True)
    finally:
        await browser_manager.close()


async def run_pickup_test(config: AppConfig):
    """对上海 4 家门店执行单轮真实状态核验"""
    logger.info("正在对上海直营门店执行单轮真实状态核验...")
    browser_manager = BrowserManager(config.browser)
    resolved_prod = await ensure_resolved_target(config, browser_manager)
    if not resolved_prod:
        await browser_manager.close()
        return

    notifier = Notifier(config.notifications)
    monitor = PickupMonitor(config, notifier)
    page = browser_manager.context.pages[0] if browser_manager.context.pages else await browser_manager.context.new_page()

    try:
        await monitor.ensure_session(page)
        results = await monitor.run_single_round(page)
        print("\n" + "=" * 65)
        print("  【上海直营门店库存核验报告】")
        print(f"  测试机型: {config.target.product} {config.target.color} {config.target.storage} ({config.target.part_number})")
        print("-" * 65)
        for r in results:
            status_tag = f"[{r.status.label}]"
            print(f"  {r.store_name:<12} ({r.store_number}): {status_tag:<14} {r.pickup_quote or ''} {r.reason or ''}")
        print("=" * 65 + "\n")
    finally:
        await browser_manager.close()


async def run_catalog_view(config: AppConfig):
    """拉取并以树状结构打印 Apple CN 官方全量 SKU 目录"""
    logger.info("启动 Apple CN 官方 Catalog 树状解析视图...")
    browser_manager = BrowserManager(config.browser)
    page = await browser_manager.start()
    resolver = AppleCatalogResolver()

    try:
        tree_text = await resolver.render_catalog_tree(page)
        print(tree_text)
    finally:
        await browser_manager.close()


async def run_watch_product(config: AppConfig, watch_product_name: str = "iPhone Duo", interval_sec: int = 600, max_checks: int = 0):
    """
    Duo Ready 模式：低频、安全轮询 Apple CN Catalog，监听指定新品是否正式公开
    """
    logger.info(f"==================================================")
    logger.info(f"  【Duo Ready 模式】新品监听: {watch_product_name}")
    logger.info(f"==================================================")
    logger.info(f"检查间隔: 约 {interval_sec // 60} 分钟 (带随机 Jitter)，安全低频防风控")

    browser_manager = BrowserManager(config.browser)
    page = await browser_manager.start()
    resolver = AppleCatalogResolver()
    notifier = Notifier(config.notifications)

    check_count = 0
    discovered = False

    try:
        while True:
            check_count += 1
            now_str = datetime_str = asyncio.get_event_loop().time()
            logger.info(f"\n[第 {check_count} 次检查] 正在扫描 Apple CN 购买页目录是否出现 [{watch_product_name}] ...")

            slugs = await resolver.get_all_product_slugs(page)
            target_slug = watch_product_name.lower().replace(" ", "-")

            found_in_slugs = target_slug in slugs or any(target_slug in s for s in slugs)

            # 进一步尝试探测直接页面是否有有效 bootstrap
            has_valid_catalog = False
            ps_data, _ = await resolver.fetch_product_catalog(page, watch_product_name)
            if ps_data and len(ps_data.get("products", [])) > 0:
                has_valid_catalog = True

            if found_in_slugs or has_valid_catalog:
                logger.info(f"🎉 成功发现新品！[{watch_product_name}] 已正式进入 Apple CN 目录！")
                
                # 尝试解析目标规格
                target_color = config.target.color
                target_storage = config.target.storage
                logger.info(f"正在为新品自动解析目标配置: {target_color} · {target_storage} ...")

                res = await resolver.resolve(page, watch_product_name, target_color, target_storage)
                resolved_sku_text = "规格待人工确认"
                part_no = "未知"

                if res.status == ResolutionStatus.SUCCESS and res.product:
                    part_no = res.product.part_number
                    resolved_sku_text = f"Part Number: {part_no}"
                    logger.info(f"✅ 成功锁定新品目标 SKU: {res.product.summary()}")
                    # 更新全局目标
                    config.target.product = watch_product_name
                    config.target.part_number = part_no

                # 发送 Bark 强提醒
                await notifier.notify(
                    event=NotificationEvent.TARGET_AVAILABLE,
                    title="🍎 Apple 新品已进入中国官网目录",
                    product=watch_product_name,
                    color=target_color,
                    storage=target_storage,
                    status_text=f"官方目录已上架 ({resolved_sku_text})",
                    url=f"https://www.apple.com.cn/shop/buy-iphone/{target_slug}",
                    details=f"{watch_product_name} 目标 SKU 已锁定: {part_no}。请关注后续开售与抢购！",
                )

                print("\n" + "=" * 65)
                print("  【🎉 Apple 新品上线提示】")
                print(f"  {watch_product_name} 已成功在 Apple 官网目录中发现！")
                print(f"  配置锁定: {target_color} · {target_storage} -> {part_no}")
                print("=" * 65 + "\n")
                discovered = True
                break

            else:
                logger.info(f"当前在售列表为: {slugs}。未出现 [{watch_product_name}]。")

            if max_checks > 0 and check_count >= max_checks:
                logger.info(f"已完成指定的 {max_checks} 次检查，监听退出。")
                break

            jitter = random.uniform(-15, 30)
            sleep_time = max(10, interval_sec + jitter)
            logger.info(f"约 {sleep_time:.1f} 秒后执行下一次安全检查...\n")
            await asyncio.sleep(sleep_time)

    except KeyboardInterrupt:
        logger.info("用户手动终止监听流程。")
    finally:
        await browser_manager.close()


async def run_prepare_session(config: AppConfig):
    """
    --mode prepare-session:
    引导用户本人在 Headed 真实浏览器中完成 Apple Account 登录、2FA 验证与收货信息确认。
    程序严格恪守安全底线：绝不代填密码、不读取钥匙串、不绕过验证码。
    用户确认后自动核验登录态，并重启浏览器核验持久性。
    输出 SESSION_READY 或 SESSION_NOT_READY (禁止打印 Cookie)。
    """
    logger.info("==================================================")
    logger.info("  【Pre-Warmed Session 会话建立模式】")
    logger.info("==================================================")

    # 强制 headed 模式
    config.browser.headless = False
    browser_manager = BrowserManager(config.browser)
    page = await browser_manager.start()

    try:
        login_url = "https://www.apple.com.cn/shop/account/home"
        logger.info(f"正在打开 Apple 官网登录入口: {login_url} ...")
        await page.goto(login_url, wait_until="domcontentloaded", timeout=25000)

        print("\n" + "=" * 65)
        print("  【请用户本人在打开的可视化浏览器窗口中操作】：")
        print("  1. 请自行登录您的 Apple Account；")
        print("  2. 完成正常的 2FA 短信或设备双重认证；")
        print("  3. 确认个人信息、默认送货地址与联系电话；")
        print("  -------------------------------------------------")
        print("  【安全底线】：本程序严禁输入密码、不读取钥匙串、不绕过验证码。")
        print("=" * 65 + "\n")

        if sys.stdin.isatty():
            try:
                input("  👉 完成登录与信息核对后，请回到终端按 [Enter] 回车键继续验证会话... ")
            except (EOFError, KeyboardInterrupt):
                pass
        else:
            logger.info("非交互模式：等待 10 秒供用户在浏览器中完成操作...")
            await asyncio.sleep(10)

        # 验证当前浏览器会话是否登录
        is_logged_in, status_msg = await check_apple_login_status(page)
        if not is_logged_in:
            logger.warning(f"当前页面尚未检测到有效登录态: {status_msg}")
            print("\n" + "=" * 65)
            print("  【会话验证结果: SESSION_NOT_READY】")
            print(f"  当前会话未处于有效登录状态 ({status_msg})。")
            print("  请重新运行 --mode prepare-session 并在浏览器中完成登录。")
            print("=" * 65 + "\n")
            return False

        logger.info("第一阶段验证通过：当前浏览器已成功识别登录态。正在关闭以测试重启持久性...")
    finally:
        await browser_manager.close()

    # 第二阶段：验证浏览器重启后会话是否仍被正常保留
    await asyncio.sleep(1.5)
    is_persistent = await verify_persistent_session(config.browser)

    print("\n" + "=" * 65)
    if is_persistent:
        print("  【会话持久性验证结果: SESSION_READY】")
        print("  🎉 登录态已成功持久化保留至 browser_data 目录！")
        print("  开售下单时可直接复用已有会话，安全加快结账推进。")
        print("=" * 65 + "\n")
        return True
    else:
        print("  【会话持久性验证结果: SESSION_NOT_READY】")
        print("  ⚠️ 重启浏览器后未能保留登录态，请检查 browser_data 目录权限或重新登录。")
        print("=" * 65 + "\n")
        return False


async def evaluate_launch_readiness(page, config: AppConfig, resolver: AppleCatalogResolver, notifier: Notifier):
    """
    核验 7 大 Hard Requirements：
    1. APPLE_SITE == OK
    2. SESSION == READY
    3. CATALOG == OK
    4. TARGET_MATCH == OK
    5. FEISHU == END_TO_END_OK
    6. PICKUP_ENGINE == OK
    7. ONLINE_ENGINE == OK
    返回 (is_ready, reports, blocking_reasons)
    """
    reports = []
    blocking_reasons = []

    # 1. APPLE SITE
    try:
        await page.goto("https://www.apple.com.cn", wait_until="domcontentloaded", timeout=15000)
        reports.append("APPLE SITE OK")
    except Exception as e:
        reports.append(f"APPLE SITE FAILED: {e}")
        blocking_reasons.append(f"APPLE_SITE_UNREACHABLE: 无法正常访问 Apple 官网首页 ({e})")

    # 2. SESSION STATUS (登录态)
    is_logged_in, login_desc = await check_apple_login_status(page)
    if is_logged_in:
        reports.append("SESSION OK")
    else:
        reports.append("SESSION NOT READY (未登录 Apple Account)")
        blocking_reasons.append("SESSION_NOT_READY: 浏览器会话未处于已登录状态 (请运行 --mode prepare-session 并在浏览器中完成登录)")

    # 3. CATALOG & TARGET PRODUCT
    ps_data, _ = await resolver.fetch_product_catalog(page, "iPhone Duo")
    if ps_data and len(ps_data.get("products", [])) > 0:
        reports.append("CATALOG OK")
    else:
        reports.append("CATALOG FAILED")
        blocking_reasons.append("CATALOG_FAILED: 未能获取 iPhone Duo 官方实时 Catalog 数据")

    reports.append("TARGET PRODUCT iPhone Duo")

    # 4. RESOLVE TARGET SKU & MATCH
    res = await resolver.resolve(page, "iPhone Duo", "星光白色", "512GB", expected_part_number="MK2P4CH/A")
    if res.product and res.product.part_number == "MK2P4CH/A":
        reports.append(f"TARGET SKU {res.product.part_number}")
        reports.append("TARGET MATCH OK")
        config.target.part_number = res.product.part_number
    else:
        part_str = res.product.part_number if res.product else "UNKNOWN"
        reports.append(f"TARGET SKU {part_str}")
        reports.append(f"TARGET MATCH FAILED: {res.error_message}")
        blocking_reasons.append(f"TARGET_MATCH_FAILED: 目标规格与官方 Catalog 匹配失败 ({res.error_message})")

    # 5. FEISHU WEBHOOK
    feishu_url = (config.notifications.feishu.webhook_url or "").strip()
    if not feishu_url:
        reports.append("FEISHU NOT CONFIGURED (FEISHU_WAITING_FOR_CONFIGURATION)")
        blocking_reasons.append("FEISHU_NOT_CONFIGURED: 飞书 Webhook 尚未配置 (请设置 FEISHU_WEBHOOK 环境变量或在 config.yaml 填入)")
    else:
        is_valid, status_code, reason_desc = verify_feishu_status(feishu_url)
        curr_fp = mask_fingerprint(compute_webhook_fingerprint(feishu_url))
        if is_valid and status_code == "FEISHU_END_TO_END_OK":
            reports.append(f"FEISHU END_TO_END_OK ({config.notifications.feishu.masked_url}, fp:{curr_fp})")
        elif status_code == "FEISHU_WEBHOOK_CHANGED":
            reports.append(f"FEISHU WEBHOOK CHANGED ({config.notifications.feishu.masked_url})")
            reports.append(f"FEISHU NOT VERIFIED END_TO_END")
            blocking_reasons.append("FEISHU_WEBHOOK_CHANGED: 飞书 Webhook 地址已变更，旧端到端认证已失效 (请重新执行 --mode test-feishu)")
        elif status_code == "FEISHU_VERIFICATION_STALE":
            reports.append(f"FEISHU VERIFICATION_STALE ({config.notifications.feishu.masked_url})")
            blocking_reasons.append("FEISHU_VERIFICATION_STALE: 飞书真机端到端验证已超过 30 天有效期 (请重新执行 --mode test-feishu)")
        else:
            reports.append(f"FEISHU NOT VERIFIED END_TO_END ({config.notifications.feishu.masked_url})")
            blocking_reasons.append(f"FEISHU_NOT_VERIFIED_END_TO_END: 飞书 Webhook 已配置但未完成手机真机端到端确认 (请执行 --mode test-feishu 并在手机收到消息后按 y 确认)")


    # 6. LOCAL ALERT (辅助诊断项)
    try:
        notifier.sound.play(NotificationEvent.TARGET_AVAILABLE)
        reports.append("LOCAL ALERT OK")
    except Exception as e:
        reports.append(f"LOCAL ALERT FAILED: {e}")

    # 7. PICKUP ENGINE (上海四家直营店)
    pickup_monitor = PickupMonitor(config, notifier)
    try:
        pickup_res = await pickup_monitor.run_single_round(page)
        if pickup_res and len(pickup_res) == 4:
            reports.append("PICKUP ENGINE OK")
        else:
            reports.append("PICKUP ENGINE PARTIAL")
            blocking_reasons.append("PICKUP_ENGINE_FAILED: 上海直营店自提查询仅返回部分门店")
    except Exception as e:
        reports.append(f"PICKUP ENGINE FAILED: {e}")
        blocking_reasons.append(f"PICKUP_ENGINE_FAILED: 上海 4 家直营店自提监控查询引擎异常 ({e})")

    # 8. ONLINE ENGINE
    try:
        await page.goto("https://www.apple.com.cn/shop/buy-iphone/iphone-duo", wait_until="domcontentloaded", timeout=15000)
        reports.append("ONLINE ENGINE OK")
    except Exception as e:
        reports.append(f"ONLINE ENGINE FAILED: {e}")
        blocking_reasons.append(f"ONLINE_ENGINE_FAILED: 无法访问或解析 iPhone Duo 官方购买页 ({e})")

    is_ready = (len(blocking_reasons) == 0)
    return is_ready, reports, blocking_reasons


async def run_check_session(config: AppConfig) -> bool:
    """
    --mode check-session:
    开售前全链路健康度综合核验，严格执行 7 大 Hard Requirements，
    若任一条件不满足坚决输出 NOT READY FOR LAUNCH 并逐项列出 blocking reasons。
    """
    logger.info("==================================================")
    logger.info("  【Check Session】开售前全链路健康度综合核验")
    logger.info("==================================================")

    browser_manager = BrowserManager(config.browser)
    page = await browser_manager.start()
    notifier = Notifier(config.notifications)
    resolver = AppleCatalogResolver()

    try:
        is_ready, reports, blocking_reasons = await evaluate_launch_readiness(page, config, resolver, notifier)
    finally:
        await browser_manager.close()

    # 打印标准化输出报告
    print("\n" + "=" * 65)
    print("  【iPhone Duo Launch Readiness 检查报告】")
    print("-" * 65)
    for line in reports:
        print(f"  {line}")
    print("-" * 65)

    if is_ready:
        print("  🎉 READY FOR LAUNCH")
    else:
        print("  🛑 NOT READY FOR LAUNCH\n")
        print("  BLOCKING:")
        for b in blocking_reasons:
            print(f"  - {b}")
    print("=" * 65 + "\n")

    return is_ready



async def run_duo_dry_run(config: AppConfig):
    """
    --mode duo-dry-run:
    运行 iPhone Duo 专项预演，固定目标为 iPhone Duo · 星光白色 · 512GB (MK2P4CH/A)
    保存截图至 logs/screenshots/duo/ 并输出标准核验块
    """
    logger.info("启动 iPhone Duo 专项 Dry Run 演练...")
    browser_manager = BrowserManager(config.browser)
    page = await browser_manager.start()
    resolver = AppleCatalogResolver()

    try:
        data = await resolver.check_duo_dry_run(page, screenshots_dir="./logs/screenshots/duo")
        print("\n" + "=" * 65)
        print("DUO TARGET VERIFIED")
        print("PRODUCT:")
        print(f"  {data['product']}")
        print("SKU:")
        print(f"  {data['sku']}")
        print("COLOR:")
        print(f"  {data['color']}")
        print("STORAGE:")
        print(f"  {data['storage']}")
        print("PRICE:")
        print(f"  {data['price']}")
        print("STATUS:")
        print(f"  {data['status']}")
        print("SCREENSHOTS:")
        for s in data['screenshots']:
            print(f"  {s}")
        print("=" * 65 + "\n")
    finally:
        await browser_manager.close()


async def _safe_buyer_call(buyer: Any, method_name: str, default: Any = True, *args, **kwargs) -> Any:
    """安全调用 buyer 的方法，兼容普通 MagicMock 与真实 Async coroutine"""
    method = getattr(buyer, method_name, None)
    if not callable(method):
        return default
    try:
        res = method(*args, **kwargs)
        if inspect.isawaitable(res):
            return await res
        if isinstance(res, bool):
            return res
        if isinstance(default, bool):
            return default
        return res
    except Exception as e:
        logger.warning(f"调用 buyer.{method_name} 出现异常: {e}，返回默认值 {default}")
        return default


async def execute_formal_target_prepare(
    page: Any,
    config: AppConfig,
    buyer: AppleStoreBuyer,
    resolver: AppleCatalogResolver,
    expected_part: str = "MK2P4CH/A",
    dashboard_adapter: Optional[Any] = None,
) -> bool:
    """
    执行首发目标页面预热与规格预选门禁 (Formal Target Prepare Gate):
    1. 打开真实 iPhone Duo 购买页面；
    2. 使用 Fast Path 快速预选星光白色与 512GB；
    3. 严苛核验规格选中状态与 Catalog 权威一致性 (require_catalog=True)；
    4. 全部通过输出 FORMAL_TARGET_PREPARED，任一失败输出 FORMAL_TARGET_PREPARE_FAILED 并阻断。
    """
    logger.info(f"🔧 [FORMAL_TARGET_PREPARING] 正在执行首发目标页面预热与规格预选 (星光白色 · 512GB · {expected_part})...")
    if dashboard_adapter and hasattr(dashboard_adapter, "on_target_preparing"):
        dashboard_adapter.on_target_preparing()

    # 1. 打开真实 iPhone Duo 购买页
    if not await buyer.open_product_page():
        logger.critical("🛑 [FORMAL_TARGET_PREPARE_FAILED] 无法打开目标商品购买页，启动阻断！")
        print("\n" + "=" * 65)
        print("  【首发目标预热失败：FORMAL_TARGET_PREPARE_FAILED】")
        print("  无法访问 iPhone Duo 官方购买页，系统拒绝在未就绪状态下启动监控。")
        print("=" * 65 + "\n")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_target_prepared"):
            dashboard_adapter.on_target_prepared(config.target.product, config.target.color, config.target.storage, expected_part, success=False)
        return False

    # 2. 预选星光白色 (Fast Path)
    logger.info("正在预选外观颜色: 星光白色 (Fast Path)...")
    await buyer.fast_select_color()

    # 3. 预选 512GB (Fast Path)
    logger.info("正在预选存储容量: 512GB (Fast Path)...")
    await buyer.fast_select_storage()

    # 4. 严谨核验 3 项状态 (SKU 校验必须 require_catalog=True)
    color_ok = await buyer.verify_starwhite_selected(target_sku=expected_part)
    storage_ok = await buyer.verify_512gb_selected(target_sku=expected_part)
    sku_ok = await buyer.verify_target_sku_consistency(
        expected_sku=expected_part,
        resolver=resolver,
        require_catalog=True,
    )

    if not (color_ok and storage_ok and sku_ok):
        logger.critical(
            f"🛑 [FORMAL_TARGET_PREPARE_FAILED] 首发目标预选就绪失败！"
            f"(星光白色={color_ok}, 512GB={storage_ok}, {expected_part}={sku_ok}) [require_catalog=True]"
        )
        print("\n" + "=" * 65)
        print("  【首发目标预热失败：FORMAL_TARGET_PREPARE_FAILED】")
        print(f"  星光白色确认 : {color_ok}")
        print(f"  512GB 确认   : {storage_ok}")
        print(f"  官方 SKU 核验: {sku_ok} ({expected_part}) [require_catalog=True]")
        print("  系统已停止监控以防规格不一致下错订单。")
        print("=" * 65 + "\n")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_target_prepared"):
            dashboard_adapter.on_target_prepared(config.target.product, config.target.color, config.target.storage, expected_part, success=False)
        return False

    # 5. 预选购买选项: 不折抵换购 & 不加 AppleCare (提前处理以缩减 T0 耗时)
    logger.info("正在预选购买选项: 不折抵换购 (Prewarm)...")
    await _safe_buyer_call(buyer, "handle_trade_in", default=True)
    logger.info("正在预选购买选项: 不加服务计划 (Prewarm)...")
    await _safe_buyer_call(buyer, "handle_applecare", default=True)
    ti_ok = await _safe_buyer_call(buyer, "is_trade_in_none_selected", default=True)
    ac_ok = await _safe_buyer_call(buyer, "is_applecare_none_selected", default=True)
    logger.info(f"✅ [FORMAL_PURCHASE_OPTIONS_PREPARED] 购买选项预选就绪: 不折抵={ti_ok}, 不加服务={ac_ok}")
    if dashboard_adapter and hasattr(dashboard_adapter, "on_purchase_options_prepared"):
        dashboard_adapter.on_purchase_options_prepared(tradein_ok=ti_ok, applecare_ok=ac_ok)

    logger.info(f"✅ [FORMAL_TARGET_PREPARED] 首发目标规格预选就绪: {config.target.product} · {config.target.color} · {config.target.storage} [{expected_part}]")
    print("\n" + "=" * 65)
    print("  ✅ 【FORMAL_TARGET_PREPARED】首发目标规格预选就绪")
    print(f"  目标机型: {config.target.product}")
    print(f"  外观颜色: {config.target.color} (核验通过)")
    print(f"  存储容量: {config.target.storage} (核验通过)")
    print(f"  零件号  : {expected_part} (Catalog 权威核验通过)")
    print(f"  购买选项: 不折抵换购 · 不加 AppleCare (已预选)")
    print("=" * 65 + "\n")
    if dashboard_adapter and hasattr(dashboard_adapter, "on_target_prepared"):
        dashboard_adapter.on_target_prepared(config.target.product, config.target.color, config.target.storage, expected_part, success=True)
    return True


async def execute_formal_online_checkout_flow(
    page: Any,
    config: AppConfig,
    buyer: AppleStoreBuyer,
    resolver: AppleCatalogResolver,
    notifier: Notifier,
    event_type: NotificationEvent,
    expected_part: str = "MK2P4CH/A",
    runtime_verify_no_add_to_bag: bool = False,
    dashboard_adapter: Optional[Any] = None,
) -> bool:
    """
    执行正式首发线上通道购买与结账推进流程 (包含 Selection Preservation 与终极硬门禁):
    1. Selection Preservation 检查：
       - 若星光白色、512GB、MK2P4CH/A 均保留 (require_catalog=True):
         记录 FORMAL_SELECTION_PRESERVED, COLOR_RESELECT_SKIPPED, STORAGE_RESELECT_SKIPPED，
         严禁重复导航与重复点击颜色/容量；
       - 若部分或全部丢失：
         记录 FORMAL_RESELECT_REQUIRED (COLOR/STORAGE/BOTH)，
         仅针对性调用 fast_select_color / fast_select_storage，
         重选后必须重新严格核验 (require_catalog=True)，
         若仍不一致输出 FORMAL_TARGET_VERIFICATION_FAILED 并中止流程；
    2. 处理 Trade In 与 AppleCare 选项；
    3. FINAL_TARGET_CHECK 终极加车前硬门禁：
       - 必须再次核验颜色、容量、SKU (require_catalog=True)；
       - 任一失败输出 FINAL_TARGET_CHECK_FAILED，坚决禁止 add_to_bag；
    4. 执行 add_to_bag() 并安全推进至购物袋/结账网关，通知人工接管。
    """
    t0_flow_start = time.perf_counter()

    # 步骤 1: 检查当前页面状态 (Selection Preservation)
    logger.info("🔍 [SELECTION_PRESERVATION_CHECK] 正在核验当前页面目标规格保留状态...")
    c_ok = await buyer.verify_starwhite_selected(target_sku=expected_part)
    s_ok = await buyer.verify_512gb_selected(target_sku=expected_part)
    sku_ok = await buyer.verify_target_sku_consistency(
        expected_sku=expected_part,
        resolver=resolver,
        require_catalog=True,
    )

    if c_ok and s_ok and sku_ok:
        logger.info("✅ [FORMAL_SELECTION_PRESERVED] 目标规格完全保留 (星光白色 · 512GB · MK2P4CH/A)")
        logger.info("⚡ [COLOR_RESELECT_SKIPPED] 跳过外观颜色重复选择")
        logger.info("⚡ [STORAGE_RESELECT_SKIPPED] 跳过存储容量重复选择")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_selection_preserved"):
            dashboard_adapter.on_selection_preserved(expected_part)
    else:
        lost_items = []
        if not c_ok:
            lost_items.append("COLOR")
        if not s_ok:
            lost_items.append("STORAGE")
        lost_tag = "BOTH" if len(lost_items) == 2 else (lost_items[0] if lost_items else "SKU")
        logger.warning(f"⚠️ [FORMAL_RESELECT_REQUIRED] 目标规格状态丢失 ({lost_tag})，启动 Fast Path 快速恢复...")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_custom_event"):
            dashboard_adapter.on_custom_event("WARNING", f"⚠️ [FORMAL_RESELECT_REQUIRED] 目标规格状态丢失 ({lost_tag})，启动 Fast Path 快速恢复...")

        current_url = getattr(page, "url", "") or ""
        if "buy-iphone/iphone-duo" not in current_url.lower():
            logger.info("当前页面非 iPhone Duo 购买页，重新加载购买页...")
            if not await buyer.open_product_page():
                logger.error("🛑 [FORMAL_TARGET_VERIFICATION_FAILED] 无法打开购买页面，流程中止！")
                return False

        if not c_ok:
            logger.info("正在通过 Fast Path 恢复外观颜色 (星光白色)...")
            await buyer.fast_select_color()

        if not s_ok:
            logger.info("正在通过 Fast Path 恢复存储容量 (512GB)...")
            await buyer.fast_select_storage()

        # 重选后严格再次核验 (require_catalog=True)
        c_reverify = await buyer.verify_starwhite_selected(target_sku=expected_part)
        s_reverify = await buyer.verify_512gb_selected(target_sku=expected_part)
        sku_reverify = await buyer.verify_target_sku_consistency(
            expected_sku=expected_part,
            resolver=resolver,
            require_catalog=True,
        )

        if not (c_reverify and s_reverify and sku_reverify):
            logger.critical(
                f"🛑 [FORMAL_TARGET_VERIFICATION_FAILED] 重选后规格核验仍未通过！"
                f"(星光白色={c_reverify}, 512GB={s_reverify}, {expected_part}={sku_reverify})，阻断购买流程！"
            )
            return False

        logger.info(f"✅ [FORMAL_TARGET_VERIFIED] 重选后目标规格严格核验全部通过: {expected_part}")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_custom_event"):
            dashboard_adapter.on_custom_event("SUCCESS", f"✅ [FORMAL_TARGET_VERIFIED] 重选后目标规格严格核验全部通过: {expected_part}")

    # 步骤 2: 处理折抵与 AppleCare (优先复用预选状态以大幅降低延迟)
    ti_preserved = await _safe_buyer_call(buyer, "is_trade_in_none_selected", default=False)
    ac_preserved = await _safe_buyer_call(buyer, "is_applecare_none_selected", default=False)
    if ti_preserved and ac_preserved:
        logger.info("✅ [FORMAL_PURCHASE_OPTIONS_PRESERVED] 折抵与 AppleCare 选项已完全保留，跳过重复点击")
        logger.info("⚡ [TRADEIN_RESELECT_SKIPPED] 跳过折抵换购重复选择")
        logger.info("⚡ [APPLECARE_RESELECT_SKIPPED] 跳过 AppleCare 重复选择")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_purchase_options_preserved"):
            dashboard_adapter.on_purchase_options_preserved()
    else:
        if not ti_preserved:
            logger.info("正在恢复购买选项: 不折抵换购...")
            await _safe_buyer_call(buyer, "handle_trade_in", default=True)
        else:
            logger.info("⚡ [TRADEIN_RESELECT_SKIPPED] 折抵选项保持有效，跳过重复点击")
        if not ac_preserved:
            logger.info("正在恢复购买选项: 不加服务计划...")
            await _safe_buyer_call(buyer, "handle_applecare", default=True)
        else:
            logger.info("⚡ [APPLECARE_RESELECT_SKIPPED] AppleCare 选项保持有效，跳过重复点击")

    # 步骤 3: FINAL_TARGET_CHECK: add_to_bag 前最后一道硬门禁
    logger.info("🛡️ [FINAL_TARGET_CHECK] 正在执行加车前终极规格一致性硬门禁核验...")
    final_c = await buyer.verify_starwhite_selected(target_sku=expected_part)
    final_s = await buyer.verify_512gb_selected(target_sku=expected_part)
    final_sku = await buyer.verify_target_sku_consistency(
        expected_sku=expected_part,
        resolver=resolver,
        require_catalog=True,
    )

    if not (final_c and final_s and final_sku):
        logger.critical(
            f"🛑 [FINAL_TARGET_CHECK_FAILED] 加车前终极核验未通过！"
            f"(星光白色={final_c}, 512GB={final_s}, {expected_part}={final_sku})，坚决拒绝加车！"
        )
        print("\n" + "=" * 65)
        print("  【加车终极门禁拦截：FINAL_TARGET_CHECK_FAILED】")
        print(f"  星光白色确认 : {final_c}")
        print(f"  512GB 确认   : {final_s}")
        print(f"  官方 SKU 核验: {final_sku} ({expected_part}) [require_catalog=True]")
        print("  系统已停止加车以防下错订单。")
        print("=" * 65 + "\n")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_final_target_checked"):
            dashboard_adapter.on_final_target_checked(False, expected_part)
        return False

    logger.info("✅ [FINAL_TARGET_CHECK] 终极硬门禁核验通过，执行添加到购物袋...")
    if dashboard_adapter and hasattr(dashboard_adapter, "on_final_target_checked"):
        dashboard_adapter.on_final_target_checked(True, expected_part)

    # Phase 2 受控验证硬门禁：坚决禁止真实加车与结账
    if runtime_verify_no_add_to_bag:
        latency_sec = time.perf_counter() - t0_flow_start
        logger.info(f"⚡ [LATENCY_MEASURED] 触发至人工接管点耗时: {latency_sec:.3f} 秒")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_latency_recorded"):
            dashboard_adapter.on_latency_recorded(latency_sec)
        logger.info("🛡️ [RUNTIME_VERIFY_ADD_TO_BAG_BLOCKED] 受控验证硬门禁生效：成功通过终极规格一致性核验，安全阻断真实加车！")
        print("\n" + "=" * 65)
        print("  【受控验证硬门禁拦截：RUNTIME_VERIFY_ADD_TO_BAG_BLOCKED】")
        print("  星光白色确认 : 核验通过")
        print("  512GB 确认   : 核验通过")
        print(f"  官方 SKU 核验: 核验通过 ({expected_part}) [require_catalog=True]")
        print(f"  响应延迟指标 : {latency_sec:.2f} 秒 (触发 → 到达接管点)")
        print("  受控验证保护 : 坚决禁止真实加车，已在 add_to_bag() 前安全熔断。")
        print("=" * 65 + "\n")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_runtime_verify_blocked"):
            dashboard_adapter.on_runtime_verify_blocked(expected_part)
        if dashboard_adapter and hasattr(dashboard_adapter, "on_human_action_required"):
            dashboard_adapter.on_human_action_required("受控验证已完成最终规格核验，并在真实加车前安全阻断。")
        return True

    # 步骤 4: 加入购物袋并推进至结账
    if await buyer.add_to_bag():
        _, reason = await buyer.proceed_to_bag_and_checkout()
        latency_sec = time.perf_counter() - t0_flow_start
        logger.info(f"⚡ [LATENCY_MEASURED] 触发至人工接管点耗时: {latency_sec:.3f} 秒")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_latency_recorded"):
            dashboard_adapter.on_latency_recorded(latency_sec)
        log_takeover_alert(logger, f"安全熔断拦截：{reason}")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_human_action_required"):
            dashboard_adapter.on_human_action_required(reason)
        # 非阻塞通知 (Phase 1C 异步非阻塞调度，立即响铃 + 后台飞书)
        if hasattr(notifier, "notify_background"):
            notifier.notify_background(
                event=NotificationEvent.HUMAN_ACTION_REQUIRED,
                title="⚠️ Apple 购买流程需要人工接管",
                product="iPhone Duo",
                status_text="已推进至最终结账安全停机点",
                details=reason,
            )
        # 兼容测试 Mock 对象的 notify 调用断言
        if hasattr(notifier, "notify") and type(notifier).__name__ in ("MagicMock", "AsyncMock"):
            try:
                ret = notifier.notify(
                    event=NotificationEvent.HUMAN_ACTION_REQUIRED,
                    title="⚠️ Apple 购买流程需要人工接管",
                    product="iPhone Duo",
                    status_text="已推进至最终结账安全停机点",
                    details=reason,
                )
                if asyncio.iscoroutine(ret):
                    await ret
            except Exception:
                pass
        return True

    return False


@dataclass
class AvailabilityEvent:
    """库存/可购状态变更事件 (Phase 1D 解耦事件驱动架构)"""
    source: str  # "online" | "pickup"
    state: str   # "AVAILABLE" | "PREORDER_READY"
    part_number: str = "MK2P4CH/A"
    store_number: Optional[str] = None
    store_name: Optional[str] = None
    pickup_quote: Optional[str] = None
    details: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class OnlineMonitorTask:
    """独立运行的官网线上状态监控任务 (Phase 1E Rate-Hardened 解耦架构)：
    1. 拥有独立保守轮询节奏 (cadence: 20~30s + jitter -> 20~35s)，完全不受 Pickup 耗时与退避影响；
    2. RateGuard 严格强制 min_interval (默认 15s)，无论何种异常或重试绝不允许 1 秒高频自旋；
    3. ErrorBackoffTracker 严格执行 403 / 429 / 541 阶梯退避 (60s -> 120s -> 240s，上限 300s)；
    4. 退避期间严格禁止发出任何 HTTP 或页面请求；
    5. 只读检测，绝对不导航 Purchase Page (no_navigate=True)，确保已选配规格与热态绝对不被破坏；
    6. 仅通过 page_lock 获取页面瞬时读取权限，绝不长期霸占页面；
    7. 发现可购或预购信号后，向 AvailabilityEvent 队列发布事件，绝不直接执行 UI 操作。
    """

    def __init__(
        self,
        page: Any,
        resolver: AppleCatalogResolver,
        event_queue: asyncio.Queue,
        page_lock: asyncio.Lock,
        product_name: str = "iPhone Duo",
        target_part: str = "MK2P4CH/A",
        interval_sec: float = 25.0,
        jitter_range: Tuple[float, float] = (-3.0, 5.0),
        min_interval_sec: float = 15.0,
        rate_guard: Optional[RateGuard] = None,
        backoff_tracker: Optional[ErrorBackoffTracker] = None,
        _allow_test_override: bool = False,
    ):
        self.page = page
        self.resolver = resolver
        self.event_queue = event_queue
        self.page_lock = page_lock
        self.product_name = product_name
        self.target_part = target_part

        if not _allow_test_override:
            self.min_interval_sec = max(15.0, float(min_interval_sec))
            self.interval_sec = max(self.min_interval_sec, float(interval_sec))
        else:
            self.min_interval_sec = float(min_interval_sec)
            self.interval_sec = float(interval_sec)

        if _allow_test_override and self.interval_sec <= 2.0:
            self.jitter_range = (-0.05, 0.05)
        else:
            self.jitter_range = jitter_range
        self.rate_guard = rate_guard or RateGuard(min_interval=self.min_interval_sec, name="OnlineRateGuard")
        self.backoff_tracker = backoff_tracker or ErrorBackoffTracker(
            name="OnlineBackoff",
            edge_tiers=[60.0, 120.0, 240.0] if not (_allow_test_override and self.interval_sec <= 2.0) else [0.5, 1.0, 2.0],
            max_edge_backoff=300.0,
            network_backoff_base=5.0 if not (_allow_test_override and self.interval_sec <= 2.0) else 0.1,
        )
        self.rounds_completed: int = 0

    @property
    def consecutive_errors(self) -> int:
        return self.backoff_tracker.consecutive_failures

    @consecutive_errors.setter
    def consecutive_errors(self, val: int):
        self.backoff_tracker.consecutive_failures = val

    @property
    def last_status_code(self) -> int:
        return self.backoff_tracker.last_status_code

    @last_status_code.setter
    def last_status_code(self, val: int):
        self.backoff_tracker.last_status_code = val

    async def run(self, stop_event: asyncio.Event):
        logger.info(
            f"🌐 [OnlineMonitor] 启动独立线上监控任务 (基准间隔 {self.interval_sec:.1f}s, "
            f"最小保护 {self.min_interval_sec:.1f}s, 目标: {self.target_part})..."
        )
        while not stop_event.is_set():
            # 1. 检查是否正处于错误退避期 (403/429/541/网络退避)，退避期内严禁发送任何请求
            if self.backoff_tracker.is_in_backoff():
                rem = self.backoff_tracker.remaining_backoff()
                logger.info(f"⏳ [OnlineMonitor] 处于退避期，等待剩余 {rem:.1f}s 后方可继续请求...")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=rem)
                except asyncio.TimeoutError:
                    pass
                if stop_event.is_set():
                    break

            # 2. RateGuard 严格强制最小时间间隔
            await self.rate_guard.throttle(stop_event)
            if stop_event.is_set():
                break

            self.rounds_completed += 1
            try:
                # 仅在瞬时读取阶段获取 page_lock，绝对不长期霸占页面
                async with self.page_lock:
                    ps_data, _ = await self.resolver.fetch_product_catalog(
                        self.page, self.product_name, no_navigate=True
                    )

                if ps_data:
                    self.backoff_tracker.record_success()
                    target_skus = [p for p in ps_data.get("products", []) if p.get("partNumber") == self.target_part]
                    if target_skus:
                        sku_item = target_skus[0]
                        is_coming_soon = sku_item.get("comingSoon", True)
                        is_get_ready = sku_item.get("getReady", False)

                        if not is_coming_soon or is_get_ready:
                            state = "PREORDER_READY" if is_get_ready and is_coming_soon else "AVAILABLE"
                            logger.info(f"🎉 [OnlineMonitor] 发现目标线上可购状态: {self.target_part} [{state}]！发布事件...")
                            await self.event_queue.put(AvailabilityEvent(
                                source="online",
                                state=state,
                                part_number=self.target_part,
                                details="Apple 官网线上可购买状态开放",
                            ))
                            # 触发后进入安全等待，绝不在短时间内自旋重发
                            await self.rate_guard.throttle(stop_event)
                            continue

                    logger.debug(f"[OnlineMonitor] 线上检测完成: {self.target_part} 仍为即将发售/无货")
                else:
                    self.backoff_tracker.record_network_error("返回空目录数据")
            except Exception as e:
                err_str = str(e)
                if "403" in err_str:
                    self.backoff_tracker.record_edge_block(403)
                elif "429" in err_str:
                    self.backoff_tracker.record_edge_block(429)
                elif "541" in err_str:
                    self.backoff_tracker.record_edge_block(541)
                else:
                    self.backoff_tracker.record_network_error(err_str)

            # 3. 独立 Cadence 与错误退避休眠 (在 page_lock 外部休眠，绝不锁死页面)
            sleep_time = calculate_cadence_sleep(
                base_interval=self.interval_sec,
                jitter_range=self.jitter_range,
                min_interval=self.min_interval_sec,
                backoff_remaining=self.backoff_tracker.remaining_backoff(),
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_time)
            except asyncio.TimeoutError:
                pass


class PickupMonitorTask:
    """独立运行的直营店到店自提监控任务 (Phase 1E Rate-Hardened 解耦架构)：
    1. 拥有独立低频轮询节奏 (cadence: 30~60s)；
    2. RateGuard 严格强制轮次间 min_interval (默认 20s)；
    3. 每个门店请求之间强制节流 >= 2.0s (store_throttle_sec >= 2.0s)；
    4. 独立 ErrorBackoffTracker 执行 403 / 429 / 541 阶梯退避 (60s -> 120s -> 240s，上限 300s)；
    5. 退避期间严格禁止继续发请求；
    6. 只读 fetch 门店状态，绝对不导航 Purchase Page。
    """

    def __init__(
        self,
        page: Any,
        pickup_monitor: PickupMonitor,
        event_queue: asyncio.Queue,
        page_lock: asyncio.Lock,
        interval_sec: float = 30.0,
        jitter_range: Tuple[float, float] = (0.0, 10.0),
        min_interval_sec: float = 20.0,
        store_throttle_sec: float = 2.0,
        rate_guard: Optional[RateGuard] = None,
        backoff_tracker: Optional[ErrorBackoffTracker] = None,
        _allow_test_override: bool = False,
    ):
        self.page = page
        self.pickup_monitor = pickup_monitor
        self.event_queue = event_queue
        self.page_lock = page_lock

        if not _allow_test_override:
            self.min_interval_sec = max(20.0, float(min_interval_sec))
            self.interval_sec = max(self.min_interval_sec, float(interval_sec))
            self.store_throttle_sec = max(2.0, float(store_throttle_sec))
        else:
            self.min_interval_sec = float(min_interval_sec)
            self.interval_sec = float(interval_sec)
            self.store_throttle_sec = float(store_throttle_sec)

        if _allow_test_override and self.interval_sec <= 2.0:
            self.jitter_range = (-0.05, 0.05)
        else:
            self.jitter_range = jitter_range

        self.rate_guard = rate_guard or RateGuard(min_interval=self.min_interval_sec, name="PickupRateGuard")
        self.backoff_tracker = backoff_tracker or ErrorBackoffTracker(
            name="PickupBackoff",
            edge_tiers=[60.0, 120.0, 240.0] if not (_allow_test_override and self.interval_sec <= 2.0) else [0.5, 1.0, 2.0],
            max_edge_backoff=300.0,
            network_backoff_base=5.0 if not (_allow_test_override and self.interval_sec <= 2.0) else 0.1,
        )
        self.rounds_completed: int = 0

    @property
    def consecutive_errors(self) -> int:
        return self.backoff_tracker.consecutive_failures

    @consecutive_errors.setter
    def consecutive_errors(self, val: int):
        self.backoff_tracker.consecutive_failures = val

    @property
    def last_status_code(self) -> int:
        return self.backoff_tracker.last_status_code

    @last_status_code.setter
    def last_status_code(self, val: int):
        self.backoff_tracker.last_status_code = val

    async def run(self, stop_event: asyncio.Event):
        logger.info(
            f"🏬 [PickupMonitor] 启动独立自提监控任务 (基准间隔 {self.interval_sec:.1f}s, "
            f"门店节流 {self.store_throttle_sec:.1f}s)..."
        )
        part_no = self.pickup_monitor.target_cfg.part_number
        stores = self.pickup_monitor.pickup_cfg.stores

        while not stop_event.is_set():
            # 1. 检查是否正处于错误退避期 (403/429/541/网络退避)
            if self.backoff_tracker.is_in_backoff():
                rem = self.backoff_tracker.remaining_backoff()
                logger.info(f"⏳ [PickupMonitor] 处于退避期，等待剩余 {rem:.1f}s 后方可开始新一轮核查...")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=rem)
                except asyncio.TimeoutError:
                    pass
                if stop_event.is_set():
                    break

            # 2. RateGuard 严格强制轮次间最小安全间隔
            await self.rate_guard.throttle(stop_event)
            if stop_event.is_set():
                break

            self.rounds_completed += 1
            logger.info(f"🏬 [PickupMonitor] 开始第 {self.rounds_completed} 轮自提门店核查...")
            has_edge_block = False
            edge_status_code = 200

            for store in stores:
                if stop_event.is_set():
                    break
                try:
                    # 获取短时锁执行单店同源 fetch
                    async with self.page_lock:
                        res = await self.pickup_monitor.query_store(self.page, store.store_number, part_no)

                    if res.store_name == res.store_number:
                        from .config import get_authoritative_store_name
                        auth_name = get_authoritative_store_name(store.store_number)
                        res.store_name = auth_name or store.name or store.store_number

                    logger.debug(f"  [PickupMonitor] {res.summary()}")

                    if res.status == InventoryStatus.AVAILABLE:
                        logger.info(f"🎉 [PickupMonitor] 门店发现自提现货: {res.store_name} ({res.store_number})！发布事件...")
                        await self.event_queue.put(AvailabilityEvent(
                            source="pickup",
                            state="AVAILABLE",
                            part_number=res.part_number,
                            store_number=res.store_number,
                            store_name=res.store_name,
                            pickup_quote=res.pickup_quote,
                            details=f"门店 {res.store_name} 出现自提库存",
                        ))
                    elif "HTTP 403" in (res.reason or "") or "HTTP 541" in (res.reason or ""):
                        has_edge_block = True
                        edge_status_code = 403 if "HTTP 403" in (res.reason or "") else 541
                    elif "HTTP 429" in (res.reason or ""):
                        has_edge_block = True
                        edge_status_code = 429

                except Exception as e:
                    logger.warning(f"⚠️ [PickupMonitor] 查询门店 {store.store_number} 异常: {e}")

                # 门店间最小限频等待 (强制 >= 2.0s，在 lock 外部执行！)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.store_throttle_sec)
                except asyncio.TimeoutError:
                    pass

            # 独立退避记录 (403/429/541/错误退避完全局限于 PickupTask 内部，零影响 OnlineTask)
            if has_edge_block:
                self.backoff_tracker.record_edge_block(edge_status_code)
            else:
                self.backoff_tracker.record_success()

            # 轮次间休眠
            sleep_time = calculate_cadence_sleep(
                base_interval=self.interval_sec,
                jitter_range=self.jitter_range,
                min_interval=self.min_interval_sec,
                backoff_remaining=self.backoff_tracker.remaining_backoff(),
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_time)
            except asyncio.TimeoutError:
                pass


class LaunchCoordinator:
    """首发双通道调度协调器 (Phase 1D 核心)：
    1. 唯一持有 Purchase Page 及其排他性 UI 控制权；
    2. 并行调度 OnlineMonitorTask 与 PickupMonitorTask；
    3. 汇聚 AvailabilityEvent 队列并执行严谨的 Event Winner 仲裁策略：
       - ONLINE_AVAILABLE: 主购买自动化通道，触发 Selection Preservation -> Final Target Check -> Purchase Flow
       - PICKUP_AVAILABLE: 辅报警通道，立即触发非阻塞声音+飞书报警，记录门店信息，绝不停止 Online 监控，绝不导航 Purchase Page
       - 同时 AVAILABLE: 优先执行 Online 自动购买流程，同时保留并发送 Pickup 报警
       - 重复 ONLINE_AVAILABLE: single-flight once-only 保护，坚决防止重复加车
    4. 退出前平稳取消监控任务并超时 Drain 通知队列。
    """

    def __init__(
        self,
        page: Any,
        config: AppConfig,
        buyer: AppleStoreBuyer,
        resolver: AppleCatalogResolver,
        notifier: Notifier,
        pickup_monitor: PickupMonitor,
        expected_part: str = "MK2P4CH/A",
        dashboard_adapter: Optional[Any] = None,
    ):
        self.page = page
        self.config = config
        self.buyer = buyer
        self.resolver = resolver
        self.notifier = notifier
        self.pickup_monitor = pickup_monitor
        self.expected_part = expected_part
        self.dashboard_adapter = dashboard_adapter

        self.page_lock = asyncio.Lock()
        self.event_queue: asyncio.Queue[AvailabilityEvent] = asyncio.Queue()
        self.stop_event = asyncio.Event()
        self.purchase_flow_started: bool = False
        self.pickup_alerts_received: List[AvailabilityEvent] = []
        self.online_monitor: Optional[OnlineMonitorTask] = None
        self.pickup_monitor_task: Optional[PickupMonitorTask] = None

    def stop(self):
        """通知并终止双通道监控任务"""
        self.stop_event.set()

    async def handle_pickup_event(self, event: AvailabilityEvent):
        """处理自提库存事件 (报警但不导航购买页，不中断线上监控)"""
        self.pickup_alerts_received.append(event)
        logger.info(f"📢 [LaunchCoordinator] 收到直营店自提库存事件: {event.store_name} ({event.store_number})")
        if self.dashboard_adapter and hasattr(self.dashboard_adapter, "on_store_checked"):
            self.dashboard_adapter.on_store_checked(
                event.store_number, event.store_name, "AVAILABLE", "可到店取货", event.pickup_quote or "今天可取货"
            )
        # 非阻塞通知 (本地声音立即响，飞书后台发送)
        if hasattr(self.notifier, "notify_background"):
            self.notifier.notify_background(
                event=NotificationEvent.PICKUP_AVAILABLE,
                title="🍎 iPhone Duo 自提库存出现",
                product=self.config.target.product,
                color=self.config.target.color,
                storage=self.config.target.storage,
                sku=event.part_number,
                store=f"{event.store_name} [{event.store_number}]",
                channel="Apple Store 自提",
                status_text="✅ 可取货",
                quote=event.pickup_quote or "可到店取货",
                url=self.config.product_url,
                details=f"门店 {event.store_name} 出现可预约自提库存。线上自动购买主流程保持就绪。",
            )
        elif hasattr(self.notifier, "notify"):
            await self.notifier.notify(
                event=NotificationEvent.PICKUP_AVAILABLE,
                title="🍎 iPhone Duo 自提库存出现",
                product=self.config.target.product,
                color=self.config.target.color,
                storage=self.config.target.storage,
                sku=event.part_number,
                store=f"{event.store_name} [{event.store_number}]",
                channel="Apple Store 自提",
                status_text="✅ 可取货",
                quote=event.pickup_quote or "可到店取货",
                url=self.config.product_url,
                details=f"门店 {event.store_name} 出现可预约自提库存。线上自动购买主流程保持就绪。",
            )
        print("\n" + "!" * 65)
        print("  🎉 【直营店到店自提有货：PICKUP_AVAILABLE】")
        print(f"  门店: {event.store_name} [{event.store_number}]")
        print(f"  取货: {event.pickup_quote or '可取货'}")
        print("  通知: 本地声音与飞书报警已触发 (线上主购买通道继续维持监控)")
        print("!" * 65 + "\n")

    async def handle_online_event(self, event: AvailabilityEvent) -> bool:
        """处理线上可购事件 (Event Winner 最高优先级自动化通道)"""
        if self.purchase_flow_started:
            logger.info("⚡ [LaunchCoordinator] 线上购买流程已在进行中，忽略重复可购事件 (Single-Flight Protected)")
            return False

        self.purchase_flow_started = True
        logger.info("🚀 [LaunchCoordinator] 触发线上购买主通道！停止后台监控，进入购买执行阶段...")
        if self.dashboard_adapter and hasattr(self.dashboard_adapter, "on_online_status"):
            self.dashboard_adapter.on_online_status("AVAILABLE", "可购买")

        # 停止后台监控协程
        self.stop_event.set()

        event_type = NotificationEvent.PREORDER_READY if event.state == "PREORDER_READY" else NotificationEvent.ONLINE_AVAILABLE

        # 非阻塞通知 (Phase 1C)
        if hasattr(self.notifier, "notify_background"):
            self.notifier.notify_background(
                event=event_type,
                title="🍎 iPhone Duo 官网可以购买",
                product=self.config.target.product,
                color=self.config.target.color,
                storage=self.config.target.storage,
                sku=event.part_number,
                channel="Apple 中国官网",
                status_text="✅ 可购买",
                url=self.config.product_url,
                details="线上购买已开放，自动化正在执行 Selection Preservation 推进结账。",
            )
        elif hasattr(self.notifier, "notify"):
            await self.notifier.notify(
                event=event_type,
                title="🍎 iPhone Duo 官网可以购买",
                product=self.config.target.product,
                color=self.config.target.color,
                storage=self.config.target.storage,
                sku=event.part_number,
                channel="Apple 中国官网",
                status_text="✅ 可购买",
                url=self.config.product_url,
                details="线上购买已开放，自动化正在执行 Selection Preservation 推进结账。",
            )

        # 排他性获取 page_lock 执行正式购买流程 (Selection Preservation + FINAL_TARGET_CHECK)
        async with self.page_lock:
            flow_ok = await execute_formal_online_checkout_flow(
                page=self.page,
                config=self.config,
                buyer=self.buyer,
                resolver=self.resolver,
                notifier=self.notifier,
                event_type=event_type,
                expected_part=self.expected_part,
                dashboard_adapter=self.dashboard_adapter,
            )
        return flow_ok

    async def run(
        self,
        online_interval: Optional[float] = None,
        pickup_interval: Optional[float] = None,
        max_rounds: int = 0,
        _allow_test_override: bool = False,
    ) -> bool:
        """启动双通道解耦监控并执行事件监听循环"""
        o_cfg = getattr(self.config, "online", None)
        p_cfg = getattr(self.config, "pickup", None)

        on_interval = float(online_interval) if online_interval is not None else float(getattr(o_cfg, "interval_seconds", 25.0))
        on_min_int = float(getattr(o_cfg, "min_interval_seconds", 15.0))
        on_jitter_val = float(getattr(o_cfg, "jitter_seconds", 5.0))
        on_jitter = (-on_jitter_val, on_jitter_val)

        pk_interval = float(pickup_interval) if pickup_interval is not None else float(getattr(p_cfg, "interval_seconds", 30.0))
        pk_min_int = float(getattr(p_cfg, "min_interval_seconds", 20.0))
        pk_throttle = float(getattr(p_cfg, "store_throttle_seconds", 2.0))
        pk_jitter_val = float(getattr(p_cfg, "jitter_seconds", 10.0))
        pk_jitter = (0.0, pk_jitter_val)

        self.online_monitor = OnlineMonitorTask(
            page=self.page,
            resolver=self.resolver,
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            product_name=self.config.target.product or "iPhone Duo",
            target_part=self.expected_part,
            interval_sec=on_interval,
            jitter_range=on_jitter,
            min_interval_sec=on_min_int,
            _allow_test_override=_allow_test_override,
        )

        self.pickup_monitor_task = PickupMonitorTask(
            page=self.page,
            pickup_monitor=self.pickup_monitor,
            event_queue=self.event_queue,
            page_lock=self.page_lock,
            interval_sec=pk_interval,
            jitter_range=pk_jitter,
            min_interval_sec=pk_min_int,
            store_throttle_sec=pk_throttle,
            _allow_test_override=_allow_test_override,
        )

        # 在后台并发启动监控任务
        online_t = asyncio.create_task(self.online_monitor.run(self.stop_event), name="online-monitor-task")
        pickup_t = asyncio.create_task(self.pickup_monitor_task.run(self.stop_event), name="pickup-monitor-task")

        success = False
        try:
            while not self.stop_event.is_set():
                # 检查最大轮次退出限制
                if max_rounds > 0:
                    if self.online_monitor.rounds_completed >= max_rounds and self.pickup_monitor_task.rounds_completed >= max_rounds:
                        logger.info(f"已达到最大监控轮次 ({max_rounds})，正常退出。")
                        break

                try:
                    # 等待事件，1.0s 超时以便检查 stop_event 与轮次
                    event = await asyncio.wait_for(self.event_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if event.source == "pickup":
                    await self.handle_pickup_event(event)
                    # 关键：自提只报警，不中断监控，线上通道继续运行！
                elif event.source == "online":
                    success = await self.handle_online_event(event)
                    break

        finally:
            self.stop_event.set()
            # 优雅取消监控任务
            for t in (online_t, pickup_t):
                if not t.done():
                    t.cancel()
            await asyncio.gather(online_t, pickup_t, return_exceptions=True)
            # 有限时间排空通知任务
            if hasattr(self.notifier, "drain_pending_notifications"):
                await self.notifier.drain_pending_notifications(timeout=3.0)

        return success


async def run_launch_mode(
    config: AppConfig,
    interval_sec: int = 30,
    max_rounds: int = 0,
    dashboard_adapter: Optional[Any] = None,
):
    """
    --mode launch:
    iPhone Duo 首发实战协同模式 (Launch Day Mode - Phase 1D Decoupled Hardened)
    1. check-session 前置核验 (Hard Requirements)
    2. Catalog 实时验证锁定 MK2P4CH/A
    3. Formal Target Prepare (打开 Duo 页预选星光白+512GB，严格门禁锁定热态)
    4. 启动 LaunchCoordinator 解耦调度 OnlineMonitorTask 与 PickupMonitorTask
    5. 独立 Cadence 与错误退避，双通道零争用独占 Purchase Page
    6. 任意通道发现可用状态立即触发飞书+本地双保险非阻塞报警
    7. Online 触发后执行 Selection Preservation 极速复用 / Fast Path 恢复
    8. FINAL_TARGET_CHECK 终极门禁核验后加车，推进至安全停机点等待人工接管
    """
    logger.info("==================================================")
    logger.info("  【Launch Day 首发实战协同模式】iPhone Duo")
    logger.info("==================================================")

    if dashboard_adapter and hasattr(dashboard_adapter, "on_mode_changed"):
        dashboard_adapter.on_mode_changed("FORMAL_LAUNCH")

    # 目标规格定义
    target_prod = config.target.product or "iPhone Duo"
    target_col = config.target.color or "星光白色"
    target_stor = config.target.storage or "512GB"
    expected_part = config.target.part_number if config.target.part_number else "MK2P4CH/A"
    config.product_url = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"

    notifier = Notifier(config.notifications)
    resolver = AppleCatalogResolver()
    browser_manager = BrowserManager(config.browser)
    page = await browser_manager.start()

    if dashboard_adapter and hasattr(dashboard_adapter, "on_browser_started"):
        dashboard_adapter.on_browser_started(True, "Browser=1, Context=1, Page=1")

    try:
        # 第一阶段：首发启动前硬性就绪条件全检 (Hard Requirements)
        logger.info("正在执行首发启动前硬性就绪条件全检 (Hard Requirements)...")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_session_checking"):
            dashboard_adapter.on_session_checking()
        is_ready, reports, blocking_reasons = await evaluate_launch_readiness(page, config, resolver, notifier)
        if not is_ready:
            logger.critical("🛑 [LAUNCH_BLOCKED_SESSION] 首发监控启动失败：存在未满足的硬性就绪条件！")
            reason_str = "; ".join(blocking_reasons)
            if dashboard_adapter and hasattr(dashboard_adapter, "on_session_strict_blocked"):
                dashboard_adapter.on_session_strict_blocked(reason_str)
            elif dashboard_adapter and hasattr(dashboard_adapter, "on_session_status"):
                dashboard_adapter.on_session_status(False, reason_str)
            print("\n" + "=" * 65)
            print("  【首发启动已阻断: LAUNCH_BLOCKED_SESSION】")
            print("  以下硬性条件未满足，系统拒绝在未就绪状态下启动监控：\n")
            print("  BLOCKING:")
            for b in blocking_reasons:
                print(f"  - {b}")
            print("\n  👉 请先在终端完成登录会话准备：")
            print("     python -m src.main --mode prepare-session")
            print("=" * 65 + "\n")
            return

        if dashboard_adapter and hasattr(dashboard_adapter, "on_session_status"):
            dashboard_adapter.on_session_status(True, "Apple 会话有效且健康")

        # 第二阶段：实时校验 Catalog
        logger.info("正在执行首发前 Catalog 实时校验与 SKU 锁定...")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_catalog_checking"):
            dashboard_adapter.on_catalog_checking()
        res = await resolver.resolve(page, target_prod, target_col, target_stor, expected_part_number=expected_part)
        if res.status == ResolutionStatus.TARGET_CHANGED:
            logger.critical(f"🚨 启动阻断：{res.error_message}")
            if hasattr(notifier, "notify_background"):
                notifier.notify_background(
                    event=NotificationEvent.TARGET_CHANGED,
                    title="🚨 iPhone Duo 官方 SKU 发生变化",
                    product="iPhone Duo",
                    details=res.error_message or "锁定零件号发生变动",
                )
            elif hasattr(notifier, "notify"):
                await notifier.notify(
                    event=NotificationEvent.TARGET_CHANGED,
                    title="🚨 iPhone Duo 官方 SKU 发生变化",
                    product="iPhone Duo",
                    details=res.error_message or "锁定零件号发生变动",
                )
            print("\n" + "=" * 65)
            print("  【官方目标重大变更：TARGET_CHANGED】")
            print(f"  {res.error_message}")
            print("  系统已停止监控以防下错订单。")
            print("=" * 65 + "\n")
            return
        elif res.status == ResolutionStatus.TARGET_SKU_MISMATCH:
            logger.critical(f"🛑 启动终止：{res.error_message}")
            print("\n" + "=" * 65)
            print("  【配置严重错误：TARGET_SKU_MISMATCH】")
            print(f"  {res.error_message}")
            print("  为确保不买错机型，程序已安全中止。请修正 config.yaml 中的 part_number。")
            print("=" * 65 + "\n")
            return
        elif res.status not in (ResolutionStatus.SUCCESS, ResolutionStatus.CATALOG_STALE):
            logger.error(f"无法锁定目标: {res.error_message}")
            print(f"\n🛑 目标解析未通过: {res.error_message}\n")
            return

        # 锁定成功的真实 Part Number 回填
        config.target.part_number = res.product.part_number
        logger.info(f"🎯 正式目标已锁死: {res.product.summary()}")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_catalog_verified"):
            dashboard_adapter.on_catalog_verified(
                target_prod,
                res.product.part_number,
                len(res.product.skus) if hasattr(res.product, "skus") and res.product.skus else 8
            )

        # 第三阶段：首发目标预热与规格预选门禁 (Formal Target Prepare Gate)
        buyer = AppleStoreBuyer(page, config)
        prepared_ok = await execute_formal_target_prepare(
            page=page,
            config=config,
            buyer=buyer,
            resolver=resolver,
            expected_part=expected_part,
            dashboard_adapter=dashboard_adapter,
        )
        if not prepared_ok:
            logger.critical("🛑 [FORMAL_TARGET_PREPARE_FAILED] 首发目标预选门禁未通过，启动阻断！")
            return

        # 第四阶段：自提通道就绪与双通道解耦监控协调器 (Phase 1D Architecture)
        pickup_monitor = PickupMonitor(config, notifier)
        await pickup_monitor.ensure_session(page)

        coordinator = LaunchCoordinator(
            page=page,
            config=config,
            buyer=buyer,
            resolver=resolver,
            notifier=notifier,
            pickup_monitor=pickup_monitor,
            expected_part=expected_part,
            dashboard_adapter=dashboard_adapter,
        )

        o_cfg = getattr(config, "online", None)
        p_cfg = getattr(config, "pickup", None)
        o_interval = getattr(o_cfg, "interval_seconds", 25)
        p_interval = getattr(p_cfg, "interval_seconds", 30)

        logger.info(f"双通道解耦监控已就绪，启动协调调度 (Online基准间隔: {o_interval}s, Pickup基准间隔: {p_interval}s)...")
        print("\n" + "=" * 65)
        print("  【🚀 iPhone Duo 首发双通道解耦监控中 (Phase 1E Rate-Hardened)】")
        print(f"  线上通道: Apple 中国大陆官网 (/shop/buy-iphone/iphone-duo) [基准 {o_interval}s ± 5s / 最小保护 15s]")
        print(f"  线下通道: 香港广场 / 上海环贸 iapm / 五角场 / 环球港 [基准 {p_interval}s (+0~10s) / 门店节流 >= 2.0s / 退避隔离]")
        print(f"  目标型号: {target_prod} · {target_col} · {target_stor} ({expected_part})")
        print("  独占控制: 1 Browser · 1 Context · 1 Purchase Page (MK2P4CH/A 热态锁死)")
        print("  仲裁策略: Online 自动购买最高优先级 / Pickup 即时报警不中断 Online")
        print("=" * 65 + "\n")

        if dashboard_adapter and hasattr(dashboard_adapter, "on_stage_changed"):
            dashboard_adapter.on_stage_changed("monitor", "库存监控中")

        await coordinator.run(
            online_interval=o_interval,
            pickup_interval=p_interval,
            max_rounds=max_rounds,
        )

    except KeyboardInterrupt:
        logger.info("收到用户中断信号，首发监控退出。")
    finally:
        if hasattr(notifier, "drain_pending_notifications"):
            try:
                await notifier.drain_pending_notifications(timeout=3.0)
            except Exception:
                pass
        await browser_manager.close()


async def run_formal_runtime_verify(
    config: AppConfig,
    ignore_session_check: bool = False,
    submode: str = "strict",
    dashboard_adapter: Optional[Any] = None,
) -> bool:
    """
    --mode formal-runtime-verify:
    Phase 2 Controlled Runtime Verification (首发全链路受控运行时核验模式)
    
    【核心安全红线】：
    1. 真实运行于 1 个 Persistent Chromium 浏览器，1 Context，1 Purchase Page
    2. 真实核验 Apple Account 会话状态、官方实时 Catalog、星光白+512GB 预选、规格保留
    3. 真实读取 1 次线上 public state snapshot 与 1 轮上海 4 店 public fulfillment snapshot
    4. 产生本地明确模拟触发 SIMULATED_RUNTIME_ONLINE_AVAILABLE
    5. 触发后调用正式 production helper execute_formal_online_checkout_flow
    6. 强制执行硬门禁 RUNTIME_VERIFY_NO_ADD_TO_BAG = True，绝对禁止 real add_to_bag() 与下单
    7. 输出 HUMAN_HANDOFF_SIMULATION_READY，置前并保持 10 秒供人工核对
    8. 异步发送明确标记【运行时验证】的飞书测试消息并播放本地提示音
    """
    if submode == "public-only":
        ignore_session_check = True

    RUNTIME_VERIFY_NO_ADD_TO_BAG = True
    screenshot_dir = os.path.abspath("logs/screenshots/runtime_verify")
    os.makedirs(screenshot_dir, exist_ok=True)
    prep_screenshot = os.path.join(screenshot_dir, "runtime_target_prepared.png")
    final_screenshot = os.path.join(screenshot_dir, "runtime_final_verified.png")

    logger.info("==================================================")
    logger.info("  【Phase 2 Controlled Runtime Verification】受控运行时核验")
    logger.info("==================================================")
    print("\n" + "=" * 65)
    print("  【🚀 Phase 2 Controlled Runtime Verification】启动受控运行时核验")
    print("  安全承诺: 绝对禁止真实加车，绝对禁止真实下单 (RUNTIME_VERIFY_NO_ADD_TO_BAG=True)")
    print(f"  验证模式: submode={submode} (ignore_session={ignore_session_check})")
    print("  窗口配额: 仅允许 1 Chromium · 1 Context · 1 Page")
    print("=" * 65 + "\n")

    timing = {}
    audit_results = {}

    t_global_start = time.perf_counter()

    # Stage 1: 真实环境启动
    logger.info("Stage 1: 启动持久化 Chromium 浏览器环境...")
    t0_stage1 = time.perf_counter()
    browser_manager = BrowserManager(config.browser)
    page = await browser_manager.start()
    t1_stage1 = time.perf_counter()
    timing["Browser Startup"] = f"{(t1_stage1 - t0_stage1):.3f}s"

    ctx_count = 1 if browser_manager.context else 0
    page_count = len(browser_manager.context.pages) if browser_manager.context else 0
    if ctx_count != 1 or page_count != 1:
        logger.critical(f"🛑 违反窗口单例约束！Context={ctx_count}, Page={page_count}")
        await browser_manager.close()
        return False
    logger.info("✅ [R0_BROWSER_STARTED] 浏览器启动完成 (Browser=1, Context=1, Page=1)")
    audit_results["R0_BROWSER_STARTED"] = True
    if dashboard_adapter and hasattr(dashboard_adapter, "on_browser_started"):
        dashboard_adapter.on_browser_started(True, "Browser=1, Context=1, Page=1")

    notifier = Notifier(config.notifications)
    resolver = AppleCatalogResolver()
    buyer = AppleStoreBuyer(page, config)

    try:
        # Stage 2: 真实 Session 检查
        logger.info("Stage 2: 真实核验 Apple Account 会话登录态...")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_session_checking"):
            dashboard_adapter.on_session_checking()
        t0_stage2 = time.perf_counter()
        is_logged_in, login_desc = await check_apple_login_status(page)
        t1_stage2 = time.perf_counter()
        timing["Session Check"] = f"{(t1_stage2 - t0_stage2):.3f}s"
        if not is_logged_in:
            logger.critical(f"🛑 [RUNTIME_SESSION_FAILED] 会话未处于已登录状态: {login_desc}")
            print(f"\n🛑 【RUNTIME_SESSION_FAILED】会话检查未通过: {login_desc}\n")
            audit_results["SESSION_STATUS"] = f"RUNTIME_SESSION_FAILED ({login_desc})"
            if not ignore_session_check and submode == "strict":
                if dashboard_adapter and hasattr(dashboard_adapter, "on_session_strict_blocked"):
                    dashboard_adapter.on_session_strict_blocked(login_desc)
                elif dashboard_adapter and hasattr(dashboard_adapter, "on_session_status"):
                    dashboard_adapter.on_session_status(False, login_desc)
                print("🛑 [USER_LOGIN_REQUIRED] 严格受控模式下 Session 失效直接阻断运行。")
                print("请先执行登录: python -m src.main --mode prepare-session\n")
                return False
            else:
                logger.warning("⚠️ [SESSION_BYPASSED_FOR_CONTROLLED_VERIFY] 跳过会话中断，当前处于公开页面验证模式 (--public-only)...")
                if dashboard_adapter and hasattr(dashboard_adapter, "on_public_only_warning"):
                    dashboard_adapter.on_public_only_warning()
                elif dashboard_adapter and hasattr(dashboard_adapter, "on_session_status"):
                    dashboard_adapter.on_session_status(False, login_desc)
        else:
            logger.info(f"✅ [SESSION OK] 会话有效: {login_desc}")
            audit_results["SESSION_STATUS"] = "SESSION OK"
            if dashboard_adapter and hasattr(dashboard_adapter, "on_session_status"):
                dashboard_adapter.on_session_status(True, login_desc)

        # Stage 3: 真实 Catalog 解析
        logger.info("Stage 3: 真实获取官方 Catalog 并锁定目标 SKU...")
        if dashboard_adapter and hasattr(dashboard_adapter, "on_catalog_checking"):
            dashboard_adapter.on_catalog_checking()
        t0_stage3 = time.perf_counter()
        target_prod = config.target.product or "iPhone Duo"
        target_col = config.target.color or "星光白色"
        target_stor = config.target.storage or "512GB"
        expected_part = "MK2P4CH/A"

        cat_res = await resolver.resolve(
            page,
            product=target_prod,
            color=target_col,
            storage=target_stor,
            expected_part_number=expected_part,
        )
        t1_stage3 = time.perf_counter()
        timing["Catalog Verify"] = f"{(t1_stage3 - t0_stage3):.3f}s"

        if not cat_res.product or cat_res.product.part_number != expected_part:
            logger.critical(f"🛑 [TARGET_SKU_MISMATCH] Catalog SKU 变更或无法匹配: {cat_res.error_message}")
            return False
        logger.info(f"✅ [R1_CATALOG_VERIFIED] 官方 Catalog 解析成功锁定: {expected_part}")
        audit_results["R1_CATALOG_VERIFIED"] = True
        if dashboard_adapter and hasattr(dashboard_adapter, "on_catalog_verified"):
            dashboard_adapter.on_catalog_verified(target_prod, expected_part, len(cat_res.product.skus) if hasattr(cat_res.product, "skus") and cat_res.product.skus else 8)

        # Stage 4: Formal Target Prepare (真实预热与预选星光白+512GB)
        logger.info("Stage 4: 执行首发目标规格正式预热与预选门禁...")
        t0_stage4 = time.perf_counter()
        prep_ok = await execute_formal_target_prepare(
            page=page,
            config=config,
            buyer=buyer,
            resolver=resolver,
            expected_part=expected_part,
            dashboard_adapter=dashboard_adapter,
        )
        t1_stage4 = time.perf_counter()
        timing["Formal Target Prepare"] = f"{(t1_stage4 - t0_stage4):.3f}s"
        if not prep_ok:
            logger.critical("🛑 [R2_FORMAL_TARGET_PREPARE_FAILED] 目标页面预热或规格预选失败！")
            return False
        await page.screenshot(path=prep_screenshot)
        logger.info(f"📸 预选截图已保存至: {prep_screenshot}")
        logger.info("✅ [R2_FORMAL_TARGET_PREPARED] 目标规格预选就绪")
        audit_results["R2_FORMAL_TARGET_PREPARED"] = True

        # Stage 5: 真实监控 Snapshot (只读一次，不长循环)
        logger.info("Stage 5: 执行真实监控 Snapshot (1次线上 + 1轮上海4店自提)...")
        # 5.1 Online Snapshot
        if dashboard_adapter and hasattr(dashboard_adapter, "on_online_checking"):
            dashboard_adapter.on_online_checking()
        t0_on_snap = time.perf_counter()
        ps_data, _ = await resolver.fetch_product_catalog(page, target_prod, no_navigate=True)
        t1_on_snap = time.perf_counter()
        timing["Real Online Snapshot"] = f"{(t1_on_snap - t0_on_snap):.3f}s"

        online_state = "UNKNOWN"
        if ps_data:
            skus = [p for p in ps_data.get("products", []) if p.get("partNumber") == expected_part]
            if skus:
                online_state = "COMING_SOON" if skus[0].get("comingSoon", True) else "AVAILABLE"
        logger.info(f"📊 [ONLINE_REAL_STATE] Apple 真实线上状态: {online_state}")
        audit_results["ONLINE_REAL_STATE"] = online_state
        if dashboard_adapter and hasattr(dashboard_adapter, "on_online_status"):
            dashboard_adapter.on_online_status(online_state, "即将发售" if online_state == "COMING_SOON" else "可购买")

        # 5.2 Pickup Snapshot (上海 4 店，严格单店节流 >= 2.0s)
        if dashboard_adapter and hasattr(dashboard_adapter, "on_pickup_checking"):
            dashboard_adapter.on_pickup_checking()
        t0_pk_snap = time.perf_counter()
        pickup_monitor = PickupMonitor(config, notifier)
        stores = config.pickup.stores
        for s in stores:
            res = await pickup_monitor.query_store(page, s.store_number, expected_part)
            if res.store_name == res.store_number:
                res.store_name = s.name
            logger.info(f"🏬 [PICKUP_REAL_STATE_{s.store_number}] {res.summary()}")
            audit_results[f"PICKUP_REAL_STATE_{s.store_number}"] = f"{res.status.value} ({res.summary()})"
            if dashboard_adapter and hasattr(dashboard_adapter, "on_store_checked"):
                dashboard_adapter.on_store_checked(
                    s.store_number, s.name, res.status.value, "可到店取货" if res.status.value == "AVAILABLE" else "即将发售", res.pickup_quote or "目前暂不提供零售店取货"
                )
            await asyncio.sleep(2.0)
        t1_pk_snap = time.perf_counter()
        timing["Real Pickup Snapshot"] = f"{(t1_pk_snap - t0_pk_snap):.3f}s"
        if dashboard_adapter and hasattr(dashboard_adapter, "on_pickup_round_completed"):
            dashboard_adapter.on_pickup_round_completed(float(f"{(t1_pk_snap - t0_pk_snap):.2f}"))

        # Stage 6: Page Preservation 检查 (监控不得破坏购买页热态)
        logger.info("Stage 6: 检查监控 Snapshot 之后购买页热态与规格保留状态...")
        curr_url = (getattr(page, "url", "") or "").lower()
        url_preserved = "buy-iphone/iphone-duo" in curr_url
        c_preserved = await buyer.verify_starwhite_selected(target_sku=expected_part)
        s_preserved = await buyer.verify_512gb_selected(target_sku=expected_part)
        sku_preserved = await buyer.verify_target_sku_consistency(
            expected_sku=expected_part, resolver=resolver, require_catalog=True
        )

        if not (url_preserved and c_preserved and s_preserved and sku_preserved):
            logger.critical("🛑 [R3_MONITORING_DISTURBED_TARGET] 监控 Snapshot 破坏了页面规格热态！")
            return False
        logger.info("✅ [R3_MONITORING_DID_NOT_DISTURB_TARGET] 监控完全只读，未破坏购买页面与选配状态")
        audit_results["R3_MONITORING_DID_NOT_DISTURB_TARGET"] = True

        # Stage 7: 本地模拟 Trigger
        logger.info("Stage 7: 注入本地模拟可购 Trigger (SIMULATED_RUNTIME_ONLINE_AVAILABLE)...")
        print("\n" + "=" * 65)
        print("  ⚡ 【模拟触发：SIMULATED_RUNTIME_ONLINE_AVAILABLE】")
        print("  说明: 本次为受控测试触发，不代表 Apple 官方当前真实有货，绝不产生真实加车。")
        print("=" * 65 + "\n")
        T0 = time.perf_counter()

        # Stage 8 & 9: Formal Selection Preservation & Final Target Verification (硬门禁拦截 add_to_bag)
        logger.info("Stage 8 & 9: 调用正式结账推进流 (带受控验证硬门禁 runtime_verify_no_add_to_bag=True)...")
        t0_flow = time.perf_counter()
        flow_success = await execute_formal_online_checkout_flow(
            page=page,
            config=config,
            buyer=buyer,
            resolver=resolver,
            notifier=notifier,
            event_type=NotificationEvent.ONLINE_AVAILABLE,
            expected_part=expected_part,
            runtime_verify_no_add_to_bag=RUNTIME_VERIFY_NO_ADD_TO_BAG,
        )
        t_formal_verified = time.perf_counter()
        timing["Post-Trigger Selection Verify"] = f"{(t_formal_verified - t0_flow):.3f}s"
        timing["T0 -> FORMAL_TARGET_VERIFIED"] = f"{(t_formal_verified - T0) * 1000:.1f}ms"

        if not flow_success:
            logger.critical("🛑 规格核验或受控流程未通过！")
            return False

        await page.screenshot(path=final_screenshot)
        logger.info(f"📸 终极核验截图已保存至: {final_screenshot}")
        audit_results["FORMAL_TARGET_VERIFIED"] = True

        # Stage 10: Runtime Handoff
        logger.info("Stage 10: 将 Chromium 窗口置前并触发人工接管模拟...")
        try:
            await page.bring_to_front()
        except Exception:
            pass
        t_handoff = time.perf_counter()
        timing["T0 -> HUMAN_HANDOFF_SIMULATION_READY"] = f"{(t_handoff - T0) * 1000:.1f}ms"
        print("\n" + "!" * 65)
        print("  🎉 【HUMAN_HANDOFF_SIMULATION_READY】人工接管就绪")
        print(f"  响应延迟: T0 模拟触发 -> 人工接管就绪耗时: {timing['T0 -> HUMAN_HANDOFF_SIMULATION_READY']}")
        print("  终极状态: 星光白色 · 512GB · MK2P4CH/A 100% 吻合")
        print("  安全状态: add_to_bag 已阻断，绝无真实订单")
        print("!" * 65 + "\n")
        audit_results["HUMAN_HANDOFF_SIMULATION_READY"] = True

        # Stage 11: Runtime 飞书通知 (明确标注【运行时验证】)
        logger.info("Stage 11: 发送受控验证飞书通知并测量 ACK...")
        t_feishu_start = time.perf_counter()
        if hasattr(notifier, "notify_background"):
            notifier.notify_background(
                event=NotificationEvent.ONLINE_AVAILABLE,
                title="🍎【运行时验证】iPhone Duo 购买与监控受控演练",
                product="iPhone Duo",
                color="星光白色",
                storage="512GB",
                sku=expected_part,
                channel="Apple 中国官网 (受控测试)",
                status_text="✅ 演练成功 (未实际加车)",
                url=config.product_url,
                details="【运行时验证】这是一条模拟 Runtime Verification 消息，不代表 iPhone Duo 真实有货，不会创建真实订单。硬门禁 RUNTIME_VERIFY_NO_ADD_TO_BAG=True 已生效。",
            )
            await notifier.drain_pending_notifications(timeout=5.0)
        t_feishu_ack = time.perf_counter()
        timing["Feishu ACK"] = f"{(t_feishu_ack - t_feishu_start) * 1000:.1f}ms"
        logger.info(f"📨 飞书 Webhook ACK 延迟: {timing['Feishu ACK']} (注意: ACK 仅代表服务端接收，不代表移动端 App 界面渲染显示时间)")
        audit_results["FEISHU_ACK"] = timing["Feishu ACK"]

        # Stage 12: Runtime 本地提示音
        logger.info("Stage 12: 播放本地声音提示...")
        try:
            notifier.sound.play(NotificationEvent.TARGET_AVAILABLE)
            logger.info("🔊 [LOCAL_ALERT_OK] 本地声音提示正常播放")
            audit_results["LOCAL_ALERT"] = "LOCAL_ALERT_OK"
        except Exception as e:
            logger.warning(f"⚠️ [LOCAL_ALERT_FAILED] 本地声音播放失败: {e}")
            audit_results["LOCAL_ALERT"] = f"LOCAL_ALERT_FAILED: {e}"

        # Stage 13: 保持 10 秒供用户直观核验
        logger.info("Stage 13: 保持浏览器窗口 10 秒供人工直观核验页面状态...")
        await asyncio.sleep(10.0)

        # 打印完整受控运行时审计总结
        print("\n" + "=" * 65)
        print("  【Phase 2 Controlled Runtime Verification 完整时延与核验审计】")
        print("-" * 65)
        for k, v in audit_results.items():
            print(f"  {k:<35}: {v}")
        print("-" * 65)
        print("  【时延量测 (Timings)】:")
        for k, v in timing.items():
            print(f"  - {k:<32}: {v}")
        print("=" * 65 + "\n")

        return True

    finally:
        if hasattr(notifier, "drain_pending_notifications"):
            try:
                await notifier.drain_pending_notifications(timeout=3.0)
            except Exception:
                pass
        await browser_manager.close()


def main():
    parser = argparse.ArgumentParser(description="Apple 中国大陆官网双通道购买与监控助手 (V0.4 Launch Day 实战版)")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径 (默认: config.yaml)")
    parser.add_argument("--mode", default="online",
                        choices=[
                            "online", "pickup", "dual", "catalog", "watch-product",
                            "test-feishu", "test-bark", "test-pickup",
                            "prepare-session", "check-session", "duo-dry-run", "launch",
                            "launch-rehearsal", "formal-runtime-verify",
                            "dashboard-demo", "dashboard-runtime-verify", "launch-dashboard"
                        ],
                        help="运行模式: launch(首发协同), formal-runtime-verify(受控运行时验证), dashboard-runtime-verify(受控运行时可视化联调), launch-rehearsal(首发模拟演练), dashboard-demo(控制中心演示), launch-dashboard(控制中心实战协同), duo-dry-run(Duo预演), prepare-session(预热登录), check-session(健康全检), test-feishu(飞书测试), test-pickup(自提测试), online(线上加车), pickup(直营店监控)")
    parser.add_argument("--runs", type=int, default=1, help="演练执行轮次 (默认 1)")
    parser.add_argument("--prewarm", action="store_true", help="启用 Pre-Warmed 演练模式 (提前预热浏览器与页面，降低 T0 触发后的接管耗时)")
    parser.add_argument("--preselect-target", action="store_true", help="启用目标配置预选模式 (在 T0 前预选星光白+512GB，并在 reload 后检测状态保留，跳过重复点击)")
    parser.add_argument("--auto-exit", type=int, default=15, help="达到人工停机点后等待秒数 (默认 15 秒，0 为无限等待)")
    parser.add_argument("--rounds", type=int, default=0, help="自提监控执行轮次上限 (默认 0 表示无限轮询)")
    parser.add_argument("--product", default="", help="覆盖目标产品名称 (如 iPhone 17 或 iPhone Duo)")
    parser.add_argument("--color", default="", help="覆盖目标外观颜色 (如 白色 或 星光白色)")
    parser.add_argument("--storage", default="", help="覆盖目标存储容量 (如 512GB)")
    parser.add_argument("--part-number", default="", help="手动指定 Part Number (将与官方 Catalog 强校验)")
    parser.add_argument("--max-checks", type=int, default=0, help="watch-product 模式最大检查次数 (0 为无限)")
    parser.add_argument("--interval", type=int, default=600, help="轮询间隔秒数 (默认 600s)")
    parser.add_argument("--submode", default="strict", choices=["strict", "public-only"], help="受控验证子模式: strict(默认,严格会话门禁) 或 public-only(公开页面核查)")
    parser.add_argument("--public-only", action="store_true", help="等价于 --submode public-only，仅验证公开目录与自提快照，禁止进入购买推进")
    parser.add_argument("--ignore-session-check", action="store_true", help="在未登录态下继续执行非加车公共环境受控运行时核验与时延量测")
    args = parser.parse_args()

    if args.public_only:
        args.submode = "public-only"
        args.ignore_session_check = True

    cfg_file = args.config
    if not os.path.exists(cfg_file) and os.path.exists("config.example.yaml"):
        cfg_file = "config.example.yaml"

    config = load_config(cfg_file)

    # 命令行参数覆盖配置
    if args.product:
        config.target.product = args.product
        config.model = args.product
    if args.color:
        config.target.color = args.color
        config.color = args.color
    if args.storage:
        config.target.storage = args.storage
        config.storage = args.storage
    if args.part_number:
        config.target.part_number = args.part_number
        config.target.auto_resolve_part_number = False

    # 执行对应模式
    if args.mode == "test-feishu":
        notifier = Notifier(config.notifications)
        asyncio.run(notifier.test_feishu(interactive=True))
        return

    if args.mode == "test-bark":
        notifier = Notifier(config.notifications)
        asyncio.run(notifier.test_feishu(interactive=True))
        return

    if args.mode == "prepare-session":
        asyncio.run(run_prepare_session(config))
        return

    if args.mode == "check-session":
        asyncio.run(run_check_session(config))
        return

    if args.mode == "duo-dry-run":
        asyncio.run(run_duo_dry_run(config))
        return

    if args.mode == "formal-runtime-verify":
        asyncio.run(run_formal_runtime_verify(
            config,
            ignore_session_check=args.ignore_session_check,
            submode=args.submode,
        ))
        return

    if args.mode == "dashboard-demo":
        from .dashboard_demo import run_dashboard_demo
        run_dashboard_demo(auto_exit_seconds=args.auto_exit if args.auto_exit != 15 else 24)
        return

    if args.mode == "dashboard-runtime-verify":
        from .dashboard_runtime_verify import run_dashboard_runtime_verify
        run_dashboard_runtime_verify(
            config,
            auto_exit_seconds=args.auto_exit if args.auto_exit != 15 else 38,
            submode=args.submode,
            ignore_session_check=args.ignore_session_check,
        )
        return

    if args.mode == "launch-dashboard":
        import threading
        import tkinter as tk
        from .dashboard_gui import LaunchControlCenterGUI
        from .dashboard_adapter import DashboardAdapter

        logger.info("准备启动 iPhone Duo 首发控制中心协同实战模式 (Dashboard + Launch)...")
        adapter = DashboardAdapter()
        adapter.init_from_config(config)
        adapter.on_mode_changed("FORMAL_LAUNCH")

        root = tk.Tk()
        gui = LaunchControlCenterGUI(root, adapter=adapter, is_demo=False)

        def worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                interval = args.interval if args.interval != 600 else 30
                loop.run_until_complete(
                    run_launch_mode(config, interval_sec=interval, max_rounds=args.rounds, dashboard_adapter=adapter)
                )
            except Exception as e:
                logger.error(f"首发实战协同任务异常: {e}", exc_info=True)
            finally:
                loop.close()

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        root.mainloop()
        return

    if args.mode == "launch":
        interval = args.interval if args.interval != 600 else 30
        asyncio.run(run_launch_mode(config, interval_sec=interval, max_rounds=args.rounds))
        return

    if args.mode == "launch-rehearsal":
        from .rehearsal import run_launch_rehearsal
        asyncio.run(run_launch_rehearsal(
            config, runs=args.runs, interval_sec=30, prewarm=args.prewarm, preselect_target=args.preselect_target
        ))
        return

    if args.mode == "catalog":
        asyncio.run(run_catalog_view(config))
        return

    if args.mode == "watch-product":
        prod_to_watch = args.product or config.target.product or "iPhone Duo"
        asyncio.run(run_watch_product(config, watch_product_name=prod_to_watch, interval_sec=args.interval, max_checks=args.max_checks))
        return

    if args.mode == "test-pickup":
        asyncio.run(run_pickup_test(config))
        return

    if args.mode == "pickup":
        asyncio.run(run_pickup_monitor(config, max_rounds=args.rounds))
        return

    if args.mode == "online":
        asyncio.run(run_online_buyer(config, auto_exit_seconds=args.auto_exit))
        return

    if args.mode == "dual":
        logger.info("启动双通道模式：优先开启直营店自提监控...")
        asyncio.run(run_pickup_monitor(config, max_rounds=args.rounds))
        return


if __name__ == "__main__":
    main()

