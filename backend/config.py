import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{DATA_DIR / 'dynacrawl.db'}")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
USE_REDIS = os.getenv("USE_REDIS", "").lower() in ("1", "true", "yes")

BROWSER_CONCURRENCY = int(os.getenv("BROWSER_CONCURRENCY", "3"))
BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "true").lower() not in ("0", "false", "no")

REQUEST_DELAY_MIN = float(os.getenv("REQUEST_DELAY_MIN", "3.0"))
REQUEST_DELAY_MAX = float(os.getenv("REQUEST_DELAY_MAX", "8.0"))

PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT", "30000"))

MAX_RETRY = int(os.getenv("MAX_RETRY", "2"))

_raw_proxy = os.getenv("PROXY_LIST", "")
PROXY_LIST = [p.strip() for p in _raw_proxy.split(",") if p.strip()]

QUEUE_KEY = os.getenv("QUEUE_KEY", "dynacrawl:queue")
COOKIE_DIR = DATA_DIR / "cookies"
