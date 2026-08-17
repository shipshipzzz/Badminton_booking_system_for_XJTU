"""Per-profile booking channels: campus-app H5 (:8080/web) and PC portal (:80)."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_CHANNEL = "8080"
VALID_CHANNELS = ("8080", "80")

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class ChannelConfig:
    id: str
    label: str
    origin: str
    base: str
    oauth_app_id: str
    cas_login_path: str
    oauth_callback_path: str
    book_path: str
    seat_mode: str
    user_agent: str
    oauth_state: str = "1234"

    @property
    def oauth_redirect_uri(self) -> str:
        return f"{self.base}{self.oauth_callback_path}"

    @property
    def cas_login_entry(self) -> str:
        return f"{self.base}{self.cas_login_path}"

    @property
    def org_oauth_entry(self) -> str:
        return (
            "https://org.xjtu.edu.cn/openplatform/oauth/authorize"
            f"?appId={self.oauth_app_id}"
            f"&redirectUri={self.oauth_redirect_uri}"
            f"&responseType=code&scope=user_info&state={self.oauth_state}"
        )

    @property
    def orders_check_url(self) -> str:
        return self.api_url("/order/seachMyOrder.html")

    @property
    def orders_detail_url(self) -> str:
        return self.api_url("/order/seachData.html")

    @property
    def userinfo_url(self) -> str:
        return self.api_url("/yyuser/userinfo.html")

    @property
    def index_url(self) -> str:
        return self.api_url("/index.html")

    @property
    def captcha_url(self) -> str:
        return f"{self.origin}/gen"

    @property
    def yzm_origin(self) -> str:
        return self.origin

    def api_url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base + path

    def rewrite_internal_url(self, url: str) -> str:
        if not url:
            return url
        for old in ("http://yudingapp", "https://yudingapp"):
            if url.startswith(old):
                rest = url[len(old):]
                if not rest.startswith("/"):
                    rest = "/" + rest
                if self.id == "8080":
                    return self.origin + rest
                if rest.startswith("/web"):
                    rest = rest[4:] or "/"
                return self.origin + rest
        for old in ("http://yudingweb", "https://yudingweb", "http://yudingweb.xjtu.edu.cn"):
            if url.startswith(old):
                rest = url[len(old):]
                if not rest.startswith("/"):
                    rest = "/" + rest
                if self.id == "8080":
                    if rest.startswith("/web"):
                        return self.origin + rest
                    return self.base + rest
                return self.origin + rest
        return url


CHANNELS = {
    "8080": ChannelConfig(
        id="8080",
        label="手机8080",
        origin="http://202.117.17.144:8080",
        base="http://202.117.17.144:8080/web",
        oauth_app_id="1659",
        cas_login_path="/cas/login.html",
        oauth_callback_path="/cas/oauth2url.html",
        book_path="/order/tobook.html",
        seat_mode="ok_area",
        user_agent=MOBILE_UA,
    ),
    "80": ChannelConfig(
        id="80",
        label="电脑80",
        origin="http://202.117.17.144",
        base="http://202.117.17.144",
        oauth_app_id="1439",
        cas_login_path="/xjtu/cas/login.html",
        oauth_callback_path="/xjtu/cas/oauth2url.html",
        book_path="/order/book.html",
        seat_mode="seat_html",
        user_agent=DESKTOP_UA,
        oauth_state="1",
    ),
}


def normalize_channel(value) -> str:
    raw = str(value or DEFAULT_CHANNEL).strip().lower()
    if raw in ("80", "pc", "web", "desktop"):
        return "80"
    return "8080"


def get_channel(value=None) -> ChannelConfig:
    return CHANNELS[normalize_channel(value)]


# Backward-compatible defaults (8080)
_DEFAULT = CHANNELS[DEFAULT_CHANNEL]
BOOKING_ORIGIN = _DEFAULT.origin
BOOKING_BASE = _DEFAULT.base
BOOKING_HOST = _DEFAULT.origin
OAUTH_APP_ID = _DEFAULT.oauth_app_id
OAUTH_REDIRECT_URI = _DEFAULT.oauth_redirect_uri
CAS_LOGIN_ENTRY = _DEFAULT.cas_login_entry
ORG_OAUTH_ENTRY = _DEFAULT.org_oauth_entry
ORDERS_CHECK_URL = _DEFAULT.orders_check_url
ORDERS_DETAIL_URL = _DEFAULT.orders_detail_url
USERINFO_URL = _DEFAULT.userinfo_url
INDEX_URL = _DEFAULT.index_url
CAPTCHA_URL = _DEFAULT.captcha_url
YZM_ORIGIN = _DEFAULT.yzm_origin
USER_AGENT = _DEFAULT.user_agent


def api_url(path: str) -> str:
    return _DEFAULT.api_url(path)


def rewrite_internal_url(url: str) -> str:
    return _DEFAULT.rewrite_internal_url(url)
