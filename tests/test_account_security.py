"""2FA is opt-in: first run logs in directly; self-service enrollment gates future logins."""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pyotp
import pytest

from tests.live_server import REPO_ROOT, free_port

ADMIN = "secadmin"
ADMIN_PW = "SecurePass123"


def next_code(secret: str) -> str:
    """A code from the step after the current one.

    Enrollment spends the current step so the confirming code cannot be replayed
    at the login form. The logins below therefore use the next step, which the
    server still accepts inside its clock-skew window.
    """
    return pyotp.TOTP(secret).at(time.time() + 30)


@pytest.fixture(scope="module")
def server():
    port = free_port()
    root = Path(tempfile.mkdtemp(prefix="smsgw-sec-"))
    env = os.environ.copy()
    env["SMSGW_DB_PATH"] = str(root / "gateway.db")
    env["SMSGW_DATA_DIR"] = str(root / "data")
    env["SMSGW_LOG_DIR"] = str(root / "logs")
    env["SMSGW_MOCK_MODEM"] = "1"
    env["SMSGW_HOST"] = "127.0.0.1"
    env["SMSGW_PORT"] = str(port)
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    deadline = __import__("time").time() + 30
    while __import__("time").time() < deadline:
        if proc.poll() is not None:
            proc.terminate(); raise RuntimeError("server exited")
        try:
            opener.open(base + "/api/v1/health", timeout=2).read(); break
        except (urllib.error.URLError, OSError):
            __import__("time").sleep(0.3)
    else:
        proc.terminate(); raise TimeoutError("server not healthy")
    try:
        yield base, opener, proc
    finally:
        proc.terminate()
        try: proc.wait(timeout=8)
        except subprocess.TimeoutExpired: proc.kill()


