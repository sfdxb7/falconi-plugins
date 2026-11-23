"""garmoosha-push's core, shared by its hooks (in the process that runs the agent) and its routes (in
the dashboard's): both may be separate processes, so everything lives in files under Hermes's root,
mode 600, changed under one lock.

  <hermes root>/garmoosha-push/
    devices.json     the phones: {id, mode: fcm|stream, token (fcm), publicKey, auth, created, label,
                     owner (a hash of who signed it up: the Hermes sign-in's user), keyHash (SHA-256 of
                     the phone's own device key, handed back once at sign-up), delivery (how the last
                     relay send went), deliveryAt}
    acts.json        Approve/Deny tokens, by their SHA-256 only (the raw token goes to the phone, sealed
                     to its key, and is never kept here): one per ask per phone, five minutes, spent
                     on first use
    relay-key.pem    this Hermes's Ed25519 key for the push relay (made once when absent, written whole;
                     never replaced when unreadable: /status says so), never sent
    events/<id>      the notes waiting for a phone on its own connection (mode stream), newest 50

Nothing here logs a note, a token or a key; a note leaves this file only encrypted to its phone.

Settings: the relay is ``plugins.entries.garmoosha-push.settings.relay`` in Hermes's config, else
GARMOOSHA_PUSH_RELAY, else https://push.alfalasi.io. ``off`` keeps every note on the app's own
connection. https only (http just for a relay on this machine, for tests).
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)

VERSION = "1.1.0"
DEFAULT_RELAY = "https://push.alfalasi.io"
MAX_DEVICES = 20
KEEP_EVENTS = 50
ACT_SECONDS = 300
TOKEN = re.compile(r"[A-Za-z0-9_:.-]{20,4096}")
DEVICE_KEY = re.compile(r"[A-Za-z0-9_-]{43}")
LABEL = re.compile(r"[^\x00-\x1f\x7f]{1,60}")
B64 = re.compile(r"[A-Za-z0-9_-]+")
# Chats that already reach the person where they are: a reply there needs no push.
MESSAGING = frozenset({
    "telegram", "discord", "slack", "whatsapp", "signal", "matrix", "email", "sms", "mattermost",
    "homeassistant", "dingtalk", "feishu", "wecom", "weixin", "qqbot", "bluebubbles", "ntfy", "raft",
})

_settings: dict[str, str] = {}
_lock = threading.Lock()
_replies: dict[str, str] = {}
_sender = ThreadPoolExecutor(max_workers=2, thread_name_prefix="garmoosha-push")
_home_override: Optional[Path] = None


def configure(*, relay: Optional[str] = None, home: Optional[Path] = None) -> None:
    global _home_override
    if relay is not None:
        _settings["relay"] = relay
    if home is not None:
        _home_override = Path(home)


# ── Files ───────────────────────────────────────────────────────────────────

def state_dir() -> Path:
    if _home_override is not None:
        root = _home_override
    else:
        try:
            from hermes_constants import get_default_hermes_root
            root = Path(get_default_hermes_root())
        except Exception:
            root = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    path = root / "garmoosha-push"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    (path / "events").mkdir(mode=0o700, exist_ok=True)
    return path


@contextlib.contextmanager
def locked() -> Iterator[Path]:
    folder = state_dir()
    with _lock, open(folder / ".lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield folder
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data: Any) -> None:
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out)
    os.replace(temp, path)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    if not isinstance(text, str) or not B64.fullmatch(text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ── Phones ──────────────────────────────────────────────────────────────────

def _receiver(public_key: str, auth: str) -> tuple[ec.EllipticCurvePublicKey, bytes]:
    raw = _unb64(public_key)
    secret = _unb64(auth)
    if len(raw) != 65 or raw[0] != 4 or len(secret) != 16:
        raise ValueError("not a push key")
    # from_encoded_point refuses a point that isn't on P-256.
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw), secret


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _label(label: Optional[str], mode: str) -> str:
    if isinstance(label, str) and LABEL.fullmatch(label.strip() or "-") and label.strip():
        return label.strip()
    return "Android phone" if mode == "fcm" else "Android phone (always connected)"


def register(mode: str, public_key: str, auth: str, token: Optional[str] = None, *, owner: Optional[str] = None,
             label: Optional[str] = None) -> tuple[str, str]:
    """Sign a phone up (or update it: the same key and way is the same phone). Returns its id and a fresh
    device key: the phone's own proof for /events, /act and its sign-out, kept here only as a SHA-256.
    PermissionError when that phone was signed up by another Hermes user (`owner`)."""
    if mode not in ("fcm", "stream"):
        raise ValueError("mode")
    _receiver(public_key, auth)
    if mode == "fcm" and not (isinstance(token, str) and TOKEN.fullmatch(token)):
        raise ValueError("token")
    key = _b64(secrets.token_bytes(32))
    with locked() as folder:
        devices = _read(folder / "devices.json", [])
        for device in devices:
            if device["publicKey"] == public_key and device["mode"] == mode:
                if device.get("owner") and owner and device["owner"] != owner:
                    raise PermissionError("another user's phone")
                if device.get("token") != (token if mode == "fcm" else None):
                    # A new FCM token hasn't been sent to yet: the last send's result was the old one's.
                    device.pop("delivery", None)
                    device.pop("deliveryAt", None)
                device.update(auth=auth, token=token if mode == "fcm" else None, keyHash=_sha(key),
                              label=_label(label, mode), **({"owner": owner} if owner else {}))
                _write(folder / "devices.json", devices)
                return device["id"], key
        # A token now on another key is that phone's new registration: the old one goes.
        if mode == "fcm":
            devices = [d for d in devices if d.get("token") != token]
        device = {"id": _b64(secrets.token_bytes(16)), "mode": mode, "token": token if mode == "fcm" else None,
                  "publicKey": public_key, "auth": auth, "created": int(time.time()), "label": _label(label, mode),
                  "keyHash": _sha(key), **({"owner": owner} if owner else {})}
        devices = (devices + [device])[-MAX_DEVICES:]
        _write(folder / "devices.json", devices)
        return device["id"], key


def holds_key(device: dict, key: Optional[str]) -> bool:
    """Whether `key` is this phone's own device key. A phone signed up before keys (no keyHash) holds none."""
    want = device.get("keyHash")
    return bool(want and isinstance(key, str) and DEVICE_KEY.fullmatch(key)
                and hmac.compare_digest(_sha(key), want))


