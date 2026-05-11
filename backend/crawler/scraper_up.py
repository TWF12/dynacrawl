import json
import logging
import asyncio
import random
from typing import Optional, Callable, Awaitable
from urllib.parse import urlencode
from playwright.async_api import Page
from backend.config import PAGE_TIMEOUT
from backend.crawler.anti_detect import random_delay, is_task_cancelled, report_page_and_rotate, get_random_ua
from backend.crawler.browser_pool import browser_pool
from backend.crawler.cookie_manager import cookie_manager
from backend.crawler.wbi_sign import sign_params, get_mixin_key

logger = logging.getLogger(__name__)

# 进度回调: (current, total, message)
VideoProgressCallback = Callable[[int, int, str], Awaitable[None]]


async def _make_direct_page(from_page: Page = None):
    """创建无代理直连 headful page (DOM 兜底专用, 不走 pool semaphore)"""
    ctx = await browser_pool.create_direct_context()
    pg = await ctx.new_page()
    return ctx, pg


async def _dom_extract_up_info(page: Page, uid: str) -> dict:
    """从空间页 DOM 提取 UP 主昵称/头像/粉丝/视频数 (适配新版 B站 无 __INITIAL_STATE__)"""
    # 用 /upload/video 页面 — 登录墙下 navBar(粉丝) + section header(真实视频数) 均可见
    space_url = f"https://space.bilibili.com/{uid}/upload/video"
    resp = await page.goto(space_url, timeout=PAGE_TIMEOUT, wait_until="load")
    if not resp or not resp.ok:
        return {}
    await page.wait_for_timeout(5000)
    return await page.evaluate("""
        function() {
            var r = {};
            // 1. 昵称: 从页面标题提取 (多格式兜底, 含/不含"的")
            var title = document.title || '';
            var m = title.match(/^(.+?)(?:的个人空间|个人动态|的投稿视频|投稿视频|的专栏|的合集|的主页)/);
            if (!m) m = title.match(/^(.+?)(?:-|\\|)/);  // 兜底: 第一个分隔符前的内容
            if (!m) {
                var ogTitle = document.querySelector('meta[property=\"og:title\"]');
                if (ogTitle) {
                    var ot = ogTitle.getAttribute('content') || '';
                    m = ot.match(/^(.+?)(?:的个人空间|个人动态|的投稿视频|投稿视频)/);
                }
            }
            if (m) r.nickname = m[1];

            // 2. 头像: 从空间页头像区图片
            var avatarImg = document.querySelector('#h-avatar img, .h-avatar img, .bili-avatar img, [class*=avatar] img');
            if (!avatarImg) {
                var imgs = document.querySelectorAll('img');
                for (var k=0; k<imgs.length; k++) {
                    var src = imgs[k].getAttribute('src') || '';
                    if (src.indexOf('hdslb.com')>=0 && (src.indexOf('face')>=0 || src.indexOf('avatar')>=0)) {
                        avatarImg = imgs[k]; break;
                    }
                }
            }
            if (avatarImg) r.avatar_url = avatarImg.getAttribute('src') || avatarImg.getAttribute('data-src') || '';

            // 3. 粉丝数: 优先从导航栏精准提取, 降级到全页扫描
            var elems = document.querySelectorAll('span, div, a');
            var bestFans = 0;
            // 优先: 导航栏 "关注28粉丝2374.0万"
            var navBar = document.querySelector('.nav-bar.space-navbar, .nav-bar__main, .nav-bar, [class*=nav-bar]');
            if (navBar) {
                var fm0 = navBar.textContent.match(/粉丝[^\\d]*([\\d.]+)\\s*万?/);
                if (fm0) {
                    var n0 = parseFloat(fm0[1].replace(/[^\\d.]/g, ''));
                    if (navBar.textContent.indexOf('万')>=0) n0 = Math.round(n0 * 10000);
                    bestFans = Math.round(n0);
                }
            }
            // 降级: 全页扫描
            if (!bestFans) for (var i=0; i<elems.length; i++) {
                var txt = (elems[i].textContent || '').trim();
                if (txt.length > 50 || txt.length < 4) continue;
                // 格式: \"粉丝835.4万\" 或 \"粉丝 2374.0万\" (标签和数字可能在不同子元素)
                var fm = txt.match(/粉丝[^\\d]*([\\d.]+)\\s*万?/);
                if (fm) {
                    var n = parseFloat(fm[1].replace(/[^\\d.]/g, ''));
                    if (txt.indexOf('万')>=0 || fm[0].indexOf('万')>=0) n = Math.round(n * 10000);
                    if (n > bestFans) bestFans = Math.round(n);
                }
            }
            if (bestFans > 0) r.follower_count = bestFans;

            // 4. 视频数: 优先导航栏/section header, 降级全页扫描
            var bestVids = 0;
            // 优先: 导航栏 "投稿999+"
            if (navBar) {
                var vm0 = navBar.textContent.match(/投稿[^\\d]*([\\d]+)/);
                if (vm0) { var nv0 = parseInt(vm0[1]); if (nv0 > bestVids) bestVids = nv0; }
            }
            // 优先: section header "视频·9938"
            var secHdr = document.querySelector('.section-wrap__header');
            if (secHdr) {
                var vm1 = secHdr.textContent.match(/视频[^\\d]*([\\d]+)/);
                if (vm1) { var nv1 = parseInt(vm1[1]); if (nv1 > bestVids) bestVids = nv1; }
            }
            // 降级: 全页扫描
            if (!bestVids) for (var j=0; j<elems.length; j++) {
                var text = (elems[j].textContent || '').trim();
                if (text.length > 80 || text.length < 4) continue;
                // 匹配 \"视频\" + 任意分隔符 + 3-7位数字
                var vm = text.match(/视频\W{0,3}?(\d{3,7})/);
                // 匹配 \"投稿\" + 任意分隔符 + 3-7位数字
                if (!vm) vm = text.match(/投稿\W{0,3}?(\d{3,7})/);
                if (vm) {
                    var n2 = parseInt(vm[1].replace(/[^\\d]/g, ''));
                    if (!isNaN(n2) && n2 > bestVids && n2 < 9999999) bestVids = n2;
                }
            }
            if (bestVids > 0) r.video_count = bestVids;
            return r;
        }
    """)


