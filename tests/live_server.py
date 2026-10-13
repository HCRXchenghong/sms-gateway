"""Spawn a real uvicorn process so tests exercise the deployed code path.

The in-process TestClient cannot validate WebSocket fan-out or audio playback
the way a running server does, so the live suite drives an actual process over
HTTP and WSS from a separate client.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import quote
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveServer:
    def __init__(self, port: Optional[int] = None, extra_env: Optional[dict] = None):
        self.port = port or free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.ws_url = f"ws://127.0.0.1:{self.port}/ws"
        self.root = Path(tempfile.mkdtemp(prefix="smsgw-live-"))
        self.proc: Optional[subprocess.Popen] = None
        self._extra_env = extra_env or {}

    def env(self) -> dict:
        env = os.environ.copy()
        env.update(
            {
                "SMSGW_HOST": "127.0.0.1",
                "SMSGW_PORT": str(self.port),
                "SMSGW_DATA_DIR": str(self.root / "data"),
                "SMSGW_LOG_DIR": str(self.root / "log"),
                "SMSGW_CONFIG_DIR": str(self.root / "etc"),
                "SMSGW_SESSION_SECRET_FILE": str(self.root / "etc" / "secret.key"),
                "SMSGW_MOCK_MODEM": "true",
                "SMSGW_MOCK_MODEM_COUNT": "2",
                "SMSGW_AUDIO_PROMPT_DIR": str(self.root / "prompts"),
                "SMSGW_AUDIO_BACKEND": "alsa",
                "SMSGW_AUDIO_LOCAL_PROMPTS": "false",
                "SMSGW_VOICE_CLCC_POLL_INTERVAL_SECONDS": "0.2",
                "SMSGW_BCRYPT_COST": "4",
                "SMSGW_LOGIN_LOCK_MAX_ATTEMPTS": "100",
                "PYTHONPATH": str(REPO_ROOT),
                "PYTHONUNBUFFERED": "1",
            }
        )
        env.update(self._extra_env)
        return env

    def start(self, timeout: float = 60.0) -> "LiveServer":
        python = sys.executable
        self.proc = subprocess.Popen(
            [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(REPO_ROOT),
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early: {self.logs()}")
            try:
                with urllib.request.urlopen(f"{self.base_url}/api/v1/health", timeout=3) as resp:
                    if resp.status == 200:
                        return self
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.4)
        self.stop()
        raise TimeoutError(f"server did not become healthy on {self.base_url}")

    def logs(self) -> str:
        if self.proc is None or self.proc.stdout is None:
            return ""
        try:
            os.set_blocking(self.proc.stdout.fileno(), False)
            return self.proc.stdout.read() or ""
        except (OSError, ValueError):
            return ""

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    # -- convenience ----------------------------------------------------

    def request(self, method: str, path: str, *, token: str = None, body=None, expect=None):
        # Percent-encode non-ASCII query values while leaving any existing
        # escapes such as %2B intact.
        url = f"{self.base_url}{quote(path, safe='/?&=%+,')}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                status_code = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            status_code = e.code
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw
        if expect is not None:
            assert status_code == expect, f"{method} {path} -> {status_code}: {raw[:600]}"
        return status_code, payload

    def create_admin(self, username: str = "liveadmin", password: str = "LivePass123") -> str:
        """Insert an account directly so tests do not need the TOTP wizard."""
        code = (
            "import os,sys;"
            "sys.path.insert(0, os.environ['PYTHONPATH']);"
            "from app import db, repo_admin, security;"
            "db.init_db();"
            f"repo_admin.create_user({username!r}, security.hash_password({password!r}), role='admin')"
            " if not repo_admin.username_taken("
            f"{username!r}) else None;"
            "print('ok')"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=str(REPO_ROOT), env=self.env(),
            capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        return password

    def login(self, username: str, password: str) -> str:
        status, payload = self.request(
            "POST", "/api/v1/auth/login", body={"username": username, "password": password}, expect=200
        )
        return payload["token"]