def may_use(device: dict, key: Optional[str], owner: Optional[str]) -> bool:
    """Who may read a phone's notes or answer its buttons: the phone itself (its key). One signed up
    before device keys, until it signs up again (every app start does): the user who signed it up."""
    if device.get("keyHash"):
        return holds_key(device, key)
    return device.get("owner") in (None, owner)


def may_remove(device: dict, key: Optional[str], owner: Optional[str]) -> bool:
    """Who may remove a phone: the phone itself (signing out), or the Hermes user who signed it up
    (Settings → Notifications → Registered phones). Never another user, blind."""
    return holds_key(device, key) or device.get("owner") in (None, owner)


def summary(device: dict, this: Optional[str] = None) -> dict:
    """What Settings may show of a phone: never its push key, auth secret, FCM token or device key."""
    return {"id": device["id"], "label": device.get("label") or _label(None, device["mode"]), "mode": device["mode"],
            "created": device.get("created"), "delivery": device.get("delivery", "unknown"),
            "deliveryAt": device.get("deliveryAt"), "isThisPhone": bool(this) and device["id"] == this}


DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{22}")


def forget(device_id: str) -> bool:
    """A phone signed out (or Always connected off). False, and nothing rewritten, for an id that isn't one here."""
    if not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id):
        return False
    with locked() as folder:
        devices = _read(folder / "devices.json", [])
        kept = [d for d in devices if d["id"] != device_id]
        if len(kept) == len(devices):
            return False
        _write(folder / "devices.json", kept)
        acts = {k: v for k, v in _read(folder / "acts.json", {}).items() if v.get("device") != device_id}
        _write(folder / "acts.json", acts)
        with contextlib.suppress(OSError):
            (folder / "events" / device_id).unlink()
        return len(kept) != len(devices)


