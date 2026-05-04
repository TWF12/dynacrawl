"""Cookie 管理器 — 多文件轮换 + 过期自动检测删除"""
import json
import os
import logging
import asyncio
from pathlib import Path
from typing import Optional

from backend.config import COOKIE_DIR, COOKIE_FILE

logger = logging.getLogger(__name__)

# B站 未登录/过期 错误码
_AUTH_ERROR_CODES = {-101, 3, -6}

# 默认 nav API 验证 URL
_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


class CookieManager:
    """管理多个 B站 Cookie 文件, 支持轮换和过期自动删除"""

    def __init__(self):
        self._files: list[Path] = []
        self._index: int = 0
        self._initialized: bool = False
        self._current_file: Optional[Path] = None

    def _scan(self):
        """扫描 cookie 目录, 优先多文件模式, 回退单文件兼容"""
        if self._initialized:
            return
        self._initialized = True

        COOKIE_DIR.mkdir(parents=True, exist_ok=True)

        # 优先扫描 cookies/ 目录
        cookie_files = sorted(COOKIE_DIR.glob("*.json"))
        if cookie_files:
            self._files = cookie_files
            logger.info("Cookie 多文件模式: %d 个文件", len(self._files))
            return

        # 回退: 旧的单文件模式
        single = Path(COOKIE_FILE)
        if single.exists():
            self._files = [single]
            logger.info("Cookie 单文件模式(兼容): %s", COOKIE_FILE)
            return

        logger.warning("未找到任何 Cookie 文件, 将无登录态运行")

    @property
    def count(self) -> int:
        self._scan()
        return len(self._files)

    def get_next(self) -> Optional[dict]:
        """返回下一个有效 cookie 的 storage_state, 自动跳过已删除的文件"""
        self._scan()
        if not self._files:
            return None

        # 清理已失效的条目
        self._files = [f for f in self._files if f.exists()]
        if not self._files:
            return None

        # 轮换: 返回当前索引, 然后前进
        idx = self._index % len(self._files)
        self._index = (self._index + 1) % len(self._files)

        filepath = self._files[idx]
        self._current_file = filepath
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("加载 Cookie 失败 %s: %s", filepath.name, e)
            return None

    def mark_current_invalid(self):
        """标记当前正在使用的 cookie 为无效并删除"""
        if self._current_file and self._current_file.exists():
            self._current_file.unlink()
            logger.warning("已删除过期 Cookie: %s", self._current_file.name)
            self._files = [f for f in self._files if f.exists()]
            self._current_file = None

    def mark_invalid(self, storage_state: dict = None, filename: str = None):
        """标记 cookie 为无效并删除文件"""
        target = None
        if filename:
            target = COOKIE_DIR / filename
        if target and target.exists():
            target.unlink()
            logger.warning("已删除过期 Cookie: %s", target.name)
            self._files = [f for f in self._files if f.exists()]

    async def validate_all(self):
        """启动时异步验证所有 cookie 有效性, 删除已过期的
        通过实际请求 B站 nav API 检测登录态"""
        self._scan()
        if not self._files:
            return

        import urllib.request

        valid = []
        for filepath in self._files:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    storage = json.load(f)

                # 提取 cookies 发 HTTP 请求验证
                cookies = storage.get("cookies", [])
                cookie_str = "; ".join(
                    f"{c.get('name', '')}={c.get('value', '')}"
                    for c in cookies
                    if c.get("name") in ("SESSDATA", "bili_jct", "DedeUserID")
                )
                if not cookie_str:
                    logger.warning("Cookie 文件缺少关键字段: %s", filepath.name)
                    filepath.unlink()
                    continue

                req = urllib.request.Request(_NAV_URL, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Cookie": cookie_str,
                })
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                if data.get("data", {}).get("isLogin"):
                    uname = data["data"].get("uname", "?")
                    logger.info("Cookie 有效: %s → %s", filepath.name, uname)
                    valid.append(filepath)
                else:
                    logger.warning("Cookie 已过期, 删除: %s", filepath.name)
                    filepath.unlink()
            except Exception as e:
                logger.warning("Cookie 验证失败 %s: %s, 暂时保留", filepath.name, e)
                valid.append(filepath)  # 网络错误不删除, 保留

        self._files = valid
        if valid:
            logger.info("Cookie 验证完成: %d/%d 有效", len(valid), len(self._files) + (len(self._files) != len(valid)))
        else:
            logger.warning("所有 Cookie 均已过期或无效!")


# 全局单例
cookie_manager = CookieManager()
