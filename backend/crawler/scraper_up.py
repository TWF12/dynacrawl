import asyncio
import json
import logging
from typing import Optional
from playwright.async_api import Page
from backend.config import PAGE_TIMEOUT
from backend.crawler.anti_detect import random_delay

logger = logging.getLogger(__name__)


async def scrape_up_info(page: Page, uid: str) -> Optional[dict]:
    """爬取 UP 主基本信息（API 优先，页面降级）"""
    result = {"uid": uid}
    await random_delay()
    try:
        api_url = f"https://api.bilibili.com/x/space/wbi/acc/info?mid={uid}"
        response = await page.goto(api_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        if response and response.ok:
            body_text = await page.evaluate("() => document.body.innerText")
            data = json.loads(body_text)
            if data.get("code") == 0 and data.get("data"):
                card = data["data"]
                result["nickname"] = card.get("name", "")
                result["avatar_url"] = card.get("face", "")
                result["follower_count"] = card.get("follower", 0)
                result["video_count"] = card.get("video_count", 0)
                result["raw_data"] = card
                return result

        logger.warning("API获取UP信息失败，尝试从页面提取")
        space_url = f"https://space.bilibili.com/{uid}"
        response = await page.goto(space_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        if response and response.ok:
            data = await page.evaluate("""
                function() {
                    var r = {};
                    if (document.title) {
                        var m = document.title.match(/^(.+?)的个人空间/);
                        if (m) r.name = m[1].trim();
                    }
                    var stats = document.querySelector('.nav-statistics, [class*="statistics"]');
                    if (stats) {
                        var lines = (stats.innerText||'').split(/\\r?\\n/);
                        for (var i = 0; i < lines.length - 1; i++) {
                            var origTxt = lines[i+1] || '';
                            var val = parseFloat(origTxt.replace(/[^0-9.]/g,'')) || 0;
                            if (lines[i].indexOf('粉丝')>=0)
                                r.follower = origTxt.indexOf('万')>=0 ? Math.round(val*10000) : val;
                        }
                    }
                    var nav = document.querySelector('.n-tab-links, [class*="tab-links"]');
                    if (nav) {
                        var nt = nav.innerText || '';
                        var nm = nt.match(/投稿\s*([\d.]+万?\+?)/);
                        if (nm) {
                            var ns = nm[1];
                            if (ns === '999+') r.videos = 999;
                            else if (ns.indexOf('万')>=0) r.videos = Math.round(parseFloat(ns)*10000)||0;
                            else r.videos = parseInt(ns)||0;
                        }
                    }
                    var av = document.querySelector('.h-avatar img, [class*="avatar"] img');
                    if (av) r.face = av.src || '';
                    return r;
                }
            """)
            result["nickname"] = data.get("name", "")
            result["avatar_url"] = data.get("face", "")
            result["follower_count"] = data.get("follower", 0)
            result["video_count"] = data.get("videos", 0)
    except Exception as e:
        logger.error(f"爬取UP信息失败 uid={uid}: {e}")
    return result


async def scrape_up_videos(page: Page, uid: str, max_pages: int = 3) -> list[dict]:
    """爬取 UP 主的视频列表（点击投稿tab方式）"""
    videos = []
    await random_delay()
    try:
        url = f"https://space.bilibili.com/{uid}"
        resp = await page.goto(url, timeout=PAGE_TIMEOUT, wait_until="networkidle")
        if not resp or not resp.ok:
            return videos
        await page.wait_for_timeout(1500)

        # 点击投稿 tab
        clicked = await page.evaluate("""() => {
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {
                if ((all[i].textContent||'').trim() === '投稿') { all[i].click(); return true; }
            }
            return false;
        }""")
        if not clicked:
            return videos
        await asyncio.sleep(3)

        for pn in range(1, max_pages + 1):
            if pn > 1:
                await random_delay()
                clicked_next = await page.evaluate("""() => {
                    var btns = document.querySelectorAll('.be-pager-next, .next, [class*="pager-next"], button');
                    for (var i = 0; i < btns.length; i++) {
                        if ((btns[i].textContent||'').trim()==='下一页') { btns[i].click(); return true; }
                    }
                    return false;
                }""")
                if not clicked_next: break
                await asyncio.sleep(2)

            await page.wait_for_timeout(1000)

            page_videos = await page.evaluate("""
                function() {
                    var seen={}, result=[];
                    document.querySelectorAll('a[href*="BV"]').forEach(function(a){
                        var m = (a.getAttribute('href')||'').match(/(BV[a-zA-Z0-9]+)/);
                        if (!m || seen[m[1]]) return;
                        seen[m[1]] = true;
                        var bvid = m[1], title = '', play = 0;
                        var card = a.closest('.bili-video-card, li, .small-item, [class*="video-card"]');
                        if (!card) { var el = a; for (var up=0;up<3;up++) { if (el.parentElement) el = el.parentElement; } card = el; }

                        var titleEl = card.querySelector('.bili-video-card__title, .title, [class*="title"]');
                        if (titleEl) title = (titleEl.getAttribute('title')||titleEl.textContent||'').trim();
                        if (!title) {
                            var t2 = (card?card.innerText:a.innerText)||'';
                            var lines = t2.split(/[\\n\\r]+/).map(function(s){return s.trim()}).filter(function(s){return s.length>0});
                            for (var i = lines.length-1; i >= 0; i--) {
                                var s = lines[i];
                                if (s.length < 3) continue;
                                if (/^\\d{1,2}:\\d{2}$/.test(s)) continue;
                                if (/^[\\d.]+万?$/.test(s)) continue;
                                if (/^\\d{1,4}$/.test(s)) continue;
                                if (/^\\d{4}-\\d{2}-\\d{2}$/.test(s)) continue;
                                title = s; break;
                            }
                        }

                        var statSpans = card.querySelectorAll('.bili-cover-card__stat span');
                        if (statSpans.length >= 1) {
                            var t = (statSpans[0].textContent||'').trim();
                            var n = parseFloat(t.replace('万',''))||0;
                            play = t.indexOf('万')>=0 ? Math.round(n*10000) : n;
                        }

                        result.push({bvid: bvid, title: title, play: play});
                    });
                    return result;
                }
            """)
            videos.extend(page_videos)
            if len(page_videos) < 5: break
    except Exception as e:
        logger.error(f"爬取视频列表失败 uid={uid}: {e}")
    return videos
