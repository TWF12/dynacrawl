# AGENTS.md

## 项目概述

DynaCrawl —— 基于 Playwright + FastAPI + asyncio 的 B 站动态爬虫数据采集平台（前后端分离）。

- 两个采集场景：UP主信息（UID → 头像/粉丝数/视频BV号/播放量）、视频详情（BV号 → 标题/播放量/点赞/评论）
- 支持断点续采、WebSocket 实时进度、CSV/JSON 导出、自动 session 轮换防风控
- 全链路 asyncio，零阻塞调用
- 内置 8 层反爬机制（浏览器指纹/HTTP头/IP轮换/Session轮换/渐进延迟/WBI签名/DOM兜底）

## 环境与运行

```bash
# 安装 Playwright Chromium（首次运行前必须执行）
uv run playwright install chromium

# 保存 B站 登录 Cookie（必须，扫码一次即可）
uv run python save_cookie.py

# 启动服务
uv run python run.py

# 带热重载
uv run python run.py --reload
```

**仅使用 uv 管理虚拟环境**，不依赖全局 Python，不依赖 pip/pipx。

## 架构概览

```
backend/
├── main.py              # FastAPI 入口 + lifespan + 启动配置检查
├── config.py            # 全局配置（25 个环境变量）
├── database.py          # SQLAlchemy async engine + session
├── models.py            # 5张表：Task / UrlRecord / UpInfo / VideoInfo / Comment
├── schemas.py           # Pydantic 请求/响应模型
├── routers/
│   ├── tasks.py         # 任务 CRUD API
│   ├── results.py       # CSV/JSON 导出 API
│   └── ws.py            # WebSocket 进度推送 + ConnectionManager
├── services/
│   ├── task_service.py  # 任务生命周期、URL 生成、断点恢复（含旧数据清理）
│   └── export_service.py# CSV/JSON 导出逻辑
├── crawler/
│   ├── browser_pool.py     # 浏览器池（headless + headful 双模式，Semaphore 控并发）
│   ├── cookie_manager.py   # Cookie 管理器（多文件轮换 + 过期检测 + 自动删除）
│   ├── anti_detect.py      # UA/视口/指纹隐身 + Clash代理轮换 + 普通代理列表轮换
│   ├── scraper_up.py    # UP主数据爬取（API优先→DOM兜底，Session轮换+渐进延迟）
│   ├── scraper_video.py # 视频数据爬取（API优先→页面降级）
│   ├── wbi_sign.py      # B站 WBI 签名（img_key + sub_key 混排 → MD5 w_rid）
│   ├── url_processor.py # URL 处理逻辑（进度回调 + 错误处理 + 重试）
│   └── dispatcher.py    # 任务调度器（MemoryQueue / RedisQueue 双模式）
└── worker/
    └── consumer.py      # 独立 Worker 进程（Redis 模式专用）

frontend/
├── index.html           # Vue 3 + Element Plus CDN 单页应用
├── style.css
└── app.js
```

**核心数据流：** 用户提交 → TaskService 生成 URL → Dispatcher 推入队列 → url_processor 从 BrowserPool 获取页面 → 爬虫执行 → 结果写入 DB → WebSocket 推送进度。

## 关键设计

### 浏览器管理

- **Cookie 多账号轮换**：`CookieManager` 管理 `data/cookies/` 目录，启动时验证有效性，运行时遇 -101 自动删除切换
- **双浏览器实例**：headless（视频详情用）+ headful（UP主采集用，绕过 B站 -352 检测）
- headful 浏览器最小化并移到屏幕外（`--start-minimized`, `--window-position=-32000,-32000`）
- Context 级别设置浏览器伪装头（Accept-Language/Accept/Sec-Ch-UA），所有新 page 自动继承
- 每个 context 创建时注入隐身脚本（STEALTH_SCRIPT）+ 随机 UA + 登录 Cookie

### 代理轮换

- **双模式**：Clash API（优先）→ 普通 PROXY_LIST 轮换（兜底）→ 无代理（直连，仅测试用）
- 每次创建新 browser context 时触发轮换
- Clash 模式：通过 REST API 切换代理组内上游节点，验证出口 IP 后日志确认
- 普通模式：从 PROXY_LIST 中选一个与当前不同的代理地址
- 并发切换保护：`asyncio.Lock()`

### Session 轮换（核心防风控）

