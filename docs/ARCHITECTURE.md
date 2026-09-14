# 系统技术架构 (System Architecture)

`iPhone Duo Launch Assistant` 是一个面向 Apple 中国官网首发场景的低频、安全、人工接管型购买辅助系统。系统基于“低频健康监控、状态严格保持、终极门禁核验、人工安全接管”四大核心理念构建。

---

## 1. 核心链路架构图

```mermaid
flowchart TD
    subgraph AppleStore [Apple 中国官网]
        BuyPage[官网购买页 /shop/buy-iphone]
        FulfillmentAPI[直营店自提库存接口]
    end

    subgraph CoreEngine [核心调度与执行引擎]
        CatalogResolver[Catalog 权威解析器\nsrc/catalog.py]
        OnlineMonitor[线上主通道监控\n带随机抖动轮询]
        PickupMonitor[线下直营店监控\n4 店独立节流]
        EventQueue[(可用性事件队列\nAvailabilityEvent Queue)]
        Coordinator[启动协调器\nLaunchCoordinator (Single-Flight)]
        Preservation[规格保持核验\nSelection Preservation]
        GateCheck[终极一致性硬门禁\nFINAL_TARGET_CHECK]
        Handoff[浏览器置前与人工接管\nHuman Handoff Point]
    end

    subgraph SafetyShield [安全护盾]
        RateGuard[RateGuard\n频控与 403/429/541 退避]
        FailClosed[会话 Fail-Closed 熔断]
        NoAutoPay[零自动支付 / 零凭据存储]
    end

    subgraph NotificationLayer [通知层]
        Feishu[飞书卡片异步通知]
        Audio[macOS 本地音频报警]
    end

    subgraph PresentationLayer [表现层]
        Adapter[DashboardAdapter\n线程安全数据投递]
        Dashboard[控制中心 GUI\n纯只读 Observer 观察者]
    end

    BuyPage --> CatalogResolver
    CatalogResolver -->|权威锁定 SKU| Coordinator
    BuyPage <-->|低频轮询| OnlineMonitor
    FulfillmentAPI <-->|严格节流| PickupMonitor
    
    OnlineMonitor --> RateGuard
    PickupMonitor --> RateGuard
    
    OnlineMonitor -->|AVAILABLE| EventQueue
    PickupMonitor -->|PICKUP_AVAILABLE| EventQueue
    
    EventQueue --> Coordinator
    Coordinator --> Preservation
    Preservation --> GateCheck
    GateCheck --> Handoff
    
    Coordinator -.->|状态快照| Adapter
    Adapter -.->|单向只读| Dashboard
    Coordinator -.->|触发告警| Feishu
    Coordinator -.->|触发告警| Audio
```

---

## 2. 核心模块职能

### 2.1 官方 Catalog 动态解析器 (`src/catalog.py`)
- 首发前自动解析 Apple 中国商品配置数据结构。
- 动态建立颜色（如星光白色）、容量（如 512GB）与官方 Part Number（如 `MK2P4CH/A`）的双向映射。
- 将解析结果缓存至本地并在运行时作为只读基准，避免硬编码失效。

### 2.2 双通道解耦监控 (`src/pickup_monitor.py` & `src/main.py`)
- **线上渠道 (Online Channel)**：针对官方购买页的售卖状态（`COMING_SOON`, `AVAILABLE`, `UNAVAILABLE`）进行带随机抖动的低频轮询。
- **自提渠道 (Pickup Channel)**：针对上海核心 4 家官方直营店（香港广场 R390、上海环贸 iapm R401、五角场 R581、环球港 R683）进行自提库存监控。单店轮询之间强制执行 $\ge 2.0$ 秒的严格休眠节流。

### 2.3 请求频控与多级退避护盾 (`src/rate_guard.py`)
- 内置请求频控看门狗，防止任何非预期的过度密集请求。
- 深度适配 Apple 边缘反爬与风控响应：当捕获到 HTTP 403、429 或 541 时，触发多级指数退避机制（60s $\to$ 120s $\to$ 240s），自动重置网络会话，杜绝高频轰炸。

### 2.4 单飞抢占与流程调度 (`src/main.py - LaunchCoordinator`)
- 采用严格的 `Single-Flight` 单飞控制锁：当线上可购事件触发主通道流程后，自动锁定执行状态，忽略后续重复事件，杜绝并发竞争与重复加车。

### 2.5 规格保持与快速路径 (`src/apple_store.py`)
- 首发预热阶段提前预选目标机型、颜色、容量及购买选项（默认无折抵、不加 AppleCare+）。
- T0 触发开售时，优先核验当前页面的预选状态是否完整保留 (`Selection Preservation`)。
- 若状态完整保留，直接跳过所有冗余点击进入下一步，实现极速响应；若检测到状态丢失，则通过 Fast Path 快速恢复选择。

### 2.6 加车前终极规格一致性硬门禁 (`src/apple_store.py - FINAL_TARGET_CHECK`)
- 在触发 `add_to_bag()` 动作前的最后一步，执行不可绕过的原子级硬门禁检查：
  - 核验目标外观颜色是否匹配；
  - 核验目标存储容量是否匹配；
  - 核验官方权威 SKU / Part Number 是否严格一致；
  - 核验已选商品配置唯一性。
- 任何一项检查未通过，立即主动抛出异常并熔断退出，坚决防止加错机型或下错订单。

### 2.7 浏览器焦点移交与人工安全接管 (`Human Handoff`)
- 系统在完成加车并推进至结账/支付准备页面后，立即主动将 Chromium 浏览器窗口最大化并前置到屏幕最前端。
- 播放高优先级本地警报音并推送飞书卡片。
- **所有后续操作（地址确认、发票信息、支付方式选择、最终提交订单）全部交由用户本人在官方界面完成**。

### 2.8 观察者模式控制中心 (`src/dashboard_*.py`)
- 控制中心 GUI（Tkinter）严格基于**只读观察者模式 (Observer Pattern)** 运行。
- GUI 运行在独立线程，仅通过 `DashboardAdapter` 消费核心调度器投递的状态快照。
- 控制中心不参与任何业务决策，即使 GUI 发生任何渲染抖动或窗口关闭，购买核心逻辑依然稳定运行。
