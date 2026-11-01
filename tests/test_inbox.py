"""The inbox: the event log, its fold, its rules, its routes, its pings, and the three Jev moments.

TypeSafe is never called. `memory.post_jev` is replaced, as the referee's tests do, and the request
that would have gone out is read back. Telegram is never called either: `inbox._ping` is replaced.
"""

import datetime as dt
import http.client
import json
import logging
import threading
from http.server import ThreadingHTTPServer

from commandcenter import broker, clock, config, cron, inbox, memory, server, voice
from tests.harness import Case, json_route
from tests.test_tools import ask as tool_ask, payload as tool_payload

KEY = "ts-not-a-real-key-0123456789"


def local(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi).timestamp()


class InboxCase(Case):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.answer = None
        real = memory.post_jev

        def fake(payload, key, timeout=None):
            self.sent.append({"payload": payload, "key": key, "timeout": timeout})
            if isinstance(self.answer, Exception):
                raise self.answer
            if callable(self.answer):
                return self.answer(payload)
            return self.answer if self.answer is not None else {"answers": {}}

        memory.post_jev = fake
        self.addCleanup(lambda: setattr(memory, "post_jev", real))
        self.pings = []
        real_ping = inbox._ping
        inbox._ping = lambda text: self.pings.append(text) or True
        self.addCleanup(lambda: setattr(inbox, "_ping", real_ping))

    def add(self, agent="pa", text="The dentist moved to four.", urgency="today", **kw):
        return inbox.add(agent, text, urgency, judge=False, **kw)

    def events(self):
        path = config.VOICE_DIR / "inbox.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def refold(self):
        inbox.reset()

    def nouls(self, ps):
        self.answer = {"answers": {i: {"type": "noul", "noul": p} for i, p in ps.items()}}

    def choices(self, picks):
        self.answer = {"answers": {i: {"type": "choice", "choice": c, "confidence": conf,
                                       "probabilities": {c: conf}}
                                   for i, (c, conf) in picks.items()}}


# -- the log and its fold --------------------------------------------------------

class Fold(InboxCase):
    def test_every_event_folds_into_the_right_state(self):
        rows = [{"event": "added", "id": f"a{n}", "agent": "pa", "text": f"item {n}",
                 "urgency": "today", "expires": clock.now() + 9000, "link": "", "at": clock.now()}
                for n in range(10)]
        rows += [
            {"event": "told", "id": "a1", "by": "jev", "p": 0.9},
            {"event": "handled", "id": "a2", "by": "jev", "p": 0.9},
            {"event": "told", "id": "a3", "by": "jev", "p": 0.9},
            {"event": "later", "id": "a3", "by": "jev", "p": 0.9},
            {"event": "dismissed", "id": "a4", "by": "jev"},
            {"event": "opened", "id": "a5", "by": "tap"},
            {"event": "replaced", "id": "a6", "by": "a7"},
            {"event": "expired", "id": "a8"},
            {"event": "removed", "id": "a9", "by": "config"},
            {"event": "removed", "id": "a0", "by": "config"},
            {"event": "restored", "id": "a0"},
            {"event": "told", "id": "nobody"},
            "not an event",
        ]
        for row in rows:
            if isinstance(row, dict):
                row.setdefault("at", clock.now())
        path = config.VOICE_DIR / "inbox.jsonl"
        config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text("".join((json.dumps(r) if isinstance(r, dict) else r) + "\n"
                                for r in rows) + "{broken\n")
        self.refold()
        got = {i: inbox.get(i) for i in (f"a{n}" for n in range(10))}
        self.assertEqual(got["a0"]["status"], "waiting")
        self.assertEqual(got["a0"]["decision"], "restored")
        self.assertEqual((got["a1"]["status"], got["a1"]["decision"], got["a1"]["p"]),
                         ("told", "told", 0.9))
        self.assertEqual((got["a2"]["status"], got["a2"]["decision"]), ("closed", "handled"))
        self.assertEqual((got["a3"]["status"], got["a3"]["decision"]), ("waiting", "later"))
        self.assertEqual(got["a4"]["decision"], "dismissed")
        self.assertEqual(got["a5"]["decision"], "opened")
        self.assertEqual((got["a6"]["decision"], got["a6"]["replaced_by"]), ("replaced", "a7"))
        self.assertEqual(got["a7"]["status"], "waiting")
        self.assertEqual(got["a8"]["decision"], "expired")
        self.assertEqual((got["a9"]["decision"], got["a9"]["by"]), ("removed", "config"))
        listing = inbox.listing()
        self.assertEqual({i["id"] for i in listing["waiting"]}, {"a0", "a3", "a7"})
        self.assertEqual({i["id"] for i in listing["told"]},
                         {"a1", "a2", "a4", "a5", "a6", "a8", "a9"})
        self.assertEqual(listing["summary"]["untold"], 3)

    def test_the_daemon_appends_and_a_restart_reads_the_same(self):
        a = self.add()
        inbox.act(a["id"], "told", by="jev", p=0.93)
        before = inbox.listing()
        self.refold()
        self.assertEqual(inbox.listing(), before)
        self.assertEqual([e["event"] for e in self.events()], ["added", "told"])

    def test_nothing_in_the_log_is_the_key(self):
        config.TYPESAFE_KEY = KEY
        self.add()
        self.assertNotIn(KEY, (config.VOICE_DIR / "inbox.jsonl").read_text())


