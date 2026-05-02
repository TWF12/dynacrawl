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
    """爬取 UP 主基本信息（card API + arc/search）"""
    result = {"uid": uid}
    await random_delay()
    try:
        # 先访问 B站首页获取必要的 cookies
        await page.goto("https://www.bilibili.com/", timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        await random_delay()

        # 1. card API 获取昵称、头像、粉丝数（无需 WBI 签名）
        card_url = f"https://api.bilibili.com/x/web-interface/card?mid={uid}"
        response = await page.goto(card_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        if response and response.ok:
            body_text = await page.evaluate("() => document.body.innerText")
            data = json.loads(body_text)
            if data.get("code") == 0:
                card = data.get("data", {}).get("card", {})
                result["nickname"] = card.get("name", "")
                result["avatar_url"] = card.get("face", "")
                result["follower_count"] = card.get("fans", 0)
                result["raw_data"] = card

        # 2. arc/search API 获取视频总数（需要 WBI 签名）
        mixin_key = await get_mixin_key(page)
        if mixin_key:
            params = sign_params({"mid": uid, "ps": 1, "pn": 1}, mixin_key)
            query_string = urlencode(params)
            arc_url = f"https://api.bilibili.com/x/space/wbi/arc/search?{query_string}"
            response = await page.goto(arc_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
            if response and response.ok:
                body_text = await page.evaluate("() => document.body.innerText")
                data = json.loads(body_text)
                if data.get("code") == 0:
                    page_info = data.get("data", {}).get("page", {})
                    result["video_count"] = page_info.get("count", 0)
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
