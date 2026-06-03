# AGENTS.md

## 项目概述

DynaCrawl —— 基于 Playwright + FastAPI + asyncio 的 B 站动态爬虫数据采集平台（前后端分离）。

- 两个采集场景：UP主信息（UID → 头像/粉丝数/视频BV号/播放量）、视频详情（BV号 → 标题/播放量/点赞/评论）
- 支持断点续采、WebSocket 实时进度、CSV/JSON 导出、自动 session 轮换防风控
- 全链路 asyncio，零阻塞调用
- 内置多层反爬机制（浏览器指纹/HTTP头/IP轮换/Session轮换/渐进延迟/WBI签名/DOM兜底/行为拟人）

## 环境与运行

```bash
# 安装 Playwright Chromium（首次运行前必须执行）
uv run playwright install chromium

# 保存 B站 登录 Cookie（必须，扫码一次即可）
uv run python save_cookie.py

# 启动服务
uv run python run.py
```

**仅使用 uv 管理虚拟环境**，不依赖全局 Python，不依赖 pip/pipx。

## 架构概览

```
backend/
├── main.py              # FastAPI 入口 + lifespan + 启动配置检查
├── config.py            # 全局配置（环境变量）
├── database.py          # SQLAlchemy async engine + session
├── models.py            # 5张表：Task / UrlRecord / UpInfo / VideoInfo / Comment
├── schemas.py           # Pydantic 请求/响应模型
├── routers/
│   ├── tasks.py         # 任务 CRUD API
│   ├── results.py       # CSV/JSON 导出 API
│   └── ws.py            # WebSocket 进度推送 + ConnectionManager
├── services/
│   ├── task_service.py  # 任务生命周期、URL 生成、断点恢复
│   └── export_service.py# CSV/JSON 导出逻辑
├── crawler/
│   ├── browser_pool.py     # 浏览器池（headless + headful 双模式，Semaphore 控并发）
│   ├── cookie_manager.py   # Cookie 管理器（多文件轮换 + 过期检测 + 自动删除）
│   ├── anti_detect.py      # 隐身/UA/代理/行为拟人/全局IP轮换（核心反爬模块）
│   ├── scraper_up.py    # UP主数据爬取（API优先→DOM兜底，Session轮换+渐进延迟）
│   ├── scraper_video.py # 视频数据爬取（API优先→DOM兜底+评论采集）
│   ├── wbi_sign.py      # B站 WBI 签名（img_key + sub_key 混排 → MD5 w_rid）
│   ├── url_processor.py # URL 处理 + 进度回调 + 错误处理 + 自动重试
│   └── dispatcher.py    # 任务调度器（内存队列 / Redis 双模式）
└── worker/
    └── consumer.py      # 独立 Worker 进程（Redis 模式专用）

frontend/
├── index.html           # Vue 3 + Element Plus CDN 单页应用
├── style.css
└── app.js
```

**核心数据流：** 用户提交 → TaskService 生成 URL → Dispatcher 推入队列 → url_processor 从 BrowserPool 获取页面 → 爬虫执行 → 结果写入 DB → WebSocket 推送进度。

## 反爬机制

### 浏览器隐身

- **pw-stealth-enhanced**（官方维护）：覆盖 Canvas/WebGL/AudioContext/字体枚举等 30+ 检测点，通过 CDP 注入
- **browserforge**：动态生成 UA 和 Sec-CH-UA headers，版本号始终匹配
- 每个 browser context 独立指纹（`make_browser_fingerprint()` 统一生成）
- headful 浏览器强制使用（B站对 headless 返回空壳 HTML）
- headful 最小化并移到屏幕外（`--start-minimized`, `--window-position=-32000,-32000`）

### Redis 分布式队列

支持 Redis 作为消息队列中间件，实现生产者-消费者模式的多 Worker 并发消费。

**启动方式：**

```powershell
# PowerShell — 主服务 (Web界面 + 3 consumers)
$env:USE_REDIS = "true"
uv run python run.py
```
```cmd
:: CMD — 主服务
set USE_REDIS=true
uv run python run.py
```
```bash
# Linux/Mac — 主服务
USE_REDIS=true uv run python run.py
```

**追加 Worker（新终端，不占端口）：**
```powershell
# PowerShell
$env:USE_REDIS = "true"
uv run python run.py --worker
```
```cmd
:: CMD
set USE_REDIS=true
uv run python run.py --worker
```
```bash
# Linux/Mac
USE_REDIS=true uv run python run.py --worker
```

**关键机制：**
- **双模式**：内存队列（默认）+ Redis 队列（`USE_REDIS=true`）
- **生产者-消费者**：主服务写入 Redis List，Worker 通过 `BRPOP` 并发消费
- **Worker 独立进程**：`--worker` 启动纯消费进程，不绑定端口，每 Worker +3 browsers
- **心跳检测**：主服务每 2 秒刷新 `master_alive` key（TTL 5s），Worker 检测停机后自动退出
- **取消任务**：Redis Set 标记已取消任务 ID，`pop` 时自动跳过
- **进度同步**：Worker 写进度到 Redis Hash，主服务轮询推 WebSocket
- **自动降级**：启动时 ping Redis，不可达自动降级内存队列，不影响单机