class Rules(InboxCase):
    def test_the_same_text_from_the_same_agent_within_the_hour_is_one_item(self):
        a = self.add(text="The dentist moved to four.")
        b = self.add(text="  the dentist moved   to four. ")
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["id"], b["id"])
        other = self.add(agent="job-search", text="The dentist moved to four.")
        self.assertNotEqual(other["id"], a["id"])
        clock.advance(3601)
        c = self.add(text="The dentist moved to four.")
        self.assertFalse(c["duplicate"])
        self.assertNotEqual(c["id"], a["id"])
        self.assertEqual(len([e for e in self.events() if e["event"] == "added"]), 3)

    def test_urgency_must_be_one_of_three_and_text_is_needed(self):
        with self.assertRaises(ValueError):
            self.add(urgency="soon")
        with self.assertRaises(ValueError):
            self.add(text="   ")
        with self.assertRaises(ValueError):
            inbox.add("", "x")

    def test_now_expires_after_two_hours(self):
        a = self.add(urgency="now")
        clock.advance(2 * 3600 - 1)
        self.assertEqual(inbox.get(a["id"])["status"], "waiting")
        inbox.listing()
        self.assertEqual(inbox.get(a["id"])["status"], "waiting")
        clock.advance(2)
        inbox.listing()
        self.assertEqual(inbox.get(a["id"])["decision"], "expired")
        self.assertEqual(self.events()[-1]["event"], "expired")

    def test_today_expires_at_local_midnight_and_fyi_after_a_day(self):
        today = self.add(text="today one")
        fyi = self.add(text="fyi one", urgency="fyi")
        midnight = dt.datetime.combine(
            dt.datetime.fromtimestamp(clock.now()).date() + dt.timedelta(days=1),
            dt.time()).timestamp()
        self.assertEqual(today["expires"], midnight)
        self.assertEqual(fyi["expires"], clock.now() + 86400)
        clock.freeze(midnight)
        inbox.summary()
        self.assertEqual(inbox.get(today["id"])["decision"], "expired")
        self.assertEqual(inbox.get(fyi["id"])["status"], "waiting")

    def test_an_item_may_say_its_own_time(self):
        until = dt.datetime.fromtimestamp(clock.now() + 600).strftime("%Y-%m-%dT%H:%M:%S")
        a = self.add(expires=until)
        self.assertAlmostEqual(a["expires"], clock.now() + 600, delta=1)
        with self.assertRaises(ValueError):
            self.add(text="bad time", expires="half past never")

    def test_an_event_time_ends_an_item_after_its_ring_and_a_past_one_does_not(self):
        soon = self.add(text="Standup at ten.", urgency="urgent", when=clock.now() + 60)
        self.assertEqual(soon["expires"], clock.now() + 60 + inbox.RING_SECONDS)
        gone = self.add(text="The 09:00 call moved.", urgency="urgent", when=clock.now() - 60)
        self.assertEqual(gone["expires"], inbox.default_expiry("urgent", clock.now()))
        clock.advance(120)
        self.assertEqual(inbox.get(soon["id"])["status"], "waiting")
        self.assertEqual(inbox.get(gone["id"])["status"], "waiting")

    def test_later_puts_a_told_item_back_for_the_next_brief(self):
        a = self.add()
        inbox.act(a["id"], "told", by="jev", p=0.9)
        self.assertEqual(inbox.for_call(), [])
        inbox.act(a["id"], "later", by="jev", p=0.9)
        self.assertEqual([i["id"] for i in inbox.for_call()], [a["id"]])

    def test_a_brief_reads_the_most_urgent_first_then_the_oldest(self):
        f = self.add(text="fyi", urgency="fyi")
        t1 = self.add(text="today one")
        clock.advance(5)
        n = self.add(text="now one", urgency="now")
        t2 = self.add(text="today two")
        self.assertEqual([i["id"] for i in inbox.for_call()],
                         [n["id"], t1["id"], t2["id"], f["id"]])

    def test_restoring_an_expired_item_gives_it_a_fresh_life(self):
        a = self.add(urgency="now")
        clock.advance(3 * 3600)
        inbox.summary()
        inbox.act(a["id"], "restored")
        inbox.summary()
        self.assertEqual(inbox.get(a["id"])["status"], "waiting")

    def test_clear_needs_confirm_and_removes_only_the_waiting(self):
        a, b = self.add(text="one"), self.add(text="two")
        inbox.act(b["id"], "told")
        with self.assertRaises(ValueError):
            inbox.clear()
        with self.assertRaises(ValueError):
            inbox.clear("yes")
        self.assertEqual(inbox.clear(True), {"removed": 1})
        self.assertEqual(inbox.get(a["id"])["decision"], "removed")
        self.assertEqual(inbox.get(b["id"])["status"], "told")

    def test_an_unknown_action_and_an_unknown_item(self):
        a = self.add()
        with self.assertRaises(ValueError):
            inbox.act(a["id"], "shred")
        with self.assertRaises(KeyError):
            inbox.act("deadbeef00", "told")


