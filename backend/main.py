"""
动态爬虫数据采集平台 - FastAPI 主入口
"""
import sys
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

from backend.config import USE_REDIS, REDIS_URL, BASE_DIR
from backend.database import init_db, async_session
from backend.crawler.browser_pool import browser_pool
from backend.crawler.dispatcher import CrawlDispatcher, MemoryQueue, RedisQueue, set_dispatcher
from backend.routers import tasks, results, ws
from backend.services import task_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("正在初始化数据库...")
    await init_db()

    logger.info("正在启动浏览器池...")
    await browser_pool.start()

    if USE_REDIS:
        logger.info("使用 Redis 队列模式")
        queue = RedisQueue(REDIS_URL)
    else:
        logger.info("使用内存队列模式")
        queue = MemoryQueue()

    dispatcher = CrawlDispatcher(
        queue=queue, browser_pool=browser_pool,
        db_session_factory=async_session,
        progress_callback=ws.progress_callback,
    )
    set_dispatcher(dispatcher)
    await dispatcher.start()

    logger.info("正在恢复未完成的任务...")
    recovered = await task_service.recover_pending_tasks(dispatcher)
    if recovered > 0:
        logger.info(f"已恢复 {recovered} 个未完成任务")
    else:
        logger.info("没有需要恢复的任务")

    yield

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

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                    allow_methods=["*"], allow_headers=["*"])

app.include_router(tasks.router)
app.include_router(results.router)
app.include_router(ws.router)

frontend_dir = BASE_DIR / "frontend"
frontend_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.errors(), "message": "请求参数验证失败"})


@app.get("/")
async def root():
    return FileResponse(str(frontend_dir / "index.html"))


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}
