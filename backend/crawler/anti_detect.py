import random
import os
import json
import logging
import asyncio
from urllib.parse import urlparse
from playwright.async_api import BrowserContext, Page
from pw_stealth_enhanced import apply_stealth as _stealth_apply, StealthConfig
from backend.config import REQUEST_DELAY_MIN, REQUEST_DELAY_MAX, PROXY_LIST

logger = logging.getLogger(__name__)

from browserforge.headers import HeaderGenerator
_header_gen = HeaderGenerator(browser='chrome', os='windows', device='desktop')

# 浏览器伪装指纹 — 每次调用生成一致的 UA + Headers (版本匹配)
def make_browser_fingerprint() -> dict:
    """返回 {'user_agent': str, 'extra_headers': dict}, UA 与 Sec-CH-UA 版本一致"""
    h = _header_gen.generate()
    ua = h.get("User-Agent", "")
    return {
        "user_agent": ua,
        "extra_headers": {
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": h.get("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"),
            "Sec-Ch-UA": h.get("sec-ch-ua", ""),
            "Sec-Ch-UA-Platform": '"Windows"',
        },
    }

# 保留向后兼容: 模块加载时生成一组默认指纹
_DEFAULT_FP = make_browser_fingerprint()
get_random_ua = lambda: make_browser_fingerprint()["user_agent"]
EXTRA_HTTP_HEADERS = _DEFAULT_FP["extra_headers"]

# Clash 代理自动轮换
CLASH_CONTROLLER = os.environ.get("CLASH_CONTROLLER", "http://127.0.0.1:9090")
CLASH_PROXY = os.environ.get("CLASH_PROXY", "http://127.0.0.1:7890")
CLASH_GROUP = os.environ.get("CLASH_GROUP", "")
_last_clash_node = None
_clash_lock = asyncio.Lock()

# 全局统一轮换: 所有任务共享 IP, 按总页数阈值触发
_total_proxy_pages = 0
_PROXY_ROTATE_THRESHOLD = 10  # 每 10 页换一次 IP

# 代理节点质量评分: 成功+1, 失败-2, 低于-3 则跳过
_proxy_scores: dict[str, int] = {}
_PROXY_MIN_SCORE = -3


async def report_page_and_rotate() -> int:
    """每成功爬完一页调用, 累计达到阈值时自动轮换代理。返回总页数"""
    global _total_proxy_pages
    need_rotate = False
    async with _clash_lock:
        _total_proxy_pages += 1
        if _total_proxy_pages >= _PROXY_ROTATE_THRESHOLD:
            need_rotate = True
    if need_rotate:
        await _rotate_clash_proxy()  # _rotate_clash_proxy 内部持有 _clash_lock, 不能嵌套
        async with _clash_lock:
            _total_proxy_pages = 0
    return _total_proxy_pages


def _safe_log(msg: str, *args):
    """Windows GBK 兼容的日志输出"""
    try:
        logger.info(msg, *args)
    except UnicodeEncodeError:
        logger.info(msg.encode("ascii", errors="replace").decode(), *args)


def _clash_get(path: str) -> dict:
    import urllib.request
    req = urllib.request.Request(f"{CLASH_CONTROLLER}{path}")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _clash_put(path: str, body: dict) -> None:
    import urllib.request
    req = urllib.request.Request(
        f"{CLASH_CONTROLLER}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT")
    with urllib.request.urlopen(req, timeout=5):
        pass


def _clash_exit_ip() -> str:
    import urllib.request
    proxy_handler = urllib.request.ProxyHandler({"https": CLASH_PROXY})
    opener = urllib.request.build_opener(proxy_handler)
    ip_req = urllib.request.Request("https://api.ip.sb/ip", headers={"User-Agent": "curl/8.0"})
    with opener.open(ip_req, timeout=5) as r:
        return r.read().decode().strip()


async def _auto_detect_group() -> str | None:
    """未指定 CLASH_GROUP 时: 优先 Selector 类型, 其次 URLTest (URLTest 会被自动测速覆盖)"""
    SKIP_GROUPS = {"DIRECT", "REJECT"}
    try:
        data = await asyncio.to_thread(_clash_get, "/proxies")
        proxies = data.get("proxies", {})
        # 优先 Selector 类型 (手动切换不会被覆盖)
        for name, info in proxies.items():
            if info.get("type") == "Selector":
                if name not in SKIP_GROUPS and "广告" not in name and "漏网" not in name:
                    _safe_log("Clash 代理组(Selector): %s", name)
                    return name
        # 降级到 URLTest
        for name, info in proxies.items():
            if info.get("type") in ("URLTest", "Fallback"):
                if name not in SKIP_GROUPS and "广告" not in name and "漏网" not in name:
                    _safe_log("Clash 代理组(URLTest): %s", name)
                    return name
    except Exception:
        pass
    return None


