import json
import logging
import asyncio
from typing import Optional
from urllib.parse import urlencode
from playwright.async_api import Page
from backend.config import PAGE_TIMEOUT
from backend.crawler.anti_detect import random_delay
from backend.crawler.wbi_sign import sign_params, get_mixin_key

logger = logging.getLogger(__name__)


async def scrape_up_info(page: Page, uid: str) -> Optional[dict]:
    """爬取 UP 主基本信息（card API + 拦截 arc/search 获取视频总数）"""
    result = {"uid": uid, "video_count": 0}
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

        # 2. 拦截空间页面的 arc/search 响应获取视频总数
        captured_page_info = {}

        async def on_response(response):
            if "x/space/wbi/arc/search" in response.url or "x/space/arc/search" in response.url:
                try:
                    d = await response.json()
                    if d.get("code") == 0:
                        page_data = d.get("data", {}).get("page") or d.get("data", {}).get("list", {}).get("page") or {}
                        count = page_data.get("count", 0)
                        if count:
                            captured_page_info["count"] = count
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            space_url = f"https://space.bilibili.com/{uid}"
            await page.goto(space_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
            await page.wait_for_timeout(2000)
        finally:
            page.remove_listener("response", on_response)

        if captured_page_info.get("count"):
            result["video_count"] = captured_page_info["count"]
    except Exception as e:
        logger.error(f"爬取UP信息失败 uid={uid}: {e}")
    return result


async def scrape_up_videos(page: Page, uid: str, max_pages: int = 3) -> list[dict]:
    """爬取 UP 主的视频列表（拦截空间页面 arc/search API 响应）"""
    videos = []
    seen = set()
    captured_responses: list[dict] = []

    async def on_response(response):
        try:
            if "x/space/wbi/arc/search" in response.url or "x/space/arc/search" in response.url:
                d = await response.json()
                captured_responses.append(d)
        except Exception:
            pass

    page.on("response", on_response)
    await random_delay()

    try:
        space_url = f"https://space.bilibili.com/{uid}"
        resp = await page.goto(space_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
        if not resp or not resp.ok:
            return videos
        await page.wait_for_timeout(2000)

        # 处理首次加载的 API 数据
        _process_captured_responses(captured_responses, videos, seen, uid)
        captured_responses.clear()

        # 滚动翻页加载更多
        for _ in range(max_pages - 1):
            await random_delay()
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

            _process_captured_responses(captured_responses, videos, seen, uid)
            if not captured_responses:
                break
            captured_responses.clear()
    except Exception as e:
        logger.error(f"爬取视频列表失败 uid={uid}: {e}")
    finally:
        page.remove_listener("response", on_response)

    return videos


def _process_captured_responses(responses: list[dict], videos: list, seen: set, uid: str):
    for data in responses:
        if data.get("code") != 0:
            logger.warning("arc/search 返回错误 code=%s msg=%s uid=%s",
                           data.get("code"), data.get("message", ""), uid)
            continue
        vlist = data.get("data", {}).get("list", {}).get("vlist", []) or []
        for v in vlist:
            bvid = v.get("bvid", "")
            if bvid and bvid not in seen:
                seen.add(bvid)
                videos.append({
                    "bvid": bvid,
                    "title": v.get("title", ""),
                    "play": v.get("play", 0),
                })
