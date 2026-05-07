import asyncio
import sys
import logging
from typing import Optional
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from backend.config import BROWSER_CONCURRENCY, BROWSER_HEADLESS
from backend.crawler.anti_detect import apply_stealth, setup_page, get_random_ua, get_random_proxy, rotate_proxy_if_needed
from backend.crawler.cookie_manager import cookie_manager

logger = logging.getLogger(__name__)


class BrowserPool:
    def __init__(self, concurrency: int = BROWSER_CONCURRENCY):
        self._concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._contexts: list[BrowserContext] = []
        self._lock = asyncio.Lock()
        self._headful_playwright = None
        self._headful_browser: Optional[Browser] = None
        self._headful_contexts: list[BrowserContext] = []

    async def start(self):
        async with self._lock:
            if self._browser is not None:
                return
            if sys.platform == "win32":
                try:
                    loop = asyncio.get_running_loop()
                    if type(loop).__name__ != "ProactorEventLoop":
                        raise RuntimeError("请使用 python run.py 启动")
                except RuntimeError as e:
                    raise e

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=BROWSER_HEADLESS,
                args=["--disable-blink-features=AutomationControlled",
                       "--disable-dev-shm-usage", "--no-sandbox", "--disable-setuid-sandbox"],
            )

    async def _ensure_headful_browser(self):
        """延迟初始化头有浏览器，用于需要绕过 B站 -352 检测的场景"""
        async with self._lock:
            if self._headful_browser is not None:
                return
            self._headful_playwright = await async_playwright().start()
            self._headful_browser = await self._headful_playwright.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled",
                       "--disable-dev-shm-usage", "--no-sandbox", "--disable-setuid-sandbox",
                       "--start-minimized", "--window-position=-32000,-32000"],
            )

    @asynccontextmanager
    async def acquire_headful_page(self):
        """获取头有浏览器页面，已配置隐身脚本和随机 UA，自动加载登录 cookie"""
        async with self._semaphore:
            await self._ensure_headful_browser()
            context = await self._new_headful_context()
            page = await context.new_page()
            await setup_page(page)
            try:
                yield page
            finally:
                await page.close()
                await context.close()

    @asynccontextmanager
    async def acquire_headful_context(self, use_proxy: bool = True):
        """获取头有浏览器 context（可建多个 page 用于并发），已加载登录 cookie"""
        async with self._semaphore:
            await self._ensure_headful_browser()
            context = await self._new_headful_context(use_proxy=use_proxy)
            self._headful_contexts.append(context)
            try:
                yield context
            finally:
                self._headful_contexts.remove(context)
                cookie_manager.unregister_context(context)
                await context.close()

    async def _new_headful_context(self, rotate: bool = True, use_proxy: bool = True) -> BrowserContext:
        """创建已配置隐身 + cookie 的头有 context, rotate=False 时不轮换IP, use_proxy=False 时直连"""
        if rotate and use_proxy:
            await rotate_proxy_if_needed()
        ua = get_random_ua()
        proxy = get_random_proxy() if use_proxy else None
        storage, filepath = await cookie_manager.get_next()
        context = await self._headful_browser.new_context(
            user_agent=ua,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            proxy=proxy,
            storage_state=storage,
        )
        cookie_manager.register_context(context, filepath)
        await apply_stealth(context)
        # context 级别浏览器伪装头, 所有新 page 自动继承
        await context.set_extra_http_headers({
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Ch-UA": '"Chromium";v="134", "Not=A?Brand";v="24"',
            "Sec-Ch-UA-Platform": '"Windows"',
        })
        return context

    async def stop(self):
        async with self._lock:
            # 清理头有浏览器资源
            if self._headful_browser:
                try:
                    await self._headful_browser.close()
                except Exception:
                    pass
                self._headful_browser = None
            if self._headful_playwright:
                try:
                    await self._headful_playwright.stop()
                except Exception:
                    pass
                self._headful_playwright = None

            for ctx in self._contexts:
                try: cookie_manager.unregister_context(ctx); await ctx.close()
                except Exception: pass
            self._contexts.clear()
            for ctx in self._headful_contexts:
                try: cookie_manager.unregister_context(ctx); await ctx.close()
                except Exception: pass
            self._headful_contexts.clear()
            if self._browser:
                await self._browser.close()
                self._browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None

    async def _create_context(self) -> BrowserContext:
        await rotate_proxy_if_needed()
        ua = get_random_ua()
        proxy = get_random_proxy()
        storage, filepath = await cookie_manager.get_next()
        context = await self._browser.new_context(
            user_agent=ua, viewport={"width": 1920, "height": 1080}, locale="zh-CN",
            proxy=proxy,
            storage_state=storage,
        )
        cookie_manager.register_context(context, filepath)
        await apply_stealth(context)
        await context.set_extra_http_headers({
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Ch-UA": '"Chromium";v="134", "Not=A?Brand";v="24"',
            "Sec-Ch-UA-Platform": '"Windows"',
        })
        self._contexts.append(context)
        return context

    async def get_context(self) -> BrowserContext:
        for ctx in self._contexts:
            if not ctx.is_closed() and len(ctx.pages) == 0:
                return ctx
        if len(self._contexts) < self._concurrency:
            return await self._create_context()
        for ctx in self._contexts:
            if not ctx.is_closed():
                return ctx
        return await self._create_context()

    @asynccontextmanager
    async def acquire_page(self):
        async with self._semaphore:
            context = await self.get_context()
            page = await context.new_page()
            try:
                await setup_page(page)
                yield page
            finally:
                await page.close()

    @property
    def concurrency(self) -> int:
        return self._concurrency


browser_pool = BrowserPool()
