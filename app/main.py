"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import db, repo
from .config import settings
from .hub import hub
from .modem_manager import ModemManager
from .pdu import IncomingSms
from .routes_api import router as api_router
from .routes_web import router as web_router

log = logging.getLogger("smsgw")


def _configure_logging() -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        fh = TimedRotatingFileHandler(
            settings.log_path, when="midnight", backupCount=14, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except (OSError, PermissionError) as e:
        log.warning("file logging disabled: %s", e)


async def _on_sms(device_id: int, sms: IncomingSms) -> None:
    """Persist an incoming SMS and broadcast over websocket."""
    msg_id = await asyncio.to_thread(repo.insert_inbox, device_id, sms)
    device = await asyncio.to_thread(repo.get_device, device_id)
    device_payload = repo.serialize_device(device) if device else None
    await hub.publish(
        "sms.received",
        {
            "id": msg_id,
            "device_id": device_id,
            "device": device_payload,
            "sender": sms.sender,
            "content": sms.content,
            "received_at": sms.received_at,
            "decode_error": sms.decode_error,
        },
    )
    await asyncio.to_thread(
        repo.audit, "system", device_id, "modem", None, "sms.received", str(msg_id), 200
    )
    log.info(
        "sms received id=%d device=%s from=%s len=%d",
        msg_id,
        device_id,
        sms.sender,
        len(sms.content),
    )


async def _retention_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(repo.cleanup_expired_messages, settings.message_retention_days)
            deleted = result["inbox_deleted"] + result["outbox_deleted"]
            if deleted:
                log.info("message retention cleanup deleted %d rows", deleted)
        except Exception as e:
            log.warning("message retention cleanup failed: %s", e)
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    db.init_db()
    await asyncio.to_thread(repo.cleanup_expired_messages, settings.message_retention_days)
    modems = ModemManager(on_sms=_on_sms)
    app.state.modem = modems
    app.state.modems = modems
    task = asyncio.create_task(modems.run(), name="modem-manager")
    retention_task = asyncio.create_task(_retention_loop(), name="message-retention")
    log.info("sms-gateway started (mock=%s)", settings.mock_modem)
    try:
        yield
    finally:
        modems.stop()
        task.cancel()
        retention_task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await retention_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="SMS Gateway", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type", "Accept"],
)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

app.include_router(web_router)
app.include_router(api_router)
