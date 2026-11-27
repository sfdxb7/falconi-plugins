"""What Falconi asks of an agent's email account, per agent (``?profile=``):

  GET  /api/plugins/garmoosha-mail/status   whether the agent has email set up, and with what (never the password)
  POST /api/plugins/garmoosha-mail/check    sign in to both servers and bring back the three newest messages

Falconi saves the account itself, key by key, with Hermes's ``PUT /api/env`` (see ../mail.py for
the keys), so this only reads it, from the requested agent's own secret scope. A check takes the saved account,
or the one in its body: with ``address`` the body is the whole account, without it the body's
fields (say, a new ``password``) go over the saved ones. A check's failure is an answer, not an
error: ``{"ok": false, "error": {"kind", "where", "detail"}}`` with ``kind`` one of incomplete,
wrong_password, app_password, unreachable, tls or unknown. The password never comes back.
"""

from __future__ import annotations

import asyncio
from hashlib import sha256
from importlib import import_module
import logging
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Optional, Union

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

log = logging.getLogger(__name__)
router = APIRouter()


def _package() -> str:
    # Hermes loads dashboard routers by path, outside the plugin's package.
    # Give each installation and release a package so a reload uses the current mail types.
    root = Path(__file__).resolve().parent.parent
    identity = str(root).encode() + b"\0" + (root / "plugin.yaml").read_bytes()
    name = "garmoosha_mail_dashboard_" + sha256(identity).hexdigest()[:16]
    package = ModuleType(name)
    package.__path__ = [str(root)]
    sys.modules.setdefault(name, package)
    return name


_name = _package()
mail = import_module(f"{_name}.mail")
review = import_module(f"{_name}.review_api")
gmail = import_module(f"{_name}.gmail")

# One check at a time per agent: each is a real sign-in, and providers lock out bursts of them.
_locks: dict[str, asyncio.Lock] = {}
CHECK_SECONDS = 60


def _home() -> str:
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home())
    except Exception:
        return ""


def _enabled() -> bool:
    """Whether this agent has the plugin switched on (its tools need that)."""
    try:
        from hermes_cli.config import load_config_readonly

        plugins = (load_config_readonly() or {}).get("plugins") or {}
    except Exception:
        return False
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    disabled = plugins.get("disabled") if isinstance(plugins, dict) else None
    on = isinstance(enabled, list) and "garmoosha-mail" in enabled
    off = isinstance(disabled, list) and "garmoosha-mail" in disabled
    return on and not off


def _saved() -> tuple[dict[str, Any], bool]:
    return mail.active_account_settings()


@router.get("/status")
async def status():
    values, inherited = _saved()
    account, missing = mail.account_from(values)
    return {
        "plugin": "garmoosha-mail",
        "version": mail.VERSION,
        "enabled": await asyncio.to_thread(_enabled),
        "configured": account is not None,
        "account": account.public() if account else None,
        # What's left to set, when some of it is (empty for an agent without email).
        "missing": missing if values else [],
        # Another agent's account, copied into this agent's .env when Hermes made it: unused, and
        # Falconi clears it away (its keys are the same GARMOOSHA_MAIL_ ones).
        "inherited": inherited,
        "workspace_gmail": gmail.available(),
    }


@router.post("/workspace/check")
async def check_workspace_gmail():
    lock = _locks.setdefault(_home(), asyncio.Lock())
    async with lock:
        try:
            address = await asyncio.wait_for(asyncio.to_thread(gmail.connected_address), timeout=30)
        except mail.MailError as exc:
            return {"ok": False, "error": exc.public()}
        except asyncio.TimeoutError:
            return _failed("unreachable", "gmail", "Gmail took too long to answer.")
    return {"ok": True, "address": address}


class CheckRequest(BaseModel):
    address: Optional[str] = None
    password: Optional[str] = None
    username: Optional[str] = None
    name: Optional[str] = None
    provider: Optional[str] = None
    imap_host: Optional[str] = None
    imap_port: Optional[Union[int, str]] = None
    imap_security: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[Union[int, str]] = None
    smtp_security: Optional[str] = None
    save_sent: Optional[Union[bool, str]] = None

    def values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for field, value in self.model_dump().items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = "1" if value else "0"
            out[field.upper()] = str(value)
        return out


def _failed(kind: str, where: str = "", detail: str = "", **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": {"kind": kind, "where": where, "detail": detail, **extra}}


@router.post("/check")
async def check(body: Optional[CheckRequest] = None):
    given = body.values() if body else {}
    given.pop("AGENT", None)
    values = given if "ADDRESS" in given else {**_saved()[0], **given}
    account, missing = mail.account_from(values)
    if account is None:
        return _failed("incomplete", missing=missing)
    lock = _locks.setdefault(_home(), asyncio.Lock())
    async with lock:
        try:
            return await asyncio.wait_for(asyncio.to_thread(mail.check, account), timeout=CHECK_SECONDS)
        except asyncio.TimeoutError:
            return _failed("unreachable", "imap", "The server took too long to answer.")
        except mail.MailError as exc:
            return {"ok": False, "error": exc.public()}
        except Exception as exc:  # anything else is still an answer, never the password
            log.warning("garmoosha-mail check failed: %s", type(exc).__name__)
            return {"ok": False, "error": mail.classify(exc, "imap", account).public()}


async def _review_action(handler, body: dict[str, Any]):
    try:
        return await asyncio.to_thread(handler, body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/review")
async def list_reviews(before: Optional[str] = None, include_actions: bool = False):
    return await _review_action(review.list_proposals, {"before": before, "include_actions": include_actions})


@router.get("/review/source")
async def review_source(id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return await asyncio.wait_for(_review_action(review.preview_source, {"id": id}), timeout=30)
    except asyncio.TimeoutError:
        return {"ok": False, "reason": "read_failed"}


@router.post("/review/edit")
async def edit_review(body: dict[str, Any]):
    return await _review_action(review.edit_proposal, body)


@router.post("/review/decide")
async def decide_review(body: dict[str, Any]):
    return await _review_action(review.decide_proposal, body)


@router.post("/review/send")
async def send_reviews(body: dict[str, Any]):
    return await _review_action(review.send_proposals, body)
