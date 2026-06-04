#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
西安交通大学 CAS 统一认证 - 纯 HTTP 登录模块

完整的 CAS OAuth2 登录流程：
  1. GET  /xjtu/cas/login.html            => 触发 OAuth 重定向链
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
import time
from typing import Callable
from urllib.parse import urlencode, urlparse, parse_qs

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend
import base64



# ======================== 常量 ========================

BOOKING_HOST = "http://202.117.17.144"
CAS_HOST = "https://login.xjtu.edu.cn"
ORG_HOST = "https://org.xjtu.edu.cn"

# OAuth2 参数（从预订系统逆向获得）
OAUTH_APP_ID = "1439"
OAUTH_REDIRECT_URI = f"{BOOKING_HOST}/xjtu/cas/oauth2url.html"
OAUTH_SCOPE = "user_info"

# CAS 登录入口
CAS_LOGIN_ENTRY = f"{BOOKING_HOST}/xjtu/cas/login.html"

# 公钥接口
PUBLIC_KEY_URL = f"{CAS_HOST}/cas/jwt/publicKey"

# MFA 检测接口
MFA_DETECT_URL = f"{CAS_HOST}/cas/mfa/detect"

# 默认 User-Agent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

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
        202.117.17.144/xjtu/cas/oauth2url.html?code=SW-xxx&employeeNo=xxx
            | 302
        202.117.17.144/index.html (已登录，Session 已建立)
    """

    def __init__(self, timeout: float = 15.0, fp_visitor_id: str = None,
                 logger: Callable[[str], None] | None = None):
        self.timeout = timeout
        self.fp_visitor_id = fp_visitor_id or DEFAULT_FP_VISITOR_ID
        self._public_key_pem: str | None = None
        self._logger = logger

    def _log(self, message: str):
        if self._logger:
            self._logger(message)
        else:
            print(message)

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
            # -------- Step 1: 访问登录入口，跟随重定向到 CAS 登录页 --------
            print(f"[CAS] Step 1: 访问登录入口 {CAS_LOGIN_ENTRY}")
            resp = client.get(CAS_LOGIN_ENTRY)
            cas_login_url = str(resp.url)
            print(f"[CAS]   => 重定向到: {cas_login_url[:100]}...")

            # 如果已登录态且完整跑完 OAuth 链，最终会落在预订系统首页
            if "202.117.17.144" in cas_login_url and "cas" not in cas_login_url.lower():
                print("[CAS]   已有登录态，OAuth 授权完成")
                self._print_session_info(client)
                return client

            # 正常情况：停在 CAS 登录页，需要走完整登录流程
            # 注: 即使 TGC cookie 有效，httpx 拿到的仍是 200 HTML 页面（
            # 因为 CAS 的 TGC 自动登录是前端 JS 触发，非 302 重定向），
            # 所以必须提取 execution 并 POST 登录
            if "login.xjtu.edu.cn" not in cas_login_url:
                raise CASLoginError(f"未重定向到 CAS 登录页，当前 URL: {cas_login_url}")

            # -------- Step 2: 解析登录页，提取 execution 令牌 --------
            print("[CAS] Step 2: 解析 CAS 登录页")
            cas_html = resp.text
            execution = extract_execution(cas_html)
            print(f"[CAS]   execution: {execution[:60]}...")

            # -------- Step 3: 获取 RSA 公钥 --------
            print("[CAS] Step 3: 获取 RSA 公钥")
            pk_resp = client.get(PUBLIC_KEY_URL)
            self._public_key_pem = pk_resp.text.strip()
            print(f"[CAS]   公钥长度: {len(self._public_key_pem)} bytes")

            # -------- Step 4: RSA 加密密码 --------
            print("[CAS] Step 4: RSA 加密密码")
            encrypted_password = encrypt_password(password, self._public_key_pem)
            print(f"[CAS]   加密后: {encrypted_password[:30]}...({len(encrypted_password)} chars)")

            # -------- Step 5: MFA 检测 --------
            print("[CAS] Step 5: MFA 多因素认证检测")
            mfa_info = self._detect_mfa_info(client, username, encrypted_password)
            mfa_state = mfa_info.get("state", "")
            print(f"[CAS]   mfaState: {mfa_state}")
            if mfa_info.get("need"):
                raise CASLoginError("CAS requires secondary verification; automatic email OTP has been removed")

            # -------- Step 6: 提交登录 --------
            print("[CAS] Step 6: 提交登录请求")

            login_data = {
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
            }

            # POST 到 CAS login URL（带 service 参数）
            # 先不自动重定向，检查登录结果
            resp = client.post(
                cas_login_url,
                data=login_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": CAS_HOST,
                    "Referer": cas_login_url,
                },
                follow_redirects=False,
            )
            print(f"[CAS]   POST status: {resp.status_code}")

            if resp.status_code in (301, 302, 303, 307, 308):
                # 302 => 登录成功，跟随 OAuth 重定向链拿 Session
                location = resp.headers.get("location", "")
                print(f"[CAS]   redirect: {location[:100]}...")
                resp = client.get(location, follow_redirects=True)
            elif resp.status_code == 200 and self._is_security_auth_page(resp.text):
                raise CASLoginError("CAS returned a security verification page; automatic email OTP has been removed")
            elif resp.status_code == 200 and "cas/login" in str(resp.url):
                # 仍在登录页 => 密码错误或验证码
                pass

            final_url = str(resp.url)
            print(f"[CAS]   => 最终 URL: {final_url[:100]}...")

            # -------- Step 7: 验证登录结果 --------
            print("[CAS] Step 7: 验证登录状态")
            login_success = self._verify_login(client, resp, final_url)

            if login_success:
                print("[CAS] [OK] 登录成功！")
                self._print_session_info(client)
                return client
            else:
                # 检查是否登录页返回了错误
                if "cas/login" in final_url:
                    error_msg = self._extract_login_error(resp.text)
                    raise CASLoginError(f"CAS 登录失败: {error_msg}")
                raise CASLoginError(f"登录流程异常，最终 URL: {final_url}")

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
            print(f"[CAS] MFA detect failed (non-fatal): {e}")
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
            print(f"[CAS]   MFA 检测失败（非致命）: {e}")
            return ""

    def _verify_booking_session(self, client: httpx.Client) -> tuple[bool, str]:
        try:
            check_resp = client.get(
                f"{BOOKING_HOST}/order/seachMyOrder.html",
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
        """验证登录是否成功"""
        # 成功标志1: 最终 URL 是预订系统首页
        if "202.117.17.144" in final_url and "cas" not in final_url.lower():
            return True

        # 成功标志2: 最终 URL 包含 oauth2url（中间页）
        if "oauth2url" in final_url:
            return True

        # 成功标志3: 检查是否能访问需要登录的页面
        try:
            check_resp = client.get(
                f"{BOOKING_HOST}/yyuser/userinfo.html",
                follow_redirects=False,
            )
            # 如果没有被重定向到登录页，说明已登录
            if check_resp.status_code == 200 and "登录" not in check_resp.text[:200]:
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
        print("[CAS] Session Cookies:")
        for cookie in client.cookies.jar:
            print(f"  {cookie.domain}: {cookie.name}={cookie.value[:40]}...")


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
        resp = client.get("http://202.117.17.144/product/findtime.html?...")
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
        resp = client.get(f"{BOOKING_HOST}/yyuser/userinfo.html", timeout=10)
        print(f"\n[测试] GET /yyuser/userinfo.html => {resp.status_code}")
        if "登录" in resp.text[:200]:
            print("  [FAIL] 未登录（session 无效）")
        else:
            print("  [OK] 已登录")

        # 测试2: 查询场地时段
        from datetime import datetime, timedelta
        target_date = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        venue_id = 103  # 二号巨构
        url = f"{BOOKING_HOST}/product/findtime.html?type=day&s_dates={target_date}&serviceid={venue_id}"
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