def devices() -> list[dict]:
    with locked() as folder:
        return _read(folder / "devices.json", [])


def _delivered(device_id: str, state: str) -> None:
    """How the last note to this phone went ("ok", "pinned_elsewhere", "relay_error", "unreachable",
    "relay_key_unreadable"), for /status: the phone's Settings says so instead of claiming it works."""
    with locked() as folder:
        devices = _read(folder / "devices.json", [])
        for device in devices:
            if device["id"] == device_id:
                if device.get("delivery") == state:
                    return
                device["delivery"] = state
                device["deliveryAt"] = int(time.time())
                _write(folder / "devices.json", devices)
                return


def delivery(device_id: str) -> Optional[dict]:
    """{state, at} for one phone: "unknown" before its first note through the relay."""
    found = next((d for d in devices() if d["id"] == device_id), None)
    if found is None:
        return None
    return {"state": found.get("delivery", "unknown"), "at": found.get("deliveryAt")}


# ── Encryption (RFC 8291, aes128gcm, one record) ───────────────────────────

def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(plain: bytes, public_key: str, auth: str) -> bytes:
    receiver, secret = _receiver(public_key, auth)
    sender = ec.generate_private_key(ec.SECP256R1())
    shared = sender.exchange(ec.ECDH(), receiver)
    ua = receiver.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    as_ = sender.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ikm = _hkdf(secret, shared, b"WebPush: info\x00" + ua + as_, 32)
    salt = secrets.token_bytes(16)
    key = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    record = AESGCM(key).encrypt(nonce, plain + b"\x02", None)
    return salt + (4096).to_bytes(4, "big") + bytes([len(as_)]) + as_ + record


# ── The relay ───────────────────────────────────────────────────────────────

def relay_url() -> Optional[str]:
    raw = (_settings.get("relay") or os.environ.get("GARMOOSHA_PUSH_RELAY") or DEFAULT_RELAY).strip().rstrip("/")
    if raw.lower() in ("off", "none", "0", "false"):
        return None
    url = urlparse(raw)
    local = url.hostname in ("127.0.0.1", "localhost", "::1")
    if url.scheme != "https" and not (url.scheme == "http" and local):
        log.warning("garmoosha-push: the relay must be an https:// address; pushes through it are off")
        return None
    if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        log.warning("garmoosha-push: the relay must be a bare origin; pushes through it are off")
        return None
    return f"{url.scheme}://{url.netloc}"


class RelayKeyError(Exception):
    """relay-key.pem is there but can't be read or isn't an Ed25519 key. It is never replaced by itself:
    the relay pins every phone's token to this key, so a new one would strand them all (403)."""


def _load_key(path: Path) -> ed25519.Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except FileNotFoundError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise RelayKeyError(type(exc).__name__) from None
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise RelayKeyError("not Ed25519")
    return key


