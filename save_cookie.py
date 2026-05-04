"""B站 Cookie 保存工具 —— 扫码登录后保存到 data/cookies/ 目录（支持多账号轮换）"""
import asyncio, json, os
from pathlib import Path
from playwright.async_api import async_playwright

COOKIE_DIR = Path(__file__).resolve().parent / "data" / "cookies"


def _next_filename() -> str:
    """自动递增命名: cookie_1.json, cookie_2.json, ..."""
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(COOKIE_DIR.glob("cookie_*.json"))
    if not existing:
        return "cookie_1.json"
    # 取最大编号 + 1
    nums = []
    for f in existing:
        try:
            nums.append(int(f.stem.split("_")[-1]))
        except ValueError:
            pass
    return f"cookie_{max(nums) + 1}.json" if nums else "cookie_1.json"


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

        # 验证登录
        await page.goto("https://api.bilibili.com/x/web-interface/nav")
        text = await page.evaluate("document.body.innerText")
        data = json.loads(text)
        if not data.get("data", {}).get("isLogin"):
            print("警告：登录验证失败，Cookie 未保存")
            await ctx.close()
            await browser.close()
            return

        uname = data["data"]["uname"]
        print(f"验证成功！已登录为: {uname}")

        # 检查是否已有此账号的 cookie
        for existing in COOKIE_DIR.glob("cookie_*.json"):
            try:
                with open(existing, "r", encoding="utf-8") as f:
                    old = json.load(f)
                old_cookies = {c["name"]: c["value"] for c in old.get("cookies", [])}
                new_cookies = {c["name"]: c["value"] for c in (await ctx.cookies())}
                if old_cookies.get("DedeUserID") == new_cookies.get("DedeUserID"):
                    print(f"此账号已存在: {existing.name}, 更新该文件")
                    filename = existing.name
                    break
            except Exception:
                pass
        else:
            filename = _next_filename()

        # 保存
        storage = await ctx.storage_state()
        filepath = COOKIE_DIR / filename
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(storage, f, ensure_ascii=False, indent=2)
        print(f"Cookie 已保存到 {filepath}")
        print(f"当前共 {len(list(COOKIE_DIR.glob('cookie_*.json')))} 个 Cookie 文件")

        await ctx.close()
        await browser.close()

asyncio.run(main())
