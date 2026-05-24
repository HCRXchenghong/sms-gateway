"""FastAPI application entrypoint.

Wires together: DB init, modem worker, web routes, API routes, websocket hub.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import db, repo
from .config import settings
from .hub import hub
from .modem import Modem
from .modem_mock import MockModem
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
    # Console
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    # File
    try:
        fh = TimedRotatingFileHandler(
            settings.log_path, when="midnight", backupCount=14, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except (OSError, PermissionError) as e:
        log.warning("file logging disabled: %s", e)


async def _on_sms(sms: IncomingSms) -> None:
    """Persist an incoming SMS and broadcast over websocket."""
    msg_id = await asyncio.to_thread(repo.insert_inbox, sms)
    await hub.publish(
        "sms.received",
        {
            "id": msg_id,
            "sender": sms.sender,
            "content": sms.content,
            "received_at": sms.received_at,
            "decode_error": sms.decode_error,
        },
    )
    await asyncio.to_thread(
        repo.audit, "system", None, "modem", None, "sms.received", str(msg_id), 200
    )
    log.info("sms received id=%d from=%s len=%d", msg_id, sms.sender, len(sms.content))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    db.init_db()
    if settings.mock_modem:
        modem = MockModem(on_sms=_on_sms)
    else:
        modem = Modem(on_sms=_on_sms)
    app.state.modem = modem
    task = asyncio.create_task(modem.run(), name="modem")
    log.info("sms-gateway started (mock=%s)", settings.mock_modem)
    try:
        yield
    finally:
        modem.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="SMS Gateway", version="0.1.0", lifespan=lifespan)

# Static & template content
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

app.include_router(web_router)
app.include_router(api_router)
