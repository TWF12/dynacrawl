import json
import logging
import asyncio
from abc import ABC, abstractmethod
from typing import Optional, Callable, Awaitable
from datetime import datetime

from backend.config import USE_REDIS, REDIS_URL, BROWSER_CONCURRENCY, MAX_RETRY

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int, int, str], Awaitable[None]]


class QueueInterface(ABC):
    @abstractmethod
    async def push(self, task_id: str, url_data: dict): pass
    @abstractmethod
    async def pop(self, timeout: float = 1.0) -> Optional[dict]: pass
    @abstractmethod
    async def length(self) -> int: pass


class MemoryQueue(QueueInterface):
    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
    async def push(self, task_id: str, url_data: dict):
        await self._queue.put({"task_id": task_id, **url_data})
    async def pop(self, timeout: float = 1.0) -> Optional[dict]:
        try: return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError: return None
    async def length(self) -> int:
        return self._queue.qsize()


class RedisQueue(QueueInterface):
    QUEUE_KEY = "dynacrawl:queue"
    def __init__(self, redis_url: str = REDIS_URL):
        self._redis_url = redis_url
        self._redis = None
    async def _ensure_redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
    async def push(self, task_id: str, url_data: dict):
        await self._ensure_redis()
        msg = json.dumps({"task_id": task_id, **url_data}, ensure_ascii=False)
        await self._redis.lpush(self.QUEUE_KEY, msg)
    async def pop(self, timeout: float = 1.0) -> Optional[dict]:
        await self._ensure_redis()
        result = await self._redis.brpop(self.QUEUE_KEY, timeout=int(timeout))
        if result:
            _, msg = result
            return json.loads(msg)
        return None
    async def length(self) -> int:
        await self._ensure_redis()
        return await self._redis.llen(self.QUEUE_KEY)