def form(opener, path, data, expect=200):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(server_base(path), data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, r.geturl(), r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        assert e.code == expect, f"{path} -> {e.code}"
        return e.code, path, e.read().decode("utf-8")


BASE = None


def server_base(path):
    return BASE + path


def json_request(opener, path, data=None, expect=200):
    req = urllib.request.Request(server_base(path), data=None if data is None else json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        assert e.code == expect, f"{path} -> {e.code}: {e.read().decode()[:300]}"
        return e.code, json.loads(e.read().decode())


def test_setup_and_security_flow(server):
    global BASE
    base, opener, _ = server
    BASE = base
    assert form(opener, "/setup", {"username": ADMIN, "password": "weak", "password_confirm": "weak"}, 400)[0] == 400
    status, url, _ = form(opener, "/setup", {"username": ADMIN, "password": ADMIN_PW, "password_confirm": ADMIN_PW})
    assert status == 200 and url.endswith("/app")
    assert form(opener, "/setup/confirm", {"user_id": 1, "code": "123456"}, 410)[0] == 410
    assert form(opener, "/setup", {"username": "second", "password": ADMIN_PW, "password_confirm": ADMIN_PW})[1].endswith("/login")
    with opener.open(server_base("/login"), timeout=10) as resp:
        assert 'style="display:none;"' in resp.read().decode("utf-8")

    assert json_request(opener, "/app/account/totp")[1] == {"totp_enabled": False, "username": ADMIN}
    assert json_request(opener, "/app/account/totp/setup", {"current_password": "WrongPass123"}, 401)[0] == 401
    assert json_request(opener, "/app/account/totp/confirm", {"code": "123456"}, 400)[0] == 400
    status, setup = json_request(opener, "/app/account/totp/setup", {"current_password": ADMIN_PW})
    secret = setup["secret"]
    assert setup["qr_data_url"].startswith("data:image/png;base64,")
    assert json_request(opener, "/app/account/totp/confirm", {"code": "111111"}, 400)[0] == 400
    status, confirmed = json_request(opener, "/app/account/totp/confirm", {"code": pyotp.TOTP(secret).now()})
    assert status == 200
    codes = confirmed["recovery_codes"]
    assert codes and len(codes) == len(set(codes))

    form(opener, "/logout", {})
    def login_html():
        with opener.open(server_base("/login"), timeout=10) as resp:
            return resp.read().decode("utf-8")

    assert 'id="totp-block" hidden' in login_html()
    status, url, _ = form(opener, "/login", {"username": ADMIN, "password": "WrongPass123"}, 401)
    assert status == 401
    assert url.endswith("/login")
    _, _, failed_html = form(opener, "/login", {"username": ADMIN, "password": "AnotherWrong1"}, 401)
    assert '此账号已开启两步验证' in failed_html
    assert 'value="secadmin"' in failed_html
    assert form(opener, "/login", {"username": ADMIN, "password": ADMIN_PW}, 401)[0] == 401
    assert form(opener, "/login", {"username": ADMIN, "password": ADMIN_PW, "code": "000000"}, 401)[0] == 401
    assert form(opener, "/login", {"username": ADMIN, "password": ADMIN_PW, "code": next_code(secret)})[1].endswith("/app")

    other_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    json_request(other_opener, "/app/account/totp", None, 401)
    assert form(other_opener, "/login", {"username": ADMIN, "password": ADMIN_PW, "code": next_code(secret)})[1].endswith("/app")
    assert json_request(other_opener, "/app/account/totp/setup", {"current_password": ADMIN_PW}, 409)[0] == 409
    assert json_request(other_opener, "/app/account/totp/confirm", {"code": pyotp.TOTP(secret).now()}, 409)[0] == 409
    assert json_request(other_opener, "/app/account/totp/disable", {"current_password": ADMIN_PW, "code": "123456"}, 400)[0] == 400
    assert json_request(other_opener, "/app/account/totp/disable", {"current_password": "WrongPass123", "code": next_code(secret)}, 401)[0] == 401
    assert json_request(other_opener, "/app/account/totp/disable", {"current_password": ADMIN_PW, "code": next_code(secret)})[1] == {"ok": True}
    assert json_request(other_opener, "/app/account/totp")[1] == {"totp_enabled": False, "username": ADMIN}

    form(other_opener, "/logout", {})
    assert json_request(other_opener, "/app/account/totp/setup", {"current_password": ADMIN_PW}, 401)[0] == 401
    assert form(other_opener, "/login", {"username": ADMIN, "password": ADMIN_PW})[1].endswith("/app")
    _, enroll = json_request(other_opener, "/app/account/totp/setup", {"current_password": ADMIN_PW})
    secret = enroll["secret"]
    spent_code = pyotp.TOTP(secret).now()
    second_codes = json_request(other_opener, "/app/account/totp/confirm", {"code": spent_code})[1]["recovery_codes"]
    assert second_codes and len(second_codes) == len(set(second_codes))
    form(other_opener, "/logout", {})
    _, _, html = form(other_opener, "/login", {"username": ADMIN, "password": "WrongPass123"}, 401)
    assert '此账号已开启两步验证' in html
    # The code that confirmed enrollment is spent and must not log the account in.
    status, _, fail_html = form(other_opener, "/login", {"username": ADMIN, "password": ADMIN_PW, "code": spent_code}, 401)
    assert status == 401
    assert '二次验证码不正确' in fail_html
    # Re-enrolling replaced the recovery codes, so the first set is dead.
    assert form(other_opener, "/login", {"username": ADMIN, "password": ADMIN_PW, "recovery": codes[0]}, 401)[0] == 401
    assert form(other_opener, "/login", {"username": ADMIN, "password": ADMIN_PW, "code": next_code(secret)})[1].endswith("/app")
    form(other_opener, "/logout", {})
    assert form(other_opener, "/login", {"username": ADMIN, "password": ADMIN_PW, "recovery": second_codes[0]})[1].endswith("/app")
    form(other_opener, "/logout", {})
    # A recovery code is single use.
    assert form(other_opener, "/login", {"username": ADMIN, "password": ADMIN_PW, "recovery": second_codes[0]}, 401)[0] == 401
    assert form(other_opener, "/login", {"username": ADMIN, "password": ADMIN_PW, "recovery": second_codes[1]})[1].endswith("/app")


def test_app_login_reports_structured_totp_error(server):
    base, _, _ = server
    url = base + "/api/v1/auth/login"
    req = urllib.request.Request(url, data=json.dumps({"username": "missing", "password": "Whatever123"}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("expected failure")
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read().decode())
        assert payload["detail"]["error"]["code"] == "login_failed"


def test_concurrent_first_admin_setup(server):
    base, _, _ = server
    results = []
    for username in ("racer1", "racer2"):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        status, url, _ = form(opener, "/setup", {"username": username, "password": ADMIN_PW, "password_confirm": ADMIN_PW})
        results.append(url.endswith("/login"))
    assert all(results), "an existing account must redirect repeated first-run setup to login"
