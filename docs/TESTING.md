# 自动化测试与质量保障体系 (Automated Testing & QA)

为确保在首发高并发、高敏感环境下的绝对稳定性与安全性，本项目建立了完备的单元测试与失败注入防护测试网，所有测试用例均为完全密封 (Hermetic) 的离线执行，无需访问外部网络。

---

## 1. 测试套件总览

- **测试用例总数**: **293 项**
- **当前执行状态**: **293 / 293 PASS (0 Failures, 0 Errors)**
- **执行耗时**: 约 10~15 秒（全并发/快速内存模拟）

---

## 2. 测试模块分布

| 测试用例文件 | 覆盖领域 | 关键验证指标 |
| :--- | :--- | :--- |
| `tests/test_catalog.py` | 官方 Catalog 解析 | 官方 JSON 结构动态解析、Part Number 匹配、星光白色/512GB 双向映射、本地缓存写入与加载。 |
| `tests/test_config.py` | 系统配置模型 | 配置项类型校验、默认值补全、`config.example.yaml` 格式完备性。 |
| `tests/test_store_mapping_integrity.py` | 直营店映射权威性 | 上海 4 店编号（R390/R401/R581/R683）双向映射、禁止重复 store_number、配置模板与代码映射一致性。 |
| `tests/test_pickup_parser.py` | 自提库存模型与解析 | Apple Fulfillment 响应解析、内部严格 7 态模型（AVAILABLE, COMING_SOON 等）、禁止将异常错判为缺货。 |
| `tests/test_rate_guard.py`<br>`tests/test_phase1e_rate_safety.py` | 频控与退避护盾 | 请求最小安全间隔、单店节流 $\ge 2.0$s、HTTP 403/429/541 多级指数退避阶梯（60s $\to$ 120s $\to$ 240s）。 |
| `tests/test_notifier.py`<br>`tests/test_feishu_persistence.py` | 通知与告警服务 | 异步非阻塞事件队列、高频状态去重、网络超时优雅降级、凭据自动化脱敏。 |
| `tests/test_dashboard*.py` | 控制中心 GUI 状态机 | 观察者线程安全快照更新、预购倒计时中文格式化、10 项核验清单状态跃迁、模拟演示驱动生命周期。 |
| `tests/test_safety_triggers.py`<br>`tests/test_phase2_failure_injection.py` | 终极门禁与失败注入 | 规格保留丢失触发 Fast Path 重选、终极硬门禁 `FINAL_TARGET_CHECK` 规格不匹配硬熔断、零真实加车保护。 |

---

## 3. 本地运行测试命令

在项目根目录下激活 Python 虚拟环境后执行：

```bash
# 运行全量 293 项自动化回归测试
.venv/bin/python -m unittest discover -s tests -p "test_*.py"

# 单独运行特定安全模块测试 (例如终极硬门禁与失败注入测试)
.venv/bin/python -m unittest tests/test_phase2_failure_injection.py
```
