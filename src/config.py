"""配置加载与校验模块 (V0.4 Launch Day 实战版)

支持：
1. 目标机型与规格配置 (TargetConfig)，默认锁定 iPhone Duo · 星光白色 · 512GB (MK2P4CH/A)
2. 线下直营店自提监控配置 (PickupConfig, StoreConfig)
3. 飞书自定义机器人 Webhook 与本地声音提醒配置 (FeishuConfig, SoundConfig, NotificationConfig)
   - 支持从环境变量 FEISHU_WEBHOOK 读取 (最高优先级)
   - 包含超低延迟、超时重试与凭证安全脱敏
4. 飞书 Webhook 凭证脱敏安全保护
5. 向下兼容 V0.1 ~ V0.3
"""

import os
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
import yaml

def mask_feishu_webhook(url: str) -> str:
    """
    对飞书自定义机器人 Webhook URL 实施严格脱敏保护，严禁输出完整 Key / Token 至日志、截屏或文档中。
    展示示例: https://open.feishu.cn/.../12****01
    """
    if not url:
        return "(未配置)"
    url_clean = url.strip()
    if "/" in url_clean:
        parts = url_clean.rsplit("/", 1)
        base = parts[0]
        token = parts[1]
        if len(token) > 6:
            masked_token = token[:2] + "****" + token[-2:]
        else:
            masked_token = "****"
        return f"https://open.feishu.cn/.../{masked_token}"
    else:
        if len(url_clean) > 6:
            return url_clean[:2] + "****" + url_clean[-2:]
        return "****"

def mask_credential(url: str) -> str:
    """兼容旧 Bark 凭证脱敏保护"""
    if not url:
        return "(未配置)"
    url_clean = url.strip()
    if "/" in url_clean:
        parts = url_clean.rsplit("/", 1)
        base = parts[0]
        key = parts[1]
        if len(key) > 6:
            masked_key = key[:2] + "****" + key[-2:]
        else:
            masked_key = "****"
        return f"{base}/{masked_key}"
    else:
        if len(url_clean) > 6:
            return url_clean[:2] + "****" + url_clean[-2:]
        return "****"

@dataclass
class BrowserConfig:
    headless: bool = False
    user_data_dir: str = "./browser_data"
    viewport_width: int = 1440
    viewport_height: int = 900
    action_delay_min_ms: int = 300
    action_delay_max_ms: int = 800
    timeout_ms: int = 30000

@dataclass
class SafetyConfig:
    stop_at_login: bool = True
    stop_at_captcha: bool = True
    stop_at_2fa: bool = True
    stop_at_checkout_confirmation: bool = True
    stop_at_payment: bool = True

@dataclass
class FeishuConfig:
    enabled: bool = True
    webhook_url: str = ""
    timeout_seconds: int = 8

    @property
    def masked_url(self) -> str:
        return mask_feishu_webhook(self.webhook_url)

@dataclass
class BarkConfig:
    enabled: bool = False
    base_url: str = ""
    repeat: int = 3
    level: str = "timeSensitive"
    volume: int = 5
    group: str = "iphone-duo-buyer"

    @property
    def masked_url(self) -> str:
        return mask_credential(self.base_url)

@dataclass
class SoundConfig:
    enabled: bool = True

@dataclass
class NotificationConfig:
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    sound: SoundConfig = field(default_factory=SoundConfig)
    bark: BarkConfig = field(default_factory=BarkConfig)
    bark_url: str = ""  # 兼容旧字段

@dataclass
class TargetConfig:
    product: str = "iPhone Duo"
    model: str = "iPhone Duo"
    color: str = "星光白色"
    storage: str = "512GB"
    part_number: Optional[str] = None
    auto_resolve_part_number: bool = True

@dataclass
class StoreConfig:
    name: str
    store_number: str

@dataclass
class OnlineConfig:
    interval_seconds: int = 25
    jitter_seconds: int = 5
    min_interval_seconds: float = 15.0
    max_consecutive_failures: int = 5

@dataclass
class LaunchConfig:
    launch_time: Optional[str] = None  # e.g. "2026-09-18T20:00:00+08:00" or None

# 本项目官方目标 4 店权威映射 (Authoritative Store Mappings)
OFFICIAL_SHANGHAI_STORES: Dict[str, str] = {
    "R390": "香港广场",
    "R401": "上海环贸 iapm",
    "R581": "五角场",
    "R683": "环球港",
}

def get_authoritative_store_name(store_number: str) -> Optional[str]:
    """返回官方权威门店名称，未知 store_number 返回 None，严禁静默映射"""
    return OFFICIAL_SHANGHAI_STORES.get(store_number)


