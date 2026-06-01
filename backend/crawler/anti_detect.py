import random
import logging
import asyncio
from urllib.parse import urlparse
from playwright.async_api import BrowserContext, Page
from pw_stealth_enhanced import apply_stealth as _stealth_apply, StealthConfig
from backend.config import REQUEST_DELAY_MIN, REQUEST_DELAY_MAX, PROXY_LIST

logger = logging.getLogger(__name__)

from browserforge.headers import HeaderGenerator

_header_gen = HeaderGenerator(browser="chrome", os="windows", device="desktop")


# 浏览器伪装指纹 — 每次调用生成一致的 UA + Headers (版本匹配)
def make_browser_fingerprint() -> dict:
    """返回 {'user_agent': str, 'extra_headers': dict}, UA 与 Sec-CH-UA 版本一致"""
    h = _header_gen.generate()
    ua = h.get("User-Agent", "")
    return {
        "user_agent": ua,
        "extra_headers": {
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": h.get(
                "Accept",
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            ),
            "Sec-Ch-UA": h.get("sec-ch-ua", ""),
            "Sec-Ch-UA-Platform": '"Windows"',
        },
    }


# 保留向后兼容: 模块加载时生成一组默认指纹
_DEFAULT_FP = make_browser_fingerprint()
get_random_ua = lambda: make_browser_fingerprint()["user_agent"]
EXTRA_HTTP_HEADERS = _DEFAULT_FP["extra_headers"]

# 全局统一页面计数器 (供监控/日志使用)
_total_proxy_pages = 0


async def report_page_and_rotate() -> int:
    """每成功爬完一页调用, 返回总页数。无 Clash 后不再自动轮换 IP, 仅计数。"""
    global _total_proxy_pages
    _total_proxy_pages += 1
    return _total_proxy_pages


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
    """轮换 PROXY_LIST 中的代理地址, 返回描述。无 PROXY_LIST 则返回 None。"""
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
    return (
        (1 - t) ** 3 * p0
        + 3 * (1 - t) ** 2 * t * p1
        + 3 * (1 - t) * t**2 * p2
        + t**3 * p3
    )


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