class Compaction(InboxCase):
    def test_items_closed_over_thirty_days_ago_move_to_their_month(self):
        start = local(2026, 7, 10, 9, 0)
        clock.freeze(start)
        old = self.add(text="old one")
        inbox.act(old["id"], "handled")
        kept = self.add(text="still waiting", urgency="fyi", expires=start + 90 * 86400)
        clock.freeze(local(2026, 8, 19, 9, 0))
        self.refold()
        self.assertIsNone(inbox.get(old["id"]))
        self.assertIsNotNone(inbox.get(kept["id"]))
        moved = config.VOICE_DIR / "inbox-2026-07.jsonl"
        self.assertTrue(moved.exists())
        ids = {json.loads(line)["id"] for line in moved.read_text().splitlines()}
        self.assertEqual(ids, {old["id"]})
        self.assertNotIn(old["id"], (config.VOICE_DIR / "inbox.jsonl").read_text())

    def test_it_runs_once_a_month(self):
        clock.freeze(local(2026, 8, 1, 9, 0))
        a = self.add()
        inbox.act(a["id"], "handled")
        clock.freeze(local(2026, 8, 31, 12, 0))
        inbox.listing()
        # Same month: the closed item stays, even though 30 days have passed.
        self.assertIsNotNone(inbox.get(a["id"]))
        clock.freeze(local(2026, 9, 2, 9, 0))
        inbox.listing()
        self.assertIsNone(inbox.get(a["id"]))
        self.assertTrue((config.VOICE_DIR / "inbox-2026-08.jsonl").exists())


# -- automatic items -------------------------------------------------------------

class Automatic(InboxCase):
    def setUp(self):
        super().setUp()
        self.pending = {}
        real = broker.pending_asks
        broker.pending_asks = lambda agent: self.pending.get(agent, [])
        self.addCleanup(lambda: setattr(broker, "pending_asks", real))

    def test_a_new_ask_is_a_now_item_closed_when_answered(self):
        frame = {"t": "ask", "id": "req-1", "kind": "perm", "tool": "Bash",
                 "summary": "git push", "input": {}}
        self.pending["pa"] = [frame]
        broker._chat_frame("pa", frame)
        waiting = inbox.listing()["waiting"]
        self.assertEqual(len(waiting), 1)
        item = waiting[0]
        self.assertEqual((item["agent"], item["urgency"], item["ask"]), ("pa", "now", "req-1"))
        self.assertIn("Bash", item["text"])
        self.assertEqual(item["link"], "?screen=chat#/a/pa")
        # The same ask again is the same item.
        broker._chat_frame("pa", frame)
        self.assertEqual(len(inbox.listing()["waiting"]), 1)
        self.pending["pa"] = []
        broker._chat_frame("pa", {"t": "ask_done", "id": "req-1"})
        got = inbox.get(item["id"])
        self.assertEqual((got["decision"], got["by"]), ("handled", "ask"))

    def test_an_ask_that_is_gone_closes_on_the_next_read(self):
        frame = {"t": "ask", "id": "q-2", "kind": "question",
                 "questions": [{"question": "Which branch?"}]}
        self.pending["pa"] = [frame]
        broker._chat_frame("pa", frame)
        item = inbox.listing()["waiting"][0]
        self.assertIn("Which branch?", item["text"])
        self.pending["pa"] = []
        self.assertEqual(inbox.summary()["untold"], 0)
        self.assertEqual(inbox.get(item["id"])["decision"], "handled")

    def test_other_frames_do_not_touch_the_inbox(self):
        broker._chat_frame("pa", {"t": "live", "text": "hi"})
        self.assertEqual(inbox.listing()["waiting"], [])

    def test_a_failed_job_is_a_today_item(self):
        alerts = self.catch_alerts()
        job = {"id": "abc123", "agent": "job-search", "task": "check the drafts folder",
               "quiet": False}
        cron._report(job, "failed", "it broke")
        self.assertEqual(len(alerts), 1)
        waiting = inbox.listing()["waiting"]
        self.assertEqual(len(waiting), 1)
        self.assertEqual((waiting[0]["agent"], waiting[0]["urgency"]), ("job-search", "today"))
        self.assertIn("abc123", waiting[0]["text"])
        cron._report(job, "succeeded", "fine")
        self.assertEqual(len(inbox.listing()["waiting"]), 1)


