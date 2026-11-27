"""A profile's email proposals and the person's decisions.

A proposal saves only the source identifier and the words shown during review.
Each approval applies to one immutable draft revision. Claiming it is a
transaction, so two clients cannot both hand the same revision to SMTP.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator
from uuid import uuid4


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_review (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    draft TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('proposed', 'approved', 'queued', 'sending', 'confirming', 'sent', 'failed', 'ignored', 'completed', 'uncertain')),
    revision INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    receipt TEXT,
    UNIQUE (account, source_key)
);
CREATE INDEX IF NOT EXISTS mail_review_account ON mail_review (account, updated_at DESC, id);
CREATE INDEX IF NOT EXISTS mail_review_delivery ON mail_review (account, status);
"""
_ABANDONED_AFTER_MS = 15 * 60 * 1000


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source": json.loads(row["source"]),
        "recommendation": row["recommendation"],
        "draft": json.loads(row["draft"]),
        "status": row["status"],
        "revision": row["revision"],
        "updated_at": row["updated_at"],
        "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
    }


def _clock() -> int:
    return time.time_ns() // 1_000_000


class ReviewQueue:
    def __init__(self, home: Path):
        self.path = home / "garmoosha-mail-review.sqlite3"

    @contextmanager
    def delivery_lock(self, proposal_id: str, timeout: float = 0) -> Iterator[bool]:
        """Hold a SQLite write lock in a separate file until this send settles.

        The OS releases the lock if the worker process exits. Use bounded
        lock files per profile. Matching slots serialize two deliveries.
        """
        folder = self.path.parent / "garmoosha-mail-delivery"
        folder.mkdir(mode=0o700, exist_ok=True)
        slot = sha256(proposal_id.encode("utf-8")).digest()[0]
        path = folder / f"{slot:02x}.sqlite3"
        with closing(sqlite3.connect(path, timeout=timeout)) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                locked = (
                    str(exc) in ("database is locked", "database table is locked")
                    if code is None else (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                )
                if not locked:
                    raise
                yield False
            else:
                yield True

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            # Set private permissions before any review data is written.
            self.path.chmod(0o600)
            db.executescript(_SCHEMA)
        except Exception:
            db.close()
            raise
        return db

    def propose(
        self,
        account: str,
        source_key: str,
        source: dict[str, Any],
        recommendation: str,
        draft: dict[str, Any],
    ) -> tuple[str, bool]:
        """Polling the same source again preserves every existing decision and edit."""
        proposal_id = uuid4().hex
        with closing(self._connect()) as db, db:
            inserted = db.execute(
                """INSERT INTO mail_review
                    (id, account, source_key, source, recommendation, draft, status, revision, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'proposed', 1, ?)
                    ON CONFLICT(account, source_key) DO NOTHING""",
                (proposal_id, account, source_key, _json(source), recommendation, _json(draft), _clock()),
            )
            if inserted.rowcount:
                return proposal_id, True
            existing = db.execute(
                "SELECT id FROM mail_review WHERE account = ? AND source_key = ?",
                (account, source_key),
            ).fetchone()
            return existing["id"], False

    def list(self, account: str, limit: int = 50, *, before: str | None = None) -> list[dict[str, Any]]:
        with closing(self._connect()) as db, db:
            now = _clock()
            db.execute(
                """UPDATE mail_review SET status = 'approved', revision = revision + 1, updated_at = ?, receipt = ?
                    WHERE account = ? AND status = 'queued' AND updated_at < ?""",
                (now, _json({"kind": "queue_expired"}), account, now - _ABANDONED_AFTER_MS),
            )
            db.execute(
                """UPDATE mail_review SET status = 'confirming', updated_at = ?
                    WHERE account = ? AND status = 'sending' AND updated_at < ?""",
                (now, account, now - _ABANDONED_AFTER_MS),
            )
            active = db.execute(
                """SELECT id, revision FROM mail_review
                    WHERE account = ? AND status IN ('sending', 'confirming')""",
                (account,),
            ).fetchall()
            for row in active:
                # A nonblocking probe inside this write transaction cannot
                # deadlock a worker holding its lock while saving the receipt.
                with self.delivery_lock(row["id"]) as idle:
                    if idle:
                        db.execute(
                            """UPDATE mail_review SET status = 'uncertain', revision = revision + 1,
                                    updated_at = ?, receipt = ?
                                WHERE account = ? AND id = ? AND revision = ?""",
                            (now, _json({"kind": "worker_stopped"}), account, row["id"], row["revision"]),
                        )
            rows = db.execute(
                """SELECT * FROM mail_review WHERE account = ? AND
                    (? IS NULL OR rowid < (SELECT rowid FROM mail_review WHERE account = ? AND id = ?))
                    ORDER BY rowid DESC
                    LIMIT ?""",
                (account, before, account, before, limit),
            )
            return [_row(row) for row in rows]

    def get(self, account: str, proposal_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM mail_review WHERE account = ? AND id = ?", (account, proposal_id),
            ).fetchone()
            return _row(row) if row else None

    def _update(
        self,
        account: str,
        proposal_id: str,
        revision: int,
        allowed: tuple[str, ...],
        status: str,
        *,
        draft: dict[str, Any] | None = None,
        receipt: dict[str, Any] | None = None,
        bump: bool = True,
        min_updated_at: int = 0,
    ) -> dict[str, Any] | None:
        places = ", ".join("?" for _ in allowed)
        with closing(self._connect()) as db, db:
            row = db.execute(
                f"""UPDATE mail_review SET status = ?, revision = revision + ?, updated_at = ?,
                        draft = COALESCE(?, draft), receipt = COALESCE(?, receipt)
                    WHERE account = ? AND id = ? AND revision = ? AND updated_at >= ? AND status IN ({places})
                    RETURNING *""",
                (status, int(bump), _clock(), _json(draft) if draft is not None else None,
                 _json(receipt) if receipt is not None else None, account, proposal_id, revision, min_updated_at, *allowed),
            ).fetchone()
            return _row(row) if row else None

    def edit(self, account: str, proposal_id: str, revision: int, draft: dict[str, Any]) -> dict[str, Any] | None:
        return self._update(account, proposal_id, revision, ("proposed", "approved", "failed"), "proposed", draft=draft, receipt={})

    def decide(self, account: str, proposal_id: str, revision: int, choice: str) -> dict[str, Any] | None:
        allowed = {
            "approved": ("proposed", "failed"),
            "ignored": ("proposed", "approved", "failed"),
            "completed": ("proposed", "approved", "failed", "uncertain"),
        }
        return self._update(account, proposal_id, revision, allowed[choice], choice)

    def queue_send(self, account: str, proposal_id: str, revision: int) -> dict[str, Any] | None:
        return self._update(account, proposal_id, revision, ("approved",), "queued", receipt={}, bump=False)

    def claim(self, account: str, proposal_id: str, revision: int) -> dict[str, Any] | None:
        return self._update(
            account, proposal_id, revision, ("queued",), "sending", bump=False,
            min_updated_at=_clock() - _ABANDONED_AFTER_MS,
        )

    def release_queued(self, account: str, proposal_id: str, revision: int) -> None:
        self._update(account, proposal_id, revision, ("queued",), "approved")

    def settle(
        self, account: str, proposal_id: str, revision: int, receipt: dict[str, Any], *, outcome: str,
    ) -> dict[str, Any] | None:
        return self._update(
            account, proposal_id, revision, ("sending", "confirming"), outcome, receipt=receipt,
        )
