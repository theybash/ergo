"""WhatsApp: the owner routes chats on Config, and agents read only routed chats.

The bridge itself is a Node service and is not run here. These tests write the files it writes and
check what the daemon makes of them.
"""

import http.client
import json
from pathlib import Path
import threading
import time
from http.server import ThreadingHTTPServer

from commandcenter import config, registry, server, whatsapp
from tests.harness import Case
from tests.test_tools import ask as tool_ask, payload as tool_payload

CONSOLE = {"X-Forwarded-For": "100.64.0.2"}
GROUP = "120363000000000001@g.us"
ASHA = "12025550101@s.whatsapp.net"
CREDIT = "120363000000000002@g.us"


class WhatsAppCase(Case):
    def setUp(self):
        super().setUp()
        self.pa = self.agent("personal-assistant")
        work = self.dir / "research"
        work.mkdir(parents=True)
        self.acme = registry.create(name="research", cwd=str(work))
        self.wa = config.VAR / "whatsapp"
        (self.wa / "messages").mkdir(parents=True)
        self.seen([{"id": GROUP, "name": "Acme", "kind": "group", "last_at": 100},
                   {"id": ASHA, "name": "Asha", "kind": "dm", "last_at": 200}])

    def seen(self, rows):
        (self.wa / "chats-seen.json").write_text(json.dumps(rows))

    def said(self, chat, text, ago=60, who="Ravi"):
        line = {"id": f"x{time.time_ns()}", "at": int(time.time() - ago), "chat": chat,
                "from": who, "text": text}
        with open(self.wa / "messages" / f"{whatsapp.safe_id(chat)}.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")


class Routing(WhatsAppCase):
    def test_a_chat_is_routed_to_its_agent(self):
        whatsapp.route({"id": ASHA, "agent": self.pa["id"]})
        whatsapp.route({"id": GROUP, "agent": self.acme["id"]})
        allow = json.loads((self.wa / "chats.json").read_text())
        self.assertEqual(allow[ASHA], {"name": "Asha", "agent": self.pa["id"],
                                       "side": "personal"})
        self.assertEqual(allow[GROUP]["agent"], self.acme["id"])

    def test_an_agent_follows_a_chat_itself_but_never_takes_another_agents(self):
        self.assertEqual(whatsapp.find("ash")["chats"][0]["id"], ASHA)
        whatsapp.follow(self.pa["id"], ASHA)
        self.assertEqual(whatsapp.allowlist()[ASHA]["agent"], self.pa["id"])
        with self.assertRaises(ValueError):
            whatsapp.follow(self.acme["id"], ASHA)
        with self.assertRaises(ValueError):
            whatsapp.follow(self.acme["id"], ASHA, on=False)
        got = whatsapp.follow(self.pa["id"], "+1 202 555 0100")
        self.assertEqual(got["id"], "12025550100@s.whatsapp.net")
        whatsapp.follow(self.pa["id"], ASHA, on=False)
        self.assertNotIn(ASHA, whatsapp.allowlist())

    def test_an_agent_that_does_not_exist_is_refused(self):
        with self.assertRaises(ValueError):
            whatsapp.route({"id": ASHA, "agent": "nobody"})
        self.assertFalse((self.wa / "chats.json").exists())

    def test_unfollowing_drops_the_route_and_the_stored_messages(self):
        whatsapp.route({"id": ASHA, "agent": self.pa["id"]})
        self.said(ASHA, "dinner at 8?")
        whatsapp.route({"id": ASHA, "agent": None})
        self.assertEqual(whatsapp.allowlist(), {})
        self.assertFalse((self.wa / "messages" / f"{whatsapp.safe_id(ASHA)}.jsonl").exists())

    def test_the_listing_shows_followed_chats_first_and_the_agents_to_pick(self):
        whatsapp.route({"id": GROUP, "agent": self.acme["id"]})
        got = whatsapp.chats()
        self.assertEqual([c["id"] for c in got["chats"]], [GROUP, ASHA])
        self.assertEqual(got["chats"][0]["agent"], self.acme["id"])
        self.assertIn(self.pa["id"], [a["id"] for a in got["agents"]])

    def test_status_shows_the_qr_only_while_not_linked(self):
        (self.wa / "qr.txt").write_text("2@abc")
        (self.wa / "status.json").write_text(json.dumps({"linked": False, "qr_at": "now"}))
        self.assertEqual(whatsapp.status()["qr"], "2@abc")
        (self.wa / "status.json").write_text(json.dumps({"linked": True, "since": "then"}))
        got = whatsapp.status()
        self.assertEqual((got["linked"], got["qr"], got["since"]), (True, None, "then"))


