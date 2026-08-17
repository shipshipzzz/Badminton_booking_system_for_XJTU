#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
西安交通大学 CAS 统一认证 - 纯 HTTP 登录模块

完整的 CAS OAuth2 登录流程（校园 App H5 / :8080/web）：
  1. GET  /web/cas/login.html             => 触发 OAuth 重定向链（appId=1659）
  2. GET  CAS login page                   => 提取 execution 令牌
  3. GET  /cas/jwt/publicKey               => 获取 RSA 公钥
  4. RSA 加密密码
  5. POST /cas/mfa/detect                  => 获取 mfaState（多因素认证检测）
  6. POST /cas/login?service=...           => 提交登录表单
  7. 跟随重定向链                            => 最终获得预订系统 Session

使用方法：
    from cas_login import CASLogin

    cas = CASLogin()
    session = cas.login("username", "password")
    # session 是已认证的 httpx.Client，可直接调用预订系统 API
"""

import re
import hashlib
import sys
import time
from typing import Callable
from urllib.parse import urlencode, urlparse, parse_qs, urljoin

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend
import base64

from site_config import (
    CAS_LOGIN_ENTRY,
    INDEX_URL,
    ORDERS_CHECK_URL,
    ORG_OAUTH_ENTRY,
    USER_AGENT,
    USERINFO_URL,
    api_url,
    rewrite_internal_url,
)


# ======================== 常量 ========================

CAS_HOST = "https://login.xjtu.edu.cn"
ORG_HOST = "https://org.xjtu.edu.cn"

# 公钥接口
PUBLIC_KEY_URL = f"{CAS_HOST}/cas/jwt/publicKey"

# MFA 检测接口
MFA_DETECT_URL = f"{CAS_HOST}/cas/mfa/detect"

# 默认浏览器指纹 ID（FingerprintJS v3 生成的 visitorId）
DEFAULT_FP_VISITOR_ID = "6b7017157af25ed068992924269fcd1b"
AUTH_CHECK_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


# ======================== RSA 加密 ========================

def encrypt_password(password: str, public_key_pem: str) -> str:
    """
    使用 RSA 公钥加密密码，与前端 JSEncrypt 行为一致。

    前端流程：
      1. GET /cas/jwt/publicKey 获取 PEM 格式公钥
      2. new JSEncrypt().setPublicKey(pem)
      3. encrypt.encrypt(password)  => Base64 字符串
      4. 拼接前缀 "__RSA__"

    Args:
        password: 明文密码
        public_key_pem: PEM 格式公钥字符串

    Returns:
        "__RSA__" + Base64(RSA_PKCS1v15(password))
    """
    public_key = serialization.load_pem_public_key(
        public_key_pem.encode("utf-8"),
        backend=default_backend(),
    )
    encrypted = public_key.encrypt(
        password.encode("utf-8"),
        asym_padding.PKCS1v15(),
    )
    b64 = base64.b64encode(encrypted).decode("utf-8")
    return f"__RSA__{b64}"


def generate_fp_visitor_id() -> str:
    """
    生成一个伪造的 FingerprintJS visitorId。
    真实场景中这是浏览器指纹的哈希值，这里用时间戳 + 随机数模拟。
    """
    raw = f"python-cas-{time.time()}"
    return hashlib.md5(raw.encode()).hexdigest()


# ======================== HTML 解析 ========================

def extract_execution(html: str) -> str:
    """
    从 CAS 登录页面 HTML 中提取 execution 令牌。

    CAS 登录页包含一个隐藏表单：
      <input type="hidden" name="execution" value="UUID_BASE64..." />

    execution 是 CAS 的 WebFlow 状态标识，每次访问登录页都会生成新值，
    提交登录时必须带上，否则 CAS 会拒绝。
    """
    match = re.search(r'name="execution"\s+value="([^"]+)"', html)
    if not match:
        raise ValueError("未能从 CAS 登录页提取 execution 令牌")
    return match.group(1)


def extract_event_id(html: str) -> str:
    """提取 _eventId（通常是 'submit'）"""
    match = re.search(r'name="_eventId"\s+value="([^"]+)"', html)
    return match.group(1) if match else "submit"


def extract_input_value(html: str, name: str, default: str = "") -> str:
    """Extract a named input value from a CAS page."""
    for tag in re.findall(r"<input\b[^>]*>", html, flags=re.I):
        name_match = re.search(r"\bname=[\"']([^\"']+)", tag, flags=re.I)
        if not name_match or name_match.group(1) != name:
            continue
        value_match = re.search(r"\bvalue=[\"']([^\"']*)", tag, flags=re.I)
        return value_match.group(1) if value_match else default
    return default


# ======================== CAS 登录类 ========================

class CASLogin:
    """
    西安交通大学 CAS 统一认证登录客户端。

    完整流程：
        CAS Login Entry (booking site)
            | 302
        org.xjtu.edu.cn/openplatform/oauth/authorize
            | 302
        login.xjtu.edu.cn/cas/oauth2.0/authorize
            | 302
        login.xjtu.edu.cn/cas/login?service=callbackAuthorize
            | (用户登录)
        POST login.xjtu.edu.cn/cas/login
            | 302 (带 ticket)
        login.xjtu.edu.cn/cas/oauth2.0/callbackAuthorize?ticket=ST-xxx
            | 302
        login.xjtu.edu.cn/cas/oauth2.0/authorize (自动授权)
            | 302
        org.xjtu.edu.cn/openplatform/oauth/authorizesw?code=OC-xxx
            | 302
        202.117.17.144:8080/web/cas/oauth2url.html?code=SW-xxx&employeeNo=xxx
            | 302
        202.117.17.144:8080/web/index.html (已登录，Session 已建立)
    """

    def __init__(self, timeout: float = 15.0, fp_visitor_id: str = None,
                 logger: Callable[[str], None] | None = None):
        self.timeout = timeout
        self.fp_visitor_id = fp_visitor_id or DEFAULT_FP_VISITOR_ID
        self._public_key_pem: str | None = None
        self._logger = logger

    def _log(self, message: str):
        try:
            if self._logger:
                self._logger(message)
            else:
                print(message)
        except UnicodeEncodeError:
            safe = message.encode("utf-8", errors="backslashreplace").decode("ascii", errors="ignore")
            try:
                print(safe)
            except Exception:
                pass

    @staticmethod
    def _rewrite_internal_url(url: str) -> str:
        return rewrite_internal_url(url)

    @staticmethod
    def _is_cas_login_page(url: str, html: str = "") -> bool:
        if "login.xjtu.edu.cn" not in url or "/cas/login" not in url:
            return False
        if html:
            return 'name="execution"' in html or "execution" in html
        return True

    @staticmethod
    def _is_booking_entry_page(url: str) -> bool:
        return "202.117.17.144" in url and "/cas/login" in url

    @staticmethod
    def _extract_js_redirect(html: str) -> str:
        if not html:
            return ""
        patterns = [
            r"window\.location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]",
            r"location\.href\s*=\s*['\"]([^'\"]+)['\"]",
            r"location\.replace\(\s*['\"]([^'\"]+)['\"]",
            r"http-equiv=['\"]refresh['\"][^>]*url=([^\"'>]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.I)
            if match:
                return match.group(1).strip()
        return ""

    def _follow_sso(self, client: httpx.Client, url: str, username: str, password: str,
                    max_hops: int = 20) -> httpx.Response | None:
        """Follow SSO redirects, rewrite internal hosts, and POST CAS if needed."""
        current = self._rewrite_internal_url(url)
        last = None
        for hop in range(max_hops):
            current = self._rewrite_internal_url(urljoin(str(last.url), current) if last and current.startswith("/") else current)
            self._log(f"[CAS]   hop {hop + 1}: {current[:140]}")
            last = client.get(current, follow_redirects=False)
            if last.status_code >= 500 and "202.117.17.144" in current:
                for attempt in range(3):
                    time.sleep(0.4 * (attempt + 1))
                    self._log(f"[CAS]   hop {hop + 1} 遇到 {last.status_code}，重试 {attempt + 1}/3")
                    last = client.get(current, follow_redirects=False)
                    if last.status_code < 500:
                        break
            if last.status_code in (301, 302, 303, 307, 308):
                location = last.headers.get("location", "")
                if not location:
                    break
                current = urljoin(str(last.url), location)
                continue
            if self._is_cas_login_page(str(last.url), last.text):
                last = self._submit_cas_login(client, str(last.url), last.text, username, password)
                if last.status_code in (301, 302, 303, 307, 308):
                    current = urljoin(str(last.url), last.headers.get("location", ""))
                    continue
                if last.status_code == 200 and self._is_security_auth_page(last.text):
                    raise CASLoginError("CAS returned a security verification page; automatic email OTP has been removed")
                current = str(last.url)
                continue
            js_url = self._extract_js_redirect(last.text)
            if js_url and js_url not in str(last.url):
                current = urljoin(str(last.url), js_url)
                continue
            break
        return last

    def _submit_cas_login(self, client: httpx.Client, cas_login_url: str, cas_html: str,
                          username: str, password: str) -> httpx.Response:
        self._log("[CAS] Step 2: 解析 CAS 登录页")
        execution = extract_execution(cas_html)
        self._log(f"[CAS]   execution: {execution[:60]}...")

        self._log("[CAS] Step 3: 获取 RSA 公钥")
        pk_resp = client.get(PUBLIC_KEY_URL)
        self._public_key_pem = pk_resp.text.strip()
        self._log(f"[CAS]   公钥长度: {len(self._public_key_pem)} bytes")

        self._log("[CAS] Step 4: RSA 加密密码")
        encrypted_password = encrypt_password(password, self._public_key_pem)
        self._log(f"[CAS]   加密后: {encrypted_password[:30]}...({len(encrypted_password)} chars)")

        self._log("[CAS] Step 5: MFA 多因素认证检测")
        mfa_info = self._detect_mfa_info(client, username, encrypted_password)
        mfa_state = mfa_info.get("state", "")
        self._log(f"[CAS]   mfaState: {mfa_state}")
        if mfa_info.get("need"):
            raise CASLoginError("CAS requires secondary verification; automatic email OTP has been removed")

        self._log("[CAS] Step 6: 提交登录请求")
        resp = client.post(
            cas_login_url,
            data={
                "username": username,
                "password": encrypted_password,
                "captcha": "",
                "currentMenu": "1",
                "failN": "0",
                "mfaState": mfa_state,
                "execution": execution,
                "fpVisitorId": self.fp_visitor_id,
                "trustAgent": "true",
                "_eventId": "submit",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": CAS_HOST,
                "Referer": cas_login_url,
            },
            follow_redirects=False,
        )
        self._log(f"[CAS]   POST status: {resp.status_code}")
        if resp.status_code == 200 and self._is_security_auth_page(resp.text):
            raise CASLoginError("CAS returned a security verification page; automatic email OTP has been removed")
        if resp.status_code == 200 and self._is_cas_login_page(str(resp.url), resp.text):
            raise CASLoginError(f"CAS 登录失败: {self._extract_login_error(resp.text)}")
        return resp

    def _open_login_entry(self, client: httpx.Client) -> httpx.Response:
        self._log(f"[CAS] Step 1: 访问登录入口 {CAS_LOGIN_ENTRY}")
        resp = client.get(CAS_LOGIN_ENTRY)
        self._log(f"[CAS]   => 重定向到: {str(resp.url)[:100]}...")
        if self._is_cas_login_page(str(resp.url), resp.text):
            return resp
        if self._is_booking_entry_page(str(resp.url)):
            self._log("[CAS]   登录入口未跳转，清除 cookie 后重试")
            client.cookies.clear()
            resp = client.get(CAS_LOGIN_ENTRY)
            self._log(f"[CAS]   => 重试后: {str(resp.url)[:100]}...")
            if self._is_cas_login_page(str(resp.url), resp.text):
                return resp
            self._log("[CAS]   改走 OpenPlatform OAuth 入口")
            resp = client.get(ORG_OAUTH_ENTRY)
            self._log(f"[CAS]   => OAuth 入口: {str(resp.url)[:100]}...")
        return resp

    def _has_booking_cookies(self, client: httpx.Client) -> bool:
        for cookie in client.cookies.jar:
            domain = (cookie.domain or "").lstrip(".")
            if cookie.name in ("SESSION", "JSESSIONID") and (
                "202.117.17.144" in domain or domain in ("", "localhost")
            ):
                return True
        return False

    @staticmethod
    def _is_oauth_callback(url: str, html: str = "") -> bool:
        if "oauth2url.html" not in url or "code=" not in url:
            return False
        if html and ("登录失败" in html or "页面未找到" in html):
            return False
        return True

    def _probe_orders(self, client: httpx.Client, retries: int = 4) -> tuple[bool, str]:
        last_reason = "no response"
        for attempt in range(retries):
            resp = client.get(
                ORDERS_CHECK_URL,
                params={"page": 1, "rows": 1, "sort": "createdate", "order": "desc"},
                headers=AUTH_CHECK_HEADERS,
                follow_redirects=False,
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                return False, resp.headers.get("location", "")
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    last_reason = "non-json"
                else:
                    if isinstance(data, dict) and ("rows" in data or "total" in data):
                        return True, f"rows={len(data.get('rows', []))}"
                    last_reason = "unexpected payload"
                    break
            else:
                last_reason = f"status={resp.status_code}"
                if resp.status_code < 500:
                    break
            time.sleep(0.4 * (attempt + 1))
        return False, last_reason

    def _userinfo_looks_logged_in(self, client: httpx.Client) -> bool:
        try:
            resp = client.get(USERINFO_URL, follow_redirects=False)
        except Exception:
            return False
        if resp.status_code != 200:
            return False
        text = resp.text[:800]
        return "登录失败" not in text and "请登录" not in text and "javascript:login()" not in text

    def _establish_booking_session(self, client: httpx.Client, username: str, password: str,
                                   oauth_url: str = "", oauth_html: str = "") -> None:
        self._log("[CAS] Step 7: 补全预订系统本地会话")
        try:
            client.get(INDEX_URL, follow_redirects=True)
        except Exception as e:
            self._log(f"[CAS]   index.html 访问失败（忽略）: {e}")

        ok, extra = self._probe_orders(client)
        if ok:
            self._log(f"[CAS]   订单接口已可用 ({extra})")
            return

        if extra.startswith("http") and any(key in extra for key in ("oauth", "cas", "ohello", "login")):
            self._log("[CAS] Step 8: 跟随订单接口 SSO 补全预订会话")
            self._follow_sso(client, extra, username, password)
            try:
                client.get(INDEX_URL, follow_redirects=True)
            except Exception:
                pass
            ok, extra = self._probe_orders(client)
            if ok:
                self._log(f"[CAS]   SSO 补全成功 ({extra})")
                return

        if self._userinfo_looks_logged_in(client):
            self._log("[CAS]   用户信息页已登录，订单接口暂不可用，继续使用当前会话")
            return

        oauth_ok = self._is_oauth_callback(oauth_url, oauth_html)
        if oauth_ok and self._has_booking_cookies(client) and extra.startswith("status=5"):
            self._log(f"[CAS]   OAuth 回调已完成，订单接口 {extra}，先保留会话")
            return

        raise CASLoginError(f"预订系统会话无效: {extra[:180]}")

    def login(self, username: str, password: str) -> httpx.Client:
        """
        执行完整的 CAS 登录流程。

        Args:
            username: CAS 用户名（手机号或学号）
            password: CAS 密码

        Returns:
            已认证的 httpx.Client，其 cookies 中包含预订系统的 Session

        Raises:
            CASLoginError: 登录失败
        """
        client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

        try:
            resp = self._open_login_entry(client)
            current_url = str(resp.url)

            if self._is_cas_login_page(current_url, resp.text):
                resp = self._submit_cas_login(client, current_url, resp.text, username, password)
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location", "")
                    self._log(f"[CAS]   redirect: {location[:100]}...")
                    resp = self._follow_sso(client, location, username, password) or resp
                current_url = str(resp.url)
                self._log(f"[CAS]   => 最终 URL: {current_url[:100]}...")
            elif "202.117.17.144" in current_url and "cas/login" not in current_url.lower():
                self._log("[CAS]   已回到预订站点，继续校验本地会话")
            else:
                raise CASLoginError(f"未重定向到 CAS 登录页，当前 URL: {current_url}")

            self._establish_booking_session(
                client, username, password,
                oauth_url=current_url,
                oauth_html=getattr(resp, "text", "") or "",
            )
            self._log("[CAS] [OK] 登录成功！")
            self._print_session_info(client)
            return client

        except CASLoginError:
            client.close()
            raise
        except Exception as e:
            client.close()
            raise CASLoginError(f"登录过程出错: {e}") from e

    def _detect_mfa_info(self, client: httpx.Client, username: str, encrypted_password: str) -> dict:
        """Return CAS MFA state and whether secondary verification is required."""
        try:
            resp = client.post(
                MFA_DETECT_URL,
                data={
                    "loginType": "passwordLogin",
                    "username": username,
                    "password": encrypted_password,
                    "fpVisitorId": self.fp_visitor_id,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": CAS_HOST,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                inner = data.get("data", {}) or {}
                state = (
                    inner.get("state")
                    or inner.get("mfaState")
                    or data.get("mfaState")
                    or data.get("state")
                    or ""
                )
                return {"state": str(state), "need": bool(inner.get("need", False)), "raw": data}
        except Exception as e:
            self._log(f"[CAS] MFA detect failed (non-fatal): {e}")
        return {"state": "", "need": False, "raw": None}

    def _is_security_auth_page(self, html: str) -> bool:
        return 'name="secState"' in html or "/cas/sec/initByType" in html

    def _detect_mfa(self, client: httpx.Client, username: str, encrypted_password: str) -> str:
        """
        调用 MFA 检测接口。

        POST /cas/mfa/detect
        Body: username=xxx&password=__RSA__xxx&fpVisitorId=xxx

        响应可能返回 mfaState 用于后续登录提交。
        如果 MFA 检测失败，返回空字符串（部分 CAS 配置不要求 MFA）。
        """
        try:
            resp = client.post(
                MFA_DETECT_URL,
                data={
                    "username": username,
                    "password": encrypted_password,
                    "fpVisitorId": self.fp_visitor_id,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": CAS_HOST,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            # 响应格式: {"code":0, "data": {"state": "xxx", "need": false, ...}}
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    inner = data.get("data", {})
                    state = (
                        inner.get("state")
                        or inner.get("mfaState")
                        or data.get("mfaState")
                        or data.get("state")
                        or ""
                    )
                    if state:
                        return str(state)
                except Exception:
                    pass
            return ""
        except Exception as e:
            self._log(f"[CAS]   MFA 检测失败（非致命）: {e}")
            return ""

    def _verify_booking_session(self, client: httpx.Client) -> tuple[bool, str]:
        try:
            check_resp = client.get(
                ORDERS_CHECK_URL,
                params={"page": 1, "rows": 1, "sort": "createdate", "order": "desc"},
                headers=AUTH_CHECK_HEADERS,
                follow_redirects=False,
            )
        except Exception as e:
            return False, f"request failed: {e}"

        if check_resp.status_code in (301, 302, 303, 307, 308):
            location = check_resp.headers.get("location", "")
            return False, f"redirect {check_resp.status_code} -> {location[:120]}"

        if check_resp.status_code != 200:
            return False, f"status={check_resp.status_code}"

        try:
            data = check_resp.json()
        except Exception:
            text = check_resp.text[:120].replace("\r", " ").replace("\n", " ")
            return False, f"non-json body: {text}"

        if isinstance(data, dict) and ("rows" in data or "total" in data):
            return True, f"rows={len(data.get('rows', []))}"

        if isinstance(data, dict):
            msg = data.get("message") or data.get("msg") or str(data)[:120]
            return False, f"unexpected payload: {msg}"

        return False, f"unexpected payload type: {type(data).__name__}"

    def _verify_login(self, client: httpx.Client, resp: httpx.Response, final_url: str) -> bool:
        """验证登录是否成功。oauth2url 只是中间页，不能当作已登录。"""
        ok, _ = self._probe_orders(client)
        if ok:
            return True
        if "202.117.17.144" in final_url and "cas" not in final_url.lower() and "oauth2url" not in final_url:
            return True
        try:
            check_resp = client.get(
                USERINFO_URL,
                follow_redirects=False,
            )
            if check_resp.status_code == 200 and "登录" not in check_resp.text[:200] and "登录失败" not in check_resp.text:
                return True
        except Exception:
            pass
        return False

    def _extract_login_error(self, html: str) -> str:
        """从 CAS 登录页提取错误信息"""
        # 搜索 Vue 数据中的错误信息
        patterns = [
            r'loginError\s*=\s*\{[^}]*message["\']?\s*:\s*["\']([^"\']+)',
            r'class="error[^"]*"[^>]*>([^<]+)',
            r'错误[：:]\s*([^<\n]+)',
            r'密码[^<]*错误',
            r'账号[^<]*不存在',
            r'验证码[^<]*错误',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1) if match.lastindex else match.group(0)
        return "未知错误（可能是密码错误或验证码要求）"

    def _print_session_info(self, client: httpx.Client):
        """打印会话信息"""
        self._log("[CAS] Session Cookies:")
        for cookie in client.cookies.jar:
            self._log(f"  {cookie.domain}: {cookie.name}={cookie.value[:40]}...")


class CASLoginError(Exception):
    """CAS 登录错误"""
    pass


# ======================== 便捷函数 ========================

def create_authenticated_client(username: str, password: str, **kwargs) -> httpx.Client:
    """
    一步完成 CAS 登录，返回已认证的 HTTP 客户端。

    Args:
        username: CAS 用户名
        password: CAS 密码
        **kwargs: 传递给 CASLogin 的额外参数

    Returns:
        已认证的 httpx.Client

    Example:
        client = create_authenticated_client("username", "password")
        resp = client.get(api_url("/product/findtime.html") + "?...")
    """
    cas = CASLogin(**kwargs)
    return cas.login(username, password)


# ======================== 主函数 ========================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python cas_login.py <username> <password>")
        sys.exit(1)

    username = sys.argv[1]
    password = sys.argv[2]

    print("=" * 60)
    print("西安交通大学 CAS 统一认证 - HTTP 登录测试")
    print("=" * 60)
    print(f"用户名: {username}")
    print()

    try:
        client = create_authenticated_client(username, password)
        print()
        print("=" * 60)
        print("登录成功！测试访问预订系统 API...")
        print("=" * 60)

        # 测试1: 访问用户信息页
        resp = client.get(USERINFO_URL, timeout=10)
        print(f"\n[测试] GET /yyuser/userinfo.html => {resp.status_code}")
        if "登录" in resp.text[:200]:
            print("  [FAIL] 未登录（session 无效）")
        else:
            print("  [OK] 已登录")

        # 测试2: 查询场地时段
        from datetime import datetime, timedelta
        target_date = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        venue_id = 103  # 二号巨构
        url = api_url(f"/product/findtime.html?type=day&s_dates={target_date}&serviceid={venue_id}")
        resp = client.get(url, headers={"X-Requested-With": "XMLHttpRequest"})
        print(f"\n[测试] GET /product/findtime.html (二号巨构, {target_date}) => {resp.status_code}")
        try:
            data = resp.json()
            if data.get("result") == "1":
                times = data.get("object", [])
                print(f"  [OK] 找到 {len(times)} 个时段")
                for t in times[:3]:
                    print(f"    {t.get('TIME_NO', '?')} (ID: {t.get('ID', '?')})")
            else:
                print(f"  结果: {data}")
        except Exception as e:
            print(f"  解析失败: {e}")
            print(f"  响应: {resp.text[:200]}")

        client.close()
        print("\n[完成] 客户端已关闭")

    except CASLoginError as e:
        print(f"\n[失败] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[错误] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
