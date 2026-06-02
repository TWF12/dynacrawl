#!/usr/bin/env python
"""
DynaCrawl 启动入口
确保在 uvicorn 启动前设置 Windows ProactorEventLoop 策略。
Redis 模式下 --worker 仅启动消费者(不绑定端口), 支持多终端分布式。
"""

import sys
import os

# Windows 强制 UTF-8 输出
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

if __name__ == "__main__":
    worker_only = "--worker" in sys.argv

    if worker_only:
        # Worker-only 模式: 只消费 Redis 队列, 不启动 Web 服务
        import asyncio as _asyncio
        from backend.config import REDIS_URL
        from backend.crawler.dispatcher import RedisQueue, CrawlDispatcher
        from backend.crawler.browser_pool import browser_pool
        from backend.database import async_session

        async def _run_worker():
            queue = RedisQueue(REDIS_URL)
            dispatcher = CrawlDispatcher(queue, browser_pool, async_session)
            await browser_pool.start()
            await dispatcher.start()
            print(f"Worker 模式已启动 (PID: {os.getpid()}), 按 Ctrl+C 停止")
            try:
                while True:
                    await _asyncio.sleep(3600)
            except KeyboardInterrupt:
                pass
            finally:
                await dispatcher.stop()
                await browser_pool.stop()

        _asyncio.run(_run_worker())
    else:
        import uvicorn

        host = os.getenv("HOST", "0.0.0.0")
        port = int(os.getenv("PORT", "8000"))
        reload = "--reload" in sys.argv
        print(f"启动 DynaCrawl 服务: http://{host}:{port}")
        uvicorn.run(
            "backend.main:app", host=host, port=port, reload=reload, log_level="warning"
        )
