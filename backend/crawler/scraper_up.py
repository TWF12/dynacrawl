import json
import logging
import asyncio
from typing import Optional
from playwright.async_api import Page
from backend.config import PAGE_TIMEOUT
from backend.crawler.anti_detect import random_delay
from backend.crawler.browser_pool import browser_pool

logger = logging.getLogger(__name__)


async def scrape_up_info(page: Page, uid: str) -> Optional[dict]:
    """爬取 UP 主基本信息（card API 获取昵称/头像/粉丝数）"""
    result = {"uid": uid, "video_count": 0}
    await random_delay()
    try:
        await page.goto("https://www.bilibili.com/", timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        await random_delay()

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
    except Exception as e:
        logger.error(f"爬取UP信息失败 uid={uid}: {e}")
    return result


async def scrape_up_videos(page: Page, uid: str, max_pages: int = 10) -> list[dict]:
    """爬取 UP 主的视频列表（拦截 arc/search API 响应，需登录 cookie）"""
    videos = []
    await random_delay()

    async with browser_pool.acquire_headful_page() as page2:
        intercepted_data: list[dict] = []

        def _on_arc_search(response):
            if "/wbi/arc/search" not in response.url:
                return
            async def _capture():
                try:
                    body = await response.json()
                    if body.get("code") == 0 and isinstance(body.get("data"), dict):
                        intercepted_data.append(body["data"])
                except Exception:
                    pass
            asyncio.ensure_future(_capture())

        page2.on("response", _on_arc_search)

        try:
            space_url = f"https://space.bilibili.com/{uid}"
            resp = await page2.goto(space_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
            if not resp or not resp.ok:
                return videos
            await page2.wait_for_timeout(3000)

            if not intercepted_data:
                logger.warning("未拦截到 arc/search 响应，检查是否已登录 uid=%s", uid)
                return videos

            seen_bvids: set = set()
            total_count = 0

            # 处理已拦截的响应
            for data in intercepted_data:
                total_count = _process_arc_data(data, videos, seen_bvids) or total_count

            logger.info("arc/search 第 1 页获取 %d 条视频 uid=%s (共 %d)",
                        len(videos), uid, total_count)

            # 翻页：滚动触发懒加载
            max_pn = min(max_pages, (total_count + 49) // 50) if total_count else max_pages

            for pn in range(2, max_pn + 1):
                await random_delay()
                before_count = len(intercepted_data)
                before_videos = len(videos)

                await page2.evaluate("window.scrollTo(0, document.body.scrollHeight)")

                # 等待新的 arc/search 响应（最多 20 秒）
                waited = 0
                while len(intercepted_data) <= before_count and waited < 20:
                    await asyncio.sleep(0.5)
                    waited += 0.5

                for data in intercepted_data[before_count:]:
                    total_count = _process_arc_data(data, videos, seen_bvids) or total_count

                added = len(videos) - before_videos
                logger.info("arc/search 第 %d 页获取 %d 条视频 uid=%s", pn, added, uid)
                if added == 0:
                    break

            if total_count and len(videos) < total_count:
                logger.warning("视频数不完整 uid=%s: 获取 %d / 应有 %d",
                               uid, len(videos), total_count)

        except Exception as exc:
            logger.error(f"爬取视频列表失败 uid={uid}: {exc}")
        finally:
            try:
                page2.remove_listener("response", _on_arc_search)
            except Exception:
                pass

    return videos


def _process_arc_data(data: dict, videos: list[dict], seen_bvids: set) -> int:
    """从 arc/search 响应 data 中提取视频，返回 total_count"""
    total = data.get("page", {}).get("count", 0)
    vlist = data.get("list", {}).get("vlist", []) or []
    for v in vlist:
        bvid = (v.get("bvid") or "").strip()
        if bvid and bvid not in seen_bvids:
            seen_bvids.add(bvid)
            videos.append({
                "bvid": bvid,
                "title": (v.get("title") or "").strip(),
                "play": v.get("play", 0),
            })
    return total