# -- settings --------------------------------------------------------------------

class Settings(InboxCase):
    def test_the_times_default_and_save(self):
        got = memory.settings()
        self.assertEqual((got["morning_brief"], got["quiet_start"], got["quiet_end"]),
                         ("07:15", "23:30", "07:00"))
        got = memory.save_settings({"morning_brief": "6:05", "quiet_end": "06:30"})
        self.assertEqual((got["morning_brief"], got["quiet_end"]), ("06:05", "06:30"))
        self.assertEqual(memory.time_setting("morning_brief"), "06:05")
        # The numbers are untouched.
        self.assertEqual(got["memory_turns"], 30)

    def test_a_bad_time_saves_nothing(self):
        for bad in ("25:00", "7", "07:5", "seven", "07:60"):
            with self.assertRaises(ValueError):
                memory.save_settings({"morning_brief": bad, "memory_turns": 5})
        self.assertEqual(memory.settings()["memory_turns"], 30)


# -- the pings -------------------------------------------------------------------

class Pings(InboxCase):
    def test_the_morning_ping_goes_once_in_its_window_and_only_with_something_untold(self):
        clock.freeze(local(2026, 8, 19, 7, 14))
        self.add()
        self.assertIsNone(inbox.tick())
        clock.freeze(local(2026, 8, 19, 7, 15))
        self.assertEqual(inbox.tick(), "The front desk has 1 thing, open the app")
        self.assertEqual(self.pings, ["The front desk has 1 thing, open the app"])
        clock.freeze(local(2026, 8, 19, 7, 20))
        self.assertIsNone(inbox.tick())
        self.assertEqual(len(self.pings), 1)

    def test_the_morning_ping_never_goes_late(self):
        self.add()
        clock.freeze(local(2026, 8, 19, 7, 25))
        self.assertIsNone(inbox.tick())
        clock.freeze(local(2026, 8, 19, 12, 0))
        self.assertIsNone(inbox.tick())
        self.assertEqual(self.pings, [])

    def test_nothing_untold_is_no_ping(self):
        clock.freeze(local(2026, 8, 19, 7, 16))
        self.assertIsNone(inbox.tick())
        a = self.add()
        inbox.act(a["id"], "told")
        self.assertIsNone(inbox.tick())
        self.assertEqual(self.pings, [])

    def test_the_morning_ping_goes_again_the_next_day(self):
        clock.freeze(local(2026, 8, 19, 7, 16))
        self.add(urgency="fyi")
        inbox.tick()
        clock.freeze(local(2026, 8, 20, 7, 15))
        self.add(text="another", urgency="fyi")
        inbox.tick()
        self.assertEqual(self.pings, ["The front desk has 1 thing, open the app",
                                      "The front desk has 2 things, open the app"])

    def test_a_now_item_pings_after_its_grace_and_once(self):
        clock.freeze(local(2026, 8, 19, 14, 0))
        self.add(text="today one")
        self.add(text="the build broke", urgency="now")
        self.assertIsNone(inbox.tick())
        clock.advance(inbox.NOW_GRACE)
        self.assertEqual(inbox.tick(), "The front desk has 2 things, open the app")
        clock.advance(60)
        self.assertIsNone(inbox.tick())
        self.add(text="second now", urgency="now")
        clock.advance(inbox.NOW_GRACE)
        self.assertEqual(inbox.tick(), "The front desk has 3 things, open the app")
        self.assertEqual(len(self.pings), 2)

    def test_a_now_item_in_quiet_hours_waits_for_the_morning(self):
        clock.freeze(local(2026, 8, 19, 23, 30))
        self.add(text="late news", urgency="now", expires=local(2026, 8, 20, 9, 0))
        clock.advance(120)
        self.assertIsNone(inbox.tick())
        clock.freeze(local(2026, 8, 20, 6, 59))
        self.assertIsNone(inbox.tick())
        clock.freeze(local(2026, 8, 20, 7, 0))
        self.assertEqual(inbox.tick(), "The front desk has 1 thing, open the app")
        # The morning time at 07:15 is its own trigger and goes once more.
        clock.freeze(local(2026, 8, 20, 7, 15))
        self.assertEqual(inbox.tick(), "The front desk has 1 thing, open the app")
        clock.freeze(local(2026, 8, 20, 7, 16))
        self.assertIsNone(inbox.tick())
        self.assertEqual(len(self.pings), 2)

    def test_quiet_hours_that_do_not_cross_midnight(self):
        memory.save_settings({"quiet_start": "13:00", "quiet_end": "15:00"})
        self.assertTrue(inbox.quiet(dt.datetime(2026, 8, 19, 14, 0)))
        self.assertFalse(inbox.quiet(dt.datetime(2026, 8, 19, 15, 0)))
        self.assertFalse(inbox.quiet(dt.datetime(2026, 8, 19, 23, 30)))
        memory.save_settings({"quiet_start": "23:00", "quiet_end": "07:00"})
        self.assertTrue(inbox.quiet(dt.datetime(2026, 8, 19, 23, 30)))
        self.assertTrue(inbox.quiet(dt.datetime(2026, 8, 19, 3, 0)))
        self.assertFalse(inbox.quiet(dt.datetime(2026, 8, 19, 12, 0)))

    def test_a_restart_does_not_ping_twice(self):
        clock.freeze(local(2026, 8, 19, 7, 16))
        self.add()
        inbox.tick()
        self.refold()
        self.assertIsNone(inbox.tick())
        self.assertEqual(len(self.pings), 1)


