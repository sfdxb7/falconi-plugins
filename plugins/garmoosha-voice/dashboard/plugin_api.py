"""garmoosha-voice routes, mounted at /api/plugins/garmoosha-voice/.

Falki's live voice on this Hermes's own ChatGPT plan (OpenAI's ``gpt-live-1-codex``, the route the
Codex CLI uses for voice with a ChatGPT sign-in). Audio and the call's events go straight between
the browser and OpenAI over WebRTC; this plugin only trades the browser's offer for OpenAI's answer.

  GET  chatgpt?profile=        {available}: whether this Hermes is signed in to ChatGPT (signed in
                               only: whether the plan covers voice shows on the first call).
  POST chatgpt/call?profile=   {sdp, instructions?, voice?, history?} -> {call, sdp}.
                               409 when not signed in (or the sign-in has lapsed), 400 for a bad
                               offer or a voice ChatGPT doesn't have, 502 when OpenAI says no.
  GET  spoken?profile=         {available}: whether this process can file spoken exchanges.
  POST spoken?profile=         {chat, you, voice} -> {rows: [user_row, assistant_row]}. An exchange the
                               live voice answered by itself (it only ever reached OpenAI), filed in
                               the chat's Hermes session as two ordinary messages: the next turn and
                               any reload see them. Both rows say where they came from in Hermes's
                               own display metadata ({"source": "garmoosha-voice"}), which is never
                               sent to a model. 404 when the chat isn't open in this process, 409
                               while a turn runs (or another surface owns the chat), 429 past
                               SPOKEN_PER_MINUTE for one chat (or SPOKEN_PER_MINUTE_CALLER for one
                               caller), 503 when the store can't take them (nothing is kept then).

Every route runs Hermes's own guard for sensitive routes (hermes_cli.web_server._require_token) as
well as the dashboard's middleware: 401 without a sign-in. `profile` must be a Hermes profile name
(400 otherwise); bodies are bounded (422 past the limits below).

The sign-in is Hermes's own: the request goes through ``credentialed_provider_request``, which
attaches the token itself, sends it only to chatgpt.com and never follows a redirect. This file
never sees the token. ``GARMOOSHA_VOICE_CHATGPT=off`` in Hermes's environment turns the chatgpt
routes off (451), for any Hermes someone runs for other people: a ChatGPT plan is for its owner only.
(Not 401 or 403: those tell Falki its own Hermes login lapsed, and it would sign in again.)
"""

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Hermes's own profile names (hermes_constants.PROFILE_ID_RE): nothing that could reach outside its
# profiles folder, and nothing it couldn't have made.
PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


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


def valid_profile(profile: Optional[str] = Query(default=None, max_length=64)) -> Optional[str]:
    """`?profile=`, when given (Hermes routes it before this; a plugin host may strip it), as a profile name."""
    if profile is not None and not PROFILE.fullmatch(profile):
        raise HTTPException(400, "Not a profile name")
    return profile


router = APIRouter(dependencies=[Depends(require_hermes_auth), Depends(valid_profile)])

