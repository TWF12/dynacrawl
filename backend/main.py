"""
动态爬虫数据采集平台 - FastAPI 主入口
"""

import sys
import os
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError

from backend.config import USE_REDIS, REDIS_URL, BASE_DIR, PROXY_LIST
from backend.crawler.cookie_manager import cookie_manager
from backend.database import init_db, async_session
from backend.crawler.browser_pool import browser_pool
from backend.crawler.dispatcher import (
    CrawlDispatcher,
    MemoryQueue,
    RedisQueue,
    set_dispatcher,
)
from backend.routers import tasks, results, ws
from backend.services import task_service

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def _print_startup_warnings():
    """启动时检查关键配置, 缺失则打印警告"""
    warnings = []

    # Cookie 检查 (验证前仅检查是否存在)
    if cookie_manager.count == 0:
        warnings.append("未找到 B站 Cookie → 无法正常采集!")
        warnings.append("  解决: uv run python save_cookie.py  扫码登录")
        warnings.append("  无登录态: API 频次受限、部分 UP 主数据为空、风控阈值低")

    # 代理检查
    proxy_list_raw = os.environ.get("PROXY_LIST", "")
    if proxy_list_raw:
        logger.info("代理已配置(PROXY_LIST): %d 个地址", len(PROXY_LIST))
    else:
        logger.info("代理未配置, 使用直连 (配合 Cookie + 合理延迟可稳定采集)")

    if warnings:
        logger.warning("=" * 50)
        logger.warning("!! 启动配置警告 !!")
        for w in warnings:
            logger.warning(w)
        logger.warning("=" * 50)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _print_startup_warnings()

    logger.info("正在验证 Cookie...")
    await cookie_manager.validate_all()
    cookie_count = cookie_manager.count
    if cookie_count >= 2:
        logger.info("Cookie: %d 个有效 (自动轮换)", cookie_count)
    elif cookie_count == 1:
        logger.info("Cookie: 1 个有效 (多账号可运行 save_cookie.py 添加)")

    logger.info("正在初始化数据库...")
    await init_db()

    logger.info("正在启动浏览器池...")
    await browser_pool.start()

    if USE_REDIS:
        import redis.asyncio as aioredis

        try:
            r = aioredis.from_url(REDIS_URL)
            await r.ping()
            await r.aclose()
            logger.info("使用 Redis 队列模式")
            queue = RedisQueue(REDIS_URL)
        except Exception as e:
            logger.warning("Redis 不可达 (%s), 降级为内存队列模式", e)
            queue = MemoryQueue()
    else:
        logger.info("使用内存队列模式")
        queue = MemoryQueue()

    dispatcher = CrawlDispatcher(
        queue=queue,
        browser_pool=browser_pool,
        db_session_factory=async_session,
        progress_callback=ws.progress_callback,
    )
    set_dispatcher(dispatcher)
    await dispatcher.start()

    # Redis 模式: 启动进度同步 + 心跳
    _redis_sync_task = None
    _heartbeat_task = None
    if USE_REDIS:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(REDIS_URL)

        async def _sync_redis_progress():
            """轮询 Redis 中 Worker 写入的进度, 推送到 WebSocket"""
            while True:
                try:
                    keys = await _redis.keys("dynacrawl:progress:*")
                    for key in keys:
                        task_id = key.decode().split(":")[-1]
                        data = await _redis.hgetall(key)
                        if data:
                            await ws.progress_callback(
                                task_id,
                                int(data.get(b"completed", 0)),
                                int(data.get(b"total", 0)),
                                int(data.get(b"failed", 0)),
                                data.get(b"message", b"").decode(),
                            )
                    await _redis.delete(*keys)  # 已推送的清理
                except Exception:
                    pass
                await asyncio.sleep(1)

        async def _master_heartbeat():
            """每2秒刷新心跳, TTL 5秒; Worker 检测不到心跳则自动停止"""
            while True:
                try:
                    await _redis.set("dynacrawl:master_alive", "1", ex=5)
                except Exception:
                    pass
                await asyncio.sleep(2)

        _redis_sync_task = asyncio.create_task(_sync_redis_progress())
        _heartbeat_task = asyncio.create_task(_master_heartbeat())

    logger.info("正在恢复未完成的任务...")
    recovered = await task_service.recover_pending_tasks(dispatcher)
    if recovered > 0:
        logger.info(f"已恢复 {recovered} 个未完成任务")
    else:
        logger.info("没有需要恢复的任务")

    yield

    # 停止心跳 (Worker 检测到后自动退出)
    if _heartbeat_task:
        _heartbeat_task.cancel()
    if USE_REDIS:
        try:
            await _redis.set("dynacrawl:master_alive", "0", ex=10)
            await _redis.aclose()
        except Exception:
            pass
    logger.info("正在停止调度器...")
    await dispatcher.stop()
    logger.info("正在关闭浏览器池...")
    await browser_pool.stop()


app = FastAPI(
    title="DynaCrawl - 动态爬虫数据采集平台",
    description="基于 Playwright 的 B 站数据采集平台，支持断点续采与并发爬取",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tasks.router)
app.include_router(results.router)
app.include_router(ws.router)

frontend_dir = BASE_DIR / "frontend"
frontend_dir.mkdir(exist_ok=True)
_no_cache_types = {".html", ".js", ".css"}


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if any(path.endswith(ext) for ext in _no_cache_types):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422, content={"detail": exc.errors(), "message": "请求参数验证失败"}
    )


@app.get("/")
async def root():
    return FileResponse(str(frontend_dir / "index.html"))


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}
