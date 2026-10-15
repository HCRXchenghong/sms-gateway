"""Boot autostart control for the gateway process.

The installer ships a systemd unit for the /opt deployment, but a checkout
run by hand has none. This module writes a unit that matches however the
gateway is running right now (working directory, user, listen port and the
SMSGW_* environment) and toggles "systemctl enable/disable" on it, so the
console switch behaves the same on both deployments.
"""
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

UNIT_NAME = "sms-gateway.service"
UNIT_DIR = Path("/etc/systemd/system")
UNIT_PATH = UNIT_DIR / UNIT_NAME
REPO = Path(__file__).resolve().parents[1]


class ServiceError(RuntimeError):
    """A user-visible refusal from the service control."""


def _systemd_present() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()


def _run(args: List[str]) -> subprocess.CompletedProcess:
    # Fixed argument lists only; nothing user supplied reaches the shell.
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run(["sudo", "-n", "systemctl", *args])


def _listen_port() -> Optional[int]:
    """The TCP port this process is serving on, read from /proc."""
    inodes = set()
    fd_dir = Path("/proc/self/fd")
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return None
    for name in names:
        try:
            target = os.readlink(fd_dir / name)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            inodes.add(target[8:-1])
    if not inodes:
        return None
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            # sl local rem st tx:rx tr:tm retrnsmt uid timeout inode
            if len(parts) < 10 or parts[3] != "0A":
                continue
            if parts[9] in inodes:
                return int(parts[1].rsplit(":", 1)[1], 16)
    return None


def _unit_text(port: int) -> str:
    user = pwd.getpwuid(os.getuid()).pw_name
    exec_start = REPO / ".venv" / "bin" / "uvicorn"
    env_lines = [
        'Environment="%s=%s"' % (key, os.environ[key].replace('"', '\\"'))
        for key in sorted(os.environ)
        if key.startswith("SMSGW_")
    ]
    env_block = "\n".join(env_lines)
    return (
        "[Unit]\n"
        "Description=VoiceSMS 4G voice and SMS gateway\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        f"WorkingDirectory={REPO}\n"
        + (env_block + "\n" if env_block else "")
        + f"ExecStart={exec_start} app.main:app --host 0.0.0.0 --port {port}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def ensure_unit() -> None:
    """Write (or refresh) the unit so it matches the running deployment."""
    port = _listen_port()
    if port is None:
        raise ServiceError("读不到当前监听端口，无法生成服务单元")
    text = _unit_text(port)
    with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as handle:
        handle.write(text)
        tmp = handle.name
    try:
        for args in (
            ["sudo", "-n", "cp", tmp, str(UNIT_PATH)],
            ["sudo", "-n", "chown", "root:root", str(UNIT_PATH)],
            ["sudo", "-n", "chmod", "644", str(UNIT_PATH)],
        ):
            proc = _run(args)
            if proc.returncode != 0:
                raise ServiceError((proc.stderr or proc.stdout).strip() or "写入服务单元失败")
    finally:
        os.unlink(tmp)
    proc = _systemctl("daemon-reload")
    if proc.returncode != 0:
        raise ServiceError((proc.stderr or proc.stdout).strip() or "systemd 重载失败")


def status() -> dict:
    info = {
        "systemd": _systemd_present(),
        "unit": UNIT_NAME,
        "unit_path": str(UNIT_PATH),
        "installed": UNIT_PATH.exists(),
        "enabled": False,
        "active": False,
        "service_managed": False,
        "pid": os.getpid(),
        "port": _listen_port(),
        "user": pwd.getpwuid(os.getuid()).pw_name,
        "workdir": str(REPO),
        "error": None,
    }
    if not info["systemd"]:
        info["error"] = "这台主机没有运行 systemd，无法设置开机自启"
        return info
    info["enabled"] = _systemctl("is-enabled", UNIT_NAME).returncode == 0
    info["active"] = _systemctl("is-active", UNIT_NAME).returncode == 0
    info["service_managed"] = info["active"]
    return info


def set_autostart(enabled: bool) -> dict:
    if not _systemd_present():
        raise ServiceError("这台主机没有运行 systemd，无法设置开机自启")
    if enabled:
        ensure_unit()
        proc = _systemctl("enable", UNIT_NAME)
    else:
        if not UNIT_PATH.exists():
            raise ServiceError("服务单元不存在，先开启一次自启再关闭")
        proc = _systemctl("disable", UNIT_NAME)
    if proc.returncode != 0:
        raise ServiceError((proc.stderr or proc.stdout).strip() or "systemctl 执行失败")
    return status()


def switch_to_service(delay_seconds: float = 2.0) -> dict:
    """Hand the running gateway over to systemd.

    The handoff script outlives this process: it waits for the HTTP reply to
    land, ends the manual process and starts the unit, so the port never has
    two owners.
    """
    if not _systemd_present():
        raise ServiceError("这台主机没有运行 systemd，无法切换到服务运行")
    if not UNIT_PATH.exists():
        ensure_unit()
    if _systemctl("is-enabled", UNIT_NAME).returncode != 0:
        proc = _systemctl("enable", UNIT_NAME)
        if proc.returncode != 0:
            raise ServiceError((proc.stderr or proc.stdout).strip() or "systemctl 执行失败")
    script = (
        f"sleep {delay_seconds:g}; kill {os.getpid()}; sleep 1; "
        f"sudo -n systemctl start {UNIT_NAME}"
    )
    subprocess.Popen(
        ["setsid", "/bin/sh", "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "unit": UNIT_NAME}
