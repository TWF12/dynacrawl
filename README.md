# DynaCrawl

B站数据采集平台 —— 基于 Playwright + FastAPI + asyncio，内置多层反爬机制。

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 安装 Chromium 浏览器
uv run playwright install chromium

# 3. 保存 B站 登录 Cookie（必须，扫码一次即可）
uv run python save_cookie.py

# 4. 启动服务
uv run python run.py

# 5. 打开浏览器访问
# http://localhost:8000
```

## 前置条件

### Cookie（必须）

B站 大部分 API 需要登录态才能正常访问。未登录时：
- card API 和 arc/search API 可能被限流或返回空数据
- 部分 UP主的视频列表可能无法获取
- 风控阈值显著降低

**获取方式：**
```bash
uv run python save_cookie.py
# → 弹出 Chromium 浏览器 → 点击右上角「登录」扫码
# → 登录成功后回到终端按 Enter → Cookie 保存到 data/cookies/cookie_1.json
```

**多账号轮换（可选）：**
多次运行 `save_cookie.py` 用不同账号扫码，Cookie 会保存为 `cookie_1.json`、`cookie_2.json`...程序每次创建新浏览器会话时自动轮换到下一个有效 Cookie。同一账号重复扫码会自动更新已有文件。

**过期自动处理：**
- 启动时通过 B站 API 验证所有 Cookie 是否有效，过期自动删除
- 采集过程中如检测到登录失效（B站返回 -101），自动删除当前 Cookie 并切换下一个
- 所有 Cookie 过期时打印警告

Cookie 文件有效期约 1-3 个月。

### 代理（强烈建议）

直连 B站 API 在高频采集时几乎必然触发风控（-352 / -412）。支持两种代理模式：

#### 方式 A：Clash 代理（推荐，支持自动 IP 轮换）

```bash
# 确保 Clash 已启动，API 端口可访问（默认 9090）
# 无需额外配置，程序自动检测 Clash 并切换节点
```

相关环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CLASH_CONTROLLER` | `http://127.0.0.1:9090` | Clash REST API 地址 |
| `CLASH_PROXY` | `http://127.0.0.1:7890` | Clash 代理端口 |
| `CLASH_GROUP` | (自动检测) | 指定代理组名，留空则自动找第一个可用组 |

程序会通过 Clash API 自动切换代理组内的上游节点，每次创建新浏览器会话时轮换一次出口 IP。切换成功后会打印日志确认新 IP。

#### 方式 B：普通代理列表

```bash
# 设置多个代理地址，程序自动轮换
export PROXY_LIST="http://user:pass@proxy1:8080,http://user:pass@proxy2:8080"
```

如果配置了多个代理地址，每次创建新浏览器会话时会自动切换到不同的代理。

#### 方式 C：无代理

不配置任何代理时程序会直连，启动时打印警告。仅供测试少量数据时使用，**大量采集必然触发风控**。

## 采集场景

| 场景 | 输入 | 采集内容 |
|------|------|----------|
| UP主信息 | UID（如 `456664753`） | 昵称、头像、粉丝数、视频总数、全部视频（BV号/标题/播放量） |
| 视频详情 | BV号（如 `BV1xx411c7mD`） | 标题、播放/点赞/投币/弹幕/评论数、热门评论内容 |

### 使用流程

1. 打开 `http://localhost:8000`
2. 选择采集场景（UP主信息 / 视频详情）
3. 输入 UID 或 BV号，点击「提交采集任务」
4. 任务列表中可实时查看进度条和状态
5. 点击「详情」查看采集结果，支持导出 CSV / JSON

## 全部环境变量

### 浏览器控制

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BROWSER_CONCURRENCY` | `3` | 浏览器最大并发数 |
| `BROWSER_HEADLESS` | `true` | 无头模式（UP主采集强制使用有头模式绕过风控） |
| `PAGE_TIMEOUT` | `30000` | 页面加载超时（毫秒） |

### 请求频率

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `REQUEST_DELAY_MIN` | `3.0` | 请求最小延迟（秒） |
| `REQUEST_DELAY_MAX` | `8.0` | 请求最大延迟（秒） |
| `MAX_RETRY` | `2` | 单 URL 失败最大重试次数 |

> **注意：** 实际采集延迟会随页数递增（渐进延迟策略），基础延迟仅用于首页和小数据量场景。详见「反爬机制」一节。

### 代理

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_LIST` | (空) | 代理地址列表，逗号分隔。留空则从 Clash 自动获取 |
| `CLASH_CONTROLLER` | `http://127.0.0.1:9090` | Clash REST API 地址 |
| `CLASH_PROXY` | `http://127.0.0.1:7890` | Clash 代理端口 |
| `CLASH_GROUP` | (自动检测) | Clash 代理组名 |

### 存储

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `sqlite+aiosqlite:///data/dynacrawl.db` | 数据库地址 |
| `USE_REDIS` | `false` | 启用 Redis 多 Worker 模式 |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 地址 |
| `QUEUE_KEY` | `dynacrawl:queue` | Redis 队列 Key |

### 运行

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |

## 反爬机制

DynaCrawl 内置多层反爬措施，按层级排列：

### 浏览器层
- **有头模式**（UP主采集）：最小化窗口移出屏幕，绕过 headless 检测
- **启动参数**：`--disable-blink-features=AutomationControlled` 去掉 webdriver 标记
- **随机 UA 池**：10 个 Chrome/Edge/Firefox UA 随机选取
- **B站 登录 Cookie**：模拟已登录真实用户

