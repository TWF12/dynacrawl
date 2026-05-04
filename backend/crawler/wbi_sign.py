import hashlib
import time
import json
import logging
from urllib.parse import urlencode
from playwright.async_api import Page

logger = logging.getLogger(__name__)

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 52, 34, 44,
]

_cached_mixin_key: str = ""
_cached_at: float = 0
_cache_ttl: float = 600  # B站 WBI 密钥约 10-20 分钟轮换, 10 分钟刷新保活

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


def _build_mixin_key(img_url: str, sub_url: str) -> str:
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    mixin = img_key + sub_key
    key_chars = []
    for pos in MIXIN_KEY_ENC_TAB:
        if pos < len(mixin):
            key_chars.append(mixin[pos])
    return "".join(key_chars[:32])


async def _fetch_mixin_key(page: Page) -> str:
    global _cached_mixin_key, _cached_at
    now = time.time()
    if _cached_mixin_key and (now - _cached_at) < _cache_ttl:
        return _cached_mixin_key

    try:
        response = await page.goto(NAV_URL, timeout=15000, wait_until="domcontentloaded")
        if response and response.ok:
            body_text = await page.evaluate("() => document.body.innerText")
            data = json.loads(body_text)
            wbi_img = data.get("data", {}).get("wbi_img", {})
            img_url = wbi_img.get("img_url", "")
            sub_url = wbi_img.get("sub_url", "")
            if img_url and sub_url:
                _cached_mixin_key = _build_mixin_key(img_url, sub_url)
                _cached_at = now
                return _cached_mixin_key
    except Exception as e:
        logger.warning(f"获取 WBI mixin_key 失败: {e}")

    return _cached_mixin_key


def sign_params(params: dict, mixin_key: str) -> dict:
    signed = dict(sorted(params.items()))
    signed["wts"] = int(time.time())
    query_string = urlencode(signed)
    w_rid = hashlib.md5((query_string + mixin_key).encode()).hexdigest()
    signed["w_rid"] = w_rid
    return signed


async def get_mixin_key(page: Page) -> str:
    return await _fetch_mixin_key(page)
