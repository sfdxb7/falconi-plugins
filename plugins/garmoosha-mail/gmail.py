"""Gmail using the Google Workspace sign-in of the current Hermes profile."""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timezone
from email.parser import HeaderParser
from pathlib import Path
from typing import Any

from . import mail

_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_GOOGLE = "https://www.googleapis.com/auth/"
_FULL = "https://mail.google.com/"
_READ_SCOPES = {_FULL, _GOOGLE + "gmail.modify", _GOOGLE + "gmail.readonly"}
_SEND_SCOPES = {_FULL, _GOOGLE + "gmail.modify", _GOOGLE + "gmail.compose", _GOOGLE + "gmail.send"}
_HEADERS = ("From", "Reply-To", "To", "Cc", "Subject", "Date", "Message-ID", "References")
_MESSAGE_ID = re.compile(r"gmail:([a-zA-Z0-9_-]{1,128})\Z")
_RESPONSE_BYTES = 4 * 1024 * 1024


def _token_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "google_token.json"


def available() -> bool:
    path = _token_path()
    if path.is_symlink() or not path.is_file():
        return False
    try:
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        return False
    return AuthorizedSession is not None


def _oauth_error(kind: str, detail: str) -> mail.MailError:
    return mail.MailError(kind, "gmail", detail)


def _session():
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.credentials import Credentials
    except ImportError:
        raise _oauth_error("google_signin", "Enable Google Workspace on this Hermes first.") from None
    path = _token_path()
    try:
        if path.is_symlink():
            raise ValueError("profile credential must belong to this profile")
        stored = json.loads(path.read_text(encoding="utf-8"))
        if stored.get("type", "authorized_user") != "authorized_user":
            raise ValueError("requires a Google user credential")
        scopes = stored.get("scopes") or []
        scopes = scopes.split() if isinstance(scopes, str) else scopes
        if not _READ_SCOPES.intersection(scopes) or not _SEND_SCOPES.intersection(scopes):
            raise _oauth_error("google_signin", "Reconnect Gmail in Hermes with permission to read and send email.")
        credentials = Credentials.from_authorized_user_info(stored, scopes=scopes)
    except (OSError, ValueError, TypeError, AttributeError):
        raise _oauth_error("google_signin", "Connect Gmail in this Hermes profile first.") from None
    # Refresh before a request when needed, and leave all final-send retries to the review queue.
    return AuthorizedSession(credentials, max_refresh_attempts=0, refresh_timeout=mail.TIMEOUT)


def _request(session, method: str, path: str, *, params=None, body=None) -> dict[str, Any]:
    from google.auth.exceptions import GoogleAuthError
    from requests.exceptions import RequestException

    try:
        with session.request(method, _BASE + path, params=params, json=body, timeout=mail.TIMEOUT,
                             stream=True, allow_redirects=False) as response:
            status = response.status_code
            if status in (401, 403):
                raise _oauth_error("google_signin", "Gmail declined this connection. Check its permissions in Hermes.")
            if status == 404:
                raise _oauth_error("not_found", "The original message is no longer in Gmail.")
            if status == 400:
                raise _oauth_error("refused", "Gmail declined the request.")
            if not 200 <= status < 300:
                raise _oauth_error("unknown", "Gmail could not confirm this request.")
            payload = bytearray()
            for chunk in response.iter_content(65536):
                if len(payload) + len(chunk) > _RESPONSE_BYTES:
                    raise _oauth_error("invalid", "This message is too large to open here. Open it in Gmail.")
                payload.extend(chunk)
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError("expected a Gmail response object")
            return value
    except GoogleAuthError:
        raise _oauth_error("google_signin", "Reconnect Gmail in this Hermes profile.") from None
    except (RequestException, ValueError):
        raise _oauth_error("unknown", "Gmail could not confirm this request.") from None