def relay_key() -> ed25519.Ed25519PrivateKey:
    """This Hermes's key for the relay: made once, when there is none, written whole (temp + rename, 600)."""
    with locked() as folder:
        path = folder / "relay-key.pem"
        try:
            return _load_key(path)
        except FileNotFoundError:
            pass
        key = ed25519.Ed25519PrivateKey.generate()
        temp = path.with_name(path.name + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        # The rename is durable only once the directory is: else a power cut could bring back "none".
        folder_fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(folder_fd)
        finally:
            os.close(folder_fd)
        return key


def relay_key_state() -> str:
    """"ok", "none" (made on the first send) or "unreadable" (sends stop until someone fixes the file)."""
    with locked() as folder:
        try:
            _load_key(folder / "relay-key.pem")
            return "ok"
        except FileNotFoundError:
            return "none"
        except RelayKeyError:
            return "unreadable"


def signed_request(body: bytes, key: ed25519.Ed25519PrivateKey, now: Optional[float] = None) -> dict[str, str]:
    """The relay's headers for `body` (relay/protocol.mjs)."""
    stamp = str(int(now if now is not None else time.time()))
    nonce = _b64(secrets.token_bytes(24))
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    canonical = f"falki-push-v1\nPOST\n/v1/send\n{stamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode()
    return {"content-type": "application/json", "x-falki-key": _b64(public), "x-falki-time": stamp,
            "x-falki-nonce": nonce, "x-falki-signature": _b64(key.sign(canonical))}


def _to_relay(device: dict, sealed: str, urgent: bool) -> None:
    base = relay_url()
    if base is None:
        return
    import httpx

    body = json.dumps({"token": device["token"], "p": sealed, "urgency": "high" if urgent else "normal"},
                      separators=(",", ":")).encode()
    try:
        key = relay_key()
    except RelayKeyError:
        log.error("garmoosha-push: relay-key.pem can't be read; pushes through the relay are off until it's fixed "
                  "(restore it, or remove it and sign the phones in again)")
        _delivered(device["id"], "relay_key_unreadable")
        return
    try:
        res = httpx.post(f"{base}/v1/send", content=body, headers=signed_request(body, key),
                         timeout=15, follow_redirects=False)
    except Exception as exc:  # the network: its kind only
        log.warning("garmoosha-push: the relay didn't answer (%s)", type(exc).__name__)
        _delivered(device["id"], "unreachable")
        return
    if res.status_code == 410:
        forget(device["id"])
    elif res.status_code == 202:
        _delivered(device["id"], "ok")
    elif res.status_code == 403:
        # The relay has this phone's token pinned to another Hermes's key: the phone must sign up again
        # with a fresh token (signing out of that Hermes does it).
        log.warning("garmoosha-push: the relay says this phone's token belongs to another Hermes (403)")
        _delivered(device["id"], "pinned_elsewhere")
    else:
        log.warning("garmoosha-push: the relay turned a push down (%s)", res.status_code)
        _delivered(device["id"], "relay_error")


# ── Notes ───────────────────────────────────────────────────────────────────

def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def first_line(markdown: str) -> str:
    """A reply's first line as plain words (shared/notes.ts firstLine, in short)."""
    prose = re.sub(r"```[\s\S]*?(?:```|$)", "\n", markdown or "")
    for raw in prose.split("\n"):
        line = re.sub(r"^\s*(?:#{1,6}\s+|>\s?|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)", "", raw)
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"\*\*|__|~~|`", "", line).replace("|", " ")
        line = re.sub(r"\s+", " ", line).strip()
        if line and not re.fullmatch(r"[-*_=:\s]+", line):
            return _clip(line, 140)
    return ""


def _profile() -> str:
    try:
        from hermes_constants import get_hermes_home, profile_name_for_home
        return profile_name_for_home(get_hermes_home()) or "default"
    except Exception:
        return "default"


def _title(profile: str) -> str:
    return "Falki" if profile == "default" else _clip(profile, 60)


ASK_ROOM = 140 - len("Wants to run “”")


def asked(command: str, keys: list[str]) -> tuple[str, bool]:
    """What an approval asks, in a line (shared/notes.ts askText for 'approval'), and whether the note
    may carry Approve once: only when that line shows the whole command and the command is a low-risk,
    read-only one (low_risk). As Hermes's own cards are Deny-only until they show the whole command
    (tools/approval.py resolve_gateway_approval_card), a clipped, multi-line, risky or "Needs your OK"
    note only opens the chat, where the whole command is and the person reviews it."""
    raw = command or ""
    command = raw.strip()
    if (not command or re.fullmatch(r"<[^<>]+>(?: \([^()]*\))?", command) or command.startswith("MCP tool '")
            or any(k == "mcp_elicitation" or str(k).startswith("plugin_rule:") for k in keys)):
        return "Needs your OK", False
    first, *more = command.split("\n")
    shown = f"{first.rstrip()}…" if more else command
    whole = not more and len(command) <= ASK_ROOM and not _unseen(raw)  # raw: strip() eats \v, \f, \x85
    return f"Wants to run “{_clip(shown, ASK_ROOM)}”", whole and low_risk(command)


