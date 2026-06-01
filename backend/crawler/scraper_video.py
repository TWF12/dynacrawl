"""视频详情采集: API 优先 + headful WBI 签名绕过 reply API 风控"""

import json
import time
import random
import logging
from typing import Optional
from urllib.parse import urlencode
from playwright.async_api import Page
from backend.config import PAGE_TIMEOUT
from backend.crawler.anti_detect import (
    random_delay,
    report_page_and_rotate,
    rotate_proxy_if_needed,
    is_task_cancelled,
)
from backend.crawler.wbi_sign import sign_params, get_mixin_key
from backend.crawler.browser_pool import browser_pool
from backend.crawler.cookie_manager import cookie_manager

logger = logging.getLogger(__name__)


def _comment_delay(pn: int, max_pages: int) -> float:
    """评论翻页渐进延迟: 跟视频列表一样按进度递增"""
    ratio = pn / max(max_pages, 3)
    if ratio <= 0.3:
        return random.uniform(1, 3)
    elif ratio <= 0.6:
        return random.uniform(2, 5)
    else:
        return random.uniform(4, 8)


def _pick_video_status(errors: list[str], has_data: bool) -> str:
    if not errors:
        return "ok"
    if has_data:
        return "fallback"
    return "failed"


async def scrape_video_info(page: Page, bv_id: str) -> dict:
    """爬取视频基本信息 (API 优先, 直连 DOM 降级)"""
    result: dict = {"bv_id": bv_id}
    errors: list[str] = []
    api_failed = False
    await page.wait_for_timeout(random.randint(500, 2000))

    # Phase 1: view API
    try:
        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv_id}"
        resp = await page.goto(api_url, timeout=15000, wait_until="domcontentloaded")
        data = None
        if resp and resp.ok:
            text = await page.evaluate("() => document.body.innerText")
            data = json.loads(text)

        if data:
            code = data.get("code")
            if code == 0 and data.get("data"):
                v = data["data"]
                stat = v.get("stat", {})
                owner = v.get("owner", {})
                result.update(
                    {
                        "title": v.get("title", ""),
                        "play_count": stat.get("view", 0),
                        "like_count": stat.get("like", 0),
                        "coin_count": stat.get("coin", 0),
                        "danmaku_count": stat.get("danmaku", 0),
                        "comment_count": stat.get("reply", 0),
                        "uid": owner.get("mid", ""),
                        "author": owner.get("name", ""),
                        "aid": v.get("aid"),
                        "raw_data": v,
                    }
                )
                result["errors"] = errors
                result["status"] = _pick_video_status(errors, True)
                return result
            elif code in (-352, -412):
                errors.append("view风控")
                logger.warning("view API code=%d bv=%s", code, bv_id)
            else:
                errors.append("view异常")
    except Exception:
        errors.append("view超时")
        api_failed = True

    # Phase 2: DOM 降级 — API 故障时用直连 headful, 否则用当前 page
    logger.warning(
        "API获取视频信息失败, 尝试DOM提取 bv_id=%s api_failed=%s", bv_id, api_failed
    )
    await random_delay()
    try:
        dom = None
        if api_failed:
            direct_ctx = await browser_pool.create_direct_context()
            direct_pg = await direct_ctx.new_page()
            try:
                video_url = f"https://www.bilibili.com/video/{bv_id}"
                response = await direct_pg.goto(
                    video_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded"
                )
                if response and response.ok:
                    await direct_pg.wait_for_timeout(2000)
                    dom = await direct_pg.evaluate("""
                        function() {
                            if (window.__INITIAL_STATE__) {
                                var vd = window.__INITIAL_STATE__.videoData || {};
                                return { title: vd.title || '', stat: vd.stat || {}, aid: vd.aid, owner: vd.owner || {} };
                            }
                            var r = { title: document.title||'', stat: {}, aid: null, owner: {} };
                            if (r.title.indexOf('_哔哩哔哩')>=0) r.title = r.title.split('_哔哩哔哩')[0];
                            return r;
                        }
                    """)
            finally:
                await direct_pg.close()
                await direct_ctx.close()
        else:
            video_url = f"https://www.bilibili.com/video/{bv_id}"
            response = await page.goto(
                video_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded"
            )
            if response and response.ok:
                await page.wait_for_timeout(2000)
                dom = await page.evaluate("""
                    function() {
                        if (window.__INITIAL_STATE__) {
                            var vd = window.__INITIAL_STATE__.videoData || {};
                            return { title: vd.title || '', stat: vd.stat || {}, aid: vd.aid, owner: vd.owner || {} };
                        }
                        var r = { title: document.title||'', stat: {}, aid: null, owner: {} };
                        if (r.title.indexOf('_哔哩哔哩')>=0) r.title = r.title.split('_哔哩哔哩')[0];
                        return r;
                    }
                """)

        if dom:
            result["title"] = dom.get("title", "")
            stat = dom.get("stat") or {}
            result["play_count"] = stat.get("view", 0)
            result["like_count"] = stat.get("like", 0)
            result["coin_count"] = stat.get("coin", 0)
            result["danmaku_count"] = stat.get("danmaku", 0)
            result["comment_count"] = stat.get("reply", 0)
            aid = dom.get("aid")
            if aid:
                result["aid"] = aid
            owner = dom.get("owner") or {}
            if owner.get("mid"):
                result["uid"] = str(owner["mid"])
                result["author"] = owner.get("name", "")
            result["raw_data"] = dom  # DOM 提取的原始数据, 供 video_comments 读取 aid
            if dom.get("title"):
                errors.append("DOM提取")
            else:
                errors.append("视频页无标题")
        else:
            errors.append("视频页失败")
    except Exception as e:
        logger.error("DOM降级失败 bv_id=%s: %s", bv_id, e)
        errors.append("DOM降级失败")

    result["errors"] = errors
    result["status"] = _pick_video_status(errors, len(result) > 1)
    return result