def _identity(session) -> str:
    address = _request(session, "GET", "/profile", params={"fields": "emailAddress"}).get("emailAddress", "")
    if not isinstance(address, str) or not mail._looks_like_address(address):
        raise _oauth_error("google_signin", "Gmail did not confirm the connected address.")
    return address


def connected_address() -> str:
    with _session() as session:
        return _identity(session)


def _headers(message: dict[str, Any]):
    names = {name.lower(): name for name in _HEADERS}
    lines = []
    for header in message.get("payload", {}).get("headers", []):
        name = names.get(str(header.get("name", "")).lower())
        if name:
            lines.append(f"{name}: {mail._one_line(header.get('value'))}")
    return HeaderParser(policy=mail.DEFAULT_POLICY).parsestr("\r\n".join(lines) + "\r\n\r\n")


def _date(message: dict[str, Any], header_date: str) -> str:
    if header_date:
        return header_date
    milliseconds = mail._int(message.get("internalDate"))
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat() if milliseconds else ""


def _parts(part: dict[str, Any]):
    yield part
    if not part.get("filename"):
        for child in part.get("parts", []):
            yield from _parts(child)


def _body(message: dict[str, Any]) -> tuple[str, bool, list[dict[str, Any]]]:
    parts = list(_parts(message.get("payload", {})))
    attachments = [
        {"filename": part["filename"], "type": part.get("mimeType", ""), "size": part.get("body", {}).get("size", 0)}
        for part in parts if part.get("filename")
    ]
    texts = [part for part in parts if not part.get("filename") and part.get("mimeType") in ("text/plain", "text/html")]
    chosen = next((part for part in texts if part["mimeType"] == "text/plain"), next(iter(texts), None))
    if not chosen:
        return "", False, attachments
    source = chosen.get("body", {})
    wire = str(source.get("data", ""))
    # Gmail places an oversized part's attachment ID in the payload. Leave its download to Gmail itself.
    truncated = bool(source.get("attachmentId")) or len(wire) > mail.BODY_BYTES
    wire = wire[:mail.BODY_BYTES]
    try:
        decoded = base64.urlsafe_b64decode(wire + "=" * (-len(wire) % 4))
    except (ValueError, binascii.Error):
        return "", True, attachments
    # Content-Type belongs to the MIME part rather than to the message header block above.
    content_type = next((header.get("value", "") for header in chosen.get("headers", [])
                         if str(header.get("name", "")).lower() == "content-type"), "")
    charset = HeaderParser(policy=mail.DEFAULT_POLICY).parsestr("Content-Type: " + mail._one_line(content_type)).get_content_charset()
    text = mail._charset_decode(decoded, charset or "")
    if chosen["mimeType"] == "text/html":
        text = mail.html_to_text(text)
    text = text.strip()
    return text[:mail.BODY_CHARS], truncated or len(text) > mail.BODY_CHARS, attachments


