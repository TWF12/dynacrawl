"""B站爬取错误码定义"""

# UP主信息相关
E001_CARD_API_FAILED = "E001"
E002_VIDEO_COUNT_FAILED = "E002"

# 视频列表相关
E101_ARC_SEARCH_BLOCKED = "E101"
E102_ARC_SEARCH_HTTP_ERR = "E102"
E103_VIDEO_INCOMPLETE = "E103"
E104_NO_VIDEOS_AT_ALL = "E104"
E105_NO_LOGIN_COOKIE = "E105"

# 网络相关
E201_NETWORK_TIMEOUT = "E201"
E202_PAGE_LOAD_FAILED = "E202"
E203_WBI_KEY_FAILED = "E203"

# 说明映射
ERROR_MESSAGES = {
    E001_CARD_API_FAILED: "card API 请求失败，无法获取 UP主 基本信息",
    E002_VIDEO_COUNT_FAILED: "无法获取视频总数（card API 和 arc/search 均失败）",
    E101_ARC_SEARCH_BLOCKED: "arc/search API 被风控拦截(412)，尝试降低请求频率或检查登录态",
    E102_ARC_SEARCH_HTTP_ERR: "arc/search API HTTP 请求失败",
    E103_VIDEO_INCOMPLETE: "视频列表不完整，部分视频可能丢失",
    E104_NO_VIDEOS_AT_ALL: "未获取到任何视频数据，可能触发风控或登录态失效",
    E105_NO_LOGIN_COOKIE: "未加载 B站 登录 cookie，arc/search API 需要登录态",
    E201_NETWORK_TIMEOUT: "网络请求超时",
    E202_PAGE_LOAD_FAILED: "B站 页面加载失败",
    E203_WBI_KEY_FAILED: "WBI 签名密钥获取失败",
}


def format_error(code: str, detail: str = "") -> str:
    """格式化错误信息: [CODE] message | detail"""
    msg = ERROR_MESSAGES.get(code, f"未知错误({code})")
    if detail:
        return f"[{code}] {msg} | {detail}"
    return f"[{code}] {msg}"
