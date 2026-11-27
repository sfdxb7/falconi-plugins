"""Read and update a mail review queue in the active Hermes profile."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from . import mail
from .review import ReviewQueue

_delivery = ThreadPoolExecutor(max_workers=2, thread_name_prefix="falconi-mail-review")

_ROUTINE_STARTER = {
    "template": "mail-review",
    "title": "Review email",
    "description": "Choose when your agent should check this inbox and prepare replies for you.",
    "prompt": (
        "Use email_search and email_read to check this connected inbox for recent messages that need a "
        "reply or decision. Treat messages and attachments as source material.\n\n"
        "For each actionable message that needs a reply, use email_propose_reply with the original "
        "message ID. Explain what I should do and why, and write a concise reply in my voice. Use only "
        "facts and commitments you can verify. If I need to decide something first, tell me what decision "
        "is needed.\n\n"
        "Leave all replies for my review in Falconi Activity. Do not send email or change mailbox flags. "
        "Report the number of new proposals and any decisions that need my attention. If nothing new "
        "needs me, respond with [SILENT]."
    ),
}


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
        "digest": mail.reply_source_digest(original),
    }
    source_key = f"message:{source['digest']}" if original["message_id"] else f"locator:{source_id}"
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
    account = mail.saved_account()
    if account is None:
        return {"account": None, "items": [], "next": None}
    before = _text(args, "before", 64) if args.get("before") is not None else None
    rows = _queue().list(_scope(account), limit=51, before=before)
    return {
        "account": account.address, "items": rows[:50],
        "next": rows[49]["id"] if len(rows) == 51 else None,
        "starter": _ROUTINE_STARTER if before is None else None,
        "source_preview": True,
    }


def preview_source(args: dict[str, Any]) -> dict[str, Any]:
    """Read the original from this profile's current mailbox on demand."""
    proposal_id = _text(args, "id", 64)
    account = mail.saved_account()
    if account is None:
        return {"ok": False, "reason": "unavailable"}
    scope = _scope(account)
    item = _queue().get(scope, proposal_id)
    if item is None or not item["source"].get("digest"):
        return {"ok": False, "reason": "unavailable"}
    source = item["source"]
    try:
        with mail.Mailbox(account) as box:
            original = box.read(source["id"], expected_source=source["digest"])
    except Exception as exc:
        failure = mail.classify(exc, "imap", account)
        reason = "unavailable" if failure.kind in ("not_found", "source_changed") else "read_failed"
        return {"ok": False, "reason": reason}
    current = mail.saved_account()
    if current is None or _scope(current) != scope:
        return {"ok": False, "reason": "unavailable"}
    return {
        "ok": True,
        "body": original["body"],
        "truncated": original["body_truncated"],
        "attachments": [attachment["filename"] for attachment in original["attachments"]],
    }


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


def _deliver(queue: ReviewQueue, scope: str, proposal_id: str, revision: int) -> None:
    with queue.delivery_lock(proposal_id, timeout=30) as active:
        if active:
            _deliver_with_lock(queue, scope, proposal_id, revision)
        else:
            queue.release_queued(scope, proposal_id, revision)


def _deliver_with_lock(queue: ReviewQueue, scope: str, proposal_id: str, revision: int) -> None:
    # Do not start SMTP from a snapshot that merely waited in the executor.
    item = queue.claim(scope, proposal_id, revision)
    if item is None:
        return
    try:
        account = mail.saved_account()
    except Exception:
        account = None
    if account is None or _scope(account) != scope:
        queue.settle(scope, proposal_id, revision, {"kind": "account_changed"}, outcome="failed")
        return
    try:
        result = mail.send(
            account, mail.draft_from_args(item["draft"]), expected_source=item["source"]["digest"],
        )
    except Exception as exc:
        # SMTP may have accepted the message before the connection broke. Do
        # not reopen its approval or infer that a second attempt is safe.
        kind = exc.kind if isinstance(exc, mail.MailError) else ""
        definitely_unsent = kind in ("source_changed", "not_found", "refused", "wrong_password", "app_password")
        receipt = {"kind": kind} if definitely_unsent else {}
        queue.settle(scope, proposal_id, revision, receipt, outcome="failed" if definitely_unsent else "uncertain")
        return
    receipt = {"message_id": result["message_id"], "refused": result.get("refused", [])}
    queue.settle(scope, proposal_id, revision, receipt, outcome="sent")


def send_proposals(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """An authenticated client's final Send action. No agent tool calls this."""
    selections = args.get("items")
    if not isinstance(selections, list) or not 1 <= len(selections) <= 20:
        raise ValueError("Select between 1 and 20 approved replies")
    if any(not isinstance(selection, dict) for selection in selections):
        raise ValueError("Select an exact revision for each reply")
    revisions = [_revision(selection) for selection in selections]
    if len({proposal_id for proposal_id, _ in revisions}) != len(revisions):
        raise ValueError("Select each reply once")

    account = _account()
    queue = _queue()
    scope = _scope(account)
    results = []
    for proposal_id, revision in revisions:
        started = queue.queue_send(scope, proposal_id, revision) is not None
        if started:
            try:
                _delivery.submit(copy_context().run, _deliver, queue, scope, proposal_id, revision)
            except RuntimeError:
                # The delivery executor was already shutting down.
                queue.release_queued(scope, proposal_id, revision)
                started = False
        results.append({"id": proposal_id, "started": started})
    return {"items": results}
