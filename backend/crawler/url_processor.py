import logging
import re
import asyncio as _asyncio
from datetime import datetime
from typing import Optional, Callable, Awaitable

from sqlalchemy import select, func

from backend.config import MAX_RETRY
from backend.models import UrlRecord, Task, UpInfo, VideoInfo, Comment
from backend.schemas import TaskStatus
from backend.crawler.scraper_up import scrape_up_info, scrape_up_videos
from backend.crawler.scraper_video import scrape_video_info, scrape_video_comments
from backend.crawler.anti_detect import clear_cancelled_task

logger = logging.getLogger(__name__)


async def _delayed_enqueue(enqueue_callback, task_id: str, msg: dict, delay: float):
    """后台延迟重入队, 不阻塞 consumer 处理其他任务"""
    await _asyncio.sleep(delay)
    await enqueue_callback(task_id, msg)


def _fmt_error(e: Exception) -> str:
    """精简异常消息: Playwright 错误→中文, 限 50 字符"""
    msg = str(e).split("\n")[0].strip()
    # 常见 Playwright 错误 → 中文短语
    _ERR_MAP = {
        "net::ERR_PROXY_CONNECTION_FAILED": "代理不通",
        "net::ERR_PROXY_CONNECTION_REFUSED": "代理拒绝",
        "net::ERR_CONNECTION_CLOSED": "连接断开",
        "net::ERR_CONNECTION_RESET": "连接重置",
        "net::ERR_TIMEOUT": "超时",
        "net::ERR_NAME_NOT_RESOLVED": "DNS失败",
        "net::ERR_TUNNEL_CONNECTION_FAILED": "隧道不通",
        "Timeout": "超时",
    }
    for en, zh in _ERR_MAP.items():
        if en in msg:
            return zh
    # 去掉 URL 和其他冗余
    msg = re.sub(r'https?://\S+', '', msg)
    msg = re.sub(r'Page\.goto:\s*', '', msg)
    msg = msg.strip()
    return msg[:50] if len(msg) > 50 else msg


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
        clear_cancelled_task(task_id)
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
                        video_current=current, video_total=total,
                    )

            # 查询已有 BV 号用于断点续爬
            existing_rows = (await session.execute(
                select(VideoInfo.bv_id).where(VideoInfo.task_id == task_id)
            )).scalars().all()
            existing_bvids = set(existing_rows)

            result = await scrape_up_videos(
                None, msg.get("uid", ""),
                progress_callback=_video_progress,
                on_page_done=_save_page,
                task_id=task_id,
                existing_bvids=existing_bvids,
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

        elif url_type == "video_comments":
            pass  # 独立代码块处理
        elif url_type == "video_api":
            # video_api 用 headful context (同 up_video_list), B站检测headless返回受限内容
            async with browser_pool.acquire_headful_context() as headful_ctx:
                hf_page = await headful_ctx.new_page()
                try:
                    result = await scrape_video_info(hf_page, msg.get("bv_id", ""))
                finally:
                    await hf_page.close()
                if result and (result.get("title") or result.get("raw_data")):
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

        # video_comments 独立处理 (不占 acquire_page semaphore)
        if url_type == "video_comments":
            bv = msg.get("bv_id", "")
            aid = None
            comment_count = 0
            vinfo_result = await session.execute(
                select(VideoInfo).where(
                    VideoInfo.task_id == task_id,
                    VideoInfo.bv_id == bv))
            vinfo = vinfo_result.scalars().first()
            if vinfo and vinfo.raw_data:
                aid = vinfo.raw_data.get("aid")
                comment_count = vinfo.comment_count or 0

            async def _comment_progress(current: int, total: int, message: str):
                if progress_callback:
                    await progress_callback(task_id, 0, 0, 0,
                        f"评论采集: {message}",
                        video_current=current, video_total=total)

            comments, pages = await scrape_video_comments(
                None, bv, aid=aid, comment_count=comment_count,
                progress_callback=_comment_progress)
            for c in comments:
                session.add(Comment(
                    task_id=task_id, bv_id=c.get("bv_id", ""),
                    username=c.get("username", ""),
                    content=c.get("content", ""),
                    like_count=c.get("like_count"),
                    posted_at=c.get("posted_at"),
                ))
            com_errors = []
            com_status = "ok"
            if comment_count and len(comments) < comment_count:
                com_errors.append(f"评论不全: {len(comments)}/{comment_count}")
                com_status = "fallback" if len(comments) > 0 else "failed"
            elif len(comments) == 0 and comment_count == 0:
                com_status = "ok"  # 视频本来就没评论
            elif len(comments) == 0 and pages > 0:
                com_errors.append("评论采集为空")
                com_status = "failed"
            result = {"comment_count": len(comments), "comment_pages": pages,
                      "errors": com_errors, "status": com_status}

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
        clear_cancelled_task(task_id)

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
            clear_cancelled_task(task_id)
            return
        try:
            url_record = await session.get(UrlRecord, url_id)
            if url_record:
                new_retry = retry_count + 1
                # 延迟重试: 快速反馈 (DOM 兜底已覆盖多数场景, 此处仅为最终保护)
                _RETRY_DELAYS = [5, 15, 60]  # 秒: 5s→15s→1min, 3次后放弃
                max_auto_retry = len(_RETRY_DELAYS)
                if new_retry <= max_auto_retry:
                    url_record.retry_count = new_retry
                    url_record.status = "pending"
                    url_record.error_msg = _fmt_error(e)
                    msg["retry_count"] = new_retry
                    delay = _RETRY_DELAYS[new_retry - 1]
                    logger.info("URL %s 第 %d 次重试, %ds 后自动重入队", url_id, new_retry, delay)
                    if enqueue_callback:
                        _asyncio.create_task(_delayed_enqueue(enqueue_callback, task_id, msg, delay))
                else:
                    url_record.status = "failed"
                    url_record.error_msg = f"重试{max_auto_retry}次仍失败: {_fmt_error(e)}"

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
