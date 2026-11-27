from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from . import mail, register, review_api
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
        with ThreadPoolExecutor(max_workers=4) as clients:
            claims = list(clients.map(lambda _: self.queue.claim("mailbox", proposal_id, revision), range(8)))
        self.assertEqual(sum(item is not None for item in claims), 1)
        self.assertIsNone(self.queue.edit("mailbox", proposal_id, revision, {"body": "Too late"}))
        settled = self.queue.settle("mailbox", proposal_id, revision, {"message_id": "fixture"}, sent=True)
        self.assertEqual((settled["status"], settled["receipt"]), ("sent", {"message_id": "fixture"}))
        self.assertIsNone(self.queue.claim("mailbox", proposal_id, settled["revision"]))

    def test_unknown_delivery_cannot_be_retried_silently(self):
        proposal_id, _ = self.propose()
        approved = self.queue.decide("mailbox", proposal_id, 1, "approved")
        self.queue.claim("mailbox", proposal_id, approved["revision"])
        uncertain = self.queue.settle("mailbox", proposal_id, approved["revision"], {}, sent=False)
        self.assertEqual(uncertain["status"], "uncertain")
        self.assertIsNone(self.queue.claim("mailbox", proposal_id, uncertain["revision"]))
        self.assertEqual(self.queue.decide("mailbox", proposal_id, uncertain["revision"], "completed")["status"], "completed")

    def test_another_mailbox_cannot_read_or_change_a_proposal(self):
        proposal_id, _ = self.propose()
        self.assertEqual(self.queue.list("other"), [])
        self.assertIsNone(self.queue.get("other", proposal_id))
        self.assertIsNone(self.queue.decide("other", proposal_id, 1, "approved"))
        self.assertIsNone(self.queue.claim("other", proposal_id, 1))
        self.assertTrue(self.queue.propose("other", "source", self.source, "Another reply", self.draft)[1])

    def test_proposal_tool_and_client_methods_are_registered_together(self):
        ctx = MagicMock()
        register(ctx)
        tools = {call.kwargs["name"] for call in ctx.register_tool.call_args_list}
        methods = {call.args[0] for call in ctx.register_rpc.call_args_list}
        self.assertIn("email_propose_reply", tools)
        self.assertEqual(methods, {"review.list", "review.edit", "review.decide"})
        ctx = MagicMock(register_rpc=None)
        register(ctx)
        self.assertNotIn("email_propose_reply", {call.kwargs["name"] for call in ctx.register_tool.call_args_list})

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