async def _rotate_clash_proxy() -> str | None:
    """通过 Clash API 切换代理组节点，返回新节点名。首次调用不切换，先记录当前节点。"""
    global _last_clash_node
    async with _clash_lock:
        try:
            group = CLASH_GROUP or await _auto_detect_group()
            if not group:
                return None

            import urllib.parse
            encoded_group = urllib.parse.quote(group, safe="")
            path = f"/proxies/{encoded_group}"
            data = await asyncio.to_thread(_clash_get, path)
            all_nodes = data.get("all", [])
            now = data.get("now", "")

            # 首次调用: 记录当前节点。如果是 REJECT/DIRECT 则立即换掉
            if _last_clash_node is None:
                if now in ("REJECT", "DIRECT") and len(all_nodes) > 2:
                    candidates = [n for n in all_nodes if n not in ("REJECT", "DIRECT")]
                    chosen = random.choice(candidates)
                    await asyncio.to_thread(_clash_put, path, {"name": chosen})
                    _last_clash_node = chosen
                    _safe_log("Clash 初始节点坏(%s), 自动切换: %s", now, chosen)
                    return chosen
                _last_clash_node = now
                try:
                    exit_ip = await asyncio.to_thread(_clash_exit_ip)
                except Exception:
                    exit_ip = "?"
                _safe_log("Clash 初始节点: %s  IP:%s", now, exit_ip)
                return now

            if len(all_nodes) <= 1:
                return None
            # 基础候选: 排除当前节点和 REJECT/DIRECT
            base = [n for n in all_nodes if n != _last_clash_node and n not in ("REJECT", "DIRECT")]
            if not base:
                base = all_nodes

            # 代理质量评分: 优先选高分节点, 低分节点跳过
            eligible = [n for n in base if _proxy_scores.get(n, 0) >= _PROXY_MIN_SCORE]
            if not eligible:
                eligible = base  # 全部低分时仍选一个
                _safe_log("Clash 所有节点低分, 强制选择")

            # 按分数加权随机选择 (高分更容易被选)
            if len(eligible) >= 2 and any(_proxy_scores.get(n, 0) > 0 for n in eligible):
                # 加权选择: 分数越高概率越大
                weights = [max(1, _proxy_scores.get(n, 0) + 5) for n in eligible]
                chosen = random.choices(eligible, weights=weights, k=1)[0]
            else:
                chosen = random.choice(eligible)

            old_node = _last_clash_node
            await asyncio.to_thread(_clash_put, path, {"name": chosen})
            _last_clash_node = chosen

            exit_ip = "?"
            try:
                exit_ip = await asyncio.to_thread(_clash_exit_ip)
            except Exception:
                pass
            _safe_log("Clash 切换: %s -> %s  IP:%s", old_node, chosen, exit_ip)
            return chosen
        except Exception as e:
            logger.debug("Clash 切换失败: %s", e)
        return None


def get_random_ua() -> str:
    """browserforge: UA 与 Sec-CH-UA 版本号一致, 无手动维护"""
    return _header_gen.generate().get("User-Agent", "")



# 代理选择与轮换 — 支持 Clash API 和普通代理列表两种模式
_current_proxy_index: int = -1


def get_random_proxy() -> dict | None:
    """返回当前轮换到的代理, 无 PROXY_LIST 则返回 None"""
    global _current_proxy_index
    if not PROXY_LIST:
        return None
    # 如果有上次轮换的索引就用它, 否则随机选一个
    if _current_proxy_index < 0 or _current_proxy_index >= len(PROXY_LIST):
        _current_proxy_index = random.randrange(len(PROXY_LIST))
    return _parse_proxy_url(PROXY_LIST[_current_proxy_index])


def _parse_proxy_url(raw: str) -> dict | None:
    proxy_url = raw.strip()
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"}
    if parsed.username:
        proxy["username"] = parsed.username
        proxy["password"] = parsed.password or ""
    return proxy


async def rotate_proxy_if_needed() -> str | None:
    """轮换出口 IP: 优先 Clash API 切换节点, 否则轮换 PROXY_LIST, 返回描述"""
    # 方式 1: Clash API 轮换
    clash_node = await _rotate_clash_proxy()
    if clash_node:
        return clash_node

    # 方式 2: PROXY_LIST 多代理轮换 (直接在代理地址间切换)
    global _current_proxy_index
    if len(PROXY_LIST) > 1:
        available = [i for i in range(len(PROXY_LIST)) if i != _current_proxy_index]
        chosen = random.choice(available)
        _current_proxy_index = chosen
        label = PROXY_LIST[chosen].split("://")[-1].split("@")[-1].split("?")[0]
        logger.info("代理切换: %s", label)
        return label

    return None


_stealth_config = StealthConfig(
    locale="zh-CN",
    timezone_id="Asia/Shanghai",
    accept_language="zh-CN,zh;q=0.9,en;q=0.8",
)

