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


async def scrape_up_videos(page: Page, uid: str, max_pages: int = 0) -> list[dict]:
    """爬取 UP 主的视频列表（/upload/video 投稿页 + arc/search API 拦截）"""
    videos = []
    await random_delay()

    async with browser_pool.acquire_headful_page() as page2:
        intercepted_data: list[dict] = []

        def _on_arc_search(response):
            if "/x/space/wbi/arc/search" in response.url and response.status == 200:
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
            # 访问投稿管理页
            upload_url = f"https://space.bilibili.com/{uid}/upload/video"
            resp = await page2.goto(upload_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
            if not resp or not resp.ok:
                return videos
            await page2.wait_for_timeout(3000)

            if not intercepted_data:
                logger.warning("未拦截到 arc/search 响应，检查是否已登录 uid=%s", uid)
                return videos

            seen_bvids: set = set()
            total_count = 0

            # 处理首页
            for data in intercepted_data:
                total_count = _process_arc_data(data, videos, seen_bvids) or total_count

            ps = intercepted_data[0].get("page", {}).get("ps", 30) if intercepted_data else 30
            total_pages = (total_count + ps - 1) // ps if total_count else max_pages
            if max_pages and max_pages < total_pages:
                total_pages = max_pages

            logger.info("投稿页第 1 页获取 %d 条 uid=%s (共 %d 条 %d 页)",
                        len(videos), uid, total_count, total_pages)

            # 翻页：点击分页按钮 "下一页"
            for pn in range(2, total_pages + 1):
                await random_delay()
                before = len(intercepted_data)

                # 先滚动到底部让分页器可见
                await page2.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(0.5)

                # 尝试点击页码按钮
                clicked = False
                for selector in [
                    f'button.vui_pagenation--btn-num:text-is("{pn}")',
                    f'button:text-is("{pn}")',
                ]:
                    try:
                        loc = page2.locator(selector)
                        if await loc.count() > 0:
                            await loc.first.click(force=True, timeout=5000)
                            clicked = True
                            break
                    except Exception:
                        continue

                if not clicked:
                    # 点击"下一页"
                    try:
                        next_btn = page2.locator(
                            'button.vui_pagenation--btn-side:text-is("下一页")')
                        if await next_btn.count() > 0:
                            await next_btn.first.click(force=True, timeout=5000)
                            clicked = True
                    except Exception:
                        pass

                if not clicked:
                    logger.info("翻页失败 pn=%d uid=%s，停止翻页", pn, uid)
                    break

                # 等待新响应
                await page2.wait_for_timeout(3000)

                added_responses = len(intercepted_data) - before
                if added_responses == 0:
                    logger.info("翻页无新响应 pn=%d uid=%s，停止", pn, uid)
                    break

                before_videos = len(videos)
                for data in intercepted_data[before:]:
                    _process_arc_data(data, videos, seen_bvids)

                added = len(videos) - before_videos
                logger.info("投稿页第 %d 页获取 %d 条 uid=%s", pn, added, uid)
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
