import json
import logging
import re
import asyncio
from typing import Optional, Callable, Awaitable
from urllib.parse import urlencode
from playwright.async_api import Page
from backend.config import PAGE_TIMEOUT
from backend.crawler.anti_detect import random_delay
from backend.crawler.browser_pool import browser_pool
from backend.crawler.wbi_sign import sign_params, get_mixin_key

logger = logging.getLogger(__name__)

FETCH_CONCURRENCY = 1
# 进度回调: (current, total, message)
VideoProgressCallback = Callable[[int, int, str], Awaitable[None]]


async def scrape_up_info(page: Page, uid: str) -> Optional[dict]:
    """爬取 UP 主基本信息 + 多途径获取真实视频总数"""
    result = {"uid": uid, "video_count": 0}
    errors = []
    await random_delay()
    try:
        # 1. card API → 基本信息 + archive_count（优先，拿到即返回）
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
                ac = card.get("archive_count", 0)
                if ac and isinstance(ac, int) and ac > 0:
                    result["video_count"] = ac
                    return result
            else:
                errors.append("API 请求失败")
        else:
            errors.append("API 请求失败")

        # 2. card API 没拿到 → arc/search?ps=1
        mixin_key = await get_mixin_key(page)
        if mixin_key:
            params = sign_params({
                "mid": uid, "ps": "1", "pn": "1",
                "tid": "0", "keyword": "", "order": "pubdate",
            }, mixin_key)
            api_url = f"https://api.bilibili.com/x/space/wbi/arc/search?{urlencode(params)}"
            resp = await page.goto(api_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
            if resp and resp.ok:
                body_text = await page.evaluate("() => document.body.innerText")
                data = json.loads(body_text)
                if data.get("code") == 0:
                    total = data.get("data", {}).get("page", {}).get("count", 0)
                    if total:
                        result["video_count"] = total
                        return result
                else:
                    errors.append("API 请求失败")
            else:
                errors.append("API 请求失败")
        else:
            errors.append("API 请求失败")

        # 3. API 都失败 → 加载 /upload/video 从 sidebar DOM 提取
        try:
            upload_url = f"https://space.bilibili.com/{uid}/upload/video"
            resp = await page.goto(upload_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
            if resp and resp.ok:
                await page.wait_for_timeout(2000)
                dom_count = await _get_video_count_from_page(page, uid)
                if dom_count:
                    result["video_count"] = dom_count
                else:
                    errors.append("DOM 提取失败")
            else:
                errors.append("页面加载失败")
        except Exception:
            errors.append("页面加载失败")

    except Exception as e:
        logger.error(f"爬取UP信息失败 uid={uid}: {e}")
        errors.append("请求超时")

    if errors:
        result["errors"] = errors
    result["status"] = _pick_status(errors, result["video_count"] > 0)

    return result


# ============================================================
# DOM 兜底提取
# ============================================================

async def _dom_extract(page: Page, uid: str, seen_bvids: set) -> list[dict]:
    """从当前页面 DOM 提取所有可见的视频卡片"""
    try:
        raw = await page.evaluate("""
            () => {
                let results = [];
                let links = document.querySelectorAll('a[href*="/video/BV"]');
                links.forEach(a => {
                    let href = a.getAttribute('href') || '';
                    let bvMatch = href.match(/BV[A-Za-z0-9]{10}/);
                    if (!bvMatch) return;
                    let bvid = bvMatch[0];
                    let img = a.querySelector('img');
                    let title = img ? (img.getAttribute('alt') || '').trim() : '';
                    let rawText = (a.textContent || '').trim();
                    let playMatch = rawText.match(/([\\d.]+万?)/);
                    let play = playMatch ? playMatch[1] : '0';
                    results.push({bvid, title, play});
                });
                return results;
            }
        """)
        videos = []
        for item in raw:
            bvid = item.get("bvid", "")
            if bvid and bvid not in seen_bvids:
                seen_bvids.add(bvid)
                title = (item.get("title") or "").strip()
                play_str = (item.get("play") or "0").strip()
                try:
                    n = float(play_str.replace("万", "").replace(",", ""))
                    play = round(n * 10000) if "万" in play_str else int(n)
                except (ValueError, TypeError):
                    play = 0
                videos.append({"bvid": bvid, "title": title, "play": play})
        return videos
    except Exception:
        return []


async def _dom_scroll_for_more(page: Page, max_scrolls: int = 10) -> int:
    """滚动页面触发懒加载，返回滚动后新增的可见链接数"""
    before = await page.evaluate(
        "document.querySelectorAll('a[href*=\"/video/BV\"]').length")
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(2)
    after = await page.evaluate(
        "document.querySelectorAll('a[href*=\"/video/BV\"]').length")
    return after - before


async def _dom_fallback(context, uid: str, seen_bvids: set, page1) -> tuple[list[dict], int]:
    """多页面试探 DOM 兜底提取视频，返回 (videos, total_count)"""
    all_videos = []
    total_count = 0

    urls_to_try = [
        f"https://space.bilibili.com/{uid}/lists",
        f"https://space.bilibili.com/{uid}/video?tid=0&pn=1&keyword=&order=pubdate",
        f"https://space.bilibili.com/{uid}",
    ]

    for attempt_url in urls_to_try:
        try:
            await page1.goto(attempt_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
            await page1.wait_for_timeout(3000)

            # 提取视频数和视频列表
            total_count = await _get_video_count_from_page(page1, uid) or total_count
            videos = await _dom_extract(page1, uid, seen_bvids)
            all_videos.extend(videos)

            if all_videos:
                # 滚动翻页
                for _ in range(10):
                    new_count = await _dom_scroll_for_more(page1)
                    if new_count == 0:
                        break
                    await random_delay()
                    more = await _dom_extract(page1, uid, seen_bvids)
                    all_videos.extend(more)
                break  # 成功获取到数据，停止尝试其他 URL
        except Exception:
            continue

    return all_videos, total_count


# ============================================================
# 视频列表主函数
# ============================================================

PageDoneCallback = Callable[[list[dict], int], Awaitable[None]]


async def scrape_up_videos(
    page: Page,
    uid: str,
    max_pages: int = 0,
    progress_callback: Optional[VideoProgressCallback] = None,
    on_page_done: Optional[PageDoneCallback] = None,
) -> dict:
    """爬取 UP 主的视频列表，每页完成后通过 on_page_done 实时回传数据"""
    videos = []
    errors: list[str] = []
    total_count = 0
    seen_bvids: set = set()
    await random_delay()

    async def _save_page(page_videos: list[dict]):
        if on_page_done and page_videos:
            try:
                await on_page_done(page_videos, len(videos))
            except Exception as exc:
                logger.warning("实时回传失败: %s", exc)

    async with browser_pool.acquire_headful_context() as context:
        page1 = await context.new_page()
        try:
            # 第 1 页：加载投稿页，尝试 arc/search
            upload_url = f"https://space.bilibili.com/{uid}/upload/video"
            resp = await page1.goto(upload_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
            if not resp or not resp.ok:
                errors.append("页面加载失败")
                return {"videos": videos, "total_count": 0, "errors": errors, "status": "failed"}
            await page1.wait_for_timeout(3000)

            # 获取 mixin_key
            mixin_key = await get_mixin_key(page1)
            if not mixin_key:
                errors.append("API 请求失败")
                logger.error("无法获取 WBI mixin_key uid=%s", uid)
                return {"videos": videos, "total_count": 0, "errors": errors, "status": "failed"}

            # 首页数据：先尝试 arc/search API，失败则 DOM 兜底
            page1_data = await _fetch_arc_page(page1, uid, 1, mixin_key)
            if not page1_data:
                # arc/search 失败 → 多页面 DOM 兜底
                logger.warning("arc/search 无响应 uid=%s，启用 DOM 兜底", uid)
                dom_videos, dom_total = await _dom_fallback(context, uid, seen_bvids, page1)
                for v in dom_videos:
                    videos.append(v)
                total_count = dom_total or await _get_video_count_from_page(page1, uid)
                if total_count:
                    errors.append(f"DOM 提取 {len(videos)} 条, 总数 {total_count}")
                elif len(videos) > 0:
                    errors.append(f"DOM 提取 {len(videos)} 条")
                else:
                    errors.append("API 请求失败")
                return {"videos": videos, "total_count": total_count, "errors": errors,
                        "status": _pick_status(errors, len(videos) > 0)}

            total_count = _process_arc_data(page1_data, videos, seen_bvids)
            ps = page1_data.get("page", {}).get("ps", 50)
            total_pages = (total_count + ps - 1) // ps if total_count else 0
            if max_pages and max_pages < total_pages:
                total_pages = max_pages

            logger.info("第 1 页获取 %d 条 uid=%s (共 %d 条 %d 页)",
                        len(videos), uid, total_count, total_pages)

            await _save_page(videos[:])

            if progress_callback:
                await progress_callback(1, total_pages,
                                        f"第 1/{total_pages} 页, 已获取 {len(videos)}/{total_count} 条")

            await page1.close()
            page1 = None

            # 第 2 页起：并发请求
            if total_pages <= 1:
                return {"videos": videos, "total_count": total_count, "errors": errors,
                        "status": _pick_status(errors, len(videos) > 0)}

            sem = asyncio.Semaphore(FETCH_CONCURRENCY)

            async def _fetch_one_page(pn: int):
                await random_delay()
                async with sem:
                    pg = await context.new_page()
                    try:
                        data = await _fetch_arc_page(pg, uid, pn, mixin_key)
                        return (pn, data)
                    finally:
                        await pg.close()

            remaining = list(range(2, total_pages + 1))
            tasks = [_fetch_one_page(pn) for pn in remaining]
            completed_pages = 1
            page_errors = 0

            for coro in asyncio.as_completed(tasks):
                try:
                    pn, data = await coro
                    if data:
                        before = len(videos)
                        _process_arc_data(data, videos, seen_bvids)
                        new_vids = videos[before:]
                        if new_vids:
                            await _save_page(new_vids)
                    else:
                        page_errors += 1
                except Exception as exc:
                    page_errors += 1
                    logger.warning("翻页失败 pn: %s", exc)

                completed_pages += 1
                if progress_callback:
                    await progress_callback(
                        completed_pages, total_pages,
                        f"第 {completed_pages}/{total_pages} 页, 已获取 {len(videos)}/{total_count} 条"
                    )

            if page_errors:
                logger.warning("翻页失败数: %d uid=%s", page_errors, uid)

            if total_count and len(videos) < total_count:
                errors.append(f"视频列表不完整: 获取 {len(videos)}/{total_count}")

        except Exception as exc:
            logger.error(f"爬取视频列表失败 uid={uid}: {exc}")
            errors.append("请求超时")
        finally:
            if page1 is not None:
                await page1.close()

    return {"videos": videos, "total_count": total_count, "errors": errors,
            "status": _pick_status(errors, len(videos) > 0)}


# ============================================================
# 工具函数
# ============================================================

async def _fetch_arc_page(page: Page, uid: str, pn: int, mixin_key: str) -> dict | None:
    """用 page.goto 调 arc/search API（设 Referer 头）"""
    params = sign_params({
        "mid": uid, "ps": "50", "pn": str(pn),
        "tid": "0", "keyword": "", "order": "pubdate",
    }, mixin_key)
    api_url = f"https://api.bilibili.com/x/space/wbi/arc/search?{urlencode(params)}"

    try:
        await page.set_extra_http_headers({
            "Referer": f"https://space.bilibili.com/{uid}/upload/video",
        })
        resp = await page.goto(api_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        if resp and resp.ok:
            text = await page.evaluate("() => document.body.innerText")
            data = json.loads(text)
            if data.get("code") == 0 and isinstance(data.get("data"), dict):
                return data["data"]
            else:
                logger.warning("arc/search pn=%d code=%d msg=%s",
                               pn, data.get("code"), data.get("message", ""))
    except Exception as exc:
        logger.warning("fetch arc/search pn=%d 失败: %s", pn, exc)
    return None


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


def _pick_status(errors: list[str], has_data: bool) -> str:
    """根据 errors 和数据有无判断状态: ok / fallback / failed"""
    if not errors:
        return "ok"
    if has_data:
        return "fallback"
    return "failed"


async def _get_video_count_from_page(page: Page, uid: str) -> int:
    """从页面的 sidebar 提取视频总数"""
    try:
        count = await page.evaluate("""
            () => {
                let activeItem = document.querySelector('.side-nav__item.active');
                if (activeItem) {
                    let subText = activeItem.querySelector('.side-nav__item__sub-text');
                    if (subText) {
                        let n = parseInt((subText.textContent || '').trim());
                        if (n > 0) return n;
                    }
                }
                let items = document.querySelectorAll('.side-nav__item');
                for (let item of items) {
                    let text = (item.textContent || '').trim();
                    let m = text.match(/视频\\s*(\\d+)/);
                    if (m) return parseInt(m[1]);
                }
                let tabs = document.querySelectorAll('.nav-tab__item');
                for (let tab of tabs) {
                    let text = (tab.textContent || '').trim();
                    let m = text.match(/投稿\\s*(\\d+)/);
                    if (m) return parseInt(m[1]);
                }
                return 0;
            }
        """)
        return count or 0
    except Exception:
        return 0