class CrawlDispatcher:
    def __init__(self, queue: QueueInterface, browser_pool, db_session_factory,
                 progress_callback: Optional[ProgressCallback] = None):
        self.queue = queue
        self.browser_pool = browser_pool
        self.db_session_factory = db_session_factory
        self.progress_callback = progress_callback
        self._running = False
        self._consumer_tasks: list[asyncio.Task] = []

    async def start(self):
        self._running = True
        for i in range(self.browser_pool.concurrency):
            task = asyncio.create_task(self._consumer_loop(i), name=f"crawler-consumer-{i}")
            self._consumer_tasks.append(task)
        logger.info(f"调度器已启动，消费者: {self.browser_pool.concurrency}")

    async def stop(self):
        self._running = False
        for task in self._consumer_tasks:
            task.cancel()
        await asyncio.gather(*self._consumer_tasks, return_exceptions=True)
        self._consumer_tasks.clear()
        logger.info("调度器已停止")

    async def submit_task(self, task_id: str, urls: list[dict]):
        for url_data in urls:
            await self.queue.push(task_id, url_data)
        logger.info(f"任务 {task_id} 已提交 {len(urls)} 个 URL")

    async def _consumer_loop(self, consumer_id: int):
        from backend.models import UrlRecord, Task, UpInfo, VideoInfo, Comment
        from backend.crawler.scraper_up import scrape_up_info, scrape_up_videos
        from backend.crawler.scraper_video import scrape_video_info, scrape_video_comments
        from sqlalchemy import select, func

        while self._running:
            msg = await self.queue.pop(timeout=2.0)
            if msg is None:
                continue

            task_id = msg["task_id"]
            url_id = msg["url_id"]
            url_type = msg["url_type"]
            retry_count = msg.get("retry_count", 0)

            async with self.db_session_factory() as session:
                try:
                    url_record = await session.get(UrlRecord, url_id)
                    if url_record:
                        url_record.status = "processing"
                        url_record.updated_at = datetime.now()
                        await session.commit()

                    result = None
                    async with self.browser_pool.acquire_page() as page:
                        if url_type == "up_api":
                            result = await scrape_up_info(page, msg.get("uid", ""))
                            if result:
                                session.add(UpInfo(
                                    task_id=task_id, uid=result.get("uid", ""),
                                    nickname=result.get("nickname", ""),
                                    avatar_url=result.get("avatar_url", ""),
                                    follower_count=result.get("follower_count"),
                                    video_count=result.get("video_count"),
                                    raw_data=result.get("raw_data"),
                                ))

                        elif url_type == "up_video_list":
                            videos = await scrape_up_videos(page, msg.get("uid", ""))
                            for v in videos:
                                session.add(VideoInfo(
                                    task_id=task_id, bv_id=v.get("bvid", ""),
                                    title=v.get("title", ""), play_count=v.get("play"),
                                    raw_data=v,
                                ))
                            result = {"video_count": len(videos)}

                        elif url_type == "video_api":
                            result = await scrape_video_info(page, msg.get("bv_id", ""))
                            if result:
                                session.add(VideoInfo(
                                    task_id=task_id, bv_id=result.get("bv_id", ""),
                                    title=result.get("title", ""),
                                    play_count=result.get("play_count"),
                                    like_count=result.get("like_count"),
                                    coin_count=result.get("coin_count"),
                                    danmaku_count=result.get("danmaku_count"),
                                    comment_count=result.get("comment_count"),
                                    raw_data=result.get("raw_data"),
                                ))

                        elif url_type == "video_comments":
                            comments = await scrape_video_comments(page, msg.get("bv_id", ""))
                            for c in comments:
                                session.add(Comment(
                                    task_id=task_id, bv_id=c.get("bv_id", ""),
                                    username=c.get("username", ""),
                                    content=c.get("content", ""),
                                    like_count=c.get("like_count"),
                                    posted_at=c.get("posted_at"),
                                ))
                            result = {"comment_count": len(comments)}

                    url_record = await session.get(UrlRecord, url_id)
                    if url_record:
                        url_record.status = "completed"
                        url_record.updated_at = datetime.now()

                    task = await session.get(Task, task_id)
                    if task:
                        completed_result = await session.execute(
                            select(func.count()).select_from(UrlRecord).where(
                                UrlRecord.task_id == task_id, UrlRecord.status == "completed"))
                        task.completed_urls = completed_result.scalar() or 0
                        failed_result = await session.execute(
                            select(func.count()).select_from(UrlRecord).where(
                                UrlRecord.task_id == task_id, UrlRecord.status == "failed"))
                        task.failed_urls = failed_result.scalar() or 0

                        all_done = await session.execute(
                            select(func.count()).select_from(UrlRecord).where(
                                UrlRecord.task_id == task_id,
                                UrlRecord.status.in_(["pending", "processing"])))
                        if (all_done.scalar() or 0) == 0:
                            task.status = "completed"
                        task.updated_at = datetime.now()

                    await session.commit()

                    if self.progress_callback and task:
                        await self.progress_callback(
                            task_id, task.completed_urls, task.total_urls, task.failed_urls,
                            f"已完成: {url_type}")

                except Exception as e:
                    logger.error(f"消费者 {consumer_id} 处理 URL {url_id} 失败: {e}")
                    try:
                        url_record = await session.get(UrlRecord, url_id)
                        if url_record:
                            if retry_count < MAX_RETRY:
                                url_record.retry_count = retry_count + 1
                                url_record.status = "pending"
                                url_record.error_msg = str(e)[:500]
                                msg["retry_count"] = retry_count + 1
                                await self.queue.push(task_id, msg)
                            else:
                                url_record.status = "failed"
                                url_record.error_msg = f"超过最大重试次数: {str(e)[:500]}"

                        task = await session.get(Task, task_id)
                        if task:
                            failed_result = await session.execute(
                                select(func.count()).select_from(UrlRecord).where(
                                    UrlRecord.task_id == task_id, UrlRecord.status == "failed"))
                            task.failed_urls = failed_result.scalar() or 0
                            task.updated_at = datetime.now()

                        await session.commit()
                    except Exception as inner_e:
                        logger.error(f"更新失败状态时出错: {inner_e}")
                        await session.rollback()


_dispatcher: Optional[CrawlDispatcher] = None


def get_dispatcher() -> Optional[CrawlDispatcher]:
    return _dispatcher


def set_dispatcher(dispatcher: CrawlDispatcher):
    global _dispatcher
    _dispatcher = dispatcher
