import json
import logging
import asyncio
from typing import Optional, Callable, Awaitable
from urllib.parse import urlencode
from playwright.async_api import Page
from backend.config import PAGE_TIMEOUT
from backend.crawler.anti_detect import random_delay
from backend.crawler.browser_pool import browser_pool
from backend.crawler.wbi_sign import sign_params, get_mixin_key

logger = logging.getLogger(__name__)

# 翻页并发数（改为 1 串行，避免触发验证码）
FETCH_CONCURRENCY = 1

# 进度回调: (current, total, message)
VideoProgressCallback = Callable[[int, int, str], Awaitable[None]]


async def scrape_up_info(page: Page, uid: str) -> Optional[dict]:
    """爬取 UP 主基本信息 + 从 arc/search 提前获取视频总数"""
    result = {"uid": uid, "video_count": 0}
    await random_delay()
    try:
        # card API 获取基本信息
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

        # 尝试从 card API 的 archive_count 获取视频数
        archive_count = (
            result.get("raw_data", {}).get("archive_count")
            if isinstance(result.get("raw_data"), dict) else None
        )
        if archive_count and isinstance(archive_count, int) and archive_count > 0:
            result["video_count"] = archive_count
            return result

        # 降级：用 WBI arc/search?ps=1 获取总数
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
                    result["video_count"] = total or 0
    except Exception as e:
        logger.error(f"爬取UP信息失败 uid={uid}: {e}")
    return result


async def scrape_up_videos(
    page: Page,
    uid: str,
    max_pages: int = 0,
    progress_callback: Optional[VideoProgressCallback] = None,
) -> list[dict]:
    """爬取 UP 主的视频列表（并发翻页 arc/search API + 进度回调）"""
    videos = []
    seen_bvids: set = set()
    await random_delay()

    async with browser_pool.acquire_headful_context() as context:
        page1 = await context.new_page()
        try:
            # 第 1 页：加载投稿页，拦截 arc/search 获取首页数据 + mixin_key
            upload_url = f"https://space.bilibili.com/{uid}/upload/video"
            resp = await page1.goto(upload_url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
            if not resp or not resp.ok:
                return videos
            await page1.wait_for_timeout(3000)

            # 获取 mixin_key
            mixin_key = await get_mixin_key(page1)
            if not mixin_key:
                logger.error("无法获取 WBI mixin_key uid=%s", uid)
                return videos

            # 用 page.evaluate + fetch 获取首页数据（保证 cookie 和 referer 正确）
            page1_data = await _fetch_arc_page(page1, uid, 1, mixin_key)
            if not page1_data:
                logger.warning("首页 arc/search 无数据 uid=%s", uid)
                return videos

            total_count = _process_arc_data(page1_data, videos, seen_bvids)
            ps = page1_data.get("page", {}).get("ps", 50)
            total_pages = (total_count + ps - 1) // ps if total_count else 0
            if max_pages and max_pages < total_pages:
                total_pages = max_pages

            logger.info("第 1 页获取 %d 条 uid=%s (共 %d 条 %d 页)",
                        len(videos), uid, total_count, total_pages)

            if progress_callback:
                await progress_callback(1, total_pages,
                                        f"第 1/{total_pages} 页, 已获取 {len(videos)}/{total_count} 条")

            await page1.close()
            page1 = None  # 标记已关闭，避免 finally 重复关闭

            # 第 2 页起：并发请求
            if total_pages <= 1:
                return videos

            sem = asyncio.Semaphore(FETCH_CONCURRENCY)

            async def _fetch_one_page(pn: int):
                async with sem:
                    await random_delay()  # 每页之间随机延迟，避免触发验证码
                    pg = await context.new_page()
                    try:
                        data = await _fetch_arc_page(pg, uid, pn, mixin_key)
                        return (pn, data)
                    finally:
                        await pg.close()

            remaining = list(range(2, total_pages + 1))
            tasks = [_fetch_one_page(pn) for pn in remaining]
            completed_pages = 1
            errors = 0

            for coro in asyncio.as_completed(tasks):
                try:
                    pn, data = await coro
                    if data:
                        _process_arc_data(data, videos, seen_bvids)
                    else:
                        errors += 1
                except Exception as exc:
                    errors += 1
                    logger.warning("翻页失败 pn: %s", exc)

                completed_pages += 1
                if progress_callback:
                    await progress_callback(
                        completed_pages, total_pages,
                        f"第 {completed_pages}/{total_pages} 页, 已获取 {len(videos)}/{total_count} 条"
                    )

            if errors:
                logger.warning("并发翻页 %d 页失败 uid=%s", errors, uid)

            if total_count and len(videos) < total_count:
                logger.warning("视频数不完整 uid=%s: 获取 %d / 应有 %d",
                               uid, len(videos), total_count)

        except Exception as exc:
            logger.error(f"爬取视频列表失败 uid={uid}: {exc}")
        finally:
            if page1 is not None:
                await page1.close()

    return videos


async def _fetch_arc_page(page: Page, uid: str, pn: int, mixin_key: str) -> dict | None:
    """用 page.goto 调 arc/search API（设 Referer 头代替访问空间页，避免多余请求）"""
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