class Reading(WhatsAppCase):
    def test_an_agent_with_no_chats_is_told_so(self):
        got = whatsapp.read(self.pa["id"])
        self.assertEqual(got["messages"], [])
        self.assertIn("not routed", got["note"])

    def test_old_messages_are_outside_the_window_and_the_newest_come_last(self):
        whatsapp.route({"id": GROUP, "agent": self.acme["id"]})
        self.said(GROUP, "two days ago", ago=48 * 3600)
        self.said(GROUP, "second", ago=60)
        self.said(GROUP, "first", ago=120)
        got = whatsapp.read(self.acme["id"], 24)
        self.assertEqual([m.rsplit(": ", 1)[1] for m in got["messages"]], ["first", "second"])

    def test_a_long_day_is_capped_to_the_newest_lines(self):
        whatsapp.route({"id": GROUP, "agent": self.acme["id"]})
        for i in range(whatsapp.MAX_LINES + 5):
            self.said(GROUP, f"m{i}", ago=3600 - i)
        got = whatsapp.read(self.acme["id"])
        self.assertEqual(len(got["messages"]), whatsapp.MAX_LINES)
        self.assertTrue(got["messages"][-1].endswith(f"m{whatsapp.MAX_LINES + 4}"))
        self.assertIn("5 older", got["note"])

    def other(self):
        (self.dir / "initech").mkdir(parents=True, exist_ok=True)
        return registry.create(name="initech", cwd=str(self.dir / "initech"))

    def test_every_agent_reads_all_routed_chats_or_the_ones_it_names(self):
        # The agent decides what it reads; the daemon only serves it.
        whatsapp.route({"id": GROUP, "agent": self.acme["id"]})
        self.said(GROUP, "LP portal goes out today")
        other = self.other()
        got = whatsapp.read(other["id"])
        self.assertTrue(any("LP portal" in m for m in got["messages"]))
        self.assertEqual(got["on_your_side"], ["Acme"])
        got = whatsapp.read(other["id"], chats="acme")
        self.assertTrue(any("LP portal" in m for m in got["messages"]))
        self.assertTrue(whatsapp.draft(other["id"], GROUP, "Noted, thanks"))

    def test_one_chat_or_several_by_comma_or_a_part_of_a_name(self):
        self.seen([{"id": GROUP, "name": "Acme", "kind": "group", "last_at": 100},
                   {"id": CREDIT, "name": "Private credit", "kind": "group", "last_at": 50}])
        whatsapp.route({"id": GROUP, "agent": self.acme["id"]})
        whatsapp.route({"id": CREDIT, "agent": self.acme["id"]})
        self.said(GROUP, "deck by friday")
        self.said(CREDIT, "term sheet in")
        other = self.other()
        for ask in ("Acme, Private credit", ["acme", "credit"], None):
            got = whatsapp.read(other["id"], chats=ask)
            self.assertEqual(len(got["messages"]), 2, ask)
        got = whatsapp.read(other["id"], chats="credit, nothing like it")
        self.assertEqual(len(got["messages"]), 1)
        self.assertIn("Not routed: nothing like it", got["note"])

    def test_the_list_shows_the_routed_chats_with_counts_and_no_messages(self):
        self.seen([{"id": GROUP, "name": "Acme", "kind": "group", "last_at": 100},
                   {"id": CREDIT, "name": "Private credit", "kind": "group", "last_at": 50},
                   {"id": ASHA, "name": "Asha", "kind": "dm", "last_at": 200}])
        whatsapp.route({"id": GROUP, "agent": self.acme["id"]})
        whatsapp.route({"id": CREDIT, "agent": self.acme["id"]})
        self.said(CREDIT, "term sheet in", ago=48 * 3600)
        self.said(GROUP, "deck by friday")
        self.said(GROUP, "and the LP list")
        got = whatsapp.list_chats(self.other()["id"], 24)
        self.assertEqual([(c["name"], c["new"]) for c in got["chats"]],
                         [("Acme", 2), ("Private credit", 0)])
        self.assertIsNotNone(got["chats"][1]["last"])
        self.assertNotIn("deck", json.dumps(got))
        self.assertNotIn("Asha", json.dumps(got), "a chat nobody routed is not listed")

    def test_the_list_shows_who_wrote_last_even_from_before_the_follow(self):
        self.seen([{"id": ASHA, "name": "Asha", "kind": "dm",
                    "last_at": int(time.time() - 9 * 86400), "last_me": True}])
        whatsapp.follow(self.pa["id"], ASHA)
        row = whatsapp.list_chats(self.pa["id"])["chats"][0]
        self.assertEqual((row["new"], row["last_from"]), (0, "The owner"))
        self.assertIsNotNone(row["last"])
        self.said(ASHA, "long time!", who="Asha")
        self.assertEqual(whatsapp.list_chats(self.pa["id"])["chats"][0]["last_from"], "them")

    def test_two_chats_with_one_name_are_told_apart_by_kind(self):
        PERSON = "10000000000001@lid"
        self.seen([{"id": GROUP, "name": "Northwind", "kind": "group", "last_at": 100},
                   {"id": PERSON, "name": "Northwind", "kind": "dm", "last_at": 90}])
        whatsapp.route({"id": GROUP, "agent": self.acme["id"]})
        whatsapp.route({"id": PERSON, "agent": self.acme["id"]})
        self.said(GROUP, "meetup on sunday")
        self.said(PERSON, "your ticket is booked")
        listed = whatsapp.list_chats(self.acme["id"])["chats"]
        self.assertEqual(sorted((c["name"], c["kind"]) for c in listed),
                         [("Northwind (group)", "group"), ("Northwind (person)", "person")])
        one = whatsapp.read(self.acme["id"], chats="northwind (person)")
        self.assertEqual([c["name"] for c in one["chats"]], ["Northwind (person)"])
        self.assertTrue(any("[Northwind (person)]" in m and "ticket" in m for m in one["messages"]))
        both = whatsapp.read(self.acme["id"], chats="Northwind")
        self.assertEqual(len(both["messages"]), 2)



