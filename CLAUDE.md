# DynaCrawl - B站数据采集平台

## 用户 TWF
全栈独立开发者，FastAPI + Playwright + Vue 3，B站反爬/风控/代理轮换。
**所有对话使用中文**，偏好直接高效的沟通。

## 自动执行权限
本项目所有操作**无需询问确认**，直接执行：git 提交、数据库操作、服务器重启、进程管理、批量任务提交、代码修改。

## 对称优化原则
对一种任务（UP主/视频/评论）优化或修复时，**同步检查另一种任务是否有相同问题**并修复。

## 每次代码修改后
1. **git commit** — 使用中文 commit message
2. **主动测试验证** — 重启服务 + 提交测试任务 + 监控至少 2 轮
3. **查看日志** — 搜索 WARNING/ERROR，分析根因
4. **核对数据** — DB 中视频数/状态是否正确
5. **对称检查** — 同类型修复是否遗漏另一种任务
6. **全局影响分析** — 修改前先想清楚：
   - 并发/semaphore 是否死锁
   - 浏览器进程/内存是否泄漏
   - Cookie/代理轮换是否正确
   - 其他模块是否受影响
   - 错误处理和重试逻辑是否完整
7. **同步文档** — 架构/功能变更后更新 AGENTS.md 和 README.md

## 主动测试规则
不等用户发现 bug，自行验证：
- 边界场景：无 Cookie、无代理、单代理、单 Cookie
- 异常模式：大面积超时、context 创建失败、连接断开
- 数据正确性：视频数、状态、重复、昵称/粉丝提取
- 多轮监控：间隔递增检查，直到确认稳定

## 技术要点
- B站反爬：pw-stealth-enhanced + browserforge 指纹隐身、headful 浏览器（headless 返回空壳）、WBI 签名
- 代理：默认直连（国内最稳），可选 PROXY_LIST 自定义代理
- DOM 兜底：`/upload/video` 页面（登录墙下仍可见 navBar）
- Cookie 管理：`data/cookies/*.json`，`cookie_manager.py` 轮换

## 测试数据集
UP主 UID (10个): `1619759224` `456664753` `400999844` `2475977` `37974444` `688379639` `95228778` `66569167` `15773384` `8047632`

视频 BV (10个): `BV1rSoLBWErZ` `BV1h29DBaEzL` `BV15C4y1Y71E` `BV1ZR96BLEif` `BV1b7411N798` `BV1MFokBkEzC` `BV1JBorBoEXh` `BV1wZ92BBEGV` `BV1LB4y1G7gG` `BV1YiaMzkEGz`

测试时从中自行挑选，不需要用户每次指定。
