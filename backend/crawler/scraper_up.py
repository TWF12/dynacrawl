import json
import logging
import time
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


async def scrape_up_videos(page: Page, uid: str, max_pages: int = 3) -> list[dict]:
    """爬取 UP 主的视频列表（拦截 arc/search API 响应，DOM 降级）"""
    videos = []
    await random_delay()

    async with browser_pool.acquire_headful_page() as page2:
        intercepted_responses: list[dict] = []

        async def _on_arc_search_response(response):
            if "/x/space/wbi/arc/search" not in response.url:
                return
            try:
                body = await response.json()
                if body.get("code") == 0 and isinstance(body.get("data"), dict):
                    intercepted_responses.append(body["data"])
            except Exception as exc:
                logger.warning("解析 arc/search 响应失败: %s", exc)

        page2.on("response", _on_arc_search_response)

        try:
            space_url = f"https://space.bilibili.com/{uid}"
            resp = await page2.goto(space_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
            if not resp or not resp.ok:
                return videos
            await page2.wait_for_timeout(3000)

            if not intercepted_responses:
                raise RuntimeError("未拦截到 arc/search API 响应")

            seen_bvids: set = set()
            seen_page_numbers: set = set()
            total_count = 0

            for data in intercepted_responses:
                pn = data.get("page", {}).get("pn", 0)
                if pn:
                    seen_page_numbers.add(pn)
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
                total_count = data.get("page", {}).get("count", 0) or total_count

            logger.info("API 拦截第 1 页获取 %d 条视频 uid=%s", len(videos), uid)

            # 翻页：滚动触发懒加载，等待新的 arc/search 响应
            expected_pages = min(max_pages, (total_count + 29) // 30) if total_count else max_pages

            for pn in range(2, expected_pages + 1):
                if pn in seen_page_numbers:
                    continue

                await random_delay()
                before_count = len(intercepted_responses)
                await page2.evaluate("window.scrollTo(0, document.body.scrollHeight)")

                deadline = time.monotonic() + 15.0
                while len(intercepted_responses) <= before_count:
                    if time.monotonic() > deadline:
                        break
                    await asyncio.sleep(0.5)

                if len(intercepted_responses) <= before_count:
                    logger.info("翻页无新响应，停止 uid=%s", uid)
                    break

                for data in intercepted_responses[before_count:]:
                    pn_new = data.get("page", {}).get("pn", 0)
                    if pn_new:
                        seen_page_numbers.add(pn_new)
                    vlist = data.get("list", {}).get("vlist", []) or []
                    before_add = len(videos)
                    for v in vlist:
                        bvid = (v.get("bvid") or "").strip()
                        if bvid and bvid not in seen_bvids:
                            seen_bvids.add(bvid)
                            videos.append({
                                "bvid": bvid,
                                "title": (v.get("title") or "").strip(),
                                "play": v.get("play", 0),
                            })
                    added = len(videos) - before_add
                    logger.info("API 拦截第 %d 页获取 %d 条视频 uid=%s", pn_new, added, uid)

                if added == 0:
                    break

        except Exception as exc:
            logger.warning("API 拦截失败，降级为 DOM 提取 uid=%s: %s", uid, exc)
            try:
                page2.remove_listener("response", _on_arc_search_response)
            except Exception:
                pass
            # 确保页面已加载
            try:
                space_url = f"https://space.bilibili.com/{uid}"
                await page2.goto(space_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
                await page2.wait_for_timeout(2000)
            except Exception:
                pass
            videos = await _extract_videos_dom(page2, uid, max_pages)

    return videos


async def _extract_videos_dom(page2: Page, uid: str, max_pages: int = 3) -> list[dict]:
    """降级方案：DOM 提取视频列表"""
    videos = []
    seen_bv = set()

    async def _extract():
        items = await page2.locator('a[href*="BV"]').all()
        for item in items:
            href = (await item.get_attribute("href") or "")
            bvid = "BV" + href.split("BV")[-1] if href and "BV" in href else ""
            if not bvid or bvid in seen_bv:
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

    await _extract()
    logger.info("DOM 降级第 1 页获取 %d 条视频 uid=%s", len(videos), uid)

    for pn in range(2, max_pages + 1):
        await random_delay()
        await page2.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)
        before = len(videos)
        await _extract()
        added = len(videos) - before
        logger.info("DOM 降级第 %d 页获取 %d 条视频 uid=%s", pn, added, uid)
        if added == 0:
            break

    return videos