CALLS = "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
MODEL = "gpt-live-1-codex"
# The voices a gpt-live-1-codex call takes, and ChatGPT's own default: the Codex CLI's list for this
# protocol (codex-rs/protocol/src/protocol.rs, RealtimeVoicesList::builtin().v3; `quicksilver=v2` is
# its v3) and its default for ChatGPT sign-ins (codex-api/src/endpoint/realtime_call.rs). Falki's
# picker shows the same list (src/features/talk/voices.ts; a test keeps them equal).
VOICES = (
    "juniper", "maple", "spruce", "ember", "vale", "breeze", "arbor", "sol", "cove", "marin",
    "aube", "bubbie", "haetsal", "hanul", "himari", "leher", "miko", "neer", "porto", "rindo",
    "rio", "rivage", "selva", "sonna", "tinta", "viola",
)
VOICE = "cove"
CALL_ID = re.compile(r"rtc_[A-Za-z0-9_-]+|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
# A live session takes at most 128 opening messages (OpenAI's limit).
MAX_HISTORY = 128
# And at most 8,192 tokens of them in all, about 4 bytes each (the Codex CLI's limits).
MAX_HISTORY_BYTES = 32_000


def _off() -> bool:
    return os.environ.get("GARMOOSHA_VOICE_CHATGPT", "").strip().lower() in {"0", "off", "false", "no"}


def _refuse_if_off() -> None:
    if _off():
        raise HTTPException(451, "ChatGPT plan voice is turned off on this Hermes")


def _recent(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The newest messages that fit the call's limits, oldest first."""
    kept: list[dict[str, Any]] = []
    size = 0
    for item in reversed(history[-MAX_HISTORY:]):
        size += len(json.dumps(item, ensure_ascii=False).encode())
        if size > MAX_HISTORY_BYTES:
            break
        kept.append(item)
    return kept[::-1]


def _signed_in() -> bool:
    # Hermes's own status snapshot, read-only: no refresh, no adoption, no write (#68004).
    from hermes_cli.auth import get_codex_auth_status

    try:
        return bool(get_codex_auth_status().get("logged_in"))
    except Exception:  # an unreadable store is no sign-in
        return False


# The longest instructions taken at all, and how much of them reaches the call.
MAX_INSTRUCTIONS = 32_000
SENT_INSTRUCTIONS = 16_000
MAX_HISTORY_TEXT = 16_000


class HistoryPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["input_text", "output_text"]
    text: str = Field(max_length=MAX_HISTORY_TEXT)


class HistoryItem(BaseModel):
    """One opening message, as a live session takes it (src/features/talk/live-history.ts LiveMessage)."""
    model_config = ConfigDict(extra="forbid")
    type: Literal["message"]
    role: Literal["developer", "system", "user", "assistant"]
    content: list[HistoryPart] = Field(min_length=1, max_length=8)


class Call(BaseModel):
    sdp: str = Field(max_length=64_000)
    instructions: Optional[str] = Field(default=None, max_length=MAX_INSTRUCTIONS)
    voice: Optional[str] = Field(default=None, max_length=32)
    history: Optional[list[HistoryItem]] = Field(default=None, max_length=4 * MAX_HISTORY)


@router.get("/chatgpt")
def chatgpt() -> dict:
    _refuse_if_off()
    return {"available": _signed_in()}


@router.post("/chatgpt/call")
def call(body: Call) -> dict:
    _refuse_if_off()
    if not body.sdp.startswith("v=0") or len(body.sdp) > 64_000:
        raise HTTPException(400, "Not a WebRTC offer")
    voice = body.voice or VOICE
    if voice not in VOICES:
        raise HTTPException(400, "ChatGPT has no voice by that name")
    from hermes_cli.plugin_provider_requests import ProviderNotSignedIn, credentialed_provider_request

    session: dict[str, Any] = {
        "model": MODEL,
        "audio": {"output": {"voice": voice}},
        "delegation": {"type": "client"},
    }
    if body.instructions:
        session["instructions"] = body.instructions[:SENT_INSTRUCTIONS]
    if body.history:
        # On a ChatGPT plan the voice hears its opening messages, not session.input (docs/hermes/voice.md).
        session["initial_items"] = _recent([item.model_dump() for item in body.history])
    try:
        res = credentialed_provider_request(
            "openai-codex", "POST", CALLS,
            json={"sdp": body.sdp, "session": session},
            headers={"OpenAI-Alpha": "quicksilver=v2", "Accept": "application/sdp"},
        )
    except ProviderNotSignedIn:
        raise HTTPException(409, "Not signed in to ChatGPT on this Hermes") from None
    except PermissionError as exc:
        # A credential that routes to a custom endpoint, or a plugin without requires_auth. (Not 403: see above.)
        raise HTTPException(502, str(exc)) from None
    except Exception as exc:  # the network: no detail beyond its kind
        raise HTTPException(502, f"ChatGPT didn't answer ({type(exc).__name__})") from None
    if res.status == 401:
        raise HTTPException(409, "ChatGPT turned this Hermes's sign-in down: sign in again")
    if not 200 <= res.status < 300:
        raise HTTPException(502, f"ChatGPT turned the call down ({res.status})")
    answer = res.text
    location = next((v for k, v in res.headers.items() if k.lower() == "location"), "")
    call_id = urlparse(location).path.rstrip("/").rsplit("/", 1)[-1]
    if not answer.startswith("v=0") or not CALL_ID.fullmatch(call_id):
        raise HTTPException(502, "ChatGPT's answer wasn't a WebRTC answer")
    return {"call": call_id, "sdp": answer}


# --- What the live voice answers by itself --------------------------------------------------------
#
# Written the way Hermes writes its own model-switch marker (tui_gateway/server.py,
# _append_model_switch_marker, v0.23.1 and v0.23.3): into the live session's history and state.db
# together, each entry stamped with its row id and as persisted, so the next turn sees them and
# doesn't save them again. Unlike the marker, nothing is written while a turn runs, since Hermes
# treats any change to the history under a turn as a desync, and a write the store refuses is taken
# back out of memory, so the two never disagree.

MAX_SPOKEN = 8_000
# What a side may be at all (longer is clipped to MAX_SPOKEN; past this it's refused).
MAX_SPOKEN_BODY = 64_000
# Where the rows came from, in Hermes's own display metadata (a persistence-only field: stored with the
# row, shown by Hermes's own clients, never sent to a model).
SOURCE = {"source": "garmoosha-voice"}
SPOKEN_PER_MINUTE = 30
SPOKEN_PER_MINUTE_CALLER = 120
_spoken_lock = threading.Lock()
_spoken_at: dict[str, deque] = {}


class Spoken(BaseModel):
    chat: str = Field(min_length=1, max_length=256, pattern=r"^[\x21-\x7e]+$")
    you: str = Field(max_length=MAX_SPOKEN_BODY)
    voice: str = Field(max_length=MAX_SPOKEN_BODY)


def _caller(request: Request) -> str:
    session = getattr(request.state, "session", None)
    user = getattr(session, "user_id", None)
    grant = getattr(request.state, "native_http_principal", None)
    profile = grant.get("profile_id") if isinstance(grant, dict) else None
    who = f"user:{user}" if user else f"native:{profile}" if profile else "local"
    return hashlib.sha256(who.encode()).hexdigest()[:32]


def _too_many(keys: list[tuple[str, int]], now: Optional[float] = None) -> bool:
    """A sliding minute per key: True (nothing counted) when any is at its limit, else each counts one."""
    now = time.monotonic() if now is None else now
    with _spoken_lock:
        for key, _ in keys:
            seen = _spoken_at.get(key)
            while seen and now - seen[0] >= 60:
                seen.popleft()
        if any(len(_spoken_at.get(key) or ()) >= limit for key, limit in keys):
            return True
        for key, _ in keys:
            _spoken_at.setdefault(key, deque()).append(now)
        # Bounded: the oldest keys go first (a dict keeps insertion order).
        while len(_spoken_at) > 4096:
            _spoken_at.pop(next(iter(_spoken_at)))
        return False


def _gateway():
    """Hermes's chat sessions, when this process holds them; None where it can't."""
    try:
        from tui_gateway import server
    except Exception:
        return None
    needed = ("_sessions", "_sessions_lock", "_session_db", "_ensure_session_db_row")
    return server if all(hasattr(server, name) for name in needed) else None


def _open_chat(server, chat: str) -> Optional[dict]:
    """The live session whose stored id (or its agent's, after a rotation) is this chat."""
    with server._sessions_lock:
        for session in list(server._sessions.values()):
            if not isinstance(session, dict):
                continue
            agent_id = getattr(session.get("agent"), "session_id", None)
            if chat in (session.get("session_key"), agent_id):
                return session
    return None


def _target(server, session: dict) -> str:
    pick = getattr(server, "_submit_row_target_key", None)
    if pick is not None:
        return pick(session)
    return str(getattr(session.get("agent"), "session_id", None) or session.get("session_key") or "")


def _append_rows(server, session: dict, entries: list[dict]) -> list[int]:
    """Writes the entries to the session's store, oldest first: their row ids."""
    from agent.message_metadata import stamp_message_uid

    agent = session.get("agent")
    db = getattr(agent, "_session_db", None) if agent is not None else None
    if db is None:
        # A chat whose row isn't written yet (Hermes writes it on the first real activity).
        server._ensure_session_db_row(session)
    rows: list[int] = []
    with (contextlib.nullcontext(db) if db is not None else server._session_db(session)) as db:
        if db is None:
            raise RuntimeError("no session store")
        target = _target(server, session)
        persistence_for = getattr(server, "_persistence_for", None)
        with (persistence_for(db) if persistence_for else contextlib.nullcontext(None)) as persistence:
            for entry in entries:
                uid = stamp_message_uid(entry)
                if persistence is not None:  # v0.23.3: the store's own handle
                    from persistence.conversation.messages import NewMessage

                    row = persistence.messages.append(
                        target, NewMessage(role=entry["role"], content=entry["content"], message_uid=uid,
                                           display_metadata=entry.get("display_metadata")))
                elif hasattr(db, "append_message"):  # v0.23.1: SessionDB
                    row = db.append_message(
                        session_id=target, role=entry["role"], content=entry["content"], message_uid=uid,
                        display_metadata=entry.get("display_metadata"))
                else:
                    raise RuntimeError("the session store can't append")
                rows.append(int(row))
    return rows


@router.get("/spoken")
def spoken_available() -> dict:
    return {"available": _gateway() is not None}


@router.post("/spoken")
def spoken(body: Spoken, request: Request) -> dict:
    you, voice = body.you.strip(), body.voice.strip()
    if not body.chat or not you or not voice:
        raise HTTPException(400, "Both sides of the exchange are needed")
    who = _caller(request)
    chat_key = hashlib.sha256(body.chat.encode()).hexdigest()[:32]
    if _too_many([(f"chat:{who}:{chat_key}", SPOKEN_PER_MINUTE), (f"caller:{who}", SPOKEN_PER_MINUTE_CALLER)]):
        raise HTTPException(429, "Too many spoken exchanges at once. Try again in a minute.", headers={"Retry-After": "60"})
    server = _gateway()
    if server is None:
        raise HTTPException(503, "This Hermes can't file spoken exchanges")
    session = _open_chat(server, body.chat)
    if session is None:
        raise HTTPException(404, "That chat isn't open")
    from agent.context_compressor import _DB_PERSISTED_MARKER

    entries = [{"role": "user", "content": you[:MAX_SPOKEN], "display_metadata": dict(SOURCE)},
               {"role": "assistant", "content": voice[:MAX_SPOKEN], "display_metadata": dict(SOURCE)}]
    with session.get("history_lock") or contextlib.nullcontext():
        if session.get("running"):
            raise HTTPException(409, "A reply is under way")
        owner = getattr(server, "_require_transcript_owner", None)
        if owner is not None:
            try:
                owner(session)
            except Exception as exc:
                if type(exc).__name__ == "SessionNotOwned":
                    raise HTTPException(409, "Another window has this chat") from None
                raise
        history = session.setdefault("history", [])
        start = len(history)
        history.extend(entries)
        try:
            rows = _append_rows(server, session, entries)
        except Exception as exc:
            del history[start:start + len(entries)]
            logger.warning("garmoosha-voice: couldn't file a spoken exchange", exc_info=True)
            raise HTTPException(503, f"Couldn't save it ({type(exc).__name__})") from None
        for entry, row in zip(entries, rows):
            entry["_row_id"] = row
            entry[_DB_PERSISTED_MARKER] = True
        session["history_version"] = int(session.get("history_version", 0)) + 1
    return {"rows": rows}