async def scrape_up_info(page: Page, uid: str) -> Optional[dict]:
    """爬取 UP 主基本信息 + 多途径获取真实视频总数。API 故障时直连 DOM 兜底"""
    result = {"uid": uid, "video_count": 0}
    errors = []
    api_failed = False
    await random_delay()

    # Phase 1: card API (无需 WBI, 轻量)
    try:
        await page.goto("https://www.bilibili.com/", timeout=15000, wait_until="domcontentloaded")
        await random_delay()
        card_url = f"https://api.bilibili.com/x/web-interface/card?mid={uid}"
        response = await page.goto(card_url, timeout=15000, wait_until="domcontentloaded")
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
                    result["errors"] = errors
                    result["status"] = _pick_status(errors, True)
                    return result
            else:
                errors.append("card异常")
        else:
            errors.append("card失败")
    except Exception:
        errors.append("card超时")
        api_failed = True

    # Phase 2: arc/search (需要 WBI)
    if not result.get("video_count"):
        try:
            mixin_key = await get_mixin_key(page)
            if mixin_key:
                params = sign_params({
                    "mid": uid, "ps": "1", "pn": "1",
                    "tid": "0", "keyword": "", "order": "pubdate",
                }, mixin_key)
                api_url = f"https://api.bilibili.com/x/space/wbi/arc/search?{urlencode(params)}"
                resp = await page.goto(api_url, timeout=15000, wait_until="domcontentloaded")
                if resp and resp.ok:
                    body_text = await page.evaluate("() => document.body.innerText")
                    data = json.loads(body_text)
                    if data.get("code") == 0:
                        total = data.get("data", {}).get("page", {}).get("count", 0)
                        if total:
                            result["video_count"] = total
                            result["errors"] = errors
                            result["status"] = _pick_status(errors, True)
                            return result
                    else:
                        errors.append("arc/search异常")
                else:
                    errors.append("arc/search失败")
            else:
                errors.append("WBI密钥失败")
        except Exception:
            errors.append("arc/search超时")
            api_failed = True

    # Phase 3: DOM 提取 — API 故障时用 headful 直连, 否则用当前 page
    try:
        dom = None
        if api_failed:
            logger.info("API 故障 uid=%s, 直连 headful DOM 提取", uid)
            direct_ctx, direct_pg = await _make_direct_page()
            try:
                dom = await _dom_extract_up_info(direct_pg, uid)
            finally:
                await direct_pg.close()
                await direct_ctx.close()
        else:
            dom = await _dom_extract_up_info(page, uid)

        if dom:
            if dom.get("nickname"): result["nickname"] = dom["nickname"]
            if dom.get("avatar_url"): result["avatar_url"] = dom["avatar_url"]
            if dom.get("follower_count"): result["follower_count"] = dom["follower_count"]
            if dom.get("video_count") and not result.get("video_count"):
                result["video_count"] = dom["video_count"]
            if dom.get("nickname") or dom.get("video_count"):
                errors.append("DOM提取")
            else:
                errors.append("空间页无数据")
        else:
            errors.append("空间页无数据")
    except Exception as e:
        logger.warning("DOM 兜底失败 uid=%s: %s", uid, e)
        errors.append("DOM兜底失败")

    result["errors"] = errors
    result["status"] = _pick_status(errors, len(result) > 1)
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


