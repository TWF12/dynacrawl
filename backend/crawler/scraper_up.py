import json
import logging
from typing import Optional
from urllib.parse import urlencode
from playwright.async_api import Page
from backend.config import PAGE_TIMEOUT
from backend.crawler.anti_detect import random_delay
from backend.crawler.wbi_sign import sign_params, get_mixin_key

logger = logging.getLogger(__name__)


async def scrape_up_info(page: Page, uid: str) -> Optional[dict]:
    """爬取 UP 主基本信息（WBI 签名 API）"""
    result = {"uid": uid}
    await random_delay()
    try:
        mixin_key = await get_mixin_key(page)
        if not mixin_key:
            logger.warning("WBI mixin_key 为空，跳过签名")
            return result
        params = sign_params({"mid": uid}, mixin_key)
        query_string = urlencode(params)
        api_url = f"https://api.bilibili.com/x/space/wbi/acc/info?{query_string}"
        response = await page.goto(api_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        if response and response.ok:
            body_text = await page.evaluate("() => document.body.innerText")
            data = json.loads(body_text)
            if data.get("code") == 0 and data.get("data"):
                card = data["data"]
                result["nickname"] = card.get("name", "")
                result["avatar_url"] = card.get("face", "")
                result["follower_count"] = card.get("follower", 0)
                result["video_count"] = card.get("video_count", 0)
                result["raw_data"] = card
                return result

        logger.warning("API获取UP信息失败，尝试从页面提取")
        space_url = f"https://space.bilibili.com/{uid}"
        response = await page.goto(space_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        if response and response.ok:
            data = await page.evaluate("""
                function() {
                    var r = {};
                    if (document.title) {
                        var m = document.title.match(/^(.+?)的个人空间/);
                        if (m) r.name = m[1].trim();
                    }
                    var nav = document.querySelector('.n-tab-links, [class*="tab-links"], .tab-container');
                    if (nav) {
                        var nt = nav.innerText || '';
                        var nm = nt.match(/投稿\\s*([\\d.]+万?\\+?)/);
                        if (nm) {
                            var ns = nm[1];
                            if (ns === '999+') r.videos = 999;
                            else if (ns.indexOf('万')>=0) r.videos = Math.round(parseFloat(ns)*10000)||0;
                            else r.videos = parseInt(ns)||0;
                        }
                    }
                    var av = document.querySelector('.h-avatar img, [class*="avatar"] img');
                    if (av) r.face = av.src || '';
                    return r;
                }
            """)
            result["nickname"] = data.get("name", "")
            result["avatar_url"] = data.get("face", "")
            result["follower_count"] = data.get("follower", 0)
            result["video_count"] = data.get("videos", 0)
    except Exception as e:
        logger.error(f"爬取UP信息失败 uid={uid}: {e}")
    return result


async def scrape_up_videos(page: Page, uid: str, max_pages: int = 3) -> list[dict]:
    """爬取 UP 主的视频列表（WBI 签名 API 分页）"""
    videos = []
    await random_delay()
    try:
        mixin_key = await get_mixin_key(page)
        if not mixin_key:
            logger.warning("WBI mixin_key 为空，跳过视频列表获取")
            return videos

        for pn in range(1, max_pages + 1):
            if pn > 1:
                await random_delay()
            params = sign_params({"mid": uid, "ps": 50, "pn": pn, "order": "pubdate"}, mixin_key)
            query_string = urlencode(params)
            api_url = f"https://api.bilibili.com/x/space/wbi/arc/search?{query_string}"
            resp = await page.goto(api_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
            if not resp or not resp.ok:
                continue
            body_text = await page.evaluate("() => document.body.innerText")
            data = json.loads(body_text)
            if data.get("code") != 0:
                break
            vlist = data.get("data", {}).get("list", {}).get("vlist", []) or []
            for v in vlist:
                videos.append({
                    "bvid": v.get("bvid", ""),
                    "title": v.get("title", ""),
                    "play": v.get("play", 0),
                })
            if len(vlist) < 50:
                break
    except Exception as e:
        logger.error(f"爬取视频列表失败 uid={uid}: {e}")
    return videos
