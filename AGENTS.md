# AGENTS.md

## 项目概述

DynaCrawl —— 基于 Playwright + FastAPI + asyncio 的 B 站动态爬虫数据采集平台（前后端分离）。

- 两个采集场景：UP主信息（UID → 头像/粉丝数/视频BV号/播放量）、视频详情（BV号 → 标题/播放量/点赞/评论）
- 支持断点续采、WebSocket 实时进度、CSV/JSON 导出
- 全链路 asyncio，零阻塞调用

## 环境与运行

```bash
# 安装 Playwright Chromium（首次运行前必须执行）
uv run playwright install chromium

# 启动服务（通过 run.py 入口，确保 Windows 事件循环兼容 Playwright）
uv run python run.py --reload

# 不带热重载的启动
uv run python run.py

# 如需 Redis 模式 + 多 Worker：
# 1. 确保本地 Redis 运行在 localhost:6379
# 2. 设置环境变量 USE_REDIS=1
# 3. 启动 API 服务（负责推送任务到 Redis 队列）
# 4. 另开终端启动 Worker 消费进程：
uv run python -m backend.worker.consumer
```

**仅使用 uv 管理虚拟环境**，不依赖全局 Python，不依赖 pip/pipx。

## 架构概览

```
backend/
├── main.py              # FastAPI 入口，lifespan 管理启动/关闭
├── config.py            # 全局配置，通过环境变量覆盖
├── database.py          # SQLAlchemy async engine + session
├── models.py            # 5张表：Task / UrlRecord / UpInfo / VideoInfo / Comment
├── schemas.py           # Pydantic 请求/响应模型
├── routers/
│   ├── tasks.py         # 任务 CRUD API
│   ├── results.py       # CSV/JSON 导出 API
│   └── ws.py            # WebSocket 进度推送 + ConnectionManager
├── services/
│   ├── task_service.py  # 任务生命周期、URL 生成、断点续采
│   └── export_service.py# CSV/JSON 导出逻辑
├── crawler/
│   ├── browser_pool.py  # Playwright BrowserPool（Semaphore 控并发）
│   ├── anti_detect.py   # UA 轮换 + Stealth 脚本注入 + 随机延迟
│   ├── scraper_up.py    # UP主数据爬取（API 优先，页面降级）
│   ├── scraper_video.py # 视频数据爬取（API 优先，页面降级）
│   └── dispatcher.py    # 任务调度器（MemoryQueue / RedisQueue 双模式）
└── worker/
    └── consumer.py      # 独立 Worker 进程（Redis 模式专用）

frontend/
├── index.html           # Vue 3 + Element Plus CDN 单页应用
├── style.css
└── app.js
```

**核心数据流：** 用户提交 → TaskService 生成 URL → Dispatcher 推入队列 → 消费者协程/Worker 从 BrowserPool 获取 Playwright 页面 → 爬虫执行 → 结果写入 DB → WebSocket 推送进度。

## 注意事项

- **B站数据策略**：优先通过 API 获取，API 不可用时降级为页面 DOM 提取。两种方式均通过 Playwright 页面发起请求，满足浏览器自动化要求。
- **并发限制**：默认 3（`BROWSER_CONCURRENCY`），每次请求间随机延迟 1-3 秒，避免触发风控。
- **断点续采**：服务启动时扫描 `pending`/`processing` 状态的任务和 URL，重置后重新入队。已完成的 URL 不会重复采集。
- **SQLite 默认路径**：`data/dynacrawl.db`，可设 `DATABASE_URL` 环境变量覆盖。
- **Redis 可选**：默认走内存 asyncio.Queue（单进程多协程），设 `USE_REDIS=1` 启用 Redis 队列 + 多 Worker 模式。
- **Playwright 安装**：首次运行前需 `uv run playwright install chromium`。
- **前端无构建**：Vue 3 + Element Plus 全部通过 CDN 引入，无需 npm/Node.js。
- **错误隔离**：单个 URL 失败不影响同一任务的其他 URL，自动重试（最多 2 次），超重试次数后标记为 failed。

## 项目语言规范

- 日常交流：简体中文
- 代码注释：中文
- Git Commit：中文，格式遵循 "feat: 新增功能"
