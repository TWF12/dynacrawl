import json
import logging
import re
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
    """爬取 UP 主的视频列表（INITIAL_STATE → API 拦截 → DOM 降级）"""
    videos = []
    await random_delay()

    async with browser_pool.acquire_headful_page() as page2:
        # 同时使用 route 拦截 arc/search 请求 + on('response') 作为兜底
        intercepted_responses: list[dict] = []

        async def _capture_arc_search(route):
            """拦截 arc/search 请求，route.fetch() 拿到响应后再 fulfill 回去"""
            response = await route.fetch()
            try:
                body = await response.json()
                if body.get("code") == 0 and isinstance(body.get("data"), dict):
                    intercepted_responses.append(body["data"])
            except Exception:
                pass
            await route.fulfill(response=response)

        await page2.route(
            lambda url: bool(re.search(r'api\.bilibili\.com/x/space/.*arc.*search', url)),
            _capture_arc_search,
        )

        try:
            space_url = f"https://space.bilibili.com/{uid}"
            resp = await page2.goto(space_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
            if not resp or not resp.ok:
                return videos
            await page2.wait_for_timeout(3000)

            seen_bvids: set = set()
            total_count = 0

            # 第一步：尝试从 __INITIAL_STATE__ 或拦截响应中提取首屏数据
            initial_used = False
            if intercepted_responses:
                _process_response_data(intercepted_responses[-1], videos, seen_bvids)
                total_count = intercepted_responses[-1].get("page", {}).get("count", 0) or total_count
                logger.info("API 拦截首屏获取 %d 条视频 uid=%s", len(videos), uid)
            else:
                # 没有 API 响应，尝试从 SSR 的 __INITIAL_STATE__ 提取
                ssr_data = await _extract_from_initial_state(page2, uid)
                if ssr_data:
                    for v in ssr_data:
                        bvid = (v.get("bvid") or "").strip()
                        if bvid and bvid not in seen_bvids:
                            seen_bvids.add(bvid)
                            videos.append({
                                "bvid": bvid,
                                "title": (v.get("title") or "").strip(),
                                "play": v.get("play", 0),
                            })
                    initial_used = True
                    logger.info("INITIAL_STATE 获取 %d 条视频 uid=%s", len(videos), uid)

            # 如果以上都没有数据，再次等待看是否有延迟 API 调用
            if not videos:
                await page2.wait_for_timeout(3000)
                if intercepted_responses:
                    for data in intercepted_responses:
                        _process_response_data(data, videos, seen_bvids)
                    total_count = intercepted_responses[-1].get("page", {}).get("count", 0) or total_count
                    logger.info("延迟 API 拦截获取 %d 条视频 uid=%s", len(videos), uid)

            if not videos:
                raise RuntimeError("首屏无数据，降级 DOM 提取")

            # 翻页：滚动触发懒加载，等待新的 arc/search 响应
            expected_pages = min(max_pages, (total_count + 29) // 30) if total_count else max_pages
            seen_page_numbers: set = {1}

            for pn in range(2, expected_pages + 1):
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

                before_add = len(videos)
                for data in intercepted_responses[before_count:]:
                    pn_new = data.get("page", {}).get("pn", 0)
                    if pn_new:
                        seen_page_numbers.add(pn_new)
                    _process_response_data(data, videos, seen_bvids)
                added = len(videos) - before_add
                logger.info("API 拦截第 %d 页获取 %d 条视频 uid=%s", pn, added, uid)

                if added == 0:
                    break

        except Exception as exc:
            logger.warning("API/SSR 提取失败，降级为 DOM 提取 uid=%s: %s", uid, exc)
            try:
                await page2.unroute(
                    lambda url: bool(re.search(r'api\.bilibili\.com/x/space/.*arc.*search', url)),
                )
            except Exception:
                pass
            try:
                await page2.goto(
                    f"https://space.bilibili.com/{uid}",
                    timeout=PAGE_TIMEOUT, wait_until="networkidle",
                )
                await page2.wait_for_timeout(2000)
            except Exception:
                pass
            videos = await _extract_videos_dom(page2, uid, max_pages)

    return videos


def _process_response_data(data: dict, videos: list[dict], seen_bvids: set):
    """从 arc/search 响应 JSON 中提取视频数据"""
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


async def _extract_from_initial_state(page2: Page, uid: str) -> list[dict]:
    """尝试从页面 SSR 的 window.__INITIAL_STATE__ 提取视频列表"""
    try:
        raw = await page2.evaluate("""
            () => {
                const st = window.__INITIAL_STATE__;
                if (!st) return null;
                // 尝试多种可能的路径
                const vlist =
                    (st.videoList && st.videoList.list && st.videoList.list.vlist) ||
                    (st.space && st.space.videoList && st.space.videoList.list && st.space.videoList.list.vlist) ||
                    null;
                if (!vlist) return null;
                return vlist.map(v => ({ bvid: v.bvid || '', title: v.title || '', play: v.play || 0 }));
            }
        """)
        return raw if raw else []
    except Exception:
        return []


async def _extract_videos_dom(page2: Page, uid: str, max_pages: int = 3) -> list[dict]:
    """降级方案：DOM 提取视频列表"""
    videos = []
    seen_bv = set()

    def _clean_bv(href: str) -> str:
        """从 href 中精确提取 BV 号，排除 query 参数和路径尾缀"""
        m = re.search(r'BV[A-Za-z0-9]{10}', href)
        return m.group(0) if m else ""

    async def _extract():
        items = await page2.locator('a[href*="BV"]').all()
        for item in items:
            href = (await item.get_attribute("href") or "")
            bvid = _clean_bv(href)
            if not bvid or bvid in seen_bv:
                continue
            seen_bv.add(bvid)

            title = ""
            play = 0
            try:
                el = await item.evaluate_handle("""
                    el => {
                        // 向上查找到视频卡片容器（最多 8 层）
                        let card = el;
                        for (let i = 0; i < 8; i++) {
                            card = card.parentElement;
                            if (!card) break;
                            let cls = String(card.className || '');
                            if (cls.indexOf('card') >= 0 || cls.indexOf('video') >= 0 ||
                                cls.indexOf('item') >= 0 || card.tagName === 'LI') break;
                        }
                        // 在 card 内查标题（多种可能选择器）
                        let t = card ? (
                            card.querySelector('.title, [class*="title"], a[title], [class*="name"]')
                        ) : null;
                        let titleText = t ? (t.getAttribute('title') || t.textContent || '').trim() : '';
                        // 如果还是没找到 title，尝试找 h3/h4 等
                        if (!titleText && card) {
                            let h = card.querySelector('h3, h4, .text, .desc');
                            if (h) titleText = (h.textContent || '').trim();
                        }
                        // 播放量：优先找带"播放""观看"的 span
                        let spans = card ? card.querySelectorAll('span') : [];
                        let playText = '';
                        spans.forEach(span => {
                            let txt = (span.textContent || '').trim();
                            if (/播放|观看/.test(txt)) {
                                let m = txt.match(/([\\d.]+万?)/);
                                if (m) playText = m[1];
                            }
                        });
                        if (!playText) {
                            spans.forEach(span => {
                                let txt = (span.textContent || '').trim();
                                if (/^[\\d.]+万?$/.test(txt) && !playText) playText = txt;
                            });
                        }
                        return { title: titleText, play: playText || '0' };
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

