import random
import os
import json
import logging
import asyncio
from urllib.parse import urlparse
from playwright.async_api import BrowserContext, Page
from playwright_stealth import Stealth
from backend.config import REQUEST_DELAY_MIN, REQUEST_DELAY_MAX, PROXY_LIST

logger = logging.getLogger(__name__)

from browserforge.headers import HeaderGenerator
_header_gen = HeaderGenerator(browser='chrome', os='windows', device='desktop')

# 动态生成版本匹配的 headers (UA + Sec-CH-UA 版本号一致)
def _make_headers() -> dict:
    h = _header_gen.generate()
    return {
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": h.get("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"),
        "Sec-Ch-UA": h.get("sec-ch-ua", '"Chromium";v="140", "Not=A?Brand";v="24"'),
        "Sec-Ch-UA-Platform": '"Windows"',
        "User-Agent": h.get("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    }

# 浏览器伪装 HTTP 头 — UA 与 Sec-CH-UA 版本号一致
EXTRA_HTTP_HEADERS = _make_headers()

# Clash 代理自动轮换
CLASH_CONTROLLER = os.environ.get("CLASH_CONTROLLER", "http://127.0.0.1:9090")
CLASH_PROXY = os.environ.get("CLASH_PROXY", "http://127.0.0.1:7890")
CLASH_GROUP = os.environ.get("CLASH_GROUP", "")
_last_clash_node = None
_clash_lock = asyncio.Lock()

# 全局统一轮换: 所有任务共享 IP, 按总页数阈值触发
_total_proxy_pages = 0
_PROXY_ROTATE_THRESHOLD = 10  # 每 10 页换一次 IP


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
            candidates = [n for n in all_nodes if n != _last_clash_node and n not in ("REJECT", "DIRECT")]
            if not candidates:
                candidates = all_nodes
            chosen = random.choice(candidates)
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


_stealth = Stealth()

async def apply_stealth(context: BrowserContext):
    """playwright-stealth: 自动对新 page 注入隐身 (覆盖 webdriver/webgl/canvas 等)"""
    context.on('page', lambda page: asyncio.create_task(_stealth.apply_stealth_async(page)))


async def setup_page(page: Page):
    w = random.randint(1400, 1920)
    h = random.randint(800, 1080)
    await page.set_viewport_size({"width": w, "height": h})
    await page.set_extra_http_headers(EXTRA_HTTP_HEADERS)


# 任务取消标记 (供 dispatcher 设置, scraper 检查, 避免循环导入)
_cancelled_tasks: set[str] = set()


def mark_task_cancelled(task_id: str):
    _cancelled_tasks.add(task_id)


def is_task_cancelled(task_id: str) -> bool:
    return task_id in _cancelled_tasks


async def random_delay():
    delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
    await asyncio.sleep(delay)
