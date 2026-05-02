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
    """爬取 UP 主的视频列表（空间页 DOM 提取 + 重试容错）"""
    videos = []
    await random_delay()

    async with browser_pool.acquire_headful_page() as page2:
        seen_bvids: set = set()
        total_count = 0

        # 最多重试 3 次加载页面
        for attempt in range(3):
            if attempt > 0:
                logger.info("重试加载视频列表 uid=%s 第 %d 次", uid, attempt + 1)
                await asyncio.sleep(3)

            try:
                # 先访问首页获取 cookies
                await page2.goto(
                    "https://www.bilibili.com/",
                    timeout=PAGE_TIMEOUT, wait_until="domcontentloaded",
                )
                await random_delay()

                # 访问空间页（会重定向到 /upload/video）
                resp = await page2.goto(
                    f"https://space.bilibili.com/{uid}",
                    timeout=PAGE_TIMEOUT, wait_until="networkidle",
                )
                if not resp or not resp.ok:
                    continue
                await page2.wait_for_timeout(4000)

                # 检查是否显示了"没投过视频"的空状态
                body_text = await page2.evaluate(
                    "(document.body.textContent || '').substring(0, 1000)")
                if "还没投过视频" in body_text or "什么也没有" in body_text:
                    logger.warning("页面显示空状态 uid=%s attempt=%d", uid, attempt + 1)
                    # 尝试滚动触发懒加载
                    await page2.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(3)
                    body_text = await page2.evaluate(
                        "(document.body.textContent || '').substring(0, 1000)")
                    if "还没投过视频" in body_text or "什么也没有" in body_text:
                        continue  # 重试

                # 提取视频总数（从空间页 tab 数字或 body text）
                if total_count == 0:
                    total_count = await _get_video_count_from_page(page2, uid)
                    logger.info("检测到视频总数: %d uid=%s", total_count, uid)

                # 提取视频数据
                await _extract_videos_from_cards(page2, videos, seen_bvids)
                logger.info("第 1 页获取 %d 条视频 uid=%s attempt=%d",
                            len(videos), uid, attempt + 1)

                if len(videos) > 0:
                    break  # 成功获取到数据

            except Exception as exc:
                logger.warning("页面加载失败 uid=%s attempt=%d: %s", uid, attempt + 1, exc)

        if len(videos) == 0:
            logger.error("所有重试均失败，未获取到视频 uid=%s", uid)
            return videos

        # 翻页：滚动触发懒加载
        actual_pages = min(max_pages, (total_count + 40) // 41) if total_count else max_pages
        for pn in range(2, actual_pages + 1):
            await random_delay()
            before = len(videos)
            await page2.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(3)
            await _extract_videos_from_cards(page2, videos, seen_bvids)
            added = len(videos) - before
            logger.info("第 %d 页获取 %d 条视频 uid=%s", pn, added, uid)
            if added == 0:
                break

    return videos


async def _get_video_count_from_page(page2: Page, uid: str) -> int:
    """从页面提取视频总数（优先从 tab 数字、其次从 body text）"""
    try:
        count = await page2.evaluate("""
            () => {
                // 优先从"投稿XX"tab 的数字提取
                let tabs = document.querySelectorAll('.nav-tab__item');
                for (let tab of tabs) {
                    let text = (tab.textContent || '').trim();
                    if (text.startsWith('投稿')) {
                        let num = parseInt(text.replace('投稿', ''));
                        if (num > 0) return num;
                    }
                }
                // 从 body text 匹配 "视频 XX"
                let bodyText = document.body.textContent || '';
                let m = bodyText.match(/视频\\s*(\\d+)/);
                if (m) return parseInt(m[1]);
                return 0;
            }
        """)
        return count or 0
    except Exception:
        return 0


async def _extract_videos_from_cards(page2: Page, videos: list[dict], seen_bvids: set):
    """从页面 DOM 提取视频卡片数据"""
    # 用 JS 一次性提取所有卡片，避免多次 Playwright 通信
    new_items = await page2.evaluate("""
        () => {
            let results = [];
            let links = document.querySelectorAll('a[href*="/video/BV"]');
            links.forEach(a => {
                let href = a.getAttribute('href') || '';
                let bvMatch = href.match(/BV[A-Za-z0-9]{10}/);
                if (!bvMatch) return;
                let bvid = bvMatch[0];
                // 标题：从封面 img 的 alt 属性取
                let img = a.querySelector('img');
                let title = img ? (img.getAttribute('alt') || '').trim() : '';
                // 播放量：取 a 标签 textContent 中的第一个匹配数字
                let raw = (a.textContent || '').trim();
                let playMatch = raw.match(/([\\d.]+万?)/);
                let play = playMatch ? playMatch[1] : '0';
                results.push({bvid, title, play});
            });
            return results;
        }
    """)

    for item in new_items:
        bvid = item.get("bvid", "").strip()
        if not bvid or bvid in seen_bvids:
            continue
        seen_bvids.add(bvid)

        title = (item.get("title") or "").strip()
        play_str = (item.get("play") or "0").strip()
        try:
            n = float(play_str.replace("万", "").replace(",", ""))
            play = round(n * 10000) if "万" in play_str else int(n)
        except (ValueError, TypeError):
            play = 0

        videos.append({"bvid": bvid, "title": title, "play": play})
