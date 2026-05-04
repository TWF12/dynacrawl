import random
import os
import json
import logging
import asyncio
from urllib.parse import urlparse
from playwright.async_api import BrowserContext, Page
from backend.config import REQUEST_DELAY_MIN, REQUEST_DELAY_MAX, PROXY_LIST

logger = logging.getLogger(__name__)

USER_AGENTS = [
    # Chrome 132-135 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    # Chrome 132-134 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    # Chrome 133 Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    # Edge 133
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0",
    # Firefox 136
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:136.0) Gecko/20100101 Firefox/136.0",
]

# 浏览器指纹隐身脚本
STEALTH_SCRIPT = """
// webdriver 检测
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
delete navigator.__proto__.webdriver;

// chrome runtime
window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {}
};

// permissions
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission, onchange: null }) :
        origQuery(parameters)
);

// plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [];
        arr.item = () => null; arr.namedItem = () => null; arr.refresh = () => {};
        arr[0] = { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 };
        arr[1] = { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 1 };
        arr[2] = { name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 2 };
        Object.defineProperty(arr, 'length', { get: () => 3 });
        return arr;
    }
});
Object.defineProperty(navigator, 'mimeTypes', {
    get: () => {
        const arr = [];
        arr.item = () => null; arr.namedItem = () => null;
        arr[0] = { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' };
        arr[1] = { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' };
        Object.defineProperty(arr, 'length', { get: () => 2 });
        return arr;
    }
});

// locale
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
Object.defineProperty(navigator, 'language', { get: () => 'zh-CN' });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

// hardware
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => [4,6,8,12,16][Math.floor(Math.random()*5)] });
Object.defineProperty(navigator, 'deviceMemory', { get: () => [4,8,8,16,16][Math.floor(Math.random()*5)] });
Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });

// canvas fingerprint noise
const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
    const ctx = this.getContext('2d');
    if (ctx) {
        const imgData = ctx.getImageData(0, 0, 1, 1);
        if (imgData && imgData.data) {
            imgData.data[0] = imgData.data[0] ^ 1;
            ctx.putImageData(imgData, 0, 0);
        }
    }
    return origToDataURL.apply(this, arguments);
};

// webgl fingerprint noise
const origGetParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return origGetParameter.call(this, p);
};
"""


# Clash 代理自动轮换
CLASH_CONTROLLER = os.environ.get("CLASH_CONTROLLER", "http://127.0.0.1:9090")
CLASH_PROXY = os.environ.get("CLASH_PROXY", "http://127.0.0.1:7890")
CLASH_GROUP = os.environ.get("CLASH_GROUP", "")
_last_clash_node = None
_clash_lock = asyncio.Lock()


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
    urllib.request.urlopen(req, timeout=5)


def _clash_exit_ip() -> str:
    import urllib.request
    proxy_handler = urllib.request.ProxyHandler({"https": CLASH_PROXY})
    opener = urllib.request.build_opener(proxy_handler)
    ip_req = urllib.request.Request("https://api.ip.sb/ip", headers={"User-Agent": "curl/8.0"})
    with opener.open(ip_req, timeout=5) as r:
        return r.read().decode().strip()


async def _auto_detect_group() -> str | None:
    """未指定 CLASH_GROUP 时自动检测第一个可用的选择组"""
    SKIP_GROUPS = {"DIRECT", "REJECT", "GLOBAL"}
    try:
        data = await asyncio.to_thread(_clash_get, "/proxies")
        proxies = data.get("proxies", {})
        for name, info in proxies.items():
            if info.get("type") in ("Selector", "URLTest", "Fallback"):
                if name not in SKIP_GROUPS and "广告" not in name and "漏网" not in name:
                    _safe_log("Clash 检测到代理组: %s", name)
                    return name
    except Exception:
        pass
    return None


async def _rotate_clash_proxy() -> str | None:
    """通过 Clash API 切换代理组节点，返回新节点名"""
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
            if len(all_nodes) <= 1:
                return None
            candidates = [n for n in all_nodes if n != _last_clash_node]
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
            _safe_log("Clash 切换: %s -> %s  IP:%s", old_node or "初始", chosen, exit_ip)
            return chosen
        except Exception as e:
            logger.debug("Clash 切换失败: %s", e)
        return None


def get_random_ua() -> str:
    return random.choice(USER_AGENTS)


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
    # 方式 1: Clash API 轮换 (切换代理组内的上游节点)
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


async def apply_stealth(context: BrowserContext):
    await context.add_init_script(STEALTH_SCRIPT)


async def setup_page(page: Page):
    w = random.randint(1400, 1920)
    h = random.randint(800, 1080)
    await page.set_viewport_size({"width": w, "height": h})
    await page.set_extra_http_headers({
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Sec-Ch-UA": '"Chromium";v="134", "Not=A?Brand";v="24"',
        "Sec-Ch-UA-Platform": '"Windows"',
    })


async def random_delay():
    delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
    await asyncio.sleep(delay)