@dataclass
class PickupConfig:
    enabled: bool = True
    region: str = "CN"
    city: str = "上海"
    stores: List[StoreConfig] = field(default_factory=lambda: [
        StoreConfig(name="香港广场", store_number="R390"),
        StoreConfig(name="上海环贸 iapm", store_number="R401"),
        StoreConfig(name="五角场", store_number="R581"),
        StoreConfig(name="环球港", store_number="R683"),
    ])
    interval_seconds: int = 30
    jitter_seconds: int = 10
    min_interval_seconds: float = 20.0
    store_throttle_seconds: float = 2.0
    max_consecutive_failures: int = 5

    def __post_init__(self):
        seen = set()
        for s in self.stores:
            if s.store_number in seen:
                raise ValueError(f"发现重复的门店编号: {s.store_number}")
            seen.add(s.store_number)

@dataclass
class AppConfig:
    product_url: str = "https://www.apple.com.cn/shop/buy-iphone/iphone-duo"
    model: str = "iPhone Duo"
    color: str = "星光白色"
    storage: str = "512GB"
    purchase_mode: str = "delivery"  # "delivery" or "pickup"
    target: TargetConfig = field(default_factory=TargetConfig)
    online: OnlineConfig = field(default_factory=OnlineConfig)
    pickup: PickupConfig = field(default_factory=PickupConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    launch: LaunchConfig = field(default_factory=LaunchConfig)


def load_config(config_path: str = "config.yaml") -> AppConfig:
    """加载 YAML 配置文件并融合环境变量，返回 AppConfig 对象"""
    raw_data: Dict[str, Any] = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            raw_data = yaml.safe_load(f) or {}
    elif os.path.exists("config.example.yaml"):
        with open("config.example.yaml", "r", encoding="utf-8") as f:
            raw_data = yaml.safe_load(f) or {}

    # 1. 目标机型配置 (默认锁定 iPhone Duo · 星光白色 · 512GB)
    target_raw = raw_data.get("target", {})
    model_val = target_raw.get("model") or raw_data.get("model", "iPhone Duo")
    color_val = target_raw.get("color") or raw_data.get("color", "星光白色")
    storage_val = target_raw.get("storage") or raw_data.get("storage", "512GB")
    product_val = target_raw.get("product") or raw_data.get("product", "iPhone Duo")
    part_num_val = target_raw.get("part_number") or raw_data.get("part_number")
    # 如果用户显式设置为 null/None 或留空
    if isinstance(part_num_val, str) and not part_num_val.strip():
        part_num_val = None

    auto_resolve_val = target_raw.get("auto_resolve_part_number", True)

    target_cfg = TargetConfig(
        product=product_val,
        model=model_val,
        color=color_val,
        storage=storage_val,
        part_number=part_num_val,
        auto_resolve_part_number=auto_resolve_val,
    )

    # 2. 线下直营店自提配置
    pickup_raw = raw_data.get("pickup", {})
    stores_raw = pickup_raw.get("stores", [])
    store_list: List[StoreConfig] = []
    if stores_raw:
        seen_numbers = set()
        for s in stores_raw:
            if isinstance(s, dict):
                st_num = str(s.get("store_number", "")).strip()
                if not st_num:
                    continue
                if st_num in seen_numbers:
                    raise ValueError(f"配置错误: 发现重复的门店编号 '{st_num}'，严禁配置重复门店")
                seen_numbers.add(st_num)
                st_name = str(s.get("name", "")).strip()
                auth_name = OFFICIAL_SHANGHAI_STORES.get(st_num)
                if auth_name:
                    if not st_name or st_name in ("上海香港广场", "上海五角场", "上海环球港", "环贸 iapm", auth_name):
                        st_name = auth_name
                store_list.append(StoreConfig(
                    name=st_name or st_num,
                    store_number=st_num
                ))
    else:
        store_list = [
            StoreConfig(name="香港广场", store_number="R390"),
            StoreConfig(name="上海环贸 iapm", store_number="R401"),
            StoreConfig(name="五角场", store_number="R581"),
            StoreConfig(name="环球港", store_number="R683"),
        ]

    online_raw = raw_data.get("online", {})
    online_cfg = OnlineConfig(
        interval_seconds=int(online_raw.get("interval_seconds", 25)),
        jitter_seconds=int(online_raw.get("jitter_seconds", 5)),
        min_interval_seconds=float(online_raw.get("min_interval_seconds", 15.0)),
        max_consecutive_failures=int(online_raw.get("max_consecutive_failures", 5)),
    )

    pickup_cfg = PickupConfig(
        enabled=pickup_raw.get("enabled", True),
        region=pickup_raw.get("region", "CN"),
        city=pickup_raw.get("city", "上海"),
        stores=store_list,
        interval_seconds=int(pickup_raw.get("interval_seconds", 30)),
        jitter_seconds=int(pickup_raw.get("jitter_seconds", 10)),
        min_interval_seconds=float(pickup_raw.get("min_interval_seconds", 20.0)),
        store_throttle_seconds=float(pickup_raw.get("store_throttle_seconds", 2.0)),
        max_consecutive_failures=int(pickup_raw.get("max_consecutive_failures", 5)),
    )

    # 3. 浏览器配置
    browser_raw = raw_data.get("browser", {})
    vp_raw = browser_raw.get("viewport", {})
    browser_cfg = BrowserConfig(
        headless=browser_raw.get("headless", False),
        user_data_dir=browser_raw.get("user_data_dir", "./browser_data"),
        viewport_width=vp_raw.get("width", 1440),
        viewport_height=vp_raw.get("height", 900),
        action_delay_min_ms=browser_raw.get("action_delay_min_ms", 300),
        action_delay_max_ms=browser_raw.get("action_delay_max_ms", 800),
        timeout_ms=browser_raw.get("timeout_ms", 30000),
    )

    # 4. 安全红线配置
    safety_raw = raw_data.get("safety", {})
    safety_cfg = SafetyConfig(
        stop_at_login=safety_raw.get("stop_at_login", True),
        stop_at_captcha=safety_raw.get("stop_at_captcha", True),
        stop_at_2fa=safety_raw.get("stop_at_2fa", True),
        stop_at_checkout_confirmation=safety_raw.get("stop_at_checkout_confirmation", True),
        stop_at_payment=safety_raw.get("stop_at_payment", True),
    )

    # 5. 通知配置 (飞书 Webhook 优先 + Sound) - 支持环境变量注入
    notif_raw = raw_data.get("notifications", {})
    feishu_raw = notif_raw.get("feishu", {})

    # 环境变量 FEISHU_WEBHOOK 优先级最高
    env_feishu_url = os.environ.get("FEISHU_WEBHOOK", "").strip()
    config_feishu_url = feishu_raw.get("webhook_url", "").strip()
    final_feishu_url = env_feishu_url if env_feishu_url else config_feishu_url

    feishu_cfg = FeishuConfig(
        enabled=feishu_raw.get("enabled", True),
        webhook_url=final_feishu_url,
        timeout_seconds=int(feishu_raw.get("timeout_seconds", 8)),
    )

    # 兼容旧 Bark 配置
    bark_raw = notif_raw.get("bark", {})
    env_bark_url = os.environ.get("BARK_URL", "").strip()
    env_bark_key = os.environ.get("BARK_KEY", "").strip()
    config_bark_url = bark_raw.get("base_url") or notif_raw.get("bark_url", "")
    final_bark_url = env_bark_url if env_bark_url else (f"https://api.day.app/{env_bark_key}" if env_bark_key else config_bark_url)

    bark_cfg = BarkConfig(
        enabled=bark_raw.get("enabled", False),
        base_url=final_bark_url,
        repeat=int(bark_raw.get("repeat", 3)),
        level=bark_raw.get("level", "timeSensitive"),
        volume=int(bark_raw.get("volume", 5)),
        group=bark_raw.get("group", "iphone-duo-buyer"),
    )

    sound_raw = notif_raw.get("sound", {})
    sound_cfg = SoundConfig(
        enabled=sound_raw.get("enabled", True)
    )
    notif_cfg = NotificationConfig(
        feishu=feishu_cfg,
        sound=sound_cfg,
        bark=bark_cfg,
        bark_url=final_bark_url
    )

    # 6. 首发倒计时配置
    launch_raw = raw_data.get("launch", {})
    launch_time_val = launch_raw.get("launch_time") if isinstance(launch_raw, dict) else None
    launch_cfg = LaunchConfig(launch_time=launch_time_val)

    # 动态推断 product_url (若未显式指定)
    default_product_url = raw_data.get("product_url", "")
    if not default_product_url:
        prod_slug = product_val.lower().replace(" ", "-")
        default_product_url = f"https://www.apple.com.cn/shop/buy-iphone/{prod_slug}"

    return AppConfig(
        product_url=default_product_url,
        model=model_val,
        color=color_val,
        storage=storage_val,
        purchase_mode=raw_data.get("purchase_mode", "delivery"),
        target=target_cfg,
        online=online_cfg,
        pickup=pickup_cfg,
        browser=browser_cfg,
        safety=safety_cfg,
        notifications=notif_cfg,
        launch=launch_cfg,
    )
