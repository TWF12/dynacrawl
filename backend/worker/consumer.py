"""
独立 Worker 进程（Redis 模式）
启动方式: uv run python -m backend.worker.consumer
"""
import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json, logging
import redis.asyncio as aioredis
from backend.config import REDIS_URL, BROWSER_CONCURRENCY
from backend.database import async_session, init_db
from backend.crawler.browser_pool import BrowserPool
from backend.crawler.url_processor import process_url_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("worker")
QUEUE_KEY = "dynacrawl:queue"


async def consumer_loop(redis_client: aioredis.Redis, browser_pool: BrowserPool):
    async def enqueue(task_id: str, msg: dict):
        await redis_client.lpush(QUEUE_KEY, json.dumps(msg, ensure_ascii=False))

    logger.info(f"Worker 消费者已启动，并发数: {browser_pool.concurrency}")
    while True:
        try:
            result = await redis_client.brpop(QUEUE_KEY, timeout=2)
            if result:
                _, msg_str = result
                msg = json.loads(msg_str)

                async def handle():
                    async with async_session() as session:
                        await process_url_message(
                            msg, browser_pool, session,
                            enqueue_callback=enqueue,
                            consumer_label="[Worker] ",
                        )
                asyncio.create_task(handle())
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"消费循环异常: {e}")
            await asyncio.sleep(1)


async def main():
    await init_db()
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    browser_pool = BrowserPool(concurrency=BROWSER_CONCURRENCY)
    await browser_pool.start()
    try:
        await consumer_loop(redis_client, browser_pool)
    finally:
        await browser_pool.stop()
        await redis_client.close()

if __name__ == "__main__":
    asyncio.run(main())