**注意：** 需先安装 Redis（WSL `sudo apt install redis-server` 或 Windows [Memurai](https://www.memurai.com/)）。PowerShell 用 `$env:VAR="val"` 设环境变量，CMD 用 `set VAR=val`，Linux/Mac 用 `VAR=val`。

### 代理

- **默认直连**：国内网络直连 B站 最稳定，配合多 Cookie + 渐进延迟
- **可选 PROXY_LIST**：支持自定义代理列表（逗号分隔的 URL），自动轮换

### Session 轮换

- 每次 `async with acquire_headful_context()` 创建新 context → 新 Cookie + 新 IP
- 按总页数自适应：`<=20` 页不轮换，`>20` 页每 `~20%` 主动轮换一次
- 遇 B站 -352/-412 风控：等待 5-10s 重试，仍失败则切换 session
- _init_session 失败：**立即走 DOM 兜底**，不再重试（代理坏了白等无意义）

### 渐进延迟

按进度比例缩放，小 UP 全程快，大 UP 越往后越慢：

| 进度 | 延迟 |
|------|------|
| 0-15% | 1-3s |
| 15-40% | 2-5s |
| 40-70% | 3-8s |
| 70-100% | 5-12s |

### 行为拟人化

- **鼠标轨迹**：`human_mouse_move()` — 三次贝塞尔曲线 + 随机偏移 + 加速/减速
- **滚动方式**：`human_scroll()` — 分段滚动 + 随机停顿（模拟人眼扫视）
- **停留模拟**：`human_dwell()` — 微量鼠标移动 + 随机看不同位置 + 偶尔点击空白

### DOM 兜底

- API 全部失败时走 `/upload/video` 页面提取（登录墙下仍可见 navBar）
- 双策略：视频列表分页 → 空间主页滚动
- 直连提取（无需代理）
- 昵称/粉丝/视频数从 navBar + section header 精准提取

### WBI 签名

- 从 `api.bilibili.com/x/web-interface/nav` 获取 `img_key` + `sub_key`
- 按固定混排表重排提取 32 位 mixin_key
- 所有 `arc/search` 请求拼接 `w_rid`（MD5）+ `wts`（时间戳）
- 缓存 TTL：600s（10 分钟）

## 断点恢复

- 启动时扫描 `pending`/`running`/`failed` 状态的任务
- 查找 `pending`/`processing`/`failed` 状态的 URL 重新入队
- **已爬数据保留不删**，`scrape_up_videos` 通过 `existing_bvids` 跳过已有 BV
- 根据已有视频数自动计算续爬起始页 `max(2, existing_count // 50 + 1)`

## 错误处理

- URL 失败后 3 级自动延迟重入队（5s → 15s → 1min），代理恢复后无需重启
- 异常消息精简：常见 Playwright 错误映射为中文短语（如 "代理不通"/"超时"/"DNS失败"）
- 任务取消标记在完成后自动清理，防止内存泄漏

## 配置要点

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BROWSER_CONCURRENCY` | 3 | 浏览器并发数 |
| `BROWSER_HEADLESS` | true | 无头模式（UP主采集强制有头） |
| `REQUEST_DELAY_MIN` | 3.0 | 基础请求最小延迟(秒) |
| `REQUEST_DELAY_MAX` | 8.0 | 基础请求最大延迟(秒) |
| `PAGE_TIMEOUT` | 30000 | 页面超时(毫秒) |
| `PROXY_LIST` | (空) | 自定义代理 URL 列表，逗号分隔；留空则直连 |
| `COOKIE_DIR` | data/cookies/ | Cookie 目录（多文件轮换） |
| `USE_REDIS` | false | 启用 Redis 多 Worker |
| `DATABASE_URL` | sqlite+aiosqlite:///data/dynacrawl.db | 数据库 |

## 注意事项

- **Cookie 必须**：无 Cookie 部分 API 限流或返回空数据，DOM 兜底可降级获取 UP 信息
- **直连推荐**：国内直连 + 多 Cookie 比机场代理更稳定（机场 IP 已被 B站 标记）
- **B站页面用 domcontentloaded**：`networkidle` 因持续连接永不触发导致超时
- **headful 是核心**：B站对 headless 返回空壳（0 个 BV 链接），UP 采集和 video_api 均使用 headful
- **前端无构建**：Vue 3 + Element Plus 全部 CDN 引入，无需 npm/Node.js
- **Windows 编码**：日志输出做了 GBK 兼容处理

## 依赖库

| 库 | 用途 | 说明 |
|---|------|------|
| `pw-stealth-enhanced` | 浏览器隐身 | playwright-stealth 官方继任者，30+ 检测点 |
| `browserforge` | UA/Headers 动态生成 | 版本匹配，消除指纹破绽 |
| `playwright` | 浏览器自动化 | headful Chromium |
| `fastapi` | Web 框架 | API + WebSocket |
| `sqlalchemy` | ORM | async + aiosqlite |
| `aiofiles` | 异步文件 | CSV/JSON 导出 |
| `redis` | 分布式队列 | 生产者-消费者模式，跨进程并发消费 |

