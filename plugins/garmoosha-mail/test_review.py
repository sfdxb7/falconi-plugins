from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from . import mail, register, review, review_api
from .review import ReviewQueue


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.home = TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.queue = ReviewQueue(Path(self.home.name))
        self.source = {"id": "INBOX:31", "subject": "Work request"}
        self.draft = {"subject": "Re: Work request", "body": "Yes."}

    def propose(self):
        return self.queue.propose("mailbox", "source", self.source, "Reply today", self.draft)

    def test_original_read_rejects_a_reused_uid_before_fetching_the_body(self):
        account = mail.Account("me@example.test", "fixture", "imap.example.test", "smtp.example.test")
        box = mail.Mailbox(account)
        headers = {"BODY[HEADER]": b"From: Actual <elsewhere@example.test>\r\nSubject: Other topic\r\n"}
        with patch.object(box, "locate", return_value=("INBOX", 31)), patch.object(box, "select"), \
                patch.object(box, "fetch", return_value=[headers]), patch.object(box, "_body_by_structure") as read:
            with self.assertRaises(mail.MailError) as caught:
                box.read("INBOX:31", expected_source="the-original-digest")
            self.assertEqual(caught.exception.kind, "source_changed")
            read.assert_not_called()

    def test_ordinary_read_preserves_internationalized_sender_addresses(self):
        account = mail.Account("me@example.test", "fixture", "imap.example.test", "smtp.example.test")
        box = mail.Mailbox(account)
        headers = {"BODY[HEADER]": "From: Sender <josé@example.test>\r\nSubject: Handoff\r\n".encode()}
        with patch.object(box, "locate", return_value=("INBOX", 31)), patch.object(box, "select"), \
                patch.object(box, "fetch", return_value=[headers]), \
                patch.object(box, "_body_by_structure", return_value=("Original message", False, [])):
            message = box.read("INBOX:31")
        self.assertEqual(message["from"], [{"name": "Sender", "address": "josé@example.test"}])
        self.assertEqual(message["body"], "Original message")

    def test_mailbox_switch_during_a_read_discards_the_old_body(self):
        first = mail.Account("me@example.test", "fixture", "imap.example.test", "smtp.example.test")
        second = mail.Account("other@example.test", "fixture", "imap.example.test", "smtp.example.test")
        source = {**self.source, "digest": "original"}
        proposal, _ = self.queue.propose(review_api._scope(first), "source", source, "Reply today", self.draft)
        with patch.object(review_api, "_queue", return_value=self.queue), \
                patch.object(mail, "saved_account", side_effect=[first, second]), \
                patch.object(mail, "Mailbox") as mailbox:
            box = mailbox.return_value.__enter__.return_value
            box.read.return_value = {"body": "Private original"}
            self.assertEqual(review_api.preview_source({"id": proposal}), {"ok": False, "reason": "unavailable"})
            box.read.assert_called_once_with("INBOX:31", expected_source="original")

    def test_polling_preserves_the_persons_edit_and_decision(self):
        proposal_id, created = self.propose()
        self.assertTrue(created)
        changed = self.queue.edit("mailbox", proposal_id, 1, {"subject": "Revised", "body": "Tomorrow."})
        self.assertEqual(changed["revision"], 2)
        self.queue.decide("mailbox", proposal_id, 2, "ignored")
        self.assertEqual(self.propose(), (proposal_id, False))
        saved = ReviewQueue(Path(self.home.name)).list("mailbox")
        self.assertEqual([(item["status"], item["draft"]["body"]) for item in saved], [("ignored", "Tomorrow.")])
        self.assertEqual(self.queue.path.stat().st_mode & 0o777, 0o600)

    def test_an_edit_cancels_approval_and_old_revisions_cannot_be_claimed(self):
        proposal_id, _ = self.propose()
        self.assertIsNone(self.queue.claim("mailbox", proposal_id, 1))
        approved = self.queue.decide("mailbox", proposal_id, 1, "approved")
        revised = self.queue.edit("mailbox", proposal_id, approved["revision"], {"body": "My edit."})
        self.assertEqual(revised["status"], "proposed")
        self.assertIsNone(self.queue.claim("mailbox", proposal_id, approved["revision"]))
        self.assertIsNone(self.queue.claim("mailbox", proposal_id, revised["revision"]))
        self.assertIsNone(self.queue.decide("mailbox", proposal_id, 1, "approved"))

    def test_even_concurrent_claims_can_send_a_revision_only_once(self):
        proposal_id, _ = self.propose()
        approved = self.queue.decide("mailbox", proposal_id, 1, "approved")
        revision = approved["revision"]
        self.assertIsNotNone(self.queue.queue_send("mailbox", proposal_id, revision))
        self.assertIsNone(self.queue.queue_send("mailbox", proposal_id, revision))
        with ThreadPoolExecutor(max_workers=4) as clients:
            claims = list(clients.map(lambda _: self.queue.claim("mailbox", proposal_id, revision), range(8)))
        self.assertEqual(sum(item is not None for item in claims), 1)
        self.assertIsNone(self.queue.edit("mailbox", proposal_id, revision, {"body": "Too late"}))
        settled = self.queue.settle("mailbox", proposal_id, revision, {"message_id": "fixture"}, outcome="sent")
        self.assertEqual((settled["status"], settled["receipt"]), ("sent", {"message_id": "fixture"}))
        self.assertIsNone(self.queue.claim("mailbox", proposal_id, settled["revision"]))

    def test_unknown_delivery_cannot_be_retried_silently(self):
        proposal_id, _ = self.propose()
        approved = self.queue.decide("mailbox", proposal_id, 1, "approved")
        self.queue.queue_send("mailbox", proposal_id, approved["revision"])
        self.queue.claim("mailbox", proposal_id, approved["revision"])
        uncertain = self.queue.settle("mailbox", proposal_id, approved["revision"], {}, outcome="uncertain")
        self.assertEqual(uncertain["status"], "uncertain")
        self.assertIsNone(self.queue.claim("mailbox", proposal_id, uncertain["revision"]))
        self.assertEqual(self.queue.decide("mailbox", proposal_id, uncertain["revision"], "completed")["status"], "completed")

    def test_expired_queued_job_cannot_send_when_the_executor_finally_runs(self):
        account = mail.Account("me@example.test", "fixture", "imap.example.test", "smtp.example.test")
        scope = review_api._scope(account)
        queued = []
        with patch.object(review_api, "_queue", return_value=self.queue), \
                patch.object(mail, "saved_account", return_value=account), \
                patch.object(mail, "send") as transmit, \
                patch.object(review_api._delivery, "submit", side_effect=lambda *job: queued.append(job)), \
                patch.object(review, "_clock", return_value=2_000_000) as clock:
            proposal_id, _ = self.queue.propose(scope, "source", self.source, "Reply", self.draft)
            approved = self.queue.decide(scope, proposal_id, 1, "approved")
            revision = approved["revision"]
            self.assertTrue(review_api.send_proposals({"items": [{"id": proposal_id, "revision": revision}]})["items"][0]["started"])
            self.assertEqual(self.queue.get(scope, proposal_id)["status"], "queued")
            clock.return_value += review._ABANDONED_AFTER_MS + 1
            deliver, *args = queued[0]
            deliver(*args)
            transmit.assert_not_called()
            expired = self.queue.list(scope)[0]
            self.assertEqual(expired["status"], "approved")
            self.assertEqual(expired["receipt"]["kind"], "queue_expired")
            self.assertIsNone(self.queue.queue_send(scope, proposal_id, revision))

    def test_a_late_confirmation_can_still_record_the_actual_delivery(self):
        with patch.object(review, "_clock", return_value=2_000_000) as clock:
            proposal_id, _ = self.propose()
            approved = self.queue.decide("mailbox", proposal_id, 1, "approved")
            revision = approved["revision"]
            self.queue.queue_send("mailbox", proposal_id, revision)
            with self.queue.delivery_lock(proposal_id) as acquired:
                self.assertTrue(acquired)
                self.queue.claim("mailbox", proposal_id, revision)
                clock.return_value += review._ABANDONED_AFTER_MS + 1
                self.assertEqual(self.queue.list("mailbox")[0]["status"], "confirming")
                self.assertIsNone(self.queue.decide("mailbox", proposal_id, revision, "completed"))
                late = self.queue.settle("mailbox", proposal_id, revision, {"message_id": "fixture"}, outcome="sent")
                self.assertEqual(late["status"], "sent")

    def test_process_exit_after_claim_becomes_uncertain_without_resending(self):
        proposal_id, _ = self.propose()
        revision = self.queue.decide("mailbox", proposal_id, 1, "approved")["revision"]
        self.queue.queue_send("mailbox", proposal_id, revision)
        script = """
from importlib import import_module
from pathlib import Path
import sys
queue = import_module(sys.argv[1]).ReviewQueue(Path(sys.argv[2]))
with queue.delivery_lock(sys.argv[3]) as acquired:
    assert acquired and queue.claim("mailbox", sys.argv[3], int(sys.argv[4]))
    print("claimed", flush=True)
    sys.stdin.read()
"""
        with subprocess.Popen(
            [sys.executable, "-c", script, review.__name__, self.home.name, proposal_id, str(revision)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        ) as worker:
            try:
                self.assertEqual(worker.stdout.readline().strip(), "claimed")
                self.assertEqual(self.queue.list("mailbox")[0]["status"], "sending")
            finally:
                worker.kill()
                worker.wait(timeout=5)
        recovered = self.queue.list("mailbox")[0]
        self.assertEqual((recovered["status"], recovered["receipt"]["kind"]), ("uncertain", "worker_stopped"))
        self.assertIsNone(self.queue.queue_send("mailbox", proposal_id, recovered["revision"]))
        self.assertEqual(self.queue.decide("mailbox", proposal_id, recovered["revision"], "completed")["status"], "completed")

    def test_another_mailbox_cannot_read_or_change_a_proposal(self):
        proposal_id, _ = self.propose()
        self.assertEqual(self.queue.list("other"), [])
        self.assertIsNone(self.queue.get("other", proposal_id))
        self.assertIsNone(self.queue.decide("other", proposal_id, 1, "approved"))
        self.assertIsNone(self.queue.claim("other", proposal_id, 1))
        self.assertTrue(self.queue.propose("other", "source", self.source, "Another reply", self.draft)[1])

    def test_pages_keep_older_items_reachable_after_a_decision_updates_the_newest(self):
        account = mail.Account("me@example.test", "fixture", "imap.example.test", "smtp.example.test")
        scope = review_api._scope(account)
        ids = [
            self.queue.propose(scope, f"source-{i}", self.source, "Reply", self.draft)[0]
            for i in range(53)
        ]
        with patch.object(review_api, "_queue", return_value=self.queue), patch.object(mail, "saved_account", return_value=account):
            first = review_api.list_proposals({})
            self.assertEqual([item["id"] for item in first["items"]], ids[:2:-1])
            self.queue.decide(scope, ids[-1], 1, "completed")
            older = review_api.list_proposals({"before": first["next"]})
            self.assertEqual([item["id"] for item in older["items"]], ids[2::-1])
            self.assertIsNone(older["next"])

    def test_queue_mutations_are_not_agent_tools(self):
        ctx = MagicMock()
        register(ctx)
        tools = {call.kwargs["name"] for call in ctx.register_tool.call_args_list}
        self.assertEqual(tools, {"email_search", "email_read", "email_draft", "email_send", "email_propose_reply"})
        ctx.register_rpc.assert_not_called()

    def test_proposal_uses_actual_mail_headers_and_an_edit_leaves_the_recipient_intact(self):
        account = mail.Account("me@example.test", "fixture", "imap.example.test", "smtp.example.test")
        box = MagicMock()
        box.__enter__.return_value.headers_of.return_value = {
            "reply_address": "reply@example.test", "message_id": "<source@example.test>",
            "from": [{"name": "Source", "address": "source@example.test"}],
            "subject": "Work request", "date": "2026-11-27T10:00:00+04:00",
        }
        with patch.object(review_api, "_queue", return_value=self.queue), \
                patch.object(mail, "Mailbox", return_value=box), patch.object(mail, "saved_account", return_value=account), \
                patch.object(mail, "send") as transmit:
            result = review_api.propose_reply(account, {
                "reply_to_id": "INBOX:31", "body": "Proposed reply",
                "recommendation": "Confirm the meeting", "to": ["wrong@example.test"],
            })
            self.assertTrue(result["created"])
            alias = review_api.propose_reply(account, {
                "reply_to_id": "All Mail:55", "body": "Duplicate alias",
                "recommendation": "The same message",
            })
            self.assertEqual(alias, {"id": result["id"], "created": False})
            proposal = review_api.list_proposals({})["items"][0]
            self.assertEqual(proposal["source"]["from"][0]["address"], "source@example.test")
            self.assertEqual(proposal["draft"]["to"], ["reply@example.test"])
            self.assertEqual(proposal["draft"]["subject"], "Re: Work request")
            changed = review_api.edit_proposal({
                "id": proposal["id"], "revision": proposal["revision"], "subject": "Mine",
                "body": "My reply", "to": ["wrong@example.test"],
            })
            self.assertEqual(changed["item"]["draft"]["to"], ["reply@example.test"])
            self.assertEqual(changed["item"]["draft"]["body"], "My reply")
            transmit.assert_not_called()

    def test_send_requires_current_approved_revisions_and_uses_the_exact_stored_text(self):
        account = mail.Account("me@example.test", "fixture", "imap.example.test", "smtp.example.test")
        scope = review_api._scope(account)
        source = {"id": "INBOX:31", "digest": "source-digest"}
        draft = {"to": ["reply@example.test"], "subject": "Original", "body": "Original"}
        proposal_id, _ = self.queue.propose(scope, "source", source, "Reply", draft)
        edit = {"to": ["reply@example.test"], "subject": "My subject", "body": "My words, exactly."}
        def inline(fn, *args):
            fn(*args)
        with patch.object(review_api, "_queue", return_value=self.queue), \
                patch.object(mail, "saved_account", return_value=account), \
                patch.object(review_api._delivery, "submit", side_effect=inline), \
                patch.object(mail, "send", return_value={"message_id": "fixture"}) as transmit:
            def send(revision, **extras):
                result = review_api.send_proposals({"items": [{"id": proposal_id, "revision": revision, **extras}]})
                return result["items"][0]["started"]
            self.assertFalse(send(1))
            first = self.queue.decide(scope, proposal_id, 1, "approved")
            changed = self.queue.edit(scope, proposal_id, first["revision"], edit)
            self.assertFalse(send(first["revision"]))
            self.assertFalse(send(changed["revision"]))
            transmit.assert_not_called()
            second = self.queue.decide(scope, proposal_id, changed["revision"], "approved")
            self.assertTrue(send(second["revision"], body="Ignored client-supplied text"))
            self.assertFalse(send(second["revision"]))
            transmit.assert_called_once_with(account, mail.draft_from_args(edit), expected_source="source-digest")
            self.assertEqual(self.queue.list(scope)[0]["status"], "sent")

    def test_send_stops_before_smtp_if_the_source_email_changed(self):
        account = mail.Account("me@example.test", "fixture", "imap.example.test", "smtp.example.test")
        draft = mail.draft_from_args({"to": ["reply@example.test"], "body": "Hello", "reply_to_id": "INBOX:31"})
        box = MagicMock()
        box.__enter__.return_value.headers_of.return_value = {"message_id": "<replacement@example.test>"}
        with patch.object(mail, "Mailbox", return_value=box), patch.object(mail, "smtp_session") as smtp:
            with self.assertRaises(mail.MailError) as raised:
                mail.send(account, draft, expected_source=mail.reply_source_digest({"message_id": "<old@example.test>"}))
            self.assertEqual(raised.exception.kind, "source_changed")
            smtp.assert_not_called()

    def test_duplicate_chat_approval_cannot_grant_a_second_send(self):
        approvals = mail.Approvals()
        first = "plugin_rule:" + approvals.ask("chat", "digest")
        self.assertTrue(approvals.answered(first, "once"))
        self.assertFalse(approvals.answered(first, "once"))
        self.assertTrue(approvals.spend("chat", "digest"))
        second = "plugin_rule:" + approvals.ask("chat", "digest")
        self.assertFalse(approvals.answered(first, "once"))
        self.assertFalse(approvals.spend("chat", "digest"))
        self.assertTrue(approvals.answered(second, "once"))
        self.assertTrue(approvals.spend("chat", "digest"))