### 指纹隐身
- webdriver 隐藏、Chrome runtime 伪造
- 随机化硬件指纹：CPU 核心数、内存、色深
- Canvas 指纹噪声：每次 toDataURL 修改像素最低位
- WebGL 指纹噪声：伪造成 Intel GPU
- Plugins / MimeTypes 伪造
- 语言/平台伪装：zh-CN / Win32

### HTTP 请求层
- Context 级伪装头：Accept-Language、Accept、Sec-Ch-UA、Sec-Ch-UA-Platform
- 每次 API 调用附加正确的 Referer 和 Origin
- 随机视口尺寸（1400-1920 × 800-1080）

### IP 轮换
- Clash 模式：每次新建浏览器会话时自动切换上游节点
- 普通代理模式：轮换 PROXY_LIST 中的不同代理地址
- 切换后验证出口 IP 并打印日志确认

### Session 轮换（核心防风控）
- 每 25-40 页（随机）自动关闭当前浏览器会话，创建新会话
- 新会话 = 新 Cookie 上下文 + 新 Clash 节点 + 新 WBI 密钥
- 遇到 -352/-412 风控立即切换会话并重试
- 连续 3 页失败也自动切换

### 时序控制
- 基础延迟：3-8 秒随机
- 渐进延迟：

| 页数范围 | 延迟范围 |
|----------|----------|
| 1-20 | 5-12 秒 |
| 21-50 | 10-25 秒 |
| 51-100 | 18-40 秒 |
| 100+ | 30-60 秒 |

- 风控重试前等待 30-60 秒

### API 签名
- 所有 arc/search 请求带 WBI 签名（w_rid + wts）
- WBI 密钥每 10 分钟自动刷新，跟随 B站 密钥轮换节奏
- 每个新 Session 重新从投稿页获取密钥

### DOM 兜底
- API 全部失败时降级为加载用户页面、滚动懒加载、从 DOM 提取视频信息

## 项目结构

```
dynacrawl/
├── run.py                  # 启动入口
├── save_cookie.py          # Cookie 扫码保存工具
├── README.md
├── pyproject.toml
├── data/
│   ├── dynacrawl.db        # SQLite 数据库
│   └── bilibili_cookies.json  # B站 登录 Cookie
├── backend/
│   ├── main.py             # FastAPI 入口 + 启动检查
│   ├── config.py           # 全局配置
│   ├── database.py         # 数据库引擎
│   ├── models.py           # ORM 模型 (Task/UrlRecord/UpInfo/VideoInfo/Comment)
│   ├── schemas.py          # Pydantic 模型
│   ├── routers/
│   │   ├── tasks.py        # 任务 CRUD API
│   │   ├── results.py      # 结果查询 + 导出 API
│   │   └── ws.py           # WebSocket 实时进度推送
│   ├── services/
│   │   ├── task_service.py # 任务创建/恢复/删除
│   │   └── export_service.py  # CSV/JSON 导出
│   └── crawler/
│       ├── scraper_up.py   # UP主采集（视频列表 + 基本信息）
│       ├── scraper_video.py   # 视频详情采集
│       ├── browser_pool.py # 浏览器池管理（headless + headful）
│       ├── anti_detect.py  # 反检测（隐身脚本/UA/代理轮换）
│       ├── wbi_sign.py     # B站 WBI 签名
│       ├── url_processor.py   # URL 处理逻辑
│       └── dispatcher.py   # 任务调度（内存/Redis 队列）
└── frontend/
    ├── index.html          # Vue 3 前端页面
    ├── app.js              # 前端逻辑
    └── style.css           # 样式
```

## 常见问题

### Q: 启动时提示「未找到 B站 Cookie 文件」？
运行 `uv run python save_cookie.py` 扫码登录。Cookie 是必须的，无 Cookie 无法正常采集。

### Q: 启动时提示「Clash API 不可达」？
确认 Clash 已启动，检查 Settings → API 端口是否为 9090。如果使用其他代理，设置 `PROXY_LIST` 环境变量。

### Q: 采集中途卡住不动？
B站页面可能触发了风控。检查日志中是否有 `风控触发` 或 `-352/-412` 字样。程序会自动切换会话重试，如果持续失败建议：
- 确认代理已配置且 IP 能正常切换
- 降低 `BROWSER_CONCURRENCY` 到 1
- 增大 `REQUEST_DELAY_MIN` 和 `REQUEST_DELAY_MAX`

### Q: 视频数量比实际少？
查看任务详情中 URL 状态列的错误信息：
- `视频不全`：风控导致部分页面采集失败，可重新提交同一 UID 补充采集
- `DOM提取`：API 采集失败后降级为 DOM 提取，数量可能不如 API 准确
- `投稿页超时`：网络问题或 B站 页面加载慢，尝试增大 `PAGE_TIMEOUT`

### Q: 如何导出数据？
在任务详情页点击「导出 CSV」或「导出 JSON」。也支持直接访问 API：
- `GET /api/tasks/{task_id}/export/csv`
- `GET /api/tasks/{task_id}/export/json`

### Q: 支持多任务并发吗？
默认使用内存队列，支持多任务排队执行。设置 `BROWSER_CONCURRENCY` 控制同时打开的浏览器数。如果部署多 Worker，设置 `USE_REDIS=true` 启用 Redis 队列模式。

### Q: Cookie 过期了怎么办？
重新运行 `uv run python save_cookie.py` 扫码登录即可，新 Cookie 会覆盖旧文件。Cookie 有效期通常 1-3 个月。
