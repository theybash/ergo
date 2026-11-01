"""Email drafts: an agent drafts, the owner's tap sends after a minute's undo."""

import time

from commandcenter import files, mail
from tests.harness import Case


class Mail(Case):
    def test_a_draft_waits_for_the_tap_then_goes_once(self):
        self.addCleanup(mail.reset)
        rec = self.agent("scout")
        box = files.outbox(rec)
        box.mkdir(parents=True, exist_ok=True)
        (box / "cv.pdf").write_bytes(b"%PDF-1.4")
        sent = []
        real = mail._send
        mail._send = lambda row: sent.append(row) or {"id": "g1"}
        self.addCleanup(setattr, mail, "_send", real)
        d = mail.draft("scout", "hr@example.com", "Application", "Hello.", attach=["cv.pdf"],
                       send_at="2030-01-02 09:30")
        self.assertEqual((d["state"], d["attach"]), ("draft", ["cv.pdf"]))
        with self.assertRaises(ValueError):
            mail.draft("scout", "not an address", "x", "y")

        queued = mail.schedule(d["id"], None, {"subject": "Application, the owner"})
        self.assertGreaterEqual(queued["send_at"], time.time() + mail.UNDO_SECONDS - 2, "a minute to undo")
        self.assertEqual(mail.cancel(d["id"])["state"], "draft", "undo")
        mail.schedule(d["id"])
        rows = mail._rows()
        rows[0]["send_at"] = time.time() - 1
        mail._save(rows)
        mail._fire(d["id"])
        mail._fire(d["id"])
        self.assertEqual(len(sent), 1, "once")
        self.assertEqual(sent[0]["subject"], "Application, the owner", "as edited")
        self.assertEqual(mail._rows(), [], "gone once sent")
        self.addCleanup(setattr, mail, "FROM", mail.FROM)
        mail.FROM = "owner@example.com"
        msg = mail._message(dict(sent[0], body="Hello."))
        self.assertEqual(msg["From"], mail.FROM)
        self.assertEqual([p.get_filename() for p in msg.iter_attachments()], ["cv.pdf"])