class Photos(WhatsAppCase):
    def photo(self, chat, name="p.jpg"):
        media = self.wa / "media"
        media.mkdir(exist_ok=True)
        (media / name).write_bytes(b"\xff\xd8 a photo")
        line = {"id": "ph1", "at": int(time.time() - 60), "chat": chat, "from": "Asha",
                "text": "[photo] the fan", "media": str(media / name)}
        with open(self.wa / "messages" / f"{whatsapp.safe_id(chat)}.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")

    def test_a_photo_is_copied_into_the_agents_inbox_and_named_on_its_line(self):
        whatsapp.route({"id": ASHA, "agent": self.pa["id"]})
        self.photo(ASHA)
        line = whatsapp.read(self.pa["id"])["messages"][0]
        self.assertIn("[photo] the fan (photo: ", line)
        path = line.split("(photo: ")[1].rstrip(")")
        self.assertTrue(path.startswith(self.pa["cwd"]))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"\xff\xd8 a photo")

    def test_a_file_someone_sent_is_named_a_file(self):
        whatsapp.route({"id": ASHA, "agent": self.pa["id"]})
        self.photo(ASHA, name="q.pdf")
        self.assertIn("(file: ", whatsapp.read(self.pa["id"])["messages"][0])

    def test_a_line_pointing_outside_the_media_folder_gives_nothing(self):
        whatsapp.route({"id": ASHA, "agent": self.pa["id"]})
        secret = self.dir / "secret.txt"
        secret.write_text("no")
        line = {"id": "ph2", "at": int(time.time() - 60), "chat": ASHA, "from": "x",
                "text": "[photo]", "media": str(secret)}
        with open(self.wa / "messages" / f"{whatsapp.safe_id(ASHA)}.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")
        self.assertNotIn("(photo:", whatsapp.read(self.pa["id"])["messages"][0])


