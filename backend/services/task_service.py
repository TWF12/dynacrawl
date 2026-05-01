import logging
from datetime import datetime
from typing import Optional
from sqlalchemy import select, func
from backend.database import async_session
from backend.models import Task, UrlRecord, UpInfo, VideoInfo, Comment
from backend.schemas import SceneType, TaskStatus

logger = logging.getLogger(__name__)


async def create_task(scene: str, input_value: str, dispatcher=None) -> Task:
    async with async_session() as session:
        task = Task(scene=scene, input_value=input_value, status=TaskStatus.PENDING.value)
        session.add(task)
        await session.flush()

        urls_to_queue = []
        if scene == SceneType.UP_INFO.value:
            uid = input_value
            url_record = UrlRecord(task_id=task.id,
                                   url=f"https://api.bilibili.com/x/space/wbi/acc/info?mid={uid}",
                                   url_type="up_api")
            session.add(url_record)
            await session.flush()
            urls_to_queue.append({"url_id": url_record.id, "url": url_record.url,
                                   "url_type": url_record.url_type, "uid": uid, "retry_count": 0})

            url_record2 = UrlRecord(task_id=task.id,
                                    url=f"https://api.bilibili.com/x/space/wbi/arc/search?mid={uid}",
                                    url_type="up_video_list")
            session.add(url_record2)
            await session.flush()
            urls_to_queue.append({"url_id": url_record2.id, "url": url_record2.url,
                                   "url_type": url_record2.url_type, "uid": uid, "retry_count": 0})

        elif scene == SceneType.VIDEO_DETAIL.value:
            bv_id = input_value
            url_record = UrlRecord(task_id=task.id,
                                   url=f"https://api.bilibili.com/x/web-interface/view?bvid={bv_id}",
                                   url_type="video_api")
            session.add(url_record)
            await session.flush()
            urls_to_queue.append({"url_id": url_record.id, "url": url_record.url,
                                   "url_type": url_record.url_type, "bv_id": bv_id, "retry_count": 0})

            url_record2 = UrlRecord(task_id=task.id,
                                    url=f"https://api.bilibili.com/x/v2/medialist/resource/list?type=1&biz_id=1&bvid={bv_id}",
                                    url_type="video_comments")
            session.add(url_record2)
            await session.flush()
            urls_to_queue.append({"url_id": url_record2.id, "url": url_record2.url,
                                   "url_type": url_record2.url_type, "bv_id": bv_id, "retry_count": 0})

        task.total_urls = len(urls_to_queue)
        await session.commit()

        if dispatcher:
            await dispatcher.submit_task(task.id, urls_to_queue)

        async with async_session() as s2:
            t = await s2.get(Task, task.id)
            if t:
                t.status = TaskStatus.RUNNING.value
                t.updated_at = datetime.now()
                await s2.commit()

        return task


async def get_tasks(page: int = 1, page_size: int = 20) -> tuple[list[Task], int]:
    async with async_session() as session:
        result = await session.execute(select(func.count()).select_from(Task))
        total = result.scalar() or 0
        result = await session.execute(
            select(Task).order_by(Task.created_at.desc()).offset((page - 1) * page_size).limit(page_size))
        return list(result.scalars().all()), total


async def get_task(task_id: str) -> Optional[Task]:
    async with async_session() as session:
        return await session.get(Task, task_id)


async def delete_task(task_id: str) -> bool:
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            return False
        await session.delete(task)
        await session.commit()
        return True


async def get_task_results(task_id: str) -> dict:
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if not task:
            return {}
        url_records = (await session.execute(select(UrlRecord).where(UrlRecord.task_id == task_id))).scalars().all()
        up_infos = (await session.execute(select(UpInfo).where(UpInfo.task_id == task_id))).scalars().all()
        video_infos = (await session.execute(select(VideoInfo).where(VideoInfo.task_id == task_id))).scalars().all()
        comments = (await session.execute(select(Comment).where(Comment.task_id == task_id))).scalars().all()
        return {"task": task, "url_records": list(url_records), "up_infos": list(up_infos),
                "video_infos": list(video_infos), "comments": list(comments)}


async def recover_pending_tasks(dispatcher=None) -> int:
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(Task.status.in_([TaskStatus.PENDING.value, TaskStatus.RUNNING.value])))
        tasks = result.scalars().all()
        recovered = 0
        for task in tasks:
            url_result = await session.execute(
                select(UrlRecord).where(UrlRecord.task_id == task.id,
                                         UrlRecord.status.in_(["pending", "processing"])))
            urls = url_result.scalars().all()
            if not urls:
                completed = (await session.execute(
                    select(func.count()).select_from(UrlRecord).where(
                        UrlRecord.task_id == task.id, UrlRecord.status == "completed"))).scalar() or 0
                failed = (await session.execute(
                    select(func.count()).select_from(UrlRecord).where(
                        UrlRecord.task_id == task.id, UrlRecord.status == "failed"))).scalar() or 0
                task.completed_urls = completed
                task.failed_urls = failed
                task.status = TaskStatus.COMPLETED.value if failed == 0 else TaskStatus.FAILED.value
                task.updated_at = datetime.now()
                continue

            urls_to_queue = []
            for url in urls:
                url.status = "pending"
                url.updated_at = datetime.now()
                extra = {"uid": task.input_value} if url.url_type in ("up_api", "up_video_list") else {"bv_id": task.input_value}
                urls_to_queue.append({"url_id": url.id, "url": url.url, "url_type": url.url_type,
                                       "retry_count": url.retry_count, **extra})

            task.status = TaskStatus.RUNNING.value
            task.updated_at = datetime.now()
            recovered += 1
            await session.commit()

            if dispatcher:
                await dispatcher.submit_task(task.id, urls_to_queue)
            logger.info(f"恢复任务 {task.id}，重新入队 {len(urls_to_queue)} 个 URL")
        return recovered
