"""Read and update a mail review queue in the active Hermes profile."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from . import mail
from .review import ReviewQueue


def _queue() -> ReviewQueue:
    from hermes_constants import get_hermes_home

    return ReviewQueue(Path(get_hermes_home()))


def _scope(account: mail.Account) -> str:
    identifying = [account.address, account.imap_host, account.imap_port, account.login,
                   account.smtp_host, account.smtp_port]
    return sha256(json.dumps(identifying).encode("utf-8")).hexdigest()


def _text(args: dict[str, Any], field: str, maximum: int) -> str:
    value = args.get(field)
    if not isinstance(value, str) or len(value) > maximum or not value.strip():
        raise ValueError(f"{field} must contain between 1 and {maximum} characters")
    return value


def propose_reply(account: mail.Account, args: dict[str, Any]) -> dict[str, Any]:
    source_id = _text(args, "reply_to_id", 1024).strip()
    body = _text(args, "body", mail.SEND_BODY_CHARS)
    recommendation = _text(args, "recommendation", 1000).strip()
    with mail.Mailbox(account) as box:
        original = box.headers_of(source_id)
    subject = args["subject"] if "subject" in args else mail._reply_subject(original["subject"])
    if not isinstance(subject, str) or len(subject) > 512:
        raise ValueError("The subject can contain at most 512 characters")
    draft = mail.draft_from_args({
        "to": [original["reply_address"]],
        "subject": subject,
        "body": body,
        "reply_to_id": source_id,
    })
    if not draft.to:
        raise ValueError("The original email has no reply address")
    source = {
        "id": source_id,
        "message_id": original["message_id"],
        "from": original["from"],
        "subject": original["subject"],
        "date": original["date"],
    }
    source_key = json.dumps([source_id, original["message_id"]], ensure_ascii=False)
    proposal_id, created = _queue().propose(_scope(account), source_key, source, recommendation, asdict(draft))
    return {"id": proposal_id, "created": created}


def _account() -> mail.Account:
    account = mail.saved_account()
    if account is None:
        raise ValueError("Connect an email account to this agent first")
    return account


def _revision(args: dict[str, Any]) -> tuple[str, int]:
    proposal_id = _text(args, "id", 64)
    revision = args.get("revision")
    if type(revision) is not int or not 1 <= revision <= 2_147_483_647:
        raise ValueError("A valid draft revision is required")
    return proposal_id, revision


def list_proposals(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    account = _account()
    return {"account": account.address, "items": _queue().list(_scope(account))}


def edit_proposal(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    account = _account()
    queue = _queue()
    scope = _scope(account)
    proposal_id, revision = _revision(args)
    current = queue.get(scope, proposal_id)
    if not current or current["revision"] != revision:
        return {"ok": False, "reason": "changed"}
    draft = current["draft"]
    subject = args.get("subject")
    if not isinstance(subject, str) or len(subject) > 512 or "\r" in subject or "\n" in subject:
        raise ValueError("Use one line of up to 512 characters for the subject")
    draft.update(subject=subject, body=_text(args, "body", mail.SEND_BODY_CHARS))
    item = queue.edit(scope, proposal_id, revision, asdict(mail.draft_from_args(draft)))
    return {"ok": True, "item": item} if item else {"ok": False, "reason": "changed"}


def decide_proposal(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    account = _account()
    proposal_id, revision = _revision(args)
    choice = args.get("choice")
    if choice not in ("approved", "ignored", "completed"):
        raise ValueError("Choose approve, ignore or complete")
    item = _queue().decide(_scope(account), proposal_id, revision, choice)
    return {"ok": True, "item": item} if item else {"ok": False, "reason": "changed"}
