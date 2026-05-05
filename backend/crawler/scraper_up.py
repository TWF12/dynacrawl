import json
import logging
import asyncio
import random
from typing import Optional, Callable, Awaitable
from urllib.parse import urlencode
from playwright.async_api import Page
from backend.config import PAGE_TIMEOUT
from backend.crawler.anti_detect import random_delay, is_task_cancelled
from backend.crawler.browser_pool import browser_pool
from backend.crawler.cookie_manager import cookie_manager
from backend.crawler.wbi_sign import sign_params, get_mixin_key

logger = logging.getLogger(__name__)

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

        await page.set_extra_http_headers({
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        })
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
                errors.append("card异常")
        else:
            errors.append("card失败")

        # 2. card API 没拿到 → arc/search?ps=1
        mixin_key = await get_mixin_key(page)
        if mixin_key:
            params = sign_params({
                "mid": uid, "ps": "1", "pn": "1",
                "tid": "0", "keyword": "", "order": "pubdate",
            }, mixin_key)
            await page.set_extra_http_headers({
                "Referer": f"https://space.bilibili.com/{uid}",
                "Origin": "https://space.bilibili.com",
            })
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
                    errors.append("arc/search异常")
            else:
                errors.append("arc/search失败")
        else:
            errors.append("WBI密钥失败")

        # 3. API 都失败 → 加载 /upload/video 从 sidebar DOM 提取
        # 用 domcontentloaded 而非 networkidle, B站页面持续网络请求永不 idle
        try:
            upload_url = f"https://space.bilibili.com/{uid}/upload/video"
            resp = await page.goto(upload_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
            if resp and resp.ok:
                await page.wait_for_timeout(3000)
                dom_count = await _get_video_count_from_page(page, uid)
                if dom_count:
                    result["video_count"] = dom_count
                else:
                    errors.append("sidebar无数据")
            else:
                errors.append("投稿页失败")
        except Exception:
            errors.append("投稿页超时")

    except Exception as e:
        logger.error(f"爬取UP信息失败 uid={uid}: {e}")
        errors.append("超时")

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


async def _dom_fallback(uid: str, seen_bvids: set, page1,
                       progress_callback=None) -> tuple[list[dict], int]:
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
            await page1.goto(attempt_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
            await page1.wait_for_timeout(3000)

            total_count = await _get_video_count_from_page(page1, uid) or total_count
            videos = await _dom_extract(page1, uid, seen_bvids)
            all_videos.extend(videos)

            if progress_callback:
                await progress_callback(1, max(1, total_count or 1),
                                        f"DOM 兜底: 已获取 {len(all_videos)}/{total_count or '?'} 条")

            if all_videos:
                for scroll_i in range(10):
                    new_count = await _dom_scroll_for_more(page1)
                    if new_count == 0:
                        break
                    await random_delay()
                    more = await _dom_extract(page1, uid, seen_bvids)
                    all_videos.extend(more)
                    if progress_callback and total_count:
                        await progress_callback(
                            min(scroll_i + 2, max(1, total_count)), max(1, total_count),
                            f"DOM 兜底: 已获取 {len(all_videos)}/{total_count} 条")
                break
        except Exception:
            continue

    return all_videos, total_count


# ============================================================
# 视频列表主函数
# ============================================================

PageDoneCallback = Callable[[list[dict], int], Awaitable[None]]

# 单 session 最大翻页数 (随机范围)
def _session_page_limit(total_pages: int) -> int:
    """按总页数自适应: 大UP多轮换(~20%), 小UP不轮换"""
    if total_pages <= 20:
        return total_pages + 1  # 一口气跑完, 不轮换
    return max(15, min(40, total_pages // 5))


def _progressive_delay(pn: int, total_pages: int) -> float:
    """渐进延迟: 按进度比例缩放, 小UP快大UP慢, 兼顾效率与风控"""
    ratio = pn / max(total_pages, 20)
    if ratio <= 0.15:
        return random.uniform(2, 6)
    elif ratio <= 0.4:
        return random.uniform(6, 15)
    elif ratio <= 0.7:
        return random.uniform(12, 25)
    else:
        return random.uniform(18, 35)


async def _init_session(ctx, uid: str) -> str | None:
    """为新 session 获取 mixin_key, 加载轻量首页替代投稿页"""
    pg = await ctx.new_page()
    try:
        resp = await pg.goto("https://www.bilibili.com/", timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        if not resp or not resp.ok:
            return None
        await pg.wait_for_timeout(500)
        return await get_mixin_key(pg)
    finally:
        await pg.close()


async def scrape_up_videos(
    page: Optional[Page],
    uid: str,
    max_pages: int = 0,
    progress_callback: Optional[VideoProgressCallback] = None,
    on_page_done: Optional[PageDoneCallback] = None,
    task_id: str = "",
    existing_bvids: set[str] = None,
) -> dict:
    """爬取 UP 主的视频列表，自动 session 轮换防风控。existing_bvids 用于断点续爬"""
    if existing_bvids is None:
        existing_bvids = set()
    existing_count = len(existing_bvids)
    videos: list[dict] = []
    errors: list[str] = []
    total_count = 0
    seen_bvids: set[str] = set(existing_bvids)
    await random_delay()

    async def _save_page(page_videos: list[dict]):
        if on_page_done and page_videos:
            try:
                await on_page_done(page_videos, len(videos))
            except Exception as exc:
                logger.warning("实时回传失败: %s", exc)

    # ================================================================
    # Phase 1: 第 1 页 — 获取 total_count 和首页视频
    # ================================================================
    async with browser_pool.acquire_headful_context() as ctx:
        mixin_key = await _init_session(ctx, uid)
        if not mixin_key:
            errors.append("WBI密钥失败")
            return {"videos": videos, "total_count": 0, "errors": errors, "status": "failed"}

        pg1 = await ctx.new_page()
        try:
            page1_data, _ = await _fetch_arc_page(pg1, uid, 1, mixin_key)
        finally:
            await pg1.close()

        if page1_data is None:
            # arc/search 失败 → DOM 兜底
            logger.warning("arc/search 无响应 uid=%s，启用 DOM 兜底", uid)
            pg_dom = await ctx.new_page()
            try:
                dom_videos, dom_total = await _dom_fallback(
                    uid, seen_bvids, pg_dom, progress_callback)
            finally:
                await pg_dom.close()
            for v in dom_videos:
                videos.append(v)
            total_count = dom_total or 0
            if total_count:
                errors.append(f"DOM提取 {len(videos)}/{total_count} 条")
            elif len(videos) > 0:
                errors.append(f"DOM提取 {len(videos)} 条")
            else:
                errors.append("API+DOM均未提取到视频")
            return {"videos": videos, "total_count": total_count, "errors": errors,
                    "status": _pick_status(errors, len(videos) > 0)}

        total_count = _process_arc_data(page1_data, videos, seen_bvids)
        ps = page1_data.get("page", {}).get("ps", 50)
        total_pages = (total_count + ps - 1) // ps if total_count else 0
        if max_pages and max_pages < total_pages:
            total_pages = max_pages

        logger.info("第 1 页获取 %d 条 uid=%s (已有 %d 共 %d 条 %d 页)",
                    len(videos), uid, existing_count, total_count, total_pages)
        await _save_page(videos[:])
        # 续爬时第1页只是验证, 没有新视频就不发进度, 避免闪现旧数量
        if progress_callback and (existing_count == 0 or len(videos) > 0):
            await progress_callback(1, total_pages,
                                    f"第 1/{total_pages} 页, 已获取 {existing_count + len(videos)}/{total_count} 条")

    if total_pages <= 1:
        return {"videos": videos, "total_count": total_count, "errors": errors,
                "status": _pick_status(errors, len(videos) > 0)}

    # ================================================================
    # Phase 2: 断点续爬 — 已有 BV 跳过, 从 last_page+1 开始
    # ================================================================
    current_pn = max(2, (existing_count // 50) + 1)
    if current_pn > total_pages:
        logger.info("所有 %d 页已采集, 无需续爬", total_pages)
        return {"videos": videos, "total_count": total_count, "errors": errors,
                "status": _pick_status(errors, len(videos) > 0)}

    max_session_pages = _session_page_limit(total_pages - current_pn + 1)
    page_errors = 0
    session_init_failures = 0

    if existing_count > 0:
        logger.info("断点续爬: 已有 %d 条, 从第 %d/%d 页开始",
                   existing_count, current_pn, total_pages)

    # 立即同步初始进度到前端, 避免 Phase 2 首页完成前无反馈
    if progress_callback:
        await progress_callback(current_pn - 1, total_pages,
                                f"第 {current_pn - 1}/{total_pages} 页, 已获取 {existing_count + len(videos)}/{total_count} 条")

    while current_pn <= total_pages:
        if task_id and is_task_cancelled(task_id):
            logger.info("任务 %s 已取消, 停止采集 (已采集 %d 页)", task_id, current_pn - 1)
            errors.append("任务已取消")
            break

        # Cookie 全部用尽, 继续也无法正常采集
        if cookie_manager.count == 0:
            logger.error("所有 Cookie 已用尽, 无法继续采集")
            errors.append("Cookie已用尽")
            break

        async with browser_pool.acquire_headful_context() as ctx:
            mixin_key = await _init_session(ctx, uid)
            if not mixin_key:
                session_init_failures += 1
                if session_init_failures >= 3:
                    errors.append("WBI密钥失败(重试3次)")
                    break
                logger.warning("_init_session 失败, 等待后重试 (%d/3)", session_init_failures)
                await asyncio.sleep(random.uniform(10, 20))
                continue  # 不 break, 让外层 while 创建新 context

            session_init_failures = 0  # 成功后重置
            session_pages = 0
            consecutive_failures = 0

            # 按 cookie 数量创建备用 context, 所有 cookie 均匀分摊请求
            alt_ctxs: list = []
            alt_pages: list = []
            extra_cookies = cookie_manager.count - 1  # 主 context 已用 1 个
            for i in range(extra_cookies):
                try:
                    actx = await browser_pool._new_headful_context(rotate=False)
                    apg = await actx.new_page()
                    await apg.goto("https://www.bilibili.com/", timeout=10000, wait_until="domcontentloaded")
                    alt_ctxs.append(actx)
                    alt_pages.append(apg)
                except Exception:
                    logger.warning("第%d个备用 context 创建失败, 跳过", i + 2)

            api_page = await ctx.new_page()
            all_pages = [api_page] + alt_pages  # 全部 cookie 对应的 page
            cookie_count = len(all_pages)
            # 延迟系数: cookie 越多越安全降延迟, 最低 0.5
            delay_factor = max(0.5, 1.0 / cookie_count + 0.15) if cookie_count >= 2 else 1.0

            try:
                await api_page.goto("https://www.bilibili.com/", timeout=10000, wait_until="domcontentloaded")

                # 预取首页: 在 sleep 期间发起请求, 隐藏网络往返延迟
                pg_first = all_pages[(current_pn - 1) % cookie_count]
                pending = asyncio.ensure_future(_fetch_arc_page(pg_first, uid, current_pn, mixin_key))
                first_delay = _progressive_delay(current_pn, total_pages) * delay_factor
                await asyncio.sleep(first_delay)

                while session_pages < max_session_pages and current_pn <= total_pages:
                    if task_id and is_task_cancelled(task_id):
                        logger.info("任务 %s 已取消, 停止采集", task_id)
                        if pending and not pending.done():
                            pending.cancel()
                        break

                    # 等待预取结果 (上一轮 sleep 期间已发起请求)
                    data, ratelimited = await pending
                    pending = None
                    should_break = False

                    if data is not None:
                        before = len(videos)
                        _process_arc_data(data, videos, seen_bvids)
                        new_vids = videos[before:]
                        if new_vids:
                            await _save_page(new_vids)
                        current_pn += 1
                        session_pages += 1
                        consecutive_failures = 0
                    elif ratelimited:
                        logger.warning("第 %d 页风控, 切换 session (当前 session 已请求 %d 页)",
                                       current_pn, session_pages)
                        should_break = True
                    else:
                        logger.warning("第 %d 页请求失败, 等待后重试", current_pn)
                        await asyncio.sleep(random.uniform(5, 10))
                        # 重试用下一个 cookie
                        pg_retry = all_pages[current_pn % cookie_count] if cookie_count > 1 else api_page
                        data2, _ = await _fetch_arc_page(pg_retry, uid, current_pn, mixin_key)
                        if data2 is not None:
                            before = len(videos)
                            _process_arc_data(data2, videos, seen_bvids)
                            new_vids = videos[before:]
                            if new_vids:
                                await _save_page(new_vids)
                            current_pn += 1
                            session_pages += 1
                            consecutive_failures = 0
                            logger.info("第 %d 页重试成功", current_pn - 1)
                        else:
                            page_errors += 1
                            consecutive_failures += 1
                            current_pn += 1
                            if consecutive_failures >= 3:
                                logger.warning("连续 %d 页失败, 切换 session", consecutive_failures)
                                should_break = True

                    if should_break:
                        break

                    if progress_callback:
                        await progress_callback(
                            current_pn - 1, total_pages,
                            f"第 {current_pn - 1}/{total_pages} 页, 已获取 {existing_count + len(videos)}/{total_count} 条"
                        )

                    # 预取下一页: sleep 与 fetch 并发, 省掉网络往返时间
                    if current_pn <= total_pages:
                        next_pg = all_pages[(current_pn - 1) % cookie_count]
                        next_delay = _progressive_delay(current_pn, total_pages) * delay_factor
                        pending = asyncio.ensure_future(_fetch_arc_page(next_pg, uid, current_pn, mixin_key))
                        await asyncio.sleep(next_delay)
            finally:
                await api_page.close()
                for apg in alt_pages:
                    try:
                        await apg.close()
                    except Exception:
                        pass
                for actx in alt_ctxs:
                    try:
                        await actx.close()
                    except Exception:
                        pass

            # 主动轮换: session 到达页数上限
            if session_pages >= max_session_pages and current_pn <= total_pages:
                logger.info("Session 已请求 %d 页, 主动轮换 (下一页: %d/%d)",
                            session_pages, current_pn, total_pages)
                max_session_pages = _session_page_limit(total_pages - current_pn + 1)

    if page_errors:
        logger.warning("翻页失败数: %d uid=%s", page_errors, uid)

    if total_count and (existing_count + len(videos)) < total_count:
        errors.append(f"视频不全: {existing_count + len(videos)}/{total_count}")

    return {"videos": videos, "total_count": total_count, "errors": errors,
            "status": _pick_status(errors, len(videos) > 0)}


# ============================================================
# 工具函数
# ============================================================

async def _fetch_arc_page(page: Page, uid: str, pn: int, mixin_key: str) -> tuple[Optional[dict], bool]:
    """调 arc/search API, 返回 (data, hit_ratelimit).
    hit_ratelimit=True 表示遇到 -352/-412 需要切换 session"""
    params = sign_params({
        "mid": uid, "ps": "50", "pn": str(pn),
        "tid": "0", "keyword": "", "order": "pubdate",
    }, mixin_key)
    api_url = f"https://api.bilibili.com/x/space/wbi/arc/search?{urlencode(params)}"
    referer = f"https://space.bilibili.com/{uid}/upload/video"

    async def _do_fetch():
        await page.set_extra_http_headers({
            "Referer": referer,
            "Origin": "https://space.bilibili.com",
        })
        resp = await page.goto(api_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        if resp and resp.ok:
            text = await page.evaluate("() => document.body.innerText")
            data = json.loads(text)
            return data
        return None

    try:
        data = await _do_fetch()
        if not data:
            logger.warning("arc/search pn=%d HTTP请求失败", pn)
            return None, False

        code = data.get("code")
        if code == 0 and isinstance(data.get("data"), dict):
            return data["data"], False

        logger.warning("arc/search pn=%d code=%d msg=%s",
                       pn, code, data.get("message", ""))
        # 登录过期 → 删除当前 Cookie, 下次换下一个
        if code in (-101, 3, -6):
            logger.error("Cookie 已过期! code=%d pn=%d, 自动删除", code, pn)
            cookie_manager.mark_current_invalid()
            if cookie_manager.count == 0:
                logger.error("所有 Cookie 已用尽! 后续请求将无登录态")
        if code in (-352, -412):
            logger.error("风控! pn=%d code=%d uid=%s, 等待30-60s后重试", pn, code, uid)
            await asyncio.sleep(random.uniform(30, 60))
            data = await _do_fetch()
            if data and data.get("code") == 0 and isinstance(data.get("data"), dict):
                logger.info("风控重试成功 pn=%d", pn)
                return data["data"], False
            logger.error("风控重试仍失败 pn=%d, 需切换session", pn)
            return None, True
        return None, False
    except Exception as exc:
        logger.warning("fetch arc/search pn=%d 失败: %s", pn, exc)
        return None, False


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
