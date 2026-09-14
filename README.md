# iPhone Duo Launch Assistant

> **A safety-first Apple China launch monitoring and purchase-assistance tool with dynamic catalog verification, dual-channel availability monitoring, and a Chinese launch dashboard.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests: 293 Passed](https://img.shields.io/badge/tests-293%20passed-brightgreen.svg)](docs/TESTING.md)
[![Safety: Fail--Closed](https://img.shields.io/badge/safety-fail--closed-red.svg)](docs/SAFETY.md)

---

## 📌 项目定位

`iPhone Duo Launch Assistant` 是一个**面向 Apple 中国官网首发场景的低频、安全、人工接管型购买辅助工具**。

系统旨在帮助个人消费者在官方新品首发日（如 2026-10-16 20:00:00 CST）安全、平稳地跟踪目标商品状态。本项目**坚决抵制任何黄牛秒杀与接口滥用行为**，严格遵循官方交互礼仪，所有不可逆步骤（登录授权、2FA、最终订单确认与支付）均由用户本人在官方界面完成。

---

## ✨ 核心功能

- 🔍 **官方 Catalog 动态解析**：首发前动态加载 Apple 中国官方商品配置目录，提取规格依赖关系。
- 🎯 **权威 SKU 锁定**：精准锁定目标配置（星光白色 · 512GB）对应的官方 Part Number（`MK2P4CH/A`），杜绝硬编码。
- 🔥 **规格预热与状态保持 (Selection Preservation)**：开售前预选目标机型、颜色、容量、无折抵换购与无服务计划；T0 时优先核验保留状态，跳过冗余点击，实现低时延就绪。
- ⚡ **加车前终极规格一致性硬门禁 (`FINAL_TARGET_CHECK`)**：点击加车前最后 1 毫秒执行原子级规格与 SKU 比对，若有任何不匹配立即硬熔断，严防下错订单。
- 🌐 **双通道解耦监控**：
  - **线上渠道 (Online)**：对官方购买页售卖状态实施带随机抖动的低频合规轮询；
  - **直营店自提渠道 (Pickup)**：并行监控上海 4 家官方直营店（香港广场、上海环贸 iapm、五角场、环球港）的店内自提现货。
- 🛡️ **RateGuard 频控与多级指数退避**：遭遇 HTTP 403 / 429 / 541 边缘拦截响应时，自动启动阶梯退避（60s $\to$ 120s $\to$ 240s）并平滑恢复，坚决不轰炸官网。
- 🔒 **严格会话 Fail-Closed**：若开售前检测到 Apple Account 登录态失效，立即全链路告警熔断，拒绝以未登录降级状态盲目推进。
- 🚀 **单飞抢占机制 (Single-Flight)**：线上主通道触发购买后自动锁定执行上下文，忽略后续重复事件，杜绝并发竞争与重复下单。
- 👤 **浏览器置前与人工安全接管 (Human Handoff)**：加车完成后毫秒级将官方浏览器窗口置前，播放本地警报音并推送飞书卡片，由用户亲自核对地址与完成付款。
- 🖥️ **纯中文首发实战控制中心 (Launch Control Center)**：基于 Tkinter 的现代化全中文 GUI 看板，实时呈现会话、倒计时、10 项就绪清单与双通道状态。

---

## 🛡️ 安全边界与合规声明

为保障账户与资产安全，本项目在底层架构上设立了绝对的安全边界：

| 行为类别 | 系统的处理方式 |
| :--- | :--- |
| **Apple ID 密码** | **绝不保存、绝不托管、绝不自动代填**。由用户在原生浏览器中输入。 |
| **双重认证 (2FA)** | **绝不拦截、绝不自动处理**。遇到 2FA 弹出时提示用户手机端点击授权。 |
| **人机验证 (CAPTCHA)** | **不内置任何破解服务**。遇到滑块或图片验证码立即触发声光告警转交人工。 |
| **网络请求频次** | **严格受控**。单店节流 $\ge 2.0$ 秒，线上轮询间隔 20~30 秒，严禁高频轰炸。 |
| **最终支付与扣款** | **绝无自动支付**。系统在到达结账/支付准备页面时强制停机，由用户本人核实金额并完成付款。 |
| **成功率承诺** | **本工具为辅助工具，不声称、不承诺任何购买成功保证**。 |

---

## 🖥️ 首发协同控制中心 (Dashboard)

控制中心采用纯简体中文与 Apple 极简设计规范构建，严格作为**只读观察者 (Observer)** 运行，与主执行引擎完全线程隔离：

```
+-----------------------------------------------------------------------+
|  【首发实战 · 生产状态】                   倒计时: 剩余 31天 23小时 28分 52秒  |
+-----------------------------------------------------------------------+
|  [Apple Account 会话]      [目标规格锁定]            [RateGuard 护盾]  |
|  🟢 会话正常 (已登录)        星光白色 / 512GB (权威锁定)   🟢 频控正常 (0 退避) |
+-----------------------------------------------------------------------+
|  [线上主通道监控]           [上海 4 店自提监控]        [通知与告警系统]   |
|  🟡 即将发售 (轮询中)        4 门店轮询 (节流 >=2.0s)   🟢 飞书已连通 / 本地正常 |
+-----------------------------------------------------------------------+
|  首发实战就绪核验清单 (10/10 READY):                                    |
|  [✔] 配置文件完整加载     [✔] 浏览器 Profile 挂载   [✔] 会话有效性核验  |
|  [✔] 官方 Catalog 解析   [✔] 目标规格预热就绪      [✔] 线上通道低频监控 |
|  [✔] 上海 4 店节流巡检   [✔] RateGuard 退避常驻    [✔] 飞书与声音告警   |
|  [✔] 人工接管与单飞锁                                                 |
+-----------------------------------------------------------------------+
|  实时事件流:                                                           |
|  [20:00:00] [INFO] Catalog 解析权威锁定: MK2P4CH/A                      |
|  [20:00:01] [INFO] 规格预选保持核验通过，跳过冗余点击                      |
|  [20:00:01] [SUCCESS] 到达人工接管就绪点，浏览器已自动置前！               |
+-----------------------------------------------------------------------+
```

---

## 🚀 快速上手

### 1. 准备运行环境

系统要求 **Python 3.10+**。推荐在独立的虚拟环境中安装：

```bash
# 1. 克隆仓库
git clone https://github.com/<your-username>/iphone-duo-launch-assistant.git
cd iphone-duo-launch-assistant

# 2. 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖与 Playwright 浏览器核心
pip install -r requirements.txt
playwright install chromium
```

### 2. 初始化配置文件

从公开模板创建本地运行配置：

```bash
cp config.example.yaml config.yaml
```

在本地 `config.yaml` 中核对目标规格（星光白色 · 512GB）、购买渠道、以及配置您私有的飞书机器人 Webhook（可选）。

> **安全提示**：`config.yaml` 已被 `.gitignore` 严格忽略，绝不会被上传至远程仓库。

### 3. 会话准备与登录 (提前进行)

首发日前，运行持久化浏览器会话准备命令：

```bash
python -m src.main --mode prepare-session
```

- 系统将拉起原生 Chromium 窗口，导航至 Apple 官网；
- **请在浏览器窗口中自行完成 Apple ID 登录与双重认证 (2FA)**；
- 登录成功后关闭浏览器，登录态将安全保存在本地 `./browser_data` 目录中。

### 4. 会话与网络连通性巡检

在首发前（例如开售前 1 小时），执行环境全检：

```bash
python -m src.main --mode check-session
```

验证项包括：Apple ID 会话有效性、上海 4 家直营店网络连通性、飞书 Webhook 告警通道连通性。

### 5. 控制中心离线演练 (Demo 模式)

可在无网络请求、不打扰 Apple 官网的情况下熟悉控制中心交互与生命周期演练：

```bash
python -m src.main --mode dashboard-demo
```

### 6. 首发实战运行

在预购开启前（例如开售前 10~15 分钟），启动正式协同控制中心：

```bash
python -m src.main --mode launch-dashboard
```

- 系统将自动加载会话、动态解析官方 Catalog 锁定 SKU、预热目标机型并保持选择；
- 启动低频双通道监控与 RateGuard 护盾；
- **当开售信号触发时，系统毫秒级核验规格并推进至结账准备界面，随后立即置前浏览器，由您本人亲自完成付款**。

---

## 🏗️ 架构与技术文档

详细的设计规范与技术文档参见 `docs/` 目录：

- 📐 [系统技术架构与全链路流程 (Architecture)](docs/ARCHITECTURE.md)
- 🛡️ [安全铁律与终极门禁说明 (Safety & Compliance)](docs/SAFETY.md)
- 🖥️ [控制中心设计与组件解析 (Dashboard)](docs/DASHBOARD.md)
- 🧪 [自动化回归测试与测试套件 (Testing)](docs/TESTING.md)

---

## 🧪 自动化测试验证

本项目拥有完备的离线自动化测试套件，全面覆盖 Catalog 动态解析、双通道状态机、指数退避、通知去重、GUI 观察者模型以及硬门禁失败注入：

```bash
# 执行全量 293 项自动化回归测试
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

当前冻结版本测试报告：
```text
Ran 293 tests in 12.268s

OK (0 failures, 0 errors)
```

---

## 📄 开源许可证

本项目基于 [MIT License](LICENSE) 协议开源。
