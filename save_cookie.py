"""B站 Cookie 保存工具 —— 扫码登录后自动检测并保存到 data/cookies/ 目录"""
import asyncio, json, os
from pathlib import Path
from playwright.async_api import async_playwright

COOKIE_DIR = Path(__file__).resolve().parent / "data" / "cookies"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
WAIT_TIMEOUT = 300  # 最长等待 5 分钟


def _next_filename() -> str:
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(COOKIE_DIR.glob("cookie_*.json"))
    if not existing:
        return "cookie_1.json"
    nums = []
    for f in existing:
        try:
            nums.append(int(f.stem.split("_")[-1]))
        except ValueError:
            pass
    return f"cookie_{max(nums) + 1}.json" if nums else "cookie_1.json"


async def _wait_for_login(page, timeout: int) -> str | None:
    """轮询 nav API 直到检测到登录, 返回用户名; 超时/关闭返回 None"""
    for i in range(timeout // 2):
        try:
            resp = await page.goto(NAV_URL, timeout=10000, wait_until="domcontentloaded")
            if resp and resp.ok:
                text = await page.evaluate("() => document.body.innerText")
                data = json.loads(text)
                if data.get("data", {}).get("isLogin"):
                    return data["data"]["uname"]
        except Exception:
            pass

        # 等待时打印点, 让用户知道还在运行
        if i % 15 == 0 and i > 0:
            print(f"  等待中... ({i * 2}s / {timeout}s)")
        elif i % 5 == 0:
            print(".", end="", flush=True)
        await asyncio.sleep(2)

    return None


async def main():
    print("=" * 50)
    print("B站 Cookie 保存工具")
    print("=" * 50)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
        page = await ctx.new_page()

        try:
            await page.goto("https://www.bilibili.com/", timeout=15000, wait_until="domcontentloaded")
        except Exception:
            print("\n✗ 无法打开 B站 页面, 请检查网络连接")
            await ctx.close()
            await browser.close()
            return

        print("\n请在浏览器中点击右上角「登录」并扫码")
        print(f"最长等待 {WAIT_TIMEOUT}s, 登录成功会自动继续...")

        try:
            uname = await _wait_for_login(page, WAIT_TIMEOUT)
        except Exception:
            print("\n\n浏览器已关闭, 退出")
            await browser.close()
            return

        if not uname:
            print(f"\n\n✗ 超时({WAIT_TIMEOUT}s)未检测到登录, 请重试")
            await ctx.close()
            await browser.close()
            return

        print(f"\n\n✓ 登录成功: {uname}")

        # 检查是否已有此账号
        filename = None
        try:
            current_cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
        except Exception:
            current_cookies = {}

        for existing in sorted(COOKIE_DIR.glob("cookie_*.json")):
            try:
                with open(existing, "r", encoding="utf-8") as f:
                    old = json.load(f)
                old_uid = {c["name"]: c["value"] for c in old.get("cookies", [])}.get("DedeUserID")
                new_uid = current_cookies.get("DedeUserID")
                if old_uid and old_uid == new_uid:
                    filename = existing.name
                    print(f"  此账号已有记录, 更新: {filename}")
                    break
            except Exception:
                pass

        if not filename:
            filename = _next_filename()

        storage = await ctx.storage_state()
        filepath = COOKIE_DIR / filename
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(storage, f, ensure_ascii=False, indent=2)

        total = len(list(COOKIE_DIR.glob("cookie_*.json")))
        print(f"  已保存: {filepath}")
        print(f"  当前共 {total} 个 Cookie 文件")
        print("=" * 50)

        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n已取消")
