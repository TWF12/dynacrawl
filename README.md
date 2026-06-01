# DynaCrawl

B站数据采集平台 —— Playwright + FastAPI + asyncio，内置多层反爬机制。

## 快速开始

```bash
# 1. 安装
uv sync
uv run playwright install chromium

# 2. 保存 Cookie（扫码一次即可，支持多账号）
uv run python save_cookie.py

# 3. 启动
uv run python run.py
# → http://localhost:8000
```

## 采集场景

| 场景 | 输入 | 采集内容 |
|------|------|---------|
| UP 主信息 | UID | 昵称、头像、粉丝数、视频总数、全部视频 BV号+标题+播放量 |
| 视频详情 | BV号 | 标题、播放/点赞/投币/弹幕/评论数、评论内容（上限 1000 条） |

## 反爬能力

- **pw-stealth-enhanced** 浏览器隐身（Canvas/WebGL/AudioContext 等 30+ 指纹点）
- **browserforge** 动态 UA/Headers 生成（版本始终匹配）
- **headful 浏览器**（B站对 headless 返回空壳）
- 多 Cookie 自动轮换 + 直连采集
- WBI 签名 + Session 轮换 + 渐进延迟
- 行为拟人化（贝塞尔鼠标轨迹、分段滚动、模拟停留）
- DOM 兜底（API 全部失败时仍可提取 UP 信息 + 视频列表）

## 配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BROWSER_CONCURRENCY` | 3 | 浏览器并发数 |
| `PROXY_LIST` | (空) | 代理 URL 列表，逗号分隔；留空则直连 |
| `COOKIE_DIR` | data/cookies/ | Cookie 存储目录 |
| `DATABASE_URL` | sqlite:///data/dynacrawl.db | 数据库 |

全部配置见 `backend/config.py`，架构详情见 `AGENTS.md`。