async def apply_stealth(context: BrowserContext):
    """pw-stealth-enhanced: 注入 Canvas/WebGL/AudioContext/字体等 30+ 检测点隐身"""
    await _stealth_apply(context, config=_stealth_config)


async def setup_page(page: Page):
    w = random.randint(1400, 1920)
    h = random.randint(800, 1080)
    await page.set_viewport_size({"width": w, "height": h})


def report_proxy_success():
    """API 调用成功时报告, 给当前节点+1分"""
    global _last_clash_node
    if _last_clash_node:
        _proxy_scores[_last_clash_node] = _proxy_scores.get(_last_clash_node, 0) + 1


def report_proxy_failure():
    """API 调用失败(超时/风控)时报告, 当前节点-2分"""
    global _last_clash_node
    if _last_clash_node:
        _proxy_scores[_last_clash_node] = _proxy_scores.get(_last_clash_node, 0) - 2


# 任务取消标记 (供 dispatcher 设置, scraper 检查, 避免循环导入)
_cancelled_tasks: set[str] = set()


def mark_task_cancelled(task_id: str):
    _cancelled_tasks.add(task_id)


def is_task_cancelled(task_id: str) -> bool:
    return task_id in _cancelled_tasks


def clear_cancelled_task(task_id: str):
    """任务完成/删除时清理取消标记, 防止内存泄漏"""
    _cancelled_tasks.discard(task_id)


async def random_delay():
    delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
    await asyncio.sleep(delay)


# ============================================================
# 行为拟人化 — 鼠标轨迹/滚动/停留
# ============================================================

def _bezier_point(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
    """三次贝塞尔曲线"""
    return (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t ** 2 * p2 + t ** 3 * p3


async def human_mouse_move(page: Page, target_x: int, target_y: int, steps: int = None):
    """拟人鼠标移动: 三次贝塞尔曲线 + 随机偏移 + 微小抖动, 模拟真实人手轨迹"""
    from_x, from_y = random.randint(0, 400), random.randint(0, 400)
    cp1_x = from_x + random.randint(-100, 300)
    cp1_y = from_y + random.randint(-100, 200)
    cp2_x = target_x + random.randint(-200, 100)
    cp2_y = target_y + random.randint(-100, 150)

    if steps is None:
        dist = max(abs(target_x - from_x), abs(target_y - from_y))
        steps = max(20, min(60, int(dist / 8)))  # 20-60 步

    for i in range(steps + 1):
        t = i / steps
        x = _bezier_point(t, from_x, cp1_x, cp2_x, target_x) + random.uniform(-1.5, 1.5)
        y = _bezier_point(t, from_y, cp1_y, cp2_y, target_y) + random.uniform(-1.5, 1.5)
        # 中间步骤快, 起止慢 (模拟加速/减速)
        delay = 0.003 + 0.015 * abs(i - steps / 2) / (steps / 2)
        await page.mouse.move(x, y)
        await asyncio.sleep(delay + random.uniform(0, 0.008))


async def human_scroll(page: Page, distance: int = None):
    """拟人滚动: 分段滚 + 随机停顿 + 加速度衰减, 模仿人眼扫视习惯"""
    if distance is None:
        distance = random.randint(300, 1200)

    segments = random.randint(3, 8)
    remaining = distance
    for seg in range(segments):
        step = remaining / (segments - seg) * random.uniform(0.5, 1.2)
        step = max(20, min(step, remaining))
        await page.mouse.wheel(0, step)
        remaining -= step
        # 扫视停顿: 人眼在处理新内容时暂停
        if seg < segments - 1 and random.random() < 0.4:
            await page.wait_for_timeout(random.randint(500, 1500))
        await asyncio.sleep(random.uniform(0.02, 0.08))


async def human_dwell(page: Page, duration: float = None):
    """拟人停留: 微量鼠标移动 + 随机看不同位置, 模拟阅读行为"""
    if duration is None:
        duration = random.uniform(1, 4)

    vp = page.viewport_size or {"width": 1920, "height": 1080}
    elapsed = 0.0

    while elapsed < duration:
        # 小幅移动鼠标 (人不会完全静止)
        dx = random.randint(-50, 50)
        dy = random.randint(-30, 30)
        await page.mouse.move(
            random.randint(vp["width"] // 4, vp["width"] * 3 // 4) + dx,
            random.randint(100, vp["height"] - 200) + dy,
        )
        await page.wait_for_timeout(random.randint(200, 600))
        elapsed += 0.5

    # 偶尔点击空白区域 (20% 概率)
    if random.random() < 0.2:
        blank_x = random.randint(vp["width"] // 3, vp["width"] * 2 // 3)
        blank_y = random.randint(vp["height"] // 3, vp["height"] * 2 // 3)
        await page.mouse.click(blank_x, blank_y)