# Approve once from a notification, for these commands only: read-only programs, without shell syntax,
# options that write or run something else, or a path that names a secret. The same rules as
# shared/notes.ts lowRiskCommand and PushRules.lowRisk; test/fixtures/approve-from-note.json holds all
# three to the same answers. Anything else opens the chat.
SHELL_SYNTAX = re.compile(r"[;&|`$<>(){}\\'\"]")
READ_ONLY: dict[str, tuple[str, ...]] = {
    "ls": (), "pwd": (), "cat": (), "head": (), "tail": (), "wc": (), "grep": (), "egrep": (), "fgrep": (),
    "rg": ("--pre",), "find": ("-delete", "-exec", "-ok", "-fprint", "-fls"), "du": (), "df": (), "stat": (),
    "echo": (), "whoami": (), "id": (), "uname": (), "uptime": (), "ps": (), "free": (), "which": (),
    "cut": (), "diff": (), "cmp": (), "md5sum": (), "sha1sum": (), "sha256sum": (), "jq": (),
    "lsblk": (), "lscpu": (), "nproc": (),
    "journalctl": ("--vacuum", "--rotate", "--flush", "--relinquish", "--setup-keys", "--update-catalog"),
    "git": ("--output", "--ext-diff", "--textconv"),
}
GIT_READS = frozenset({"status", "log", "diff", "show", "blame", "ls-files", "rev-parse", "describe", "shortlog"})
SENSITIVE = (".ssh", ".gnupg", ".aws", ".kube", ".netrc", ".env", "shadow", "id_rsa", "id_ed25519", "id_ecdsa",
             "credential", "auth.json", ".pem", "secret", "token", "passw")


def low_risk(command: str) -> bool:
    if not command or any(not " " <= c <= "~" for c in command) or SHELL_SYNTAX.search(command):
        return False
    words = command.split(" ")
    words = [w for w in words if w]
    if not words or words[0] not in READ_ONLY:
        return False
    lowered = command.lower()
    if any(s in lowered for s in SENSITIVE):
        return False
    if words[0] == "git" and (len(words) < 2 or words[1] not in GIT_READS):
        return False
    refused = READ_ONLY[words[0]]
    return not any(w.startswith(r) for w in words[1:] for r in refused)


def _unseen(command: str) -> bool:
    """Whether the command has a character that isn't shown as itself: a control (Cc: tab, \\v, \\f,
    NEL...) or a format character (Cf: bidi overrides and isolates, zero-width), which can make the note
    read differently from what runs. Also line and paragraph separators. Plain space is fine."""
    return any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in command)


def _digest(command: Optional[str]) -> str:
    return hashlib.sha256((command or "").encode()).hexdigest()


def act_key(token: str) -> str:
    """How acts.json keys a button's token: its SHA-256, never the token itself."""
    return hashlib.sha256(token.encode()).hexdigest()