# -- brief mode for the front desk -------------------------------------------------------

class BriefMode(InboxCase):
    def setUp(self):
        super().setUp()
        config.OPENAI_KEY = "sk-not-a-real-key-0123456789"
        self.opened = []
        real = voice.post_json

        def fake(url, payload, key, timeout=None):
            self.opened.append(payload)
            return {"session": {"id": "s1"}, "transport": {"sdp": "v=0\r\nanswer\r\n"}}

        voice.post_json = fake
        self.addCleanup(lambda: setattr(voice, "post_json", real))

    def test_a_call_opens_with_the_untold_items_most_urgent_first(self):
        self.add(text="The invoice is overdue.")
        self.add(agent="job-search", text="Interview at three.", urgency="now")
        told = self.add(text="Already said this.")
        inbox.act(told["id"], "told", by="jev", p=0.9)
        got = voice.open_call("v=0\r\noffer\r\n")
        live = self.opened[-1]["session"]["instructions"]
        back = self.opened[-1]["session"]["delegation"]["responses"]["instructions"]
        for text in (live, back):
            self.assertIn("## Things to tell them", text)
            self.assertLess(text.index("Interview at three."), text.index("The invoice is overdue."))
            self.assertNotIn("Already said this.", text)
            self.assertIn("Then stop and wait.", text)
        self.assertIn("After your opening line", live)
        self.assertEqual([i["text"] for i in got["brief"]],
                         ["Interview at three.", "The invoice is overdue."])
        self.assertEqual(got["brief"][0]["agent"], "job-search")
        # A first call speaks first by the persona; no extra line is needed.
        self.assertEqual(got["brief_say"], "")
        # Their first finished line marks the pushed items told, without Jev.
        inbox.judge_told("Interview at three, and the invoice.", [i["id"] for i in got["brief"]])
        self.assertEqual(self.sent, [])
        self.assertEqual(inbox.get(got["brief"][0]["id"])["by"], "opening")

    def test_a_resume_reads_only_what_is_still_untold_and_does_not_greet(self):
        a = self.add(text="One.")
        b = self.add(text="Two.")
        inbox.act(a["id"], "told", by="jev", p=0.9)
        got = voice.open_call("v=0\r\noffer\r\n", resume=[{"who": "voice", "text": "Two things."}])
        live = self.opened[-1]["session"]["instructions"]
        self.assertIn("do not greet them", live)
        self.assertIn("Now say it in one sentence", live)
        self.assertNotIn(": One.", live)
        self.assertIn("1. pa (today, left 09:00): Two.", live)
        # News to tell, so the resume section leaves out "wait in silence".
        self.assertNotIn(voice.WAIT_SILENT, live)
        self.assertEqual([i["id"] for i in got["brief"]], [b["id"]])
        self.assertIn("Two.", got["brief_say"])
        self.assertIn("One sentence.", got["brief_say"])

    def test_a_greeting_alone_marks_no_pushed_item_told(self):
        a = self.add(text="The 10:05 alarm will not ring.", urgency="urgent")
        b = self.add(text="Dinner at three this evening.")
        got = voice.open_call("v=0\r\noffer\r\n")
        ids = [i["id"] for i in got["brief"]]
        inbox.judge_told("Good evening, sir. I have three things.", ids)
        self.assertEqual(inbox.get(a["id"])["status"], "waiting")
        self.assertEqual(inbox.get(b["id"])["status"], "waiting")
        inbox.judge_told("Your ten oh five alarm will not ring.", ids)
        self.assertEqual(inbox.get(a["id"])["by"], "opening")

    def test_an_opening_says_three_and_counts_the_rest(self):
        for n in range(5):
            self.add(text=f"Item {n}.")
        got = voice.open_call("v=0\r\noffer\r\n")
        live = self.opened[-1]["session"]["instructions"]
        self.assertIn("Item 2.", live)
        self.assertNotIn("Item 3.", live)
        self.assertIn("are 2 more items waiting", live)
        self.assertEqual(len(got["brief"]), 3)

    def test_no_items_no_section(self):
        voice.open_call("v=0\r\noffer\r\n")
        self.assertNotIn("Things to tell them", self.opened[-1]["session"]["instructions"])


