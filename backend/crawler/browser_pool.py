import asyncio
import sys
import logging
from typing import Optional
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from backend.config import BROWSER_CONCURRENCY, BROWSER_HEADLESS
from backend.crawler.anti_detect import apply_stealth, setup_page, get_random_ua, get_random_proxy

logger = logging.getLogger(__name__)


class BrowserPool:
    def __init__(self, concurrency: int = BROWSER_CONCURRENCY):
        self._concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._contexts: list[BrowserContext] = []
        self._lock = asyncio.Lock()

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

    async def stop(self):
        async with self._lock:
            for ctx in self._contexts:
                try: await ctx.close()
                except Exception: pass
            self._contexts.clear()
            if self._browser:
                await self._browser.close()
                self._browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None

    async def _create_context(self) -> BrowserContext:
        ua = get_random_ua()
        proxy = get_random_proxy()
        context = await self._browser.new_context(
            user_agent=ua, viewport={"width": 1920, "height": 1080}, locale="zh-CN",
            proxy=proxy,
        )
        await apply_stealth(context)
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