def deliver(note: dict, *, ask: Optional[dict] = None) -> int:
    """Encrypt `note` to every phone and send it on its way. `ask` {session_key, request_id, command}
    gives each phone its own Approve/Deny token when it has a request_id. The tokens are saved before
    any note leaves, so a tap the moment it lands finds its token. Returns how many phones it went to."""
    phones = devices()
    if not phones:
        return 0
    now = int(time.time())
    acts: dict[str, dict] = {}
    outgoing: list[tuple[dict, str]] = []
    for device in phones:
        payload = {"v": 1, "id": _b64(secrets.token_bytes(9)), "at": now * 1000, "device": device["id"], **note}
        if ask is not None:
            payload["ask"] = True
            if ask.get("request_id"):
                token = _b64(secrets.token_bytes(18))
                # Only its hash is kept: acts.json alone can't answer anything.
                acts[act_key(token)] = {"device": device["id"], "session_key": ask["session_key"],
                               "request_id": ask["request_id"], "command": _digest(ask.get("command")),
                               "pid": os.getpid(), "until": now + ACT_SECONDS}
                payload["act"] = token
        sealed = _b64(encrypt(json.dumps(payload, separators=(",", ":")).encode(), device["publicKey"], device["auth"]))
        outgoing.append((device, sealed))
    if acts:
        with locked() as folder:
            kept = {k: v for k, v in _read(folder / "acts.json", {}).items() if v.get("until", 0) > now}
            kept.update(acts)
            _write(folder / "acts.json", kept)
    for device, sealed in outgoing:
        if device["mode"] == "fcm":
            _sender.submit(_to_relay, device, sealed, ask is not None)
        else:
            _file_event(device["id"], sealed)
    return len(phones)