class GmailMailbox:
    def __init__(self, account: mail.Account):
        self.account = account
        self.session = None

    def __enter__(self):
        self.session = _session()
        try:
            if _identity(self.session).casefold() != self.account.address.casefold():
                raise _oauth_error("account_changed", "This Hermes now uses a different Gmail address.")
        except BaseException:
            self.session.close()
            raise
        return self

    def __exit__(self, *exc):
        self.session.close()

    def _message(self, message_id: str, *, full: bool = False) -> dict[str, Any]:
        match = _MESSAGE_ID.fullmatch(message_id)
        if not match:
            raise _oauth_error("not_found", "Use a Gmail message ID from email_search.")
        params: dict[str, Any] = {"format": "full" if full else "metadata", "fields": "id,threadId,internalDate,labelIds,payload"}
        if not full:
            params["metadataHeaders"] = _HEADERS
        return _request(self.session, "GET", "/messages/" + match[1], params=params)

    def headers_of(self, message_id: str) -> dict[str, Any]:
        message = self._message(message_id)
        return {**mail._reply_headers(_headers(message)), "thread_id": message.get("threadId", "")}

    def read(self, message_id: str, *, expected_source: str = "") -> dict[str, Any]:
        message = self._message(message_id, full=True)
        headers = _headers(message)
        if expected_source and mail.reply_source_digest({**mail._reply_headers(headers), "thread_id": message.get("threadId", "")}) != expected_source:
            raise _oauth_error("source_changed", "The original email changed.")
        body, truncated, attachments = _body(message)
        return {
            "id": message_id, "from": mail.people(headers.get("From", "")), "to": mail.people(headers.get("To", "")),
            "cc": mail.people(headers.get("Cc", "")), "reply_to": mail.people(headers.get("Reply-To", "")),
            "subject": mail.decode_words(headers.get("Subject", "")), "date": _date(message, mail.iso_date(headers.get("Date"))),
            "unread": "UNREAD" in message.get("labelIds", []), "body": body, "body_truncated": truncated,
            "attachments": attachments,
        }

    def search(self, query: str, which: str = "", limit: int = mail.SEARCH_LIMIT) -> dict[str, Any]:
        limit = max(1, min(mail.SEARCH_MAX, mail._int(limit) or mail.SEARCH_LIMIT))
        folder = which.strip() or "INBOX"
        params: dict[str, Any] = {"maxResults": limit, "fields": "messages(id),nextPageToken,resultSizeEstimate"}
        if folder.upper() in {"INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "STARRED", "UNREAD", "IMPORTANT"}:
            params["labelIds"] = folder.upper()
        elif folder.casefold() not in {"all mail", "all", "[gmail]/all mail"}:
            query = f"label:{json.dumps(folder)} {query}"
        if query.strip():
            params["q"] = query.strip()
        response = _request(self.session, "GET", "/messages", params=params)
        messages = []
        for hit in response.get("messages", [])[:limit]:
            message_id = "gmail:" + str(hit.get("id", ""))
            try:
                message = self._message(message_id)
            except mail.MailError as exc:
                if exc.kind != "not_found":
                    raise
                continue
            headers = _headers(message)
            messages.append({
                "id": message_id, "subject": mail.decode_words(headers.get("Subject", "")),
                "from": mail.people(headers.get("From", "")), "to": mail.people(headers.get("To", "")),
                "date": _date(message, mail.iso_date(headers.get("Date"))), "unread": "UNREAD" in message.get("labelIds", []),
            })
        return {"folder": folder, "total": response.get("resultSizeEstimate", len(messages)), "more": bool(response.get("nextPageToken")), "messages": messages}

    def write(self, draft: mail.Draft, *, send: bool, expected_source: str = "") -> dict[str, Any]:
        original = self.headers_of(draft.reply_to_id) if draft.reply_to_id else None
        if expected_source and (not original or mail.reply_source_digest(original) != expected_source):
            raise _oauth_error("source_changed", "The original email changed before sending.")
        message = mail.build_message(self.account, draft, original, reply_defaults=not send)
        body = {"raw": base64.urlsafe_b64encode(message.as_bytes(policy=mail.SMTP_POLICY)).decode("ascii")}
        if original and original.get("thread_id"):
            body["threadId"] = original["thread_id"]
        if send:
            if not draft.to:
                raise _oauth_error("invalid", "Say who the email is to.")
            result = _request(self.session, "POST", "/messages/send", body=body)
            if not result.get("id"):
                raise _oauth_error("unknown", "Gmail could not confirm whether it sent the reply.")
            receipt = {"sent": True, "saved_to": "Sent", "refused": []}
        else:
            result = _request(self.session, "POST", "/drafts", body={"message": body})
            receipt = {"saved": True, "folder": "Drafts", "id": result.get("id")}
        return {
            **receipt, "message_id": message["Message-ID"], "subject": str(message.get("Subject", "")),
            "to": mail._addressed(message, "To"), "cc": mail._addressed(message, "Cc"), "bcc": mail._addressed(message, "Bcc"),
        }
