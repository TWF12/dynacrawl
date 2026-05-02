"""B站 Cookie 保存工具 —— 扫码登录后自动保存到 data/bilibili_cookies.json"""
import asyncio, json, os
from pathlib import Path
from playwright.async_api import async_playwright

COOKIE_FILE = str(Path(__file__).resolve().parent / "data" / "bilibili_cookies.json")

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
        page = await ctx.new_page()

        # 打开 B站 首页，等用户扫码登录
        await page.goto("https://www.bilibili.com/")
        print("请在浏览器中点击右上角「登录」并扫码...")
        print("登录成功后，回到终端按 Enter 继续...")
        input()

        # 保存登录态
        os.makedirs("data", exist_ok=True)
        storage = await ctx.storage_state()
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(storage, f, ensure_ascii=False, indent=2)

        print(f"Cookie 已保存到 {COOKIE_FILE}")

        # 验证登录
        await page.goto("https://api.bilibili.com/x/web-interface/nav")
        text = await page.evaluate("document.body.innerText")
        data = json.loads(text)
        if data.get("data", {}).get("isLogin"):
            uname = data["data"]["uname"]
            print(f"验证成功！已登录为: {uname}")
        else:
            print("警告：登录验证失败，请重试")

        await ctx.close()
        await browser.close()

asyncio.run(main())
