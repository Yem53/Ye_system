# Quant News Codex

一个可持续运行的币安公告监控与人工审核交易系统，具备以下能力：

- Docker Compose 一键启动 Postgres + FastAPI 仪表盘
- 自动轮询/补抓币安 Alpha 与合约公告，入库并等待人工审核
- 仪表盘支持在线批准/拒绝公告，自动生成交易计划
- 交易计划默认 5 倍杠杆、50% 仓位、15% 滑动退出与 5% 止损，可在仪表盘实时调整
- 秒级/分钟级/小时级窗口收益分析：支持 5m~144h 多时间段收益展示
- 手动计划：直接在仪表盘录入合约和上线时间，系统在指定时刻自动下单
- 预留执行引擎（Binance Futures API）、日报邮件发送与滑点统计接口

## 快速开始

## 本地运行（推荐）

1. 安装 Postgres（本地或云端均可），创建数据库 `quant_news_codex` 以及拥有读写权限的用户（示例：`quantnews/quantnews`）。
2. 创建 Python 虚拟环境并安装依赖：
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows PowerShell 使用 .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. 复制配置模板并填写实际参数：`cp .env.example .env`，按照注释修改数据库连接串、API Key、SMTP 等。
4. 启动服务：
   - macOS/Linux: `./scripts/run_local.sh`
   - Windows PowerShell: `.scripts\run_local.ps1`

脚本会自动加载 `.env`、设置 `PYTHONPATH` 并以热重载方式运行 Uvicorn。默认监听 [http://127.0.0.1:8000](http://127.0.0.1:8000)。如需关闭热重载或调整端口，可修改脚本参数。详尽使用说明见 `docs/operations.md`。

## Docker 运行（可选）

```bash
cp .env.example .env   # 填写实际的 API Key/SMTP 等配置
docker compose up --build
```

`.env` 位于项目根目录，`docker compose` 会自动加载其中的环境变量。启动后访问 [http://127.0.0.1:8000](http://127.0.0.1:8000) 查看仪表盘（如果 `localhost` 被代理/VPN 劫持，也可以直接使用本机 IPv4）。仪表盘提供待审核公告列表、已批准队列、历史公告及多窗口收益表，可直接在页面批准/拒绝或微调策略参数。

## 配置

通过环境变量或 `.env` 文件配置：

- `DATABASE_URL`：Postgres 连接串（在 compose 中默认指向容器内 `quant_news_codex` 数据库）
- `BINANCE_API_KEY` / `BINANCE_API_SECRET`：合约交易 API Key
- `SMTP_*`：日报邮件发送配置，暂为空位，待提供凭证后即可启用
- `ANALYSIS_WINDOWS`：逗号分隔的收益窗口（默认 5m,10m...144h）
- 其它策略参数可在 `app/core/config.py` 中查看，均有默认值

## 目录结构

```
backend/app
├── api           # REST API 与仪表盘接口
├── core          # 配置、调度等核心组件
├── db            # SQLAlchemy 会话与建表工具
├── models        # 数据表定义
├── schemas       # Pydantic 模型
├── services      # 公告抓取、交易、执行、日报等服务
├── templates     # 仪表盘页面
└── main.py       # FastAPI 入口
```

## 下一步

- 在 `services/execution_service.py` 中接入真实行情价格与滑动退出逻辑
- 根据实际公告格式完善解析器 `_parse_alpha/_parse_futures`
- 使用前端框架（如 HTMX/React）增强仪表盘交互，支持实时刷新
- 添加 Alembic 迁移、单元测试与更完善的错误告警
- 为历史补抓和窗口收益计算提供命令行触发脚本或后台 API 调度
