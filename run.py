#!/usr/bin/env python
"""
DynaCrawl 启动入口
确保在 uvicorn 启动前设置 Windows ProactorEventLoop 策略。
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
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    reload = "--reload" in sys.argv
    print(f"启动 DynaCrawl 服务: http://{host}:{port}")
    uvicorn.run(
        "backend.main:app", host=host, port=port, reload=reload, log_level="warning"
    )
