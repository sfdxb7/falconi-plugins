"""garmoosha-push routes, mounted at /api/plugins/garmoosha-push/, behind this Hermes's own sign-in
(Falki's app calls them through HermesLink, with its bearer). Every route also runs Hermes's own guard
for sensitive routes itself (hermes_cli.web_server._require_token), so none answers without a sign-in
even if the dashboard's middleware didn't run first: 401.

Who: each phone is tied to the Hermes user who signed it up (`owner`, a hash of the sign-in's user)
and holds its own device key, handed back once by POST devices and kept here only as a SHA-256. The
phone sends it as `X-Garmoosha-Device-Key` to read its notes, answer its buttons and sign out.

  GET    status?device=            {plugin, version, relay, devices, relayKey, approvals, pid, delivery?}:
                                   that the plugin is here, the relay's host (null when it's off),
                                   whether this Hermes's relay key is readable ("ok", "none",
                                   "unreadable"), and with `device` how its last relay send went
                                   ({state: ok|pinned_elsewhere|relay_error|unreachable|
                                   relay_key_unreadable|unknown, at})
  POST   devices                   {mode: 'fcm', token, publicKey, auth, label?} or {mode: 'stream',
                                   publicKey, auth, label?} -> {device, key}: 403 for a phone another
                                   Hermes user signed up
  GET    devices?device=           {devices: [{id, label, mode, created, delivery, deliveryAt,
                                   isThisPhone}]}: this user's phones for Settings (never a push key,
                                   auth secret, FCM token or device key)
  DELETE devices/{id}              -> {ok}: signed out (with the phone's key), Always connected turned
                                   off, or removed in Settings by the user who signed it up (404 for
                                   an id that isn't one, or someone else's)
  GET    events?device=&after=     (the phone's key) the notes for a phone on its own connection (mode stream), as
                                   server-sent events: `id: <n>` and `data: {"p": <ciphertext>}`, a
                                   comment every 20 s, and the stream ends after 25 minutes (reconnect
                                   with `after`)
  POST   act                       (the phone's key) {device, token, action: approve|deny} -> {ok}: a notification's
                                   button. 409 when the question was already answered or has gone,
                                   or waits in another Hermes process (see push.py, Which process)
"""

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

_NAME = "garmoosha_push_core"
if _NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_NAME, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "push.py"))
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_NAME] = _module
    _spec.loader.exec_module(_module)
push = sys.modules[_NAME]

def require_hermes_auth(request: Request) -> None:
    """Hermes's own guard for sensitive routes, run here too (not only the dashboard's middleware).
    In a profile's plugin host (plugins.isolation: host) the parent ran that guard and passes its
    answer as `hosted_http_authenticated`. Fails closed: 401 without a sign-in, 503 if it can't check."""
    hosted = getattr(request.state, "hosted_http_authenticated", None)
    if hosted is not None:
        if hosted is True:
            return
        raise HTTPException(401, "Unauthorized")
    try:
        from hermes_cli.web_server import _require_token
    except Exception:
        raise HTTPException(503, "This Hermes can't check the sign-in for this plugin") from None
    _require_token(request)


def caller(request: Request) -> str:
    """Who is asking, as a hash: the signed-in Hermes user (OAuth gate), the profile a native app's
    ticket is for, else this Hermes's one local owner (its session token)."""
    session = getattr(request.state, "session", None)
    user = getattr(session, "user_id", None)
    if user:
        who = f"user:{getattr(session, 'provider', '')}:{getattr(session, 'issuer', '')}:{user}"
    else:
        grant = getattr(request.state, "native_http_principal", None)
        profile = grant.get("profile_id") if isinstance(grant, dict) else None
        who = f"native:{profile}" if profile else "local"
    return hashlib.sha256(who.encode()).hexdigest()


def _served() -> None:
    """Every route says this process answers Falki: approvals asked here get buttons (push.py, Which process)."""
    push.mark_served()


# The sign-in first: a request without one never marks this process as the one answering Falki.
router = APIRouter(dependencies=[Depends(require_hermes_auth), Depends(_served)])
KEY_HEADER = "x-garmoosha-device-key"
STREAM_SECONDS = 25 * 60
PING_SECONDS = 20