- 每次 `async with acquire_headful_context()` 创建新 context → 新 Cookie + 新 IP
- 间隔按总页数自适应：`<=20` 页不轮换，`>20` 页每 `~20%` 主动轮换一次（15-40 页）
- 遇 B站 -352/-412 风控：立即关闭当前 session，重建后重试**同一页**
- 非风控失败（网络/超时）：等 5-10s 重试同一页，仍失败才计入错误；连续 3 页失败切 session
- `_init_session` 失败不断开循环，等 10-20s 后重试新 context（最多 3 次）

### 渐进延迟

按进度比例 `pn / max(total_pages, 20)` 缩放，小 UP 全程快，大 UP 越往后越慢：

| 进度 | 延迟 |
|------|------|
| 0-15% | 3-8s |
| 15-40% | 8-20s |
| 40-70% | 15-35s |
| 70-100% | 25-50s |

基础延迟（非翻页场景）：3-8s 随机

### WBI 签名

- 从 `api.bilibili.com/x/web-interface/nav` 获取 `img_key` + `sub_key`
- 按固定混排表重排提取 32 位 mixin_key
- 所有 `arc/search` 请求拼接 `w_rid`（MD5）+ `wts`（时间戳）
- 缓存 TTL：600s（10 分钟），跟随 B站 密钥轮换节奏

### 断点恢复

- 启动时扫描 `pending`/`running`/`failed` 状态的任务
- 查找 `pending`/`processing`/`failed` 状态的 URL 重新入队
- **已爬数据保留不删**，`scrape_up_videos` 通过 `existing_bvids` 跳过已有 BV
- 根据已有视频数自动计算续爬起始页 `max(2, existing_count // 50 + 1)`
- 全部已爬完则跳过不再重复采集
- 重算 `task.completed_urls` / `task.failed_urls` 确保进度条准确

### 错误处理

- 异常消息精简：只取异常首行 + 限 200 字符
- Scraper 错误文案统一短格式（如 "card异常" 而非 "card接口异常"）
- url_processor 中拼接后 error_msg 限 120 字符，超出截断
- 前端错误列宽 300px，配合 CSS ellipsis 显示

## 配置要点

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BROWSER_CONCURRENCY` | 3 | 浏览器并发数 |
| `BROWSER_HEADLESS` | true | 无头模式（UP主采集强制有头） |
| `REQUEST_DELAY_MIN` | 3.0 | 基础请求最小延迟(秒) |
| `REQUEST_DELAY_MAX` | 8.0 | 基础请求最大延迟(秒) |
| `PAGE_TIMEOUT` | 30000 | 页面超时(毫秒) |
| `MAX_RETRY` | 2 | 失败重试次数 |
| `PROXY_LIST` | (空) | 代理列表，逗号分隔；留空从 Clash 获取 |
| `CLASH_CONTROLLER` | http://127.0.0.1:9090 | Clash REST API |
| `CLASH_PROXY` | http://127.0.0.1:7890 | Clash 代理端口 |
| `CLASH_GROUP` | (自动检测) | Clash 代理组名 |
| `COOKIE_DIR` | data/cookies/ | Cookie 目录（多文件轮换） |
| `USE_REDIS` | false | 启用 Redis 多 Worker |
| `DATABASE_URL` | sqlite+aiosqlite:///data/dynacrawl.db | 数据库 |

## 注意事项

- **Cookie 必须**：启动时检查，缺失会打印获取指引。无 Cookie 部分 API 限流或返回空数据。
- **代理强烈建议**：无代理启动会打印警告。大规模采集直连必然触发风控。
- **B站页面用 domcontentloaded**：B站页面有持续广告/统计请求，`networkidle` 永远等不到导致超时。
- **前端无构建**：Vue 3 + Element Plus 全部 CDN 引入，无需 npm/Node.js。
- **多任务并发**：`up_video_list` 不占 `acquire_page` semaphore 槽（仅占 headful context 1 槽），3 任务可真正并发。任务提交后保持 PENDING，consumer 拾取时才切 RUNNING。
- **错误隔离**：单个 URL 失败不影响同一任务的其他 URL，自动重试（最多 2 次）。
- **Windows 编码**：日志输出做了 GBK 兼容处理（`_safe_log`）。

## 每次修改后必须同步

- `git add` + `git commit`（中文，格式如 `fix: xxx` / `feat: xxx`）
- 更新 `AGENTS.md`（本文档）—— 如果架构/配置/关键设计有变化
- 更新 `README.md` —— 如果环境变量、用法、FAQ 有变化

## 项目语言规范

- 日常交流：简体中文
- 代码注释：中文
- Git Commit：中文，格式遵循 `fix:` / `feat:` / `docs:` / `refactor:`
