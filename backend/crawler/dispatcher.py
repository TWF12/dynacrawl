import json
import logging
import asyncio
from abc import ABC, abstractmethod
from typing import Optional

from backend.config import REDIS_URL, QUEUE_KEY
from backend.crawler.anti_detect import mark_task_cancelled, is_task_cancelled
from backend.crawler.url_processor import process_url_message, ProgressCallback

logger = logging.getLogger(__name__)


class QueueInterface(ABC):
    @abstractmethod
    async def push(self, task_id: str, url_data: dict): pass
    @abstractmethod
    async def pop(self, timeout: float = 1.0) -> Optional[dict]: pass
    @abstractmethod
    async def length(self) -> int: pass
    @abstractmethod
    async def remove_task(self, task_id: str) -> int: pass


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
    async def remove_task(self, task_id: str) -> int:
        removed = 0
        kept = []
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item.get("task_id") == task_id:
                    removed += 1
                else:
                    kept.append(item)
            except asyncio.QueueEmpty:
                break
        for item in kept:
            await self._queue.put(item)
        return removed


class RedisQueue(QueueInterface):
    QUEUE_KEY = QUEUE_KEY
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
    async def remove_task(self, task_id: str) -> int:
        # Redis 队列无法高效过滤单个任务, 由 consumer 侧跳过 + 过期 collection 清理
        return 0


class CrawlDispatcher:
    def __init__(self, queue: QueueInterface, browser_pool, db_session_factory,
                 progress_callback: Optional[ProgressCallback] = None):
        self.queue = queue
        self.browser_pool = browser_pool
        self.db_session_factory = db_session_factory
        self.progress_callback = progress_callback
        self._running = False
        self._consumer_tasks: list[asyncio.Task] = []
        self._cancelled_tasks: set[str] = set()

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

    async def cancel_task(self, task_id: str):
        """取消任务: 全局标记 + 清理队列中待处理的 URL"""
        self._cancelled_tasks.add(task_id)
        mark_task_cancelled(task_id)
        removed = await self.queue.remove_task(task_id)
        logger.info(f"任务 {task_id} 已取消, 队列中移除 {removed} 个 URL")

    async def _consumer_loop(self, consumer_id: int):
        label = f"消费者 {consumer_id} "

        async def enqueue(task_id: str, msg: dict):
            if not is_task_cancelled(task_id):
                await self.queue.push(task_id, msg)

        while self._running:
            msg = await self.queue.pop(timeout=2.0)
            if msg is None:
                continue

            task_id = msg.get("task_id", "")
            if is_task_cancelled(task_id):
                logger.info("%s跳过已取消任务 %s 的 URL", label, task_id)
                continue

            async with self.db_session_factory() as session:
                await process_url_message(
                    msg, self.browser_pool, session,
                    enqueue_callback=enqueue,
                    progress_callback=self.progress_callback,
                    consumer_label=label,
                )


_dispatcher: Optional[CrawlDispatcher] = None


def get_dispatcher() -> Optional[CrawlDispatcher]:
    return _dispatcher


def set_dispatcher(dispatcher: CrawlDispatcher):
    global _dispatcher
    _dispatcher = dispatcher
