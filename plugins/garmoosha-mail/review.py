"""A profile's email proposals and the person's decisions.

A proposal saves only the source identifier and the words shown during review.
Each approval applies to one immutable draft revision. Claiming it is a
transaction, so two clients cannot both hand the same revision to SMTP.
"""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any
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
        ('proposed', 'approved', 'sending', 'sent', 'ignored', 'completed', 'uncertain')),
    revision INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    receipt TEXT,
    UNIQUE (account, source_key)
);
CREATE INDEX IF NOT EXISTS mail_review_account ON mail_review (account, updated_at DESC, id);
"""


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

    def _connect(self) -> sqlite3.Connection:
        # Create with private permissions even if the host's umask permits group access.
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
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

    def list(self, account: str, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT * FROM mail_review WHERE account = ?
                    ORDER BY CASE WHEN status IN ('proposed', 'approved', 'sending', 'uncertain') THEN 0 ELSE 1 END,
                        updated_at DESC, id
                    LIMIT ?""",
                (account, limit),
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
    ) -> dict[str, Any] | None:
        places = ", ".join("?" for _ in allowed)
        with closing(self._connect()) as db, db:
            row = db.execute(
                f"""UPDATE mail_review SET status = ?, revision = revision + ?, updated_at = ?,
                        draft = COALESCE(?, draft), receipt = COALESCE(?, receipt)
                    WHERE account = ? AND id = ? AND revision = ? AND status IN ({places})
                    RETURNING *""",
                (status, int(bump), _clock(), _json(draft) if draft is not None else None,
                 _json(receipt) if receipt is not None else None, account, proposal_id, revision, *allowed),
            ).fetchone()
            return _row(row) if row else None

    def edit(self, account: str, proposal_id: str, revision: int, draft: dict[str, Any]) -> dict[str, Any] | None:
        return self._update(account, proposal_id, revision, ("proposed", "approved"), "proposed", draft=draft)

    def decide(self, account: str, proposal_id: str, revision: int, choice: str) -> dict[str, Any] | None:
        allowed = {
            "approved": ("proposed",),
            "ignored": ("proposed", "approved"),
            "completed": ("proposed", "approved", "uncertain"),
        }
        return self._update(account, proposal_id, revision, allowed[choice], choice)

    def claim(self, account: str, proposal_id: str, revision: int) -> dict[str, Any] | None:
        return self._update(account, proposal_id, revision, ("approved",), "sending", bump=False)

    def settle(
        self, account: str, proposal_id: str, revision: int, receipt: dict[str, Any], *, sent: bool,
    ) -> dict[str, Any] | None:
        return self._update(
            account, proposal_id, revision, ("sending",), "sent" if sent else "uncertain", receipt=receipt,
        )