class Sending(WhatsAppCase):
    def setUp(self):
        super().setUp()
        self.posted = []
        real = whatsapp.post_bridge
        whatsapp.post_bridge = lambda path, body, timeout=30.0: (
            self.posted.append((path, body)) or {"ok": True, "id": "M1"})
        self.addCleanup(lambda: setattr(whatsapp, "post_bridge", real))
        whatsapp.route({"id": ASHA, "agent": self.pa["id"]})

    def test_sending_is_off_by_default_and_a_send_is_refused(self):
        self.assertEqual(whatsapp.settings(), {"sending": False})
        with self.assertRaises(PermissionError):
            whatsapp.send(ASHA, "on my way")
        self.assertEqual(self.posted, [])

    def test_a_chat_that_is_not_followed_is_refused_even_with_sending_on(self):
        whatsapp.save_settings({"sending": True})
        with self.assertRaises(PermissionError):
            whatsapp.send(GROUP, "hello")
        self.assertEqual(self.posted, [])

    def test_with_sending_on_a_followed_chat_goes_to_the_bridge(self):
        whatsapp.save_settings({"sending": True})
        self.assertEqual(whatsapp.send(ASHA, "on my way")["id"], "M1")
        self.assertEqual(self.posted, [("/send", {"chat": ASHA, "text": "on my way"})])
        self.assertNotIn("on my way", (self.wa / "sent.json").read_text())

    def test_twenty_an_hour_and_no_more(self):
        whatsapp.save_settings({"sending": True})
        for i in range(whatsapp.SENDS_PER_HOUR):
            whatsapp.send(ASHA, f"m{i}")
        with self.assertRaises(PermissionError):
            whatsapp.send(ASHA, "one too many")
        self.assertEqual(len(self.posted), whatsapp.SENDS_PER_HOUR)

    def test_an_agent_drafts_only_for_routed_chats(self):
        got = whatsapp.draft(self.pa["id"], "Asha", "8 works")
        self.assertEqual(got["chat"], ASHA)
        with self.assertRaises(PermissionError):
            whatsapp.draft(self.pa["id"], GROUP, "deck is coming")
        self.assertEqual(len(whatsapp.drafts()["drafts"]), 1)
        self.assertEqual(self.posted, [])

    def test_a_draft_is_sent_as_edited_and_then_gone_but_kept_when_refused(self):
        item = whatsapp.draft(self.pa["id"], ASHA, "8 works")
        with self.assertRaises(PermissionError):
            whatsapp.send_draft(item["id"])
        self.assertEqual(len(whatsapp.drafts()["drafts"]), 1)
        whatsapp.save_settings({"sending": True})
        whatsapp.send_draft(item["id"], "8:30 works")
        self.assertEqual(self.posted[-1][1]["text"], "8:30 works")
        self.assertEqual(whatsapp.drafts()["drafts"], [])


    def test_a_draft_with_a_file_from_the_outbox_sends_the_file(self):
        from commandcenter import files
        box = files.outbox(self.pa)
        box.mkdir(parents=True, exist_ok=True)
        (box / "plan.pdf").write_bytes(b"%PDF the plan")
        with self.assertRaises(ValueError):
            whatsapp.draft(self.pa["id"], ASHA, "", "../../secret.txt")
        item = whatsapp.draft(self.pa["id"], ASHA, "", "plan.pdf")
        self.assertEqual((item["file"], item["photo"]), ("plan.pdf", False))
        whatsapp.save_settings({"sending": True})
        whatsapp.send_draft(item["id"])
        sent = self.posted[-1][1]
        self.assertEqual(sent["file_name"], "plan.pdf")
        self.assertTrue(sent["file"].startswith(str(self.wa / "outgoing")))
        self.assertFalse(Path(sent["file"]).exists())

