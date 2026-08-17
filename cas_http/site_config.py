"""Official booking site: campus-app H5 on :8080/web, not the flaky port-80 PC portal."""

BOOKING_ORIGIN = "http://202.117.17.144:8080"
BOOKING_BASE = f"{BOOKING_ORIGIN}/web"
BOOKING_HOST = BOOKING_ORIGIN

OAUTH_APP_ID = "1659"
OAUTH_REDIRECT_URI = f"{BOOKING_BASE}/cas/oauth2url.html"
OAUTH_SCOPE = "user_info"

CAS_LOGIN_ENTRY = f"{BOOKING_BASE}/cas/login.html"
ORG_OAUTH_ENTRY = (
    "https://org.xjtu.edu.cn/openplatform/oauth/authorize"
    f"?appId={OAUTH_APP_ID}"
    f"&redirectUri={OAUTH_REDIRECT_URI}"
    f"&responseType=code&scope={OAUTH_SCOPE}&state=1234"
)
ORDERS_CHECK_URL = f"{BOOKING_BASE}/order/seachMyOrder.html"
ORDERS_DETAIL_URL = f"{BOOKING_BASE}/order/seachData.html"
USERINFO_URL = f"{BOOKING_BASE}/yyuser/userinfo.html"
INDEX_URL = f"{BOOKING_BASE}/index.html"
CAPTCHA_URL = f"{BOOKING_ORIGIN}/gen"
YZM_ORIGIN = BOOKING_ORIGIN

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Mobile Safari/537.36"
)

_INTERNAL_APP_HOSTS = (
    "http://yudingapp",
    "https://yudingapp",
)
_INTERNAL_WEB_HOSTS = (
    "http://yudingweb",
    "https://yudingweb",
    "http://yudingweb.xjtu.edu.cn",
)


def api_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return BOOKING_BASE + path


def rewrite_internal_url(url: str) -> str:
    if not url:
        return url
    for old in _INTERNAL_APP_HOSTS:
        if url.startswith(old):
            rest = url[len(old):]
            if not rest.startswith("/"):
                rest = "/" + rest
            return BOOKING_ORIGIN + rest
    for old in _INTERNAL_WEB_HOSTS:
        if url.startswith(old):
            rest = url[len(old):]
            if not rest.startswith("/"):
                rest = "/" + rest
            if rest.startswith("/web"):
                return BOOKING_ORIGIN + rest
            return BOOKING_BASE + rest
    return url