# -- the three Jev moments -------------------------------------------------------

class JevTold(InboxCase):
    def setUp(self):
        super().setUp()
        config.TYPESAFE_KEY = KEY
        self.a = self.add(text="The dentist moved to four.")
        self.b = self.add(agent="job-search", text="Interview at three.")

    def test_one_request_one_noul_per_untold_item(self):
        self.nouls({self.a["id"]: 0.95, self.b["id"]: 0.1})
        got = inbox.judge_told("Personal assistant says your dentist is now at four.",
                               [self.a["id"], self.b["id"]])
        self.assertEqual(len(self.sent), 1)
        payload = self.sent[0]["payload"]
        self.assertEqual(payload["model"], memory.JEV_MODEL)
        self.assertEqual(set(payload["questions"]), {self.a["id"], self.b["id"]})
        q = payload["questions"][self.a["id"]]
        self.assertEqual(q["type"], "noul")
        self.assertEqual(q["instructions"]["item"]["text"], "The dentist moved to four.")
        self.assertEqual(payload["state"],
                         {"desk_said": "Personal assistant says your dentist is now at four."})
        self.assertEqual(self.sent[0]["key"], KEY)
        self.assertNotIn(KEY, json.dumps(payload))
        self.assertEqual(got["decisions"], [{"id": self.a["id"], "decision": "told", "p": 0.95,
                                             "agent": "pa"}])
        item = inbox.get(self.a["id"])
        self.assertEqual((item["status"], item["by"], item["p"]), ("told", "jev", 0.95))
        self.assertEqual(inbox.get(self.b["id"])["status"], "waiting")

    def test_the_threshold_is_point_eight(self):
        self.nouls({self.a["id"]: 0.79, self.b["id"]: 0.8})
        inbox.judge_told("line", [self.a["id"], self.b["id"]])
        self.assertEqual(inbox.get(self.a["id"])["status"], "waiting")
        self.assertEqual(inbox.get(self.b["id"])["status"], "told")

    def test_only_untold_items_are_asked_about(self):
        inbox.act(self.a["id"], "told")
        self.nouls({self.b["id"]: 0.9})
        inbox.judge_told("line", [self.a["id"], self.b["id"], "notanitem"])
        self.assertEqual(set(self.sent[0]["payload"]["questions"]), {self.b["id"]})

    def test_a_failure_or_no_key_changes_nothing(self):
        self.answer = RuntimeError(f"TypeSafe refused the request (500): Bearer {KEY}")
        got = inbox.judge_told("line", [self.a["id"]])
        self.assertEqual(got["decisions"], [])
        self.assertEqual(inbox.get(self.a["id"])["status"], "waiting")
        config.TYPESAFE_KEY = ""
        self.sent.clear()
        inbox.judge_told("line", [self.a["id"]])
        self.assertEqual(self.sent, [])
        self.assertEqual(inbox.get(self.a["id"])["status"], "waiting")

    def test_nothing_to_ask_is_no_request(self):
        self.assertEqual(inbox.judge_told("", [self.a["id"]])["decisions"], [])
        self.assertEqual(inbox.judge_told("line", [])["decisions"], [])
        self.assertEqual(self.sent, [])