class Routes(WhatsAppCase):
    def setUp(self):
        super().setUp()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def hit(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json", **(headers or {})})
        reply = conn.getresponse()
        got = json.loads(reply.read() or b"{}")
        conn.close()
        return reply.status, got

    def test_an_agent_on_the_laptop_cannot_route_a_chat(self):
        code, _ = self.hit("POST", "/api/whatsapp/chats", {"id": ASHA, "agent": self.pa["id"]})
        self.assertEqual(code, 403)
        self.assertEqual(whatsapp.allowlist(), {})

    def test_the_console_routes_and_the_agent_reads_through_its_tool(self):
        code, got = self.hit("POST", "/api/whatsapp/chats",
                             {"id": ASHA, "agent": self.pa["id"]}, CONSOLE)
        self.assertEqual((code, got["chats"][0]["agent"]), (200, self.pa["id"]))
        code, _ = self.hit("POST", "/api/whatsapp/chats", {"id": ASHA, "agent": "nobody"},
                           CONSOLE)
        self.assertEqual(code, 400)
        self.said(ASHA, "dinner at 8?", who="Asha")
        got = tool_payload(tool_ask("whatsapp_read", {"since_hours": 2},
                                    agent=self.pa["id"], api=self.base))
        self.assertIn("dinner at 8?", got["messages"][0])

    def test_an_agent_on_the_laptop_cannot_send_or_turn_sending_on(self):
        whatsapp.route({"id": ASHA, "agent": self.pa["id"]})
        whatsapp.save_settings({"sending": True})
        posted = []
        real = whatsapp.post_bridge
        whatsapp.post_bridge = lambda path, body, timeout=30.0: posted.append(body) or {}
        self.addCleanup(lambda: setattr(whatsapp, "post_bridge", real))
        code, _ = self.hit("POST", "/api/whatsapp/send", {"chat": ASHA, "text": "hi"})
        self.assertEqual(code, 403)
        code, _ = self.hit("POST", "/api/whatsapp/settings", {"sending": False})
        self.assertEqual(code, 403)
        item = tool_payload(tool_ask("whatsapp_draft", {"chat": "Asha", "text": "hi"},
                                     agent=self.pa["id"], api=self.base))
        code, _ = self.hit("POST", f"/api/whatsapp/drafts/{item['draft']}/send", {})
        self.assertEqual(code, 403)
        self.assertEqual(posted, [])
        code, got = self.hit("POST", f"/api/whatsapp/drafts/{item['draft']}/send", {}, CONSOLE)
        self.assertEqual((code, len(posted)), (200, 1))

    def test_the_console_send_is_refused_while_the_switch_is_off(self):
        whatsapp.route({"id": ASHA, "agent": self.pa["id"]})
        code, got = self.hit("POST", "/api/whatsapp/send", {"chat": ASHA, "text": "hi"}, CONSOLE)
        self.assertEqual(code, 403)
        self.assertIn("off", got["error"])

    def test_status_and_chats_are_there_for_config(self):
        code, got = self.hit("GET", "/api/whatsapp/status", headers=CONSOLE)
        self.assertEqual((code, got["linked"]), (200, False))
        code, got = self.hit("GET", "/api/whatsapp/chats", headers=CONSOLE)
        self.assertEqual((code, len(got["chats"])), (200, 2))

    def test_the_bridge_pokes_and_the_console_hears_the_status_pushed(self):
        heard = []
        real = server.hub.publish
        server.hub.publish = lambda event, payload=None, topic=None: heard.append((event, payload, topic))
        self.addCleanup(lambda: setattr(server.hub, "publish", real))
        (self.wa / "qr.txt").write_text("2@abc")
        code, _ = self.hit("POST", "/api/whatsapp/changed", {"what": "status"})
        self.assertEqual(code, 200)
        self.assertEqual(heard[0][0], "whatsapp")
        self.assertEqual(heard[0][2], "whatsapp")
        self.assertEqual(heard[0][1]["status"]["qr"], "2@abc")


class Deaf(Case):
    """15 October: the bridge stayed linked and heard nothing, and an @ergo was missed. It now
    restarts itself, and the owner hears of it each time."""

    def setUp(self):
        super().setUp()
        whatsapp._deaf.update(day=None, count=0)
        self.sent = self.catch_alerts()

    def test_each_deaf_spell_reaches_owner(self):
        whatsapp.went_deaf()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("heard nothing", self.sent[0])
        self.assertNotIn("needs a look", self.sent[0])

    def test_the_third_in_a_day_says_the_fix_is_not_holding(self):
        for _ in range(3):
            whatsapp.went_deaf()
        self.assertIn("3 times today", self.sent[-1])
