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
_clash_groups_cache = None


def _safe_log(msg: str, *args):
    """Windows GBK 兼容的日志输出"""
    try:
        logger.info(msg, *args)
    except UnicodeEncodeError:
        logger.info(msg.encode("ascii", errors="replace").decode(), *args)


def _auto_detect_group() -> str | None:
    """未指定 CLASH_GROUP 时自动检测第一个可用的选择组"""
    global _clash_groups_cache
    SKIP_GROUPS = {"DIRECT", "REJECT", "GLOBAL"}
    try:
        import urllib.request
        req = urllib.request.Request(f"{CLASH_CONTROLLER}/proxies")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        proxies = data.get("proxies", {})
        for name, info in proxies.items():
            if info.get("type") in ("Selector", "URLTest", "Fallback"):
                if name not in SKIP_GROUPS and "广告" not in name and "漏网" not in name:
                    _safe_log("Clash 检测到代理组: %s", name)
                    return name
    except Exception:
        pass
    return None


def _rotate_clash_proxy() -> str | None:
    """通过 Clash API 切换代理组节点，返回新节点名"""
    global _last_clash_node
    try:
        group = CLASH_GROUP or _auto_detect_group()
        if not group:
            return None

        import urllib.request, urllib.parse
        encoded_group = urllib.parse.quote(group, safe="")
        url = f"{CLASH_CONTROLLER}/proxies/{encoded_group}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        all_nodes = data.get("all", [])
        if len(all_nodes) <= 1:
            return None
        candidates = [n for n in all_nodes if n != _last_clash_node]
        if not candidates:
            candidates = all_nodes
        chosen = random.choice(candidates)
        switch_req = urllib.request.Request(
            url, data=json.dumps({"name": chosen}).encode(),
            headers={"Content-Type": "application/json"}, method="PUT")
        urllib.request.urlopen(switch_req, timeout=5)
        _last_clash_node = chosen

        # 查出口 IP
        exit_ip = ""
        try:
            proxy_handler = urllib.request.ProxyHandler({"https": CLASH_PROXY})
            opener = urllib.request.build_opener(proxy_handler)
            with opener.open(urllib.request.Request("https://api.ip.sb/ip"), timeout=5) as r:
                exit_ip = r.read().decode().strip()
        except Exception:
            exit_ip = "?"
        _safe_log("Clash 切换: %s -> %s  IP:%s", _last_clash_node or "初始", chosen, exit_ip)
        return chosen
    except Exception as e:
        logger.debug("Clash 切换失败: %s", e)
    return None


def get_random_ua() -> str:
    return random.choice(USER_AGENTS)


def get_random_proxy() -> dict | None:
    if not PROXY_LIST:
        return None
    proxy_url = random.choice(PROXY_LIST).strip()
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"}
    if parsed.username:
        proxy["username"] = parsed.username
        proxy["password"] = parsed.password or ""
    return proxy


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


async def rotate_proxy_if_needed():
    """在创建新 context 前调用，自动轮换 Clash 节点"""
    _rotate_clash_proxy()


async def random_delay():
    delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
    await asyncio.sleep(delay)
