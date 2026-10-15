"""Console service autostart endpoints, with systemd fully faked."""
from __future__ import annotations

import subprocess
import types

import pytest

from app import service_control


class FakeSystemctl:
    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        self.calls.append(args)
        rc = 0
        if args[0] == "is-enabled":
            rc = 0 if self.enabled else 1
        elif args[0] == "is-active":
            rc = 1  # the service never owns the process in these tests
        elif args[0] == "enable":
            self.enabled = True
        elif args[0] == "disable":
            self.enabled = False
        return subprocess.CompletedProcess(list(args), rc, "", "")


@pytest.fixture()
def fake_service(tmp_path, monkeypatch):
    unit = tmp_path / "sms-gateway.service"
    monkeypatch.setattr(service_control, "UNIT_PATH", unit)
    monkeypatch.setattr(service_control, "_systemd_present", lambda: True)
    monkeypatch.setattr(service_control, "_listen_port", lambda: 8811)
    ctl = FakeSystemctl()
    monkeypatch.setattr(service_control, "_systemctl", ctl)
    written: list[str] = []

    def fake_ensure() -> None:
        written.append(unit.read_text() if unit.exists() else "<new>")
        unit.write_text("fake unit")

    monkeypatch.setattr(service_control, "ensure_unit", fake_ensure)
    return types.SimpleNamespace(unit=unit, ctl=ctl, written=written)


def test_status_reports_manual_process(client, admin_headers, fake_service):
    fake_service.unit.write_text("fake unit")
    response = client.get("/app/admin/service", headers=admin_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["installed"] is True
    assert data["enabled"] is False
    assert data["active"] is False
    assert data["service_managed"] is False
    assert data["port"] == 8811


def test_operator_cannot_read_service(client, operator_headers):
    response = client.get("/app/admin/service", headers=operator_headers)
    assert response.status_code == 403


def test_enable_writes_unit_and_enables(client, admin_headers, fake_service):
    response = client.post(
        "/app/admin/service/autostart",
        json={"enabled": True},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert fake_service.unit.exists()
    assert ("enable", service_control.UNIT_NAME) in fake_service.ctl.calls
    assert response.json()["enabled"] is True


def test_disable_keeps_running_gateway(client, admin_headers, fake_service):
    fake_service.unit.write_text("fake unit")
    fake_service.ctl.enabled = True
    response = client.post(
        "/app/admin/service/autostart",
        json={"enabled": False},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert ("disable", service_control.UNIT_NAME) in fake_service.ctl.calls
    assert response.json()["enabled"] is False
    assert fake_service.unit.exists()


def test_disable_without_unit_is_refused(client, admin_headers, fake_service):
    response = client.post(
        "/app/admin/service/autostart",
        json={"enabled": False},
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "service_control"


def test_switch_hands_over_after_reply(client, admin_headers, fake_service, monkeypatch):
    fake_service.unit.write_text("fake unit")
    fake_service.ctl.enabled = True
    spawned: list[list[str]] = []

    def fake_popen(args, **kwargs):
        spawned.append(list(args))
        return None

    monkeypatch.setattr(
        service_control,
        "subprocess",
        types.SimpleNamespace(Popen=fake_popen, DEVNULL=-3, run=lambda *a, **k: None),
    )
    response = client.post("/app/admin/service/switch", json={}, headers=admin_headers)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert len(spawned) == 1
    script = spawned[0][-1]
    assert script.startswith("setsid") or spawned[0][0] == "setsid"
    assert "kill" in script and "systemctl start" in script