async def _dom_fallback(uid: str, seen_bvids: set, page,
                       progress_callback=None, start_pn: int = 1,
                       max_pages: int = 0, task_id: str = "") -> tuple[list[dict], int]:
    """DOM 兜底: 视频列表页 + 空间主页 + 滚动加载, 返回 (videos, total_count)"""
    all_videos: list[dict] = []
    total_count = 0

    async def _try_page(url: str, wait_sec: float = 3):
        resp = await page.goto(url, timeout=PAGE_TIMEOUT, wait_until="load")
        if not resp or not resp.ok:
            return False
        await page.wait_for_timeout(int(wait_sec * 1000))
        return True

    async def _extract_and_report(pn: int):
        videos = await _dom_extract(page, uid, seen_bvids)
        if videos:
            all_videos.extend(videos)
            if progress_callback:
                await progress_callback(
                    pn, max(1, total_count or 1),
                    f"DOM 兜底: 已获取 {len(all_videos)}/{total_count or '?'} 条")
        return videos

    try:
        # 策略 1: 视频列表分页 (带 &pn=N)
        base_url = f"https://space.bilibili.com/{uid}/video?tid=0&keyword=&order=pubdate"
        first_url = f"{base_url}&pn={start_pn}"
        if await _try_page(first_url):
            total_count = await _get_video_count_from_page(page, uid)
            pn = start_pn
            dom_max = max_pages or 50
            while pn - start_pn < dom_max:
                if task_id and is_task_cancelled(task_id):
                    break
                videos = await _extract_and_report(pn - start_pn + 1)
                if not videos:
                    break
                pn += 1
                if not await _try_page(f"{base_url}&pn={pn}", random.uniform(1.5, 3)):
                    break

        # 策略 2: 分页没拿到 → 投稿页 + 空间主页 + 滚动 (重试一次)
        if not all_videos:
            fallback_urls = [
                f"https://space.bilibili.com/{uid}/upload/video",  # 投稿页: 登录墙下仍渲染视频列表
                f"https://space.bilibili.com/{uid}",               # 空间主页兜底
            ]
            for attempt in range(2):
                url = fallback_urls[attempt % len(fallback_urls)]
                logger.info("DOM 分页无数据 uid=%s, 切 %s (attempt %d)", uid, url.split('/')[-1] or 'home', attempt + 1)
                ok = await _try_page(url, 4)
                if not ok:
                    logger.warning("页面加载失败 uid=%s url=%s attempt=%d", uid, url, attempt + 1)
                    await asyncio.sleep(2)
                    continue
                total_count = await _get_video_count_from_page(page, uid) or total_count
                logger.info("页面加载成功 uid=%s total=%d, 开始滚动提取", uid, total_count)
                await _extract_and_report(1)
                for scroll_i in range(min(max_pages or 30, 50)):
                    before = len(all_videos)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(random.randint(2000, 4000))
                    await _extract_and_report(scroll_i + 2)
                    if len(all_videos) == before:
                        logger.info("滚动无新内容 uid=%s scroll=%d total=%d", uid, scroll_i, len(all_videos))
                        break
                if all_videos:
                    break
                logger.warning("页面未提取到视频 uid=%s attempt=%d, 重试", uid, attempt + 1)
                await asyncio.sleep(2)

    except Exception as e:
        logger.warning("DOM 兜底异常 uid=%s: %s", uid, e)

    if not all_videos:
        logger.warning("DOM 兜底最终无数据 uid=%s", uid)
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
        return random.uniform(1, 3)
    elif ratio <= 0.4:
        return random.uniform(2, 5)
    elif ratio <= 0.7:
        return random.uniform(3, 8)
    else:
        return random.uniform(5, 12)


