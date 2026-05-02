import logging
from datetime import datetime
from typing import Optional, Callable, Awaitable

from sqlalchemy import select, func

from backend.config import MAX_RETRY
from backend.models import UrlRecord, Task, UpInfo, VideoInfo, Comment
from backend.crawler.scraper_up import scrape_up_info, scrape_up_videos
from backend.crawler.scraper_video import scrape_video_info, scrape_video_comments

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int, int, str], Awaitable[None]]
EnqueueCallback = Callable[[str, dict], Awaitable[None]]


async def process_url_message(
    msg: dict,
    browser_pool,
    session,
    enqueue_callback: Optional[EnqueueCallback] = None,
    progress_callback: Optional[ProgressCallback] = None,
    consumer_label: str = "",
):
    task_id = msg["task_id"]
    url_id = msg["url_id"]
    url_type = msg["url_type"]
    retry_count = msg.get("retry_count", 0)

    try:
        url_record = await session.get(UrlRecord, url_id)
        if url_record:
            url_record.status = "processing"
            url_record.updated_at = datetime.now()
            await session.commit()

        result = None
        async with browser_pool.acquire_page() as page:
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

                # 回写 UpInfo.video_count
                up_result = await session.execute(
                    select(UpInfo).where(UpInfo.task_id == task_id))
                up_info = up_result.scalars().first()
                if up_info:
                    up_info.video_count = len(videos)

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

        if progress_callback and task:
            await progress_callback(
                task_id, task.completed_urls, task.total_urls, task.failed_urls,
                f"已完成: {url_type}")

    except Exception as e:
        logger.error(f"{consumer_label}处理 URL {url_id} 失败: {e}")
        try:
            url_record = await session.get(UrlRecord, url_id)
            if url_record:
                if retry_count < MAX_RETRY:
                    url_record.retry_count = retry_count + 1
                    url_record.status = "pending"
                    url_record.error_msg = str(e)[:500]
                    msg["retry_count"] = retry_count + 1
                    if enqueue_callback:
                        await enqueue_callback(task_id, msg)
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
