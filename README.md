# DynaCrawl

B站数据采集平台 —— 基于 Playwright + FastAPI + asyncio。

## 快速开始

```bash
# 安装浏览器
uv run playwright install chromium

# 保存登录 Cookie（扫码一次即可）
uv run python save_cookie.py

# 启动
uv run python run.py
# 访问 http://localhost:8000
```

## 采集场景

| 场景 | 输入 | 说明 |
|------|------|------|
| UP主信息 | UID | 昵称、头像、粉丝数、视频数、全部视频(BV/标题/播放量) |
| 视频详情 | BV号 | 标题、播放/点赞/投币/弹幕/评论数、评论内容 |

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BROWSER_CONCURRENCY` | 3 | 浏览器并发数 |
| `BROWSER_HEADLESS` | true | 无头模式 |
| `REQUEST_DELAY_MIN` | 1.0 | 请求最小延迟(秒) |
| `REQUEST_DELAY_MAX` | 3.0 | 请求最大延迟(秒) |
| `PAGE_TIMEOUT` | 30000 | 页面超时(毫秒) |
| `MAX_RETRY` | 2 | 失败重试次数 |
| `DATABASE_URL` | sqlite+aiosqlite:///data/dynacrawl.db | 数据库地址 |
| `USE_REDIS` | false | 启用 Redis 多 Worker 模式 |
| `REDIS_URL` | redis://localhost:6379/0 | Redis 地址 |
| `PROXY_LIST` | (空) | 代理列表，逗号分隔 |

## 项目结构

```
backend/
├── main.py              # FastAPI 入口
├── config.py            # 全局配置
├── database.py          # 数据库引擎
├── models.py            # ORM 模型
├── schemas.py           # Pydantic 模型
├── routers/             # API 路由
├── services/            # 业务逻辑
├── crawler/             # 爬虫核心
│   ├── scraper_up.py    # UP主采集
│   ├── scraper_video.py # 视频采集
│   ├── browser_pool.py  # 浏览器池
│   ├── anti_detect.py   # 反检测
│   ├── wbi_sign.py      # WBI签名
│   ├── error_codes.py   # 错误码
│   └── dispatcher.py    # 任务调度
└── worker/              # Redis Worker
frontend/                # Vue 3 前端
```

## 错误码

访问 `GET /api/tasks/error-codes` 查看所有错误码说明。

| 码 | 含义 |
|----|------|
| E001 | card API 请求失败 |
| E002 | 无法获取视频总数 |
| E101 | arc/search API 被风控拦截 |
| E103 | 视频列表不完整 |
| E104 | 未获取到任何视频数据 |
| E105 | 未加载登录 cookie |
| E201 | 网络请求超时 |
| E202 | 页面加载失败 |
| E203 | WBI 签名密钥获取失败 |