class Device(BaseModel):
    mode: str = Field(max_length=16)
    publicKey: str = Field(max_length=256)
    auth: str = Field(max_length=64)
    token: str | None = Field(default=None, max_length=4096)
    label: str | None = Field(default=None, max_length=60)


class Act(BaseModel):
    device: str = Field(max_length=64)
    token: str = Field(max_length=64)
    action: str = Field(max_length=16)


def _known(device_id: str) -> dict:
    found = next((d for d in push.devices() if d["id"] == device_id), None)
    if found is None:
        raise HTTPException(404, "That phone isn't signed up here")
    return found


def _usable(device_id: str, key: Optional[str], owner: str) -> dict:
    """The phone, when the caller is that phone (its key): else the same 404 as an unknown one."""
    found = _known(device_id)
    if not push.may_use(found, key, owner):
        raise HTTPException(404, "That phone isn't signed up here")
    return found


@router.get("/status")
def status(device: str | None = None) -> dict:
    relay = push.relay_url()
    out = {"plugin": "garmoosha-push", "version": push.VERSION, "relay": relay.split("://", 1)[1] if relay else None,
           "devices": len(push.devices()), "relayKey": push.relay_key_state(),
           # Approve/Deny from a note works for approvals asked in the process answering this;
           # those raised in another (a messaging gateway, cron) get a note that opens the chat.
           "approvals": "this-process", "pid": os.getpid()}
    if device is not None:
        if not push.DEVICE_ID.fullmatch(device):
            raise HTTPException(404, "That phone isn't signed up here")
        out["delivery"] = push.delivery(device)
    return out


@router.post("/devices")
def add(body: Device, owner: str = Depends(caller)) -> dict:
    try:
        device, key = push.register(body.mode, body.publicKey, body.auth, body.token, owner=owner, label=body.label)
    except ValueError:
        raise HTTPException(400, "Not a phone's push registration") from None
    except PermissionError:
        raise HTTPException(403, "Another Hermes user signed this phone up") from None
    return {"device": device, "key": key}


@router.get("/devices")
def listed(device: str | None = None, owner: str = Depends(caller)) -> dict:
    this = device if device is not None and push.DEVICE_ID.fullmatch(device) else None
    return {"devices": [push.summary(d, this) for d in push.devices() if d.get("owner") in (None, owner)]}


@router.delete("/devices/{device_id}")
def remove(device_id: str, owner: str = Depends(caller),
           key: Optional[str] = Header(default=None, alias=KEY_HEADER)) -> dict:
    found = next((d for d in push.devices() if d["id"] == device_id), None) if push.DEVICE_ID.fullmatch(device_id) else None
    if found is None or not push.may_remove(found, key, owner) or not push.forget(device_id):
        raise HTTPException(404, "That phone isn't signed up here")
    return {"ok": True}


@router.get("/events")
async def events(device: str, after: int = 0, owner: str = Depends(caller),
                 key: Optional[str] = Header(default=None, alias=KEY_HEADER)) -> StreamingResponse:
    if _usable(device, key, owner)["mode"] != "stream":
        raise HTTPException(409, "That phone gets its notifications through the relay")

    async def stream():
        last = after
        started = pinged = time.monotonic()
        yield ": connected\n\n"
        while time.monotonic() - started < STREAM_SECONDS:
            rows = await asyncio.to_thread(push.events_after, device, last)
            for row in rows:
                last = row["id"]
                yield f"id: {row['id']}\ndata: {json.dumps({'p': row['p']})}\n\n"
            if time.monotonic() - pinged >= PING_SECONDS:
                pinged = time.monotonic()
                yield ": ping\n\n"
            if not any(d["id"] == device for d in await asyncio.to_thread(push.devices)):
                return
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"cache-control": "no-store", "x-accel-buffering": "no"})


@router.post("/act")
def act(body: Act, owner: str = Depends(caller), key: Optional[str] = Header(default=None, alias=KEY_HEADER)) -> dict:
    _usable(body.device, key, owner)
    try:
        settled = push.answer(body.device, body.token, body.action)
    except ValueError:
        raise HTTPException(400, "Approve or deny only") from None
    except push.Elsewhere:
        raise HTTPException(409, "That question is waiting somewhere this can't answer. Open the chat to answer it.") from None
    if not settled:
        raise HTTPException(409, "That question was already answered, or has gone.")
    return {"ok": True}
