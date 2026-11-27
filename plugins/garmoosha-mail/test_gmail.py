import base64
from email.parser import BytesParser
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch

from . import gmail, mail, review_api
from .review import ReviewQueue


def provider_message(thread="thread1"):
    headers = {
        "From": "Sender <sender@example.test>", "To": "me@example.test", "Reply-To": "reply@example.test",
        "Message-ID": "<original@example.test>", "Subject": "Handoff", "References": "<root@example.test>",
    }
    html = b'<p>Original handoff</p><img src="https://fixture.example.test/tracker">'
    return {
        "id": "original", "threadId": thread, "labelIds": ["UNREAD"],
        "payload": {"mimeType": "multipart/mixed", "headers": [
            {"name": name, "value": value} for name, value in headers.items()
        ], "parts": [{"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(html).decode()}},
                      {"filename": "agenda.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "PRIVATE"}}]},
    }


class WorkspaceMailTests(unittest.TestCase):
    def setUp(self):
        self.storage = TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.queue = ReviewQueue(Path(self.storage.name))
        self.account = mail.Account("me@example.test", "", "", "", provider="gmail", workspace=True)
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.session.credentials.scopes = [gmail._GOOGLE + "gmail.modify"]
        self.identity = self.account.address
        self.source = provider_message()
        self.requests = []
        self.intercept = None
        self.patches = [patch.object(gmail, "_session", return_value=self.session),
                        patch.object(gmail, "_request", side_effect=self.request),
                        patch.object(mail, "saved_account", return_value=self.account),
                        patch.object(review_api, "_queue", return_value=self.queue)]
        for handle in self.patches:
            handle.start()
            self.addCleanup(handle.stop)

    def request(self, session, method, path, **kwargs):
        self.assertIs(session, self.session)
        self.requests.append((method, path, kwargs))
        if self.intercept:
            self.intercept(method, path)
        if path == "/profile":
            return {"emailAddress": self.identity}
        if path == "/messages":
            return {"messages": [{"id": "original"}], "resultSizeEstimate": 1}
        if path == "/messages/original":
            return self.source
        if path == "/messages/send":
            return {"id": "sent"}
        if path == "/drafts":
            return {"id": "saved-draft", "message": {"id": "draftMessage"}}
        self.fail(f"Unexpected Gmail route {path}")

    def proposal(self):
        answer = review_api.propose_reply(self.account, {
            "reply_to_id": "gmail:original", "recommendation": "Confirm your availability.", "body": "Friday works.",
        })
        return self.queue.get(review_api._scope(self.account), answer["id"])

    def test_binding_belongs_to_its_profile_and_does_not_reuse_imap_approvals(self):
        config = ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {
            "plugins": {"entries": {"garmoosha-mail": {"settings": {"workspace_gmail": ["work", self.account.address]}}}},
        }
        with patch.dict(sys.modules, {config.__name__: config}), \
                patch.object(mail, "current_agent", side_effect=("work", "personal")), \
                patch.object(mail, "profile_secrets", return_value={}):
            work, inherited = mail.active_account_settings()
            self.assertFalse(inherited)
            self.assertEqual(mail.account_from(work), (self.account, []))
            personal, _ = mail.active_account_settings()
            self.assertFalse(personal)
        original = mail.Account(self.account.address, "fixture", "imap.example.test", "smtp.example.test")
        draft = mail.draft_from_args({"to": ["reply@example.test"], "body": "Test"})
        self.assertNotEqual(review_api._scope(self.account), review_api._scope(original))
        self.assertNotEqual(draft.digest(self.account), draft.digest(original))

    def test_source_and_search_are_read_only_and_mail_is_plain_text(self):
        with mail.open_mailbox(self.account) as box:
            results = box.search("newer_than:1d")
            self.assertEqual(results["folder"], "All Mail")
            self.assertTrue(results["messages"][0]["unread"])
        item = self.proposal()
        self.assertEqual(item["draft"]["to"], ["reply@example.test"])
        preview = review_api.preview_source({"id": item["id"]})
        self.assertEqual(preview, {"ok": True, "body": "Original handoff", "truncated": False, "attachments": ["agenda.pdf"]})
        self.assertTrue(all(verb == "GET" for verb, _, _ in self.requests))
        self.assertTrue(any(params.get("params", {}).get("q") == "newer_than:1d" for _, _, params in self.requests))

    def test_search_folders_translate_to_gmail_labels_or_archive_filter(self):
        with mail.open_mailbox(self.account) as box:
            for folder in ("", "drafts", "junk", "trash", "archive"):
                box.search("Handoff", folder)
        requests = [values["params"] for _, path, values in self.requests if path == "/messages"]
        self.assertNotIn("labelIds", requests[0])
        self.assertEqual(requests[1]["labelIds"], "DRAFT")
        self.assertEqual((requests[2]["labelIds"], requests[2]["includeSpamTrash"]), ("SPAM", "true"))
        self.assertEqual((requests[3]["labelIds"], requests[3]["includeSpamTrash"]), ("TRASH", "true"))
        self.assertEqual(requests[4]["q"], "in:archive Handoff")

    def test_mailbox_drafts_need_compose_permission_and_return_the_message_id(self):
        draft = mail.draft_from_args({"reply_to_id": "gmail:original", "body": "Friday works."})
        self.session.credentials.scopes = [gmail._GOOGLE + "gmail.readonly", gmail._GOOGLE + "gmail.send"]
        with self.assertRaisesRegex(mail.MailError, "email_propose_reply"):
            mail.draft(self.account, draft)
        self.assertFalse(any(method == "POST" for method, _, _ in self.requests))
        self.session.credentials.scopes.append(gmail._GOOGLE + "gmail.compose")
        self.assertEqual(mail.draft(self.account, draft)["id"], "gmail:draftMessage")

    def test_only_a_claimed_approval_sends_the_edited_reply_in_its_thread(self):
        item = self.proposal()
        selection = {"id": item["id"], "revision": item["revision"]}
        with patch.object(review_api._delivery, "submit", side_effect=lambda function, *args: function(*args)):
            self.assertFalse(review_api.send_proposals({"items": [selection]})["items"][0]["started"])
            changed = review_api.edit_proposal({**selection, "subject": "Re: Handoff", "body": "Monday works."})["item"]
            approved = review_api.decide_proposal({"id": item["id"], "revision": changed["revision"], "choice": "approved"})["item"]
            selection["revision"] = approved["revision"]
            self.assertTrue(review_api.send_proposals({"items": [selection]})["items"][0]["started"])
            self.assertFalse(review_api.send_proposals({"items": [selection]})["items"][0]["started"])
        sent, = [kwargs["body"] for method, path, kwargs in self.requests if method == "POST" and path == "/messages/send"]
        self.assertEqual(sent["threadId"], "thread1")
        message = BytesParser(policy=mail.DEFAULT_POLICY).parsebytes(base64.urlsafe_b64decode(sent["raw"]))
        self.assertEqual(str(message["To"]), "reply@example.test")
        self.assertEqual(str(message["In-Reply-To"]), "<original@example.test>")
        self.assertEqual(str(message["References"]), "<root@example.test> <original@example.test>")
        self.assertEqual(message.get_content().strip(), "Monday works.")
        self.assertEqual(self.queue.get(review_api._scope(self.account), item["id"])["status"], "sent")

    def test_identity_or_thread_changes_can_neither_read_nor_send_that_proposal(self):
        item = self.proposal()
        self.source = provider_message(thread="replacement")
        self.assertEqual(review_api.preview_source({"id": item["id"]}), {"ok": False, "reason": "unavailable"})
        self.identity = "elsewhere@example.test"
        self.requests.clear()
        with self.assertRaises(mail.MailError) as caught:
            mail.send(self.account, mail.draft_from_args(item["draft"]))
        self.assertEqual(caught.exception.kind, "account_changed")
        self.assertEqual([path for _, path, _ in self.requests], ["/profile"])

    def test_an_unconfirmed_gmail_send_remains_uncertain(self):
        item = self.proposal()
        approved = review_api.decide_proposal({"id": item["id"], "revision": item["revision"], "choice": "approved"})["item"]

        def unconfirmed(method, path):
            if method == "POST" and path == "/messages/send":
                raise mail.MailError("unknown", "gmail", "Gmail did not confirm delivery")

        self.intercept = unconfirmed
        with patch.object(review_api._delivery, "submit", side_effect=lambda function, *args: function(*args)):
            self.assertTrue(review_api.send_proposals({"items": [{"id": item["id"], "revision": approved["revision"]}]})["items"][0]["started"])
        saved = self.queue.get(review_api._scope(self.account), item["id"])
        self.assertEqual(saved["status"], "uncertain")
        self.assertFalse(review_api.decide_proposal({"id": item["id"], "revision": saved["revision"], "choice": "approved"})["ok"])

    def test_denied_gmail_send_requires_a_new_user_approval(self):
        item = self.proposal()
        approved = review_api.decide_proposal({"id": item["id"], "revision": item["revision"], "choice": "approved"})["item"]

        def denied(method, path):
            if method == "POST" and path == "/messages/send":
                raise mail.MailError("google_signin", "gmail", "Gmail declined the connection")

        self.intercept = denied
        with patch.object(review_api._delivery, "submit", side_effect=lambda function, *args: function(*args)):
            self.assertTrue(review_api.send_proposals({"items": [{"id": item["id"], "revision": approved["revision"]}]})["items"][0]["started"])
        failed = self.queue.get(review_api._scope(self.account), item["id"])
        self.assertEqual((failed["status"], failed["receipt"]), ("failed", {"kind": "google_signin"}))
        attempt = {"id": item["id"], "revision": failed["revision"]}
        self.assertFalse(review_api.send_proposals({"items": [attempt]})["items"][0]["started"])
        self.assertTrue(review_api.decide_proposal({**attempt, "choice": "approved"})["ok"])