def _file_event(device_id: str, sealed: str) -> None:
    with locked() as folder:
        path = folder / "events" / device_id
        rows = _read(path, [])
        last = rows[-1]["id"] if rows else 0
        rows = (rows + [{"id": max(last + 1, time.time_ns() // 1000), "p": sealed}])[-KEEP_EVENTS:]
        _write(path, rows)


def events_after(device_id: str, after: int) -> list[dict]:
    with locked() as folder:
        return [r for r in _read(folder / "events" / device_id, []) if r["id"] > after]


# ── Which process can answer ─────────────────────────────────────────────────
#
# Hermes v0.23.3 keeps a waiting approval only in the memory of the process whose agent asked
# (tools/approval.py _gateway_queues), and has no supported way to settle it from another process:
# resolve_gateway_approval acts on this process's queue; the gateway's control socket has no approval
# verb; the API server's POST /v1/runs/{id}/approval settles only its own runs.
#
# Which process that is varies: under the image's s6 setup the dashboard app (and so these routes)
# is imported by `hermes dashboard`, by the gateway (gateway/run_api.py mounts it on its own
# listener, which serves /api/ws and so Falki's chats), and by other processes that load it; in the
# e2e run the gateway served every route and ran the agent. Which one imported last says nothing, so
# it's decided in-process: a note carries Approve/Deny only when this process has itself served a
# request on these routes (the app calls /status and /devices on every start) and so is the one
# the app's /act reaches. Elsewhere (a messaging gateway, cron, a profile gateway, or right after a
# restart before the app has called) the note opens the chat, never dead buttons. An act also keeps
# the pid that made it, and /act from any other process answers 409 (Elsewhere).

_ROUTES = "garmoosha_push_core"  # the copy of this module dashboard/plugin_api.py loads
_served = False


def mark_served() -> None:
    """A route of this plugin answered a request in this process (dashboard/plugin_api.py)."""
    global _served
    _served = True


def answerable_here() -> bool:
    routes = sys.modules.get(_ROUTES)
    return bool(routes is not None and getattr(routes, "_served", False))


class Elsewhere(Exception):
    """The approval waits in another process than this route's (Hermes restarted, or a gateway's)."""


def answer(device_id: str, token: str, action: str) -> int:
    """A notification's Approve once or Deny. Returns how many approvals it settled (0: gone or answered).
    Raises Elsewhere when the token was made for an approval this process can't see."""
    if action not in ("approve", "deny"):
        raise ValueError("action")
    now = int(time.time())
    with locked() as folder:
        acts = _read(folder / "acts.json", {})
        hashed = act_key(token) if isinstance(token, str) else ""
        act = acts.get(hashed)
        if not act or act.get("device") != device_id or act.get("until", 0) <= now:
            return 0
        if act.get("pid") != os.getpid():
            acts.pop(hashed, None)
            _write(folder / "acts.json", acts)
            raise Elsewhere()
        # Spent for every phone it went to: one answer per ask.
        acts = {k: v for k, v in acts.items() if v.get("request_id") != act["request_id"] and v.get("until", 0) > now}
        _write(folder / "acts.json", acts)
    from tools.approval import resolve_gateway_approval

    return resolve_gateway_approval(act["session_key"], "once" if action == "approve" else "deny",
                                    request_id=act["request_id"])


# ── Hooks ───────────────────────────────────────────────────────────────────

def _chat(session_id: Optional[str], profile: str) -> dict:
    sid = session_id or ""
    from urllib.parse import quote
    return {"tag": f"garmoosha:chat:{sid}", "link": f"/chat/{quote(profile, safe='')}/{quote(sid, safe='')}"}


def on_approval_request(**kw: Any) -> None:
    # Only a question waiting for the person: not Hermes's smart check deciding first (it asks again,
    # as the gateway, if it can't), not a terminal's own prompt, not another plugin's transport.
    surface = str(kw.get("surface") or "")
    if surface in ("smart", "cli") or surface.startswith("transport:"):
        return
    # A follower of an identical approval already asked: the leader's note covers it (one note, one token).
    if kw.get("coalesced"):
        return
    try:
        session_key = kw.get("session_key") or ""
        request_id = None
        try:
            from tools.approval import list_gateway_approvals
            waiting = [a for a in list_gateway_approvals(session_key) if a.get("command") == kw.get("command")]
            request_id = waiting[-1].get("request_id") if waiting else None
        except Exception:
            pass
        profile = _profile()
        keys = [k for k in [kw.get("pattern_key"), *(kw.get("pattern_keys") or [])] if isinstance(k, str)]
        body, whole = asked(kw.get("command") or "", keys)
        note = {"title": _title(profile), "body": body, **_chat(kw.get("session_id"), profile)}
        # Buttons only for a whole, low-risk command (asked), and only where /act can settle it.
        buttons = whole and request_id is not None and answerable_here()
        deliver(note, ask={"session_key": session_key, "request_id": request_id if buttons else None,
                           "command": kw.get("command")})
    except Exception as exc:
        log.warning("garmoosha-push: couldn't tell the phone about an approval (%s)", type(exc).__name__)


def on_approval_response(**kw: Any) -> None:
    """Answered anywhere: that approval's buttons' tokens go (the phone's Approve then says it was answered).
    Only that one: by request_id when Hermes gives it, else by session and command; another approval
    still waiting in the same session keeps its buttons."""
    if kw.get("coalesced"):
        return
    try:
        session_key = kw.get("session_key")
        request_id = kw.get("request_id")
        command = _digest(kw.get("command")) if kw.get("command") is not None else None

        def answered(act: dict) -> bool:
            if act.get("session_key") != session_key:
                return False
            if request_id:
                return act.get("request_id") == request_id
            return command is None or act.get("command") in (None, command)

        with locked() as folder:
            acts = _read(folder / "acts.json", {})
            kept = {k: v for k, v in acts.items() if not answered(v)}
            if len(kept) != len(acts):
                _write(folder / "acts.json", kept)
    except Exception:
        pass


def on_reply(**kw: Any) -> None:
    sid = kw.get("session_id")
    if sid and isinstance(kw.get("assistant_response"), str):
        with _lock:
            _replies[sid] = kw["assistant_response"]
            while len(_replies) > 200:
                _replies.pop(next(iter(_replies)))


def on_turn_end(**kw: Any) -> None:
    sid = kw.get("session_id")
    with _lock:
        said = _replies.pop(sid, None) if sid else None
    try:
        if not sid or not kw.get("completed") or kw.get("interrupted") or kw.get("failed"):
            return
        if str(kw.get("platform") or "").lower() in MESSAGING:
            return
        profile = _profile()
        deliver({"title": _title(profile), "body": first_line(said or "") or "Finished", **_chat(sid, profile)})
    except Exception as exc:
        log.warning("garmoosha-push: couldn't tell the phone about a reply (%s)", type(exc).__name__)
