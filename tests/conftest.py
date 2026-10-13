"""Shared fixtures. Environment is configured before app import because
Settings is instantiated at module load time."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="smsgw-test-"))

os.environ.update(
    {
        "SMSGW_HOST": "127.0.0.1",
        "SMSGW_PORT": "8099",
        "SMSGW_DATA_DIR": str(_ROOT / "data"),
        "SMSGW_LOG_DIR": str(_ROOT / "log"),
        "SMSGW_CONFIG_DIR": str(_ROOT / "etc"),
        "SMSGW_SESSION_SECRET_FILE": str(_ROOT / "etc" / "secret.key"),
        "SMSGW_MOCK_MODEM": "true",
        "SMSGW_MOCK_MODEM_COUNT": "2",
        "SMSGW_AUDIO_PROMPT_DIR": str(_ROOT / "prompts"),
        "SMSGW_AUDIO_BACKEND": "alsa",
        # Keep CI quiet: prompts are still rendered and routed, just not played.
        "SMSGW_AUDIO_LOCAL_PROMPTS": "false",
        "SMSGW_VOICE_CLCC_POLL_INTERVAL_SECONDS": "0.2",
        "SMSGW_BCRYPT_COST": "4",
        "SMSGW_LOGIN_LOCK_MAX_ATTEMPTS": "100",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import repo_admin, security  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402

ADMIN_USER = "admin"
ADMIN_PASSWORD = "TestPass123"
OPERATOR_USER = "operator01"
OPERATOR_PASSWORD = "OperPass123"


def _ensure_accounts() -> None:
    if not repo_admin.username_taken(ADMIN_USER):
        repo_admin.create_user(
            ADMIN_USER,
            security.hash_password(ADMIN_PASSWORD),
            role="admin",
            display_name="测试管理员",
        )
    if not repo_admin.username_taken(OPERATOR_USER):
        repo_admin.create_user(
            OPERATOR_USER,
            security.hash_password(OPERATOR_PASSWORD),
            role="operator",
            display_name="测试坐席",
        )


@pytest.fixture(scope="session")
def client():
    with TestClient(fastapi_app) as test_client:
        _ensure_accounts()
        yield test_client


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


@pytest.fixture(scope="session")
def admin_token(client: TestClient) -> str:
    return _login(client, ADMIN_USER, ADMIN_PASSWORD)


@pytest.fixture(scope="session")
def admin_headers(admin_token: str) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="session")
def operator_token(client: TestClient) -> str:
    return _login(client, OPERATOR_USER, OPERATOR_PASSWORD)


@pytest.fixture(scope="session")
def operator_headers(operator_token: str) -> dict:
    return {"Authorization": f"Bearer {operator_token}"}


@pytest.fixture()
def online_device_id(client: TestClient, admin_headers: dict) -> int:
    response = client.get("/app/devices", headers=admin_headers)
    assert response.status_code == 200, response.text
    devices = [d for d in response.json()["items"] if d["status"] == "online"]
    assert devices, "expected at least one online mock device"
    return int(devices[0]["id"])


def wait_for_call_state(client, headers, device_id, wanted, timeout=8.0):
    """Poll the live call state until it reaches the wanted value."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        response = client.get("/app/voice/state", headers=headers)
        assert response.status_code == 200, response.text
        for session in response.json()["active"]:
            if session["device_id"] == device_id:
                last = session
                if session["state"] == wanted:
                    return session
        if wanted == "ended" and last is None:
            return {"state": "ended"}
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for state={wanted}, last={last}")


def clear_call(client, headers, device_id) -> None:
    """Best effort teardown so one test cannot leak a call into the next."""
    client.post(f"/app/voice/{device_id}/hangup", headers=headers)
    deadline = time.time() + 4
    while time.time() < deadline:
        response = client.get("/app/voice/state", headers=headers)
        active = [s for s in response.json()["active"] if s["device_id"] == device_id]
        if not active:
            return
        time.sleep(0.1)
