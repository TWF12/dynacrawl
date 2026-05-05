import json
import time
import random
import logging
from typing import Optional
from playwright.async_api import Page
from backend.config import PAGE_TIMEOUT
from backend.crawler.anti_detect import random_delay

logger = logging.getLogger(__name__)

# API 调用轻量延迟, 页面降级仍用标准延迟
_API_DELAY = (0.5, 2.0)


def _comment_delay(pn: int, max_pages: int) -> float:
    """评论翻页渐进延迟: 跟视频列表一样按进度递增, 避免风控"""
    ratio = pn / max(max_pages, 3)
    if ratio <= 0.3:
        return random.uniform(1, 3)
    elif ratio <= 0.6:
        return random.uniform(2, 5)
    else:
        return random.uniform(4, 8)


async def _fetch_json(page: Page, url: str, referer: str = "https://www.bilibili.com/") -> Optional[dict]:
    """通过 page API 请求 JSON, 带 Referer 防检测"""
    await page.set_extra_http_headers({
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
    })
    resp = await page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
    if resp and resp.ok:
        text = await page.evaluate("() => document.body.innerText")
        return json.loads(text)
    return None


async def _handle_rate_limit(page: Page, url: str, referer: str, code: int) -> Optional[dict]:
    """-352/-412 风控重试: 等待 30-60s 后重试一次"""
    logger.warning("风控 code=%d, 等待30-60s后重试", code)
    await page.wait_for_timeout(random.randint(30000, 60000))
    return await _fetch_json(page, url, referer)


async def scrape_video_info(page: Page, bv_id: str) -> Optional[dict]:
    """爬取视频基本信息（API 优先，页面降级）"""
    result = {"bv_id": bv_id}
    await page.wait_for_timeout(random.randint(500, 2000))  # API 轻量延迟

    try:
        # Tier 1: B站 view API
        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv_id}"
        data = await _fetch_json(page, api_url)

        if data:
            code = data.get("code")
            if code == 0 and data.get("data"):
                v = data["data"]
                stat = v.get("stat", {})
                owner = v.get("owner", {})
                result.update({
                    "title": v.get("title", ""),
                    "play_count": stat.get("view", 0),
                    "like_count": stat.get("like", 0),
                    "coin_count": stat.get("coin", 0),
                    "danmaku_count": stat.get("danmaku", 0),
                    "comment_count": stat.get("reply", 0),
                    "uid": owner.get("mid", ""),
                    "author": owner.get("name", ""),
                    "aid": v.get("aid"),  # 传给评论采集, 省一次 API 调用
                    "raw_data": v,
                })
                return result
            elif code in (-352, -412):
                data = await _handle_rate_limit(page, api_url, "https://www.bilibili.com/", code)
                if data and data.get("code") == 0 and data.get("data"):
                    v = data["data"]
                    stat = v.get("stat", {})
                    owner = v.get("owner", {})
                    result.update({
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
                    })
                    return result

        # Tier 2: 页面 DOM 降级
        logger.warning("API获取视频信息失败，尝试从页面提取 bv_id=%s", bv_id)
        await random_delay()
        video_url = f"https://www.bilibili.com/video/{bv_id}"
        response = await page.goto(video_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        if response and response.ok:
            data = await page.evaluate("""
                function() {
                    if (window.__INITIAL_STATE__) {
                        var vd = window.__INITIAL_STATE__.videoData || {};
                        return { title: vd.title || '', stat: vd.stat || {} };
                    }
                    var r = { title: document.title||'', stat: {} };
                    if (r.title.indexOf('_哔哩哔哩')>=0) r.title = r.title.split('_哔哩哔哩')[0];
                    document.querySelectorAll('.video-info-detail span, .video-stat span').forEach(function(el){
                        var txt = el.textContent || '';
                        var num = parseInt(txt.replace(/[^0-9.]/g,''))||0;
                        if (txt.indexOf('播放')>=0||txt.indexOf('观看')>=0) r.stat.view = num;
                        if (txt.indexOf('弹幕')>=0) r.stat.danmaku = num;
                        if (txt.indexOf('点赞')>=0) r.stat.like = num;
                        if (txt.indexOf('投币')>=0||txt.indexOf('硬币')>=0) r.stat.coin = num;
                        if (txt.indexOf('评论')>=0) r.stat.reply = num;
                    });
                    return r;
                }
            """)
            result["title"] = data.get("title", "")
            stat = data.get("stat") or {}
            result["play_count"] = stat.get("view", 0)
            result["like_count"] = stat.get("like", 0)
            result["coin_count"] = stat.get("coin", 0)
            result["danmaku_count"] = stat.get("danmaku", 0)
            result["comment_count"] = stat.get("reply", 0)
    except Exception as e:
        logger.error("爬取视频信息失败 bv_id=%s: %s", bv_id, e)
    return result


async def scrape_video_comments(
    page: Page, bv_id: str, aid: int = None, comment_count: int = 0, max_pages: int = 5
) -> tuple[list[dict], int]:
    """爬取视频评论。aid/comment_count 从 video_info 传入可省一次 API 调用"""
    comments = []
    api_pages = 0

    # 获取 aid
    if not aid:
        await page.wait_for_timeout(random.randint(500, 2000))
        try:
            view_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv_id}"
            data = await _fetch_json(page, view_url)
            if data and data.get("code") == 0:
                aid = data["data"].get("aid")
                comment_count = data["data"].get("stat", {}).get("reply", 0)
        except Exception as e:
            logger.error("获取视频 aid 失败 bv_id=%s: %s", bv_id, e)

    if not aid:
        logger.warning("无法获取 aid，跳过评论采集 bv_id=%s", bv_id)
        return comments, 0

    # 动态页数: 按评论数估算, 最少 1 页, 最多 max_pages
    if comment_count > 0:
        api_pages = min(max_pages, max(1, (comment_count + 19) // 20))

    for pn in range(1, api_pages + 1):
        if pn > 1:
            delay = _comment_delay(pn, api_pages)
            await page.wait_for_timeout(int(delay * 1000))

        try:
            reply_url = (
                f"https://api.bilibili.com/x/v2/reply/main"
                f"?oid={aid}&type=1&ps=20&pn={pn}&sort=2"
            )
            referer = f"https://www.bilibili.com/video/{bv_id}"
            data = await _fetch_json(page, reply_url, referer)

            if not data:
                continue

            code = data.get("code")
            if code in (-352, -412):
                data = await _handle_rate_limit(page, reply_url, referer, code)

            if not data or data.get("code") != 0:
                break

            replies = data.get("data", {}).get("replies", []) or []
            for r in replies:
                comments.append({
                    "bv_id": bv_id,
                    "username": r.get("member", {}).get("uname", ""),
                    "content": r.get("content", {}).get("message", ""),
                    "like_count": r.get("like", 0),
                    "posted_at": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(r.get("ctime", 0))
                    ) if r.get("ctime") else "",
                })
            if len(replies) < 20:
                break
        except Exception as e:
            logger.error("爬取评论失败 pn=%d bv_id=%s: %s", pn, bv_id, e)

    return comments, api_pages
