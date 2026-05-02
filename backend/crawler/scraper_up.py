import json
import logging
import asyncio
from typing import Optional
from playwright.async_api import Page, async_playwright
from backend.config import PAGE_TIMEOUT
from backend.crawler.anti_detect import random_delay

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


async def scrape_up_videos(page: Page, uid: str, max_pages: int = 3) -> list[dict]:
    """爬取 UP 主的视频列表（headful DOM 提取，绕过 headless 检测）"""
    videos = []
    await random_delay()
    playwright = None
    browser = None
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page2 = await context.new_page()

        space_url = f"https://space.bilibili.com/{uid}"
        resp = await page2.goto(space_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
        if not resp or not resp.ok:
            return videos
        await page2.wait_for_timeout(2000)

        seen_bv = set()

        async def extract_videos():
            items = await page2.locator('a[href*="BV"]').all()
            for item in items:
                href = await item.get_attribute("href") or ""
                bvid = href.split("BV")[-1]
                if bvid:
                    bvid = "BV" + bvid
                else:
                    continue
                if bvid in seen_bv:
                    continue
                seen_bv.add(bvid)

                title = ""
                play = 0
                try:
                    el = await item.evaluate_handle("""
                        el => {
                            let p = el;
                            for (let i=0; i<6; i++) {
                                p = p.parentElement;
                                if (!p) break;
                                if (p.tagName === 'LI' || String(p.className||'').indexOf('card')>=0) break;
                            }
                            let t = p ? p.querySelector('[class*=\"title\"]') : null;
                            let s = p ? p.querySelectorAll('span') : [];
                            let nums = [];
                            s.forEach(span => {
                                let txt = (span.textContent||'').trim();
                                if (/[\\d.]+万?/.test(txt)) nums.push(txt);
                            });
                            return {
                                title: t ? (t.getAttribute('title') || t.textContent || '') : '',
                                play: nums.length > 0 ? nums[0] : '0'
                            };
                        }
                    """)
                    data = await el.json_value()
                    title = (data.get("title") or "").strip()
                    play_str = (data.get("play") or "0").strip()
                    n = float(play_str.replace("万", "").replace(",", ""))
                    play = round(n * 10000) if "万" in play_str else int(n)
                except Exception:
                    pass
                videos.append({"bvid": bvid, "title": title, "play": play})

        await extract_videos()
        logger.info("第 1 页获取 %d 条视频 uid=%s", len(videos), uid)

        for pn in range(2, max_pages + 1):
            await random_delay()
            await page2.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
            before = len(videos)
            await extract_videos()
            added = len(videos) - before
            logger.info("第 %d 页获取 %d 条视频 uid=%s", pn, added, uid)
            if added == 0:
                break

        await context.close()
    except Exception as e:
        logger.error(f"爬取视频列表失败 uid={uid}: {e}")
    finally:
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()

    return videos
