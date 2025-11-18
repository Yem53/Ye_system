# 半自动交易系统使用手册

## 1. 系统概览
- **公告监控**：依旧后台轮询币安公告，可在仪表盘查看并人工审批。
- **手动计划**：新增“手动下单计划”卡片，支持输入合约、方向、上线时间及策略参数，系统会在预定时间自动下单。
- **执行器**：后台任务每秒检查手动计划，满足条件即调用 Binance Futures API 下单，并根据结果更新状态。
- **统计分析**：保留历史窗口收益、日报、日志等模块，方便回溯策略表现。

## 2. 本地使用步骤
1. 启动 Postgres（本地或 Docker 容器均可），确保连接串与 `.env` 中的 `DATABASE_URL` 匹配。
2. 在项目根目录创建并激活虚拟环境，执行 `pip install -r requirements.txt`。
3. 根据 `.env.example` 填写 `.env`（包括 `HTTP_PROXY` 如需代理）。
4. 运行 `python run_local.py` 或 `./scripts/run_local.sh`/`powershell .\scripts\run_local.ps1` 启动服务。
5. 浏览器访问 `http://127.0.0.1:8000`，在页面顶部“手动下单计划”表单输入：
   - 合约符号：例如 `FOLKSUSDT`
   - 上线时间：ISO 格式（`2025-11-06T20:30:00+08:00`）
   - 杠杆、仓位比例、滑动退出、止损、方向等。
6. 提交后计划进入列表，状态为 `pending`。到点后后台自动下单，状态切换为 `executed` 或 `failed`。

## 3. API 参考
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/manual-plans` | 查询全部手动计划 |
| `POST` | `/api/manual-plans` | 创建计划，JSON 结构同表单字段 |
| `POST` | `/api/manual-plans/{id}/cancel` | 取消计划 |
| `POST` | `/api/backfill?months=6` | 补抓历史公告 |

请求示例：
```bash
curl -X POST http://127.0.0.1:8000/api/manual-plans \
     -H "Content-Type: application/json" \
     -d '{
           "symbol": "FOLKSUSDT",
           "side": "BUY",
           "listing_time": "2025-11-06T12:30:00+08:00",
           "leverage": 5,
           "position_pct": 0.5,
           "trailing_exit_pct": 0.15,
           "stop_loss_pct": 0.05
         }'
```

## 4. AWS 部署建议
1. 创建东京区 EC2（建议 Amazon Linux / Ubuntu，c6i.large 以上），安装 Docker 或直接使用系统 Python。
2. 安装依赖：`sudo apt update && sudo apt install python3.11 python3.11-venv git`。
3. `git clone` 项目，创建 `.env`（API Key、数据库、代理等可使用 AWS Secrets Manager 管理）。
4. 若 Postgres 也部署在 AWS，可选择：
   - **RDS PostgreSQL**：生产建议使用托管数据库。
   - **EC2 容器**：使用 `docker run ... postgres:15`。
5. 安装依赖并运行 `python run_local.py`（或使用 `systemd`/`supervisor` 保持常驻）。
6. 为减少延迟：
   - EC2 与 Binance 服务器在同一区域（东京）。
   - 关闭无关服务，保持网络稳定。
   - 将 `ANNOUNCEMENT_POLL_INTERVAL`、手动计划检查间隔调低（默认 1 秒）。

## 5. 未来扩展方向
- **自动化审批**：结合公告解析结果（链、项目方、风险等级）自动决定是否生成计划。
- **滑点控制**：在 ExecutionService 中加入深度预估、订单拆分、失败重试机制。
- **实时监控**：将执行日志推送到 Grafana/CloudWatch，并提供 Telegram/Email 告警。
- **策略回测**：完善 `announcement_returns`，利用更长历史窗口评估收益分布与风险。
- **多账户支持**：抽象账户配置，按计划分配不同 API Key/子账户。
- **安全与审批**：手动计划提供双人审核、签名和属性校验，避免误操作。
