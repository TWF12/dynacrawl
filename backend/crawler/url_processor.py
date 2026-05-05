import logging
import re
from datetime import datetime
from typing import Optional, Callable, Awaitable

from sqlalchemy import select, func

from backend.config import MAX_RETRY
from backend.models import UrlRecord, Task, UpInfo, VideoInfo, Comment
from backend.schemas import TaskStatus
from backend.crawler.scraper_up import scrape_up_info, scrape_up_videos
from backend.crawler.scraper_video import scrape_video_info, scrape_video_comments

logger = logging.getLogger(__name__)


def _fmt_error(e: Exception) -> str:
    """精简异常消息: 去冗长 URL + 限 100 字符"""
    msg = str(e).split("\n")[0].strip()
    msg = re.sub(r'https?://\S+', '[URL]', msg)
    return msg[:100] if len(msg) > 100 else msg


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

    # 检查任务是否已被删除
    task = await session.get(Task, task_id)
    if not task:
        logger.info("任务 %s 已删除, 跳过 URL %s", task_id, url_id)
        return

    # 首次被 consumer 拾取, 切为运行中
    if task.status == TaskStatus.PENDING.value:
        task.status = TaskStatus.RUNNING.value
        task.updated_at = datetime.now()
        await session.commit()

    try:
        url_record = await session.get(UrlRecord, url_id)
        if url_record:
            url_record.status = "processing"
            url_record.updated_at = datetime.now()
            await session.commit()

        result = None

        # up_video_list 内部自行创建 headful context, 不要占 acquire_page 的 semaphore
        # 否则 3 任务各占 2 槽 = 需要 6, BROWSER_CONCURRENCY=3 直接死锁
        if url_type == "up_video_list":
            total_videos = [0]

            async def _save_page(page_videos: list[dict], cumulative: int):
                session.expire_all()
                if not await session.get(Task, task_id):
                    return
                for v in page_videos:
                    session.add(VideoInfo(
                        task_id=task_id, bv_id=v.get("bvid", ""),
                        title=v.get("title", ""), play_count=v.get("play"),
                        raw_data=v,
                    ))
                total_videos[0] = cumulative + len(page_videos)
                await session.commit()

            async def _video_progress(current: int, total: int, message: str):
                if progress_callback:
                    await progress_callback(
                        task_id, 0, 0, 0,
                        f"视频采集: {message}",
                    )

            result = await scrape_up_videos(
                None, msg.get("uid", ""),
                progress_callback=_video_progress,
                on_page_done=_save_page,
                task_id=task_id,
            )
            videos = result.get("videos", [])
            api_total = result.get("total_count", 0)

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

        else:
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

        # 采集期间任务可能被删除(级联删除 URL 记录), expire 后重新检查
        session.expire_all()
        task_still_exists = await session.get(Task, task_id)
        if not task_still_exists:
            logger.info("任务 %s 在采集期间被删除, 跳过后续更新", task_id)
            return

        url_record = await session.get(UrlRecord, url_id)
        if url_record:
            url_record.updated_at = datetime.now()
            scrape_errors = []
            scraper_status = "completed"  # 默认
            if isinstance(result, dict):
                scrape_errors = result.get("errors", [])
                scraper_status = result.get("status", "completed")
            if scrape_errors:
                msg = "; ".join(scrape_errors)
                url_record.error_msg = msg[:120] + "..." if len(msg) > 120 else msg
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
        await session.rollback()  # 先回滚损坏的事务
        session.expire_all()
        task_exists = await session.get(Task, task_id)
        if not task_exists:
            logger.info("任务 %s 已删除, 跳过错误处理", task_id)
            return
        try:
            url_record = await session.get(UrlRecord, url_id)
            if url_record:
                if retry_count < MAX_RETRY:
                    url_record.retry_count = retry_count + 1
                    url_record.status = "pending"
                    url_record.error_msg = _fmt_error(e)
                    msg["retry_count"] = retry_count + 1
                    if enqueue_callback:
                        await enqueue_callback(task_id, msg)
                else:
                    url_record.status = "failed"
                    url_record.error_msg = f"重试{MAX_RETRY}次仍失败: {_fmt_error(e)}"

            task = await session.get(Task, task_id)
            if task:
                # 重算所有计数器
                completed = (await session.execute(
                    select(func.count()).select_from(UrlRecord).where(
                        UrlRecord.task_id == task_id, UrlRecord.status == "completed"))).scalar() or 0
                partial = (await session.execute(
                    select(func.count()).select_from(UrlRecord).where(
                        UrlRecord.task_id == task_id, UrlRecord.status == "partial"))).scalar() or 0
                failed = (await session.execute(
                    select(func.count()).select_from(UrlRecord).where(
                        UrlRecord.task_id == task_id, UrlRecord.status == "failed"))).scalar() or 0
                task.completed_urls = completed + partial + failed
                task.failed_urls = failed
                task.updated_at = datetime.now()

                # 所有 URL 都处理完 → 更新任务最终状态
                remaining = (await session.execute(
                    select(func.count()).select_from(UrlRecord).where(
                        UrlRecord.task_id == task_id,
                        UrlRecord.status.in_(["pending", "processing"])))
                ).scalar() or 0
                if remaining == 0:
                    if failed > 0:
                        task.status = TaskStatus.FAILED.value
                    elif partial > 0:
                        task.status = "partial"
                    else:
                        task.status = TaskStatus.COMPLETED.value

            await session.commit()

            if progress_callback and task:
                await progress_callback(
                    task_id, task.completed_urls, task.total_urls, task.failed_urls,
                    f"失败: {url_type}")

        except Exception as inner_e:
            logger.error(f"更新失败状态时出错: {inner_e}")
            await session.rollback()