async def scrape_video_comments(
    page: Optional[Page],
    bv_id: str,
    aid: int = None,
    comment_count: int = 0,
    max_pages: int = 1,
    progress_callback=None,
    task_id: str = "",
) -> tuple[list[dict], int]:
    """爬取视频评论(headful + WBI签名)。B站 reply API 分页已失效, 仅采第1页(最多30条)。"""
    comments = []

    if not aid:
        if page is not None:
            await page.wait_for_timeout(random.randint(500, 2000))
            try:
                view_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv_id}"
                resp = await page.goto(view_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
                if resp and resp.ok:
                    text = await page.evaluate("() => document.body.innerText")
                    data = json.loads(text)
                    if data.get("code") == 0:
                        aid = data["data"].get("aid")
                        comment_count = data["data"].get("stat", {}).get("reply", 0)
            except Exception as e:
                logger.error("获取视频 aid 失败 bv_id=%s: %s", bv_id, e)

    if not aid:
        logger.warning("无法获取 aid, 跳过评论采集 bv_id=%s", bv_id)
        return comments, 0

    if task_id and is_task_cancelled(task_id):
        return comments, 0

    async with browser_pool.acquire_headful_context() as ctx:
        pg = await ctx.new_page()
        try:
            await pg.wait_for_timeout(2000)
            mixin_key = None
            for _ in range(2):
                try:
                    resp = await pg.goto(
                        "https://www.bilibili.com/", timeout=30000, wait_until="domcontentloaded"
                    )
                    if resp and resp.ok:
                        await pg.wait_for_timeout(500)
                        mixin_key = await get_mixin_key(pg)
                        if mixin_key:
                            break
                except Exception:
                    await pg.wait_for_timeout(2000)

            if not mixin_key:
                logger.warning("mixin_key 获取失败 bv=%s, 跳过评论采集", bv_id)
                return comments, 0

            params = {"oid": str(aid), "type": "1", "ps": "30", "pn": "1", "sort": "2"}
            params = sign_params(params, mixin_key)
            reply_url = f"https://api.bilibili.com/x/v2/reply/main?{urlencode(params)}"

            resp = await pg.goto(reply_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
            if not resp or not resp.ok:
                return comments, 0
            text = await pg.evaluate("() => document.body.innerText")
            data = json.loads(text)

            if data.get("code") != 0:
                logger.warning("reply API code=%d bv=%s", data.get("code"), bv_id)
                return comments, 0

            replies = data.get("data", {}).get("replies", []) or []
            for r in replies:
                comments.append({
                    "bv_id": bv_id,
                    "username": r.get("member", {}).get("uname", ""),
                    "content": r.get("content", {}).get("message", ""),
                    "like_count": r.get("like", 0),
                    "posted_at": (
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.get("ctime", 0)))
                        if r.get("ctime") else ""
                    ),
                })
            logger.info("评论采集完成 bv=%s 共 %d 条", bv_id, len(comments))
            if progress_callback:
                await progress_callback(1, 1, f"评论采集完成, 共 {len(comments)} 条")
        finally:
            await pg.close()

    return comments, 1


