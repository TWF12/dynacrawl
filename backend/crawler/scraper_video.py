import json
import logging
from typing import Optional
from playwright.async_api import Page
from backend.config import PAGE_TIMEOUT
from backend.crawler.anti_detect import random_delay

logger = logging.getLogger(__name__)


async def scrape_video_info(page: Page, bv_id: str) -> Optional[dict]:
    """爬取视频基本信息（API 优先，页面降级）"""
    result = {"bv_id": bv_id}
    await random_delay()
    try:
        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bv_id}"
        response = await page.goto(api_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        if response and response.ok:
            body_text = await page.evaluate("() => document.body.innerText")
            data = json.loads(body_text)
            if data.get("code") == 0 and data.get("data"):
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
                    "raw_data": v,
                })
                return result

        logger.warning(f"API获取视频信息失败，尝试从页面提取 bv_id={bv_id}")
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
        logger.error(f"爬取视频信息失败 bv_id={bv_id}: {e}")
    return result


async def scrape_video_comments(page: Page, bv_id: str, max_pages: int = 3) -> list[dict]:
    """爬取视频评论"""
    comments = []
    for pn in range(1, max_pages + 1):
        await random_delay()
        try:
            url = f"https://www.bilibili.com/video/{bv_id}"
            resp = await page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
            if not resp or not resp.ok: continue
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, 800)")
                await page.wait_for_timeout(1000)
            page_comments = await page.evaluate("""
                function() {
                    var result = [];
                    document.querySelectorAll('.reply-item, [class*="reply-item"], [class*="comment-item"]').forEach(function(item){
                        var u = item.querySelector('.user-name, .name, [class*="user-name"]');
                        var c = item.querySelector('.reply-content, .text, [class*="reply-content"]');
                        var l = item.querySelector('.like-count, [class*="like"]');
                        var t = item.querySelector('.reply-time, .time, [class*="time"]');
                        var un = (u?u.textContent:'').trim();
                        var ct = (c?c.textContent:'').trim();
                        if (un && ct) result.push({
                            bv_id: bv_id, username: un, content: ct,
                            like_count: parseInt((l?l.textContent:'0').replace(/[^0-9]/g,''))||0,
                            posted_at: (t?t.textContent:'').trim()
                        });
                    });
                    return result;
                }
            """)
            comments.extend(page_comments)
            if len(page_comments) < 5: break
        except Exception as e:
            logger.error(f"爬取评论失败 bv_id={bv_id}: {e}")
    return comments