async def _init_session(ctx, uid: str) -> str | None:
    """为新 session 获取 mixin_key。异常(代理超时等)返回 None, 由调用方走 DOM 兜底"""
    pg = await ctx.new_page()
    try:
        resp = await pg.goto("https://www.bilibili.com/", timeout=15000, wait_until="domcontentloaded")
        if not resp or not resp.ok:
            return None
        await pg.wait_for_timeout(500)
        return await get_mixin_key(pg)
    except Exception:
        return None
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
    # Phase 1: 第 1 页 — 获取 total_count 和首页视频 (整体 try-except, 异常时直连 DOM)
    # ================================================================
    phase1_ok = False
    try:
        async with browser_pool.acquire_headful_context(rotate=False) as ctx:
            mixin_key = await _init_session(ctx, uid)
            if not mixin_key:
                logger.warning("WBI密钥失败 uid=%s, 直连 headful DOM 兜底", uid)
                errors.append("WBI密钥失败")
                raise Exception("WBI密钥失败")

            pg1 = await ctx.new_page()
            try:
                page1_data, _ = await _fetch_arc_page(pg1, uid, 1, mixin_key)
            finally:
                await pg1.close()

            if page1_data is None:
                logger.warning("arc/search 无响应 uid=%s，直连 headful DOM 兜底", uid)
                errors.append("arc/search失败")
                raise Exception("arc/search失败")

            total_count = _process_arc_data(page1_data, videos, seen_bvids)
            ps = page1_data.get("page", {}).get("ps", 50)
            total_pages = (total_count + ps - 1) // ps if total_count else 0
            if max_pages and max_pages < total_pages:
                total_pages = max_pages

            logger.info("第 1 页获取 %d 条 uid=%s (已有 %d 共 %d 条 %d 页)",
                        len(videos), uid, existing_count, total_count, total_pages)
            await _save_page(videos[:])
            await report_page_and_rotate()
            if progress_callback and (existing_count == 0 or len(videos) > 0):
                await progress_callback(1, total_pages,
                                        f"第 1/{total_pages} 页, 已获取 {existing_count + len(videos)}/{total_count} 条")
            phase1_ok = True
    except Exception as e:
        logger.warning("Phase1 异常 uid=%s: %s, 直连 headful DOM 兜底", uid, str(e)[:80])

    if not phase1_ok:
        # API 路径全失败 → 直连 headful DOM 兜底
        try:
            direct_ctx = await browser_pool.create_direct_context()
            direct_pg = await direct_ctx.new_page()
            try:
                dom_videos, dom_total = await _dom_fallback(
                    uid, seen_bvids, direct_pg, progress_callback, task_id=task_id)
            finally:
                await direct_pg.close()
                await direct_ctx.close()
            for v in dom_videos:
                videos.append(v)
            total_count = dom_total or 0
            if len(videos) > 0:
                errors.append(f"DOM提取 {len(videos)}/{total_count} 条")
            else:
                errors.append("API+DOM均未提取到视频")
        except Exception as e2:
            logger.error("直连 DOM 兜底也失败 uid=%s: %s", uid, e2)
            errors.append("DOM兜底失败")
        return {"videos": videos, "total_count": total_count, "errors": errors,
                "status": _pick_status(errors, len(videos) > 0)}

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

        # Cookie 全部用尽 → 直连 DOM 兜底 (无需登录态, 不走代理)
        if cookie_manager.count == 0:
            logger.warning("所有 Cookie 已用尽, 剩余 %d 页用直连 DOM 兜底", total_pages - current_pn + 1)
            errors.append("Cookie已用尽")
            try:
                async with browser_pool.acquire_direct_context() as direct_ctx:
                    direct_pg = await direct_ctx.new_page()
                    try:
                        dom_videos, _ = await _dom_fallback(
                            uid, seen_bvids, direct_pg, progress_callback,
                            start_pn=current_pn, max_pages=total_pages - current_pn + 1,
                            task_id=task_id)
                    finally:
                        await direct_pg.close()
                for v in dom_videos:
                    videos.append(v)
                if dom_videos:
                    errors.append(f"DOM续爬 {len(dom_videos)} 条")
            except Exception as e:
                logger.error("直连 DOM 兜底异常: %s", e)
                errors.append("DOM兜底失败")
            break

        async with browser_pool.acquire_headful_context(rotate=False) as ctx:
            mixin_key = await _init_session(ctx, uid)
            if not mixin_key:
                session_init_failures += 1
                if session_init_failures >= 3:
                    logger.warning("WBI密钥连续失败, 剩余 %d 页用直连 headful DOM 兜底", total_pages - current_pn + 1)
                    errors.append("WBI密钥失败(重试3次)")
                    direct_ctx = await browser_pool.create_direct_context()
                    direct_pg = await direct_ctx.new_page()
                    try:
                        dom_videos, _ = await _dom_fallback(
                            uid, seen_bvids, direct_pg, progress_callback,
                            start_pn=current_pn, max_pages=total_pages - current_pn + 1,
                            task_id=task_id)
                    finally:
                        await direct_pg.close()
                        await direct_ctx.close()
                    for v in dom_videos:
                        videos.append(v)
                    if dom_videos:
                        errors.append(f"DOM续爬 {len(dom_videos)} 条")
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
                actx = None
                apg = None
                try:
                    actx = await browser_pool._new_headful_context(rotate=False)
                    apg = await actx.new_page()
                    # 新 context 首次导航较慢, 给 20s
                    await apg.goto("https://www.bilibili.com/", timeout=20000, wait_until="domcontentloaded")
                    alt_ctxs.append(actx)
                    alt_pages.append(apg)
                except Exception as e:
                    logger.warning("备用 context[%d/%d] 创建失败: %s", i + 2, cookie_manager.count,
                                   str(e)[:80])
                    # 必须清理已创建的资源, 否则浏览器进程泄漏
                    if apg:
                        try:
                            await apg.close()
                        except Exception:
                            pass
                    if actx:
                        try:
                            cookie_manager.unregister_context(actx)
                            await actx.close()
                        except Exception:
                            pass

            api_page = await ctx.new_page()
            all_pages = [api_page] + alt_pages  # 全部 cookie 对应的 page
            cookie_count = len(all_pages)
            # 延迟系数: cookie 越多越安全降延迟, 最低 0.5
            delay_factor = max(0.5, 1.0 / cookie_count + 0.15) if cookie_count >= 2 else 1.0

            try:
                await api_page.goto("https://www.bilibili.com/", timeout=20000, wait_until="domcontentloaded")

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
                        await report_page_and_rotate()  # 全局统一轮换
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
                            await report_page_and_rotate()  # 全局统一轮换
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
                        cookie_manager.unregister_context(actx)
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
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Ch-UA": '"Chromium";v="134", "Not=A?Brand";v="24"',
            "Sec-Ch-UA-Platform": '"Windows"',
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
            await cookie_manager.mark_invalid(page.context)
            if cookie_manager.count == 0:
                logger.error("所有 Cookie 已用尽! 后续请求将无登录态")
        if code in (-352, -412):
            logger.error("风控! pn=%d code=%d uid=%s, 等待5-10s后重试", pn, code, uid)
            await asyncio.sleep(random.uniform(5, 10))
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
    """从页面 DOM 提取视频总数 (适配新版 B站 无 __INITIAL_STATE__)"""
    try:
        count = await page.evaluate("""
            () => {
                var elems = document.querySelectorAll('span, div, a');
                var best = 0;
                for (var i=0; i<elems.length; i++) {
                    var t = (elems[i].textContent || '').trim();
                    if (t.length > 80 || t.length < 4) continue;
                    var m = t.match(/视频\\W{0,3}?(\\d{3,7})/);
                    if (!m) m = t.match(/投稿\\W{0,3}?(\\d{3,7})/);
                    if (m) {
                        var n = parseInt(m[1].replace(/[^\\d]/g, ''));
                        if (n > best && n < 9999999) best = n;
                    }
                }
                return best;
            }
        """)
        return count or 0
    except Exception:
        return 0
