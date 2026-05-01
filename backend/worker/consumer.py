"""
独立 Worker 进程（Redis 模式）
启动方式: uv run python -m backend.worker.consumer
"""
import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json, logging
from datetime import datetime
import redis.asyncio as aioredis
from backend.config import REDIS_URL, BROWSER_CONCURRENCY, MAX_RETRY
from backend.database import async_session, init_db
from backend.crawler.browser_pool import BrowserPool
from backend.crawler.scraper_up import scrape_up_info, scrape_up_videos
from backend.crawler.scraper_video import scrape_video_info, scrape_video_comments
from backend.models import UrlRecord, Task, UpInfo, VideoInfo, Comment
from sqlalchemy import select, func

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("worker")
QUEUE_KEY = "dynacrawl:queue"


async def process_url(msg: dict, browser_pool: BrowserPool):
    task_id = msg["task_id"]
    url_id = msg["url_id"]
    url_type = msg["url_type"]
    retry_count = msg.get("retry_count", 0)

    async with async_session() as session:
        try:
            url_record = await session.get(UrlRecord, url_id)
            if url_record:
                url_record.status = "processing"
                url_record.updated_at = datetime.now()
                await session.commit()

            async with browser_pool.acquire_page() as page:
                if url_type == "up_api":
                    result = await scrape_up_info(page, msg.get("uid", ""))
                    if result:
                        session.add(UpInfo(task_id=task_id, uid=result.get("uid",""),
                                            nickname=result.get("nickname",""),
                                            avatar_url=result.get("avatar_url",""),
                                            follower_count=result.get("follower_count"),
                                            video_count=result.get("video_count"),
                                            raw_data=result.get("raw_data")))

                elif url_type == "up_video_list":
                    videos = await scrape_up_videos(page, msg.get("uid", ""))
                    for v in videos:
                        session.add(VideoInfo(task_id=task_id, bv_id=v.get("bvid",""),
                                               title=v.get("title",""), play_count=v.get("play"), raw_data=v))

                elif url_type == "video_api":
                    result = await scrape_video_info(page, msg.get("bv_id", ""))
                    if result:
                        session.add(VideoInfo(task_id=task_id, bv_id=result.get("bv_id",""),
                                               title=result.get("title",""),
                                               play_count=result.get("play_count"),
                                               like_count=result.get("like_count"),
                                               coin_count=result.get("coin_count"),
                                               danmaku_count=result.get("danmaku_count"),
                                               comment_count=result.get("comment_count"),
                                               raw_data=result.get("raw_data")))

                elif url_type == "video_comments":
                    comments = await scrape_video_comments(page, msg.get("bv_id", ""))
                    for c in comments:
                        session.add(Comment(task_id=task_id, bv_id=c.get("bv_id",""),
                                             username=c.get("username",""),
                                             content=c.get("content",""),
                                             like_count=c.get("like_count"),
                                             posted_at=c.get("posted_at")))

            url_record = await session.get(UrlRecord, url_id)
            if url_record:
                url_record.status = "completed"
                url_record.updated_at = datetime.now()

            task = await session.get(Task, task_id)
            if task:
                c = (await session.execute(select(func.count()).select_from(UrlRecord).where(
                    UrlRecord.task_id==task_id, UrlRecord.status=="completed"))).scalar() or 0
                f = (await session.execute(select(func.count()).select_from(UrlRecord).where(
                    UrlRecord.task_id==task_id, UrlRecord.status=="failed"))).scalar() or 0
                task.completed_urls = c
                task.failed_urls = f
                d = (await session.execute(select(func.count()).select_from(UrlRecord).where(
                    UrlRecord.task_id==task_id, UrlRecord.status.in_(["pending","processing"])))).scalar() or 0
                if d == 0:
                    task.status = "completed"
                task.updated_at = datetime.now()

            await session.commit()
            logger.info(f"URL 处理完成: {url_id} ({url_type})")

        except Exception as e:
            logger.error(f"处理 URL {url_id} 失败: {e}")
            try:
                url_record = await session.get(UrlRecord, url_id)
                if url_record:
                    if retry_count < MAX_RETRY:
                        url_record.retry_count = retry_count + 1
                        url_record.status = "pending"
                        url_record.error_msg = str(e)[:500]
                    else:
                        url_record.status = "failed"
                        url_record.error_msg = f"超过最大重试次数: {str(e)[:500]}"
                await session.commit()
            except Exception:
                await session.rollback()


async def consumer_loop(redis_client: aioredis.Redis, browser_pool: BrowserPool):
    logger.info(f"Worker 消费者已启动，并发数: {browser_pool.concurrency}")
    while True:
        try:
            result = await redis_client.brpop(QUEUE_KEY, timeout=2)
            if result:
                _, msg_str = result
                msg = json.loads(msg_str)
                asyncio.create_task(process_url(msg, browser_pool))
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
