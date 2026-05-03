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
                    up_existing = await session.execute(
                        select(UpInfo).where(UpInfo.task_id == task_id))
                    up_row = up_existing.scalars().first()
                    if up_row:
                        up_row.nickname = result.get("nickname", "")
                        up_row.avatar_url = result.get("avatar_url", "")
                        up_row.follower_count = result.get("follower_count")
                        # 用 API 返回的真实视频数覆盖
                        if result.get("video_count", 0) > 0:
                            up_row.video_count = result["video_count"]
                        if result.get("raw_data"):
                            up_row.raw_data = result.get("raw_data")
                    else:
                        session.add(UpInfo(
                            task_id=task_id, uid=result.get("uid", ""),
                            nickname=result.get("nickname", ""),
                            avatar_url=result.get("avatar_url", ""),
                            follower_count=result.get("follower_count"),
                            video_count=result.get("video_count"),
                            raw_data=result.get("raw_data"),
                        ))

            elif url_type == "up_video_list":
                total_videos = [0]  # 用 list 装 int 以便闭包修改

                # 每页完成后立即存入数据库
                async def _save_page(page_videos: list[dict], cumulative: int):
                    for v in page_videos:
                        session.add(VideoInfo(
                            task_id=task_id, bv_id=v.get("bvid", ""),
                            title=v.get("title", ""), play_count=v.get("play"),
                            raw_data=v,
                        ))
                    total_videos[0] = cumulative + len(page_videos)
                    await session.commit()  # 每页立即提交，后续页失败不丢已爬数据

                async def _video_progress(current: int, total: int, message: str):
                    if progress_callback:
                        await progress_callback(
                            task_id, 0, 0, 0,  # 不覆盖 URL 进度数字
                            f"视频采集: {message}",
                        )

                result = await scrape_up_videos(
                    page, msg.get("uid", ""),
                    progress_callback=_video_progress,
                    on_page_done=_save_page,
                )
                videos = result.get("videos", [])
                api_total = result.get("total_count", 0)

                # 如果 on_page_done 未生效（旧逻辑兼容），统一存入
                if not total_videos[0]:
                    for v in videos:
                        session.add(VideoInfo(
                            task_id=task_id, bv_id=v.get("bvid", ""),
                            title=v.get("title", ""), play_count=v.get("play"),
                            raw_data=v,
                        ))

                up_result = await session.execute(
                    select(UpInfo).where(UpInfo.task_id == task_id))
                up_info = up_result.scalars().first()
                if up_info:
                    if up_info.video_count == 0:
                        up_info.video_count = api_total or total_videos[0] or len(videos)
                else:
                    session.add(UpInfo(
                        task_id=task_id, uid=msg.get("uid", ""),
                        video_count=api_total or total_videos[0] or len(videos),
                    ))

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
            url_record.updated_at = datetime.now()
            scrape_errors = []
            scraper_status = "completed"  # 默认
            if isinstance(result, dict):
                scrape_errors = result.get("errors", [])
                scraper_status = result.get("status", "completed")
            if scrape_errors:
                url_record.error_msg = "; ".join(scrape_errors)
            # status 映射: ok→completed, fallback→partial, failed→failed
            url_record.status = {"ok": "completed", "fallback": "partial", "failed": "failed"}.get(
                scraper_status, "completed")

        task = await session.get(Task, task_id)
        if task:
            completed_count = (await session.execute(
                select(func.count()).select_from(UrlRecord).where(
                    UrlRecord.task_id == task_id, UrlRecord.status == "completed"))
            ).scalar() or 0
            partial_count = (await session.execute(
                select(func.count()).select_from(UrlRecord).where(
                    UrlRecord.task_id == task_id, UrlRecord.status == "partial"))
            ).scalar() or 0
            failed_count = (await session.execute(
                select(func.count()).select_from(UrlRecord).where(
                    UrlRecord.task_id == task_id, UrlRecord.status == "failed"))
            ).scalar() or 0

            task.completed_urls = completed_count + partial_count + failed_count
            task.failed_urls = failed_count

            all_done = (await session.execute(
                select(func.count()).select_from(UrlRecord).where(
                    UrlRecord.task_id == task_id,
                    UrlRecord.status.in_(["pending", "processing"])))
            ).scalar() or 0
            if all_done == 0:
                if failed_count > 0:
                    task.status = "failed"
                elif partial_count > 0:
                    task.status = "partial"
                else:
                    task.status = "completed"
            task.updated_at = datetime.now()

        await session.commit()

        if progress_callback and task:
            scraper_status = result.get("status", "ok") if isinstance(result, dict) else "ok"
            status_label = {"ok": "已完成", "fallback": "异常", "failed": "失败"}.get(scraper_status, "已完成")
            await progress_callback(
                task_id, task.completed_urls, task.total_urls, task.failed_urls,
                f"{status_label}: {url_type}")

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