class JevReply(InboxCase):
    def setUp(self):
        super().setUp()
        config.TYPESAFE_KEY = KEY
        self.a = self.add(text="The dentist moved to four.")
        self.b = self.add(agent="job-search", text="Interview at three.")
        for item in (self.a, self.b):
            inbox.act(item["id"], "told", by="jev", p=0.9)

    def test_one_choice_per_told_item_with_five_options(self):
        self.choices({self.a["id"]: ("handled", 0.93), self.b["id"]: ("unrelated", 0.9)})
        got = inbox.judge_reply("done", [self.a["id"], self.b["id"]],
                                desk="The dentist moved to four.")
        payload = self.sent[0]["payload"]
        q = payload["questions"][self.a["id"]]
        self.assertEqual(q["type"], "choice")
        self.assertEqual(set(q["criteria"]),
                         {"handled", "later", "dismissed", "open", "unrelated"})
        self.assertEqual(payload["state"], {"desk_said": "The dentist moved to four.",
                                            "owner_said": "done"})
        self.assertEqual([d["decision"] for d in got["decisions"]], ["handled"])
        self.assertEqual(inbox.get(self.a["id"])["decision"], "handled")
        self.assertEqual(inbox.get(self.b["id"])["status"], "told")

    def test_later_and_dismissed_and_open(self):
        self.choices({self.a["id"]: ("later", 0.9), self.b["id"]: ("open", 0.88)})
        got = inbox.judge_reply("remind me tonight, and open the other", [self.a["id"],
                                                                        self.b["id"]])
        self.assertEqual(inbox.get(self.a["id"])["status"], "waiting")
        self.assertEqual(inbox.get(self.b["id"])["decision"], "opened")
        opened = [d for d in got["decisions"] if d["decision"] == "opened"][0]
        self.assertEqual((opened["agent"], opened["link"]),
                         ("job-search", "?screen=chat#/a/job-search"))

    def test_only_the_surest_open_opens(self):
        self.choices({self.a["id"]: ("open", 0.85), self.b["id"]: ("open", 0.95)})
        got = inbox.judge_reply("open it", [self.a["id"], self.b["id"]])
        self.assertEqual([d["id"] for d in got["decisions"]], [self.b["id"]])
        self.assertEqual(inbox.get(self.a["id"])["status"], "told")

    def test_below_the_threshold_nothing_happens(self):
        self.choices({self.a["id"]: ("dismissed", 0.79)})
        inbox.judge_reply("hmm drop it maybe", [self.a["id"]])
        self.assertEqual(inbox.get(self.a["id"])["status"], "told")

    def test_untold_items_are_not_asked_about(self):
        c = self.add(text="Not told yet.")
        self.choices({})
        inbox.judge_reply("done", [c["id"]])
        self.assertEqual(self.sent, [])

    def test_a_failure_changes_nothing(self):
        self.answer = RuntimeError("TypeSafe could not be reached: timed out")
        got = inbox.judge_reply("done", [self.a["id"]])
        self.assertEqual(got["decisions"], [])
        self.assertEqual(inbox.get(self.a["id"])["status"], "told")


class JevReplace(InboxCase):
    def setUp(self):
        super().setUp()
        config.TYPESAFE_KEY = KEY

    def test_one_noul_per_waiting_item_from_the_same_agent(self):
        old = self.add(text="The build is running.")
        other = self.add(agent="job-search", text="Interview at three.")
        told = self.add(text="Told already.")
        inbox.act(told["id"], "told")
        new = self.add(text="The build passed.")
        self.nouls({old["id"]: 0.92})
        got = inbox.judge_replace(new["id"])
        payload = self.sent[0]["payload"]
        self.assertEqual(set(payload["questions"]), {old["id"]})
        self.assertEqual(payload["state"]["new_item"]["text"], "The build passed.")
        self.assertEqual(payload["questions"][old["id"]]["instructions"]["older_item"]["text"],
                         "The build is running.")
        self.assertEqual(got["decisions"][0]["by"], new["id"])
        item = inbox.get(old["id"])
        self.assertEqual((item["decision"], item["replaced_by"]), ("replaced", new["id"]))
        self.assertEqual(inbox.get(other["id"])["status"], "waiting")
        self.assertEqual(self.events()[-1]["event"], "replaced")

    def test_below_the_threshold_both_stay(self):
        old = self.add(text="Dentist at four.")
        new = self.add(text="The invoice is overdue.")
        self.nouls({old["id"]: 0.3})
        inbox.judge_replace(new["id"])
        self.assertEqual(inbox.get(old["id"])["status"], "waiting")

    def test_no_older_item_is_no_request_and_no_key_is_nothing(self):
        new = self.add(text="Alone.")
        inbox.judge_replace(new["id"])
        self.assertEqual(self.sent, [])
        old = self.add(text="Older.")
        newer = self.add(text="Newer.")
        config.TYPESAFE_KEY = ""
        inbox.judge_replace(newer["id"])
        self.assertEqual(self.sent, [])
        self.assertEqual(inbox.get(old["id"])["status"], "waiting")

    def test_adding_runs_it_off_the_caller(self):
        old = self.add(text="The build is running.")
        done = threading.Event()
        real = inbox.judge_replace

        def spy(new_id):
            try:
                return real(new_id)
            finally:
                done.set()
        inbox.judge_replace = spy
        self.addCleanup(lambda: setattr(inbox, "judge_replace", real))
        self.nouls({old["id"]: 0.95})
        inbox.add("pa", "The build passed.")
        self.assertTrue(done.wait(5))
        self.assertEqual(inbox.get(old["id"])["decision"], "replaced")


