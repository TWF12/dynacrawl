import json
import logging
import re
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


async def scrape_up_videos(page: Page, uid: str, max_pages: int = 5) -> list[dict]:
    """爬取 UP 主的视频列表（从 /upload/video 页面 DOM 提取）"""
    videos = []
    await random_delay()

    async with browser_pool.acquire_headful_page() as page2:
        seen_bvids: set = set()
        try:
            space_url = f"https://space.bilibili.com/{uid}/video"
            resp = await page2.goto(space_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
            if not resp or not resp.ok:
                return videos
            await page2.wait_for_timeout(3000)

            # 从页面提取视频总数
            total_count = await _get_video_count_from_page(page2, uid)

            await _extract_videos_from_cards(page2, videos, seen_bvids)
            logger.info("第 1 页获取 %d 条视频 uid=%s", len(videos), uid)

            # 翻页
            actual_pages = min(max_pages, (total_count + 40) // 41) if total_count else max_pages
            for pn in range(2, actual_pages + 1):
                await random_delay()
                before = len(videos)
                await page2.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)
                await _extract_videos_from_cards(page2, videos, seen_bvids)
                added = len(videos) - before
                logger.info("第 %d 页获取 %d 条视频 uid=%s", pn, added, uid)
                if added == 0:
                    break

        except Exception as exc:
            logger.error(f"爬取视频列表失败 uid={uid}: {exc}")
            # 重试一次 DOM 提取
            try:
                await page2.goto(
                    f"https://space.bilibili.com/{uid}/video",
                    timeout=PAGE_TIMEOUT, wait_until="networkidle",
                )
                await page2.wait_for_timeout(3000)
                await _extract_videos_from_cards(page2, videos, seen_bvids)
            except Exception:
                pass

    return videos


async def _get_video_count_from_page(page2: Page, uid: str) -> int:
    """从页面统计区提取视频总数"""
    try:
        count = await page2.evaluate("""
            () => {
                let text = document.body.textContent || '';
                let m = text.match(/视频\\s*(\\d+)/);
                if (m) return parseInt(m[1]);
                m = text.match(/投稿\\s*(\\d+)/);
                if (m) return parseInt(m[1]);
                return 0;
            }
        """)
        return count or 0
    except Exception:
        return 0


async def _extract_videos_from_cards(page2: Page, videos: list[dict], seen_bvids: set):
    """从 bili-video-card 卡片中提取视频数据"""
    items = await page2.locator('a[href*="/video/BV"]').all()
    for item in items:
        try:
            href = (await item.get_attribute("href") or "")
            m = re.search(r'BV[A-Za-z0-9]{10}', href)
            if not m:
                continue
            bvid = m.group(0)
            if bvid in seen_bvids:
                continue
            seen_bvids.add(bvid)

            # 标题：优先从 img.alt 取，其次从外层 textContent 找
            data = await item.evaluate_handle("""
                el => {
                    // 标题：img.alt（封面图片的 alt 属性就是标题）
                    let img = el.querySelector('img');
                    let title = img ? (img.getAttribute('alt') || '').trim() : '';

                    // 播放量：取 el.textContent 中的第一个数字（如 "16100:14" → 取 "161"）
                    let raw = (el.textContent || '').trim();
                    let firstNum = raw.match(/([\\d.]+万?)/);
                    let play = firstNum ? firstNum[1] : '0';

                    return { title, play };
                }
            """)
            info = await data.json_value()
            title = (info.get("title") or "").strip()
            play_str = (info.get("play") or "0").strip()

            n = float(play_str.replace("万", "").replace(",", ""))
            play = round(n * 10000) if "万" in play_str else int(n)

            videos.append({"bvid": bvid, "title": title, "play": play})
        except Exception:
            pass