class Scrub(InboxCase):
    def test_a_failure_that_repeats_the_key_never_reaches_the_log_or_the_answer(self):
        config.TYPESAFE_KEY = KEY
        a = self.add()
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        lg = logging.getLogger("cc")
        grab = Grab()
        lg.addHandler(grab)
        lg.setLevel(logging.INFO)
        self.addCleanup(lambda: lg.removeHandler(grab))
        self.answer = RuntimeError(memory._safe(f"TypeSafe refused the request (401): {KEY}"))
        got = inbox.judge_told("line", [a["id"]])
        self.assertNotIn(KEY, json.dumps(got))
        self.assertTrue(records)
        self.assertFalse([r for r in records if KEY in r])


# -- the routes ------------------------------------------------------------------

class Routes(InboxCase):
    def setUp(self):
        super().setUp()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]

    def hit(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json"})
        reply = conn.getresponse()
        got = json.loads(reply.read() or b"{}")
        conn.close()
        return reply.status, got

    def test_add_list_act_and_state(self):
        code, item = self.hit("POST", "/api/inbox", {"agent": "pa", "text": "Standup at ten.",
                                                     "urgency": "now"})
        self.assertEqual(code, 200)
        self.assertEqual((item["agent"], item["urgency"], item["duplicate"]), ("pa", "now", False))
        code, listing = self.hit("GET", "/api/inbox")
        self.assertEqual([i["id"] for i in listing["waiting"]], [item["id"]])
        self.assertEqual(listing["summary"]["untold"], 1)
        code, state = self.hit("GET", "/api/state")
        self.assertEqual((state["inbox"]["untold"], state["inbox"]["urgent"]), (1, 1))
        for action in ("told", "later", "handled", "restored", "dismissed", "restored",
                       "opened", "restored", "removed"):
            code, got = self.hit("POST", f"/api/inbox/{item['id']}/{action}", {})
            self.assertEqual(code, 200, action)
        self.assertEqual(got["decision"], "removed")
        self.assertEqual(got["by"], "config")
        code, listing = self.hit("GET", "/api/inbox")
        self.assertEqual(listing["waiting"], [])
        self.assertEqual(listing["told"][0]["decision"], "removed")

    def test_refusals(self):
        code, _ = self.hit("POST", "/api/inbox", {"agent": "pa", "text": ""})
        self.assertEqual(code, 400)
        code, _ = self.hit("POST", "/api/inbox", {"agent": "pa", "text": "x", "urgency": "soon"})
        self.assertEqual(code, 400)
        code, item = self.hit("POST", "/api/inbox", {"agent": "pa", "text": "x"})
        code, _ = self.hit("POST", f"/api/inbox/{item['id']}/shred", {})
        self.assertEqual(code, 400)
        code, _ = self.hit("POST", "/api/inbox/deadbeef00/told", {})
        self.assertEqual(code, 404)
        code, _ = self.hit("POST", "/api/inbox/clear", {})
        self.assertEqual(code, 400)
        self.assertEqual(len(self.hit("GET", "/api/inbox")[1]["waiting"]), 1)
        code, got = self.hit("POST", "/api/inbox/clear", {"confirm": True})
        self.assertEqual((code, got), (200, {"removed": 1}))
        code, _ = self.hit("POST", "/api/inbox/judge", {"moment": "guess"})
        self.assertEqual(code, 400)

    def test_the_judge_route(self):
        config.TYPESAFE_KEY = KEY
        a = self.add()
        self.nouls({a["id"]: 0.97})
        code, got = self.hit("POST", "/api/inbox/judge", {"moment": "told", "line": "Dentist at "
                                                          "four, says the assistant.",
                                                          "ids": [a["id"]]})
        self.assertEqual(code, 200)
        self.assertEqual(got["decisions"][0]["decision"], "told")
        self.choices({a["id"]: ("later", 0.9)})
        code, got = self.hit("POST", "/api/inbox/judge", {"moment": "reply", "line": "remind "
                                                          "me tonight", "ids": [a["id"]],
                                                          "desk": "Dentist at four."})
        self.assertEqual(got["decisions"][0]["decision"], "later")
        self.assertEqual(inbox.get(a["id"])["status"], "waiting")

    def test_the_voice_settings_route_takes_the_times(self):
        code, got = self.hit("POST", "/api/voice/settings", {"morning_brief": "06:45"})
        self.assertEqual((code, got["morning_brief"]), (200, "06:45"))
        code, got = self.hit("GET", "/api/voice/settings")
        self.assertEqual(got["morning_brief"], "06:45")
        code, _ = self.hit("POST", "/api/voice/settings", {"quiet_start": "midnight"})
        self.assertEqual(code, 400)

    def test_an_event_goes_to_the_listeners_with_the_summary(self):
        seen = []
        inbox.on_change(lambda event, payload: seen.append(payload))
        self.add()
        self.assertEqual(seen[-1]["event"], "added")
        self.assertEqual(seen[-1]["summary"]["untold"], 1)
