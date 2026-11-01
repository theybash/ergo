"""Tasks: the cards agents offer, the owner's answers, the reminder, the routes and the tools.

No agent runs. `broker.send` is replaced and what it would have sent is read back, and
`tasks.busy` is replaced to say whether the agent is working.
"""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from commandcenter import broker, clock, config, inbox, registry, server, tasks
from tests.harness import Case
from tests.test_tools import ask as tool_ask, payload as tool_payload


class TaskCase(Case):
    def setUp(self):
        super().setUp()
        self.sent = []
        real_send = broker.send

        def send(agent, text, wake=False):
            self.sent.append({"agent": agent, "text": text, "wake": wake})
            return "sent"

        broker.send = send
        self.addCleanup(lambda: setattr(broker, "send", real_send))
        self.working = set()
        real_busy = tasks.busy
        tasks.busy = lambda agent: agent in self.working
        self.addCleanup(lambda: setattr(tasks, "busy", real_busy))
        self.juno = self.agent("portfolio-manager")
        registry.update(self.juno["id"], name="Juno")

    def offer(self, agent=None, title="Check if Sam's handout has a translation layer", **kw):
        return tasks.offer(agent or self.juno["id"], title,
                           kw.get("source", "Meeting with Sam, Thursday 15:00"),
                           kw.get("deliverable", "yes or no, with page references"))

    def events(self):
        path = config.VAR / "tasks.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def outbox(self, rec, name, text="x"):
        folder = Path(rec["cwd"]) / ".platform" / "outbox" / rec["id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(text)


# -- the store -------------------------------------------------------------------

class Store(TaskCase):
    def test_an_offer_is_a_card_with_no_plan(self):
        task = self.offer()
        self.assertEqual(task["state"], "offered")
        self.assertEqual(task["plan"], [])
        self.assertEqual(task["plan_state"], "none")
        self.assertEqual(task["side"], "personal")
        self.assertEqual(task["agent_name"], "Juno")
        self.assertEqual(task["source"], "Meeting with Sam, Thursday 15:00")
        self.assertEqual(self.events()[0]["event"], "offered")

    def test_the_log_folds_back_after_a_restart(self):
        task = self.offer()
        tasks.no(task["id"], "Not mine")
        tasks.reset()
        again = tasks.get(task["id"])
        self.assertEqual((again["state"], again["why"]), ("no", "Not mine"))

    def test_an_offer_needs_all_three_fields(self):
        for kw in ({"title": ""}, {"source": ""}, {"deliverable": ""}):
            with self.assertRaises(ValueError):
                self.offer(**kw)

    def test_an_unknown_agent_cannot_offer(self):
        with self.assertRaises(KeyError):
            tasks.offer("nobody", "a", "b", "c")

    def test_ten_a_day_and_then_a_new_day(self):
        for n in range(10):
            self.offer(title=f"task {n}")
        with self.assertRaises(PermissionError):
            self.offer(title="task 11")
        clock.advance(86400)
        self.assertEqual(self.offer(title="task 11")["state"], "offered")

    def test_the_same_open_title_is_refused_but_a_closed_one_may_come_back(self):
        task = self.offer()
        with self.assertRaises(ValueError):
            self.offer(title=task["title"].upper())
        tasks.dismiss(task["id"])
        self.assertEqual(self.offer()["state"], "offered")

    def test_open_cards_come_first_and_old_answers_drop_out(self):
        first = self.offer(title="one")
        clock.advance(60)
        second = self.offer(title="two")
        clock.advance(60)
        tasks.no(first["id"])
        got = tasks.listing()
        self.assertEqual([t["id"] for t in got["tasks"]], [second["id"], first["id"]])
        self.assertEqual(got["offered"], 1)
        clock.advance(3 * 86400 + 1)
        self.assertEqual([t["id"] for t in tasks.listing()["tasks"]], [second["id"]])
        tasks.reset()
        self.assertEqual(tasks.get(first["id"])["state"], "no", "the file keeps it")


# -- the owner's answers ------------------------------------------------------------

class Answers(TaskCase):
    def test_yes_wakes_the_agent_with_the_rules(self):
        task = self.offer()
        got = tasks.yes(task["id"])
        self.assertEqual(got["state"], "yes")
        said = self.sent[-1]
        self.assertTrue(said["wake"])
        self.assertTrue(said["text"].startswith("[Task yes] Check if Sam's handout"))
        self.assertIn("work out the steps yourself", said["text"])
        self.assertIn(f"task_done({task['id']}, result, file)", said["text"])
        self.assertIn("pushes code still needs their OK at that moment", said["text"])

    def test_yes_carries_a_ready_plan(self):
        task = self.offer()
        tasks.ask_plan(task["id"])
        tasks.plan(task["id"], self.juno["id"], ["Read the v2 handout", "List the pages"])
        tasks.yes(task["id"])
        self.assertIn("1. Read the v2 handout\n2. List the pages", self.sent[-1]["text"])
        self.assertNotIn("work out the steps", self.sent[-1]["text"])

    def test_plan_sends_the_configurable_prompt_and_waits(self):
        task = self.offer()
        tasks.save_settings({"plan_prompt": "Three steps, please."})
        got = tasks.ask_plan(task["id"])
        self.assertEqual((got["state"], got["plan_state"]), ("offered", "asked"))
        text = self.sent[-1]["text"]
        self.assertIn("Three steps, please.", text)
        self.assertIn("From: Meeting with Sam, Thursday 15:00", text)
        self.assertIn("They get: yes or no, with page references", text)
        self.assertTrue(self.sent[-1]["wake"])

    def test_a_bare_no_tells_nobody_and_a_reason_reaches_the_agent_quietly(self):
        task = self.offer()
        before = len(self.sent)
        tasks.no(task["id"])
        self.assertEqual(len(self.sent), before)
        got = tasks.because(task["id"], "Already done")
        self.assertEqual(got["why"], "Already done")
        self.assertFalse(self.sent[-1]["wake"])
        self.assertIn("Already done", self.sent[-1]["text"])

    def test_no_with_a_reason_says_it(self):
        task = self.offer()
        tasks.no(task["id"], "Too risky")
        self.assertIn("Their reason: Too risky", self.sent[-1]["text"])

    def test_dismiss_tells_nobody(self):
        task = self.offer()
        self.assertEqual(tasks.dismiss(task["id"])["state"], "dismissed")
        self.assertEqual(self.sent, [])

    def test_an_answered_card_cannot_be_answered_again(self):
        task = self.offer()
        tasks.no(task["id"])
        with self.assertRaises(ValueError):
            tasks.yes(task["id"])

    def test_a_busy_agent_changes_nothing_without_a_mode(self):
        task = self.offer()
        self.working.add(self.juno["id"])
        for fn in (tasks.yes, tasks.ask_plan):
            got = fn(task["id"])
            self.assertEqual((got["busy"], got["agent_name"]), (True, "Juno"))
        self.assertEqual(self.sent, [])
        self.assertEqual(tasks.get(task["id"])["state"], "offered")

    def test_now_steers_a_busy_agent(self):
        task = self.offer()
        self.working.add(self.juno["id"])
        self.assertEqual(tasks.yes(task["id"], "now")["state"], "yes")
        self.assertEqual(len(self.sent), 1)


# -- the reminder ----------------------------------------------------------------

class Reminder(TaskCase):
    def test_remind_waits_for_the_agent_and_tells_him_once(self):
        task = self.offer()
        self.working.add(self.juno["id"])
        got = tasks.yes(task["id"], "remind")
        self.assertEqual((got["state"], got["remind"]), ("waiting_remind", "yes"))
        self.assertEqual(self.sent, [], "the agent is not told anything yet")
        self.assertEqual(tasks.free(self.juno["id"]), 0, "still working")
        self.working.clear()
        self.assertEqual(tasks.free(self.juno["id"]), 1)
        self.assertEqual(tasks.free(self.juno["id"]), 0, "once")
        waiting = inbox.listing()["waiting"]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["text"],
                         "Juno is free. Start 'Check if Sam's handout has a translation layer'?")
        self.assertEqual(waiting[0]["link"], f"?screen=chat#/a/{self.juno['id']}")
        self.assertEqual(waiting[0]["kind"], "free", "the phone posts it on its own loud channel")
        after = tasks.get(task["id"])
        self.assertEqual((after["state"], after["remind"]), ("offered", None))
        self.assertTrue(after["reminded_at"])

    def test_a_plan_reminder_asks_to_plan(self):
        task = self.offer()
        self.working.add(self.juno["id"])
        got = tasks.ask_plan(task["id"], "remind")
        self.assertEqual((got["state"], got["plan_state"], got["remind"]),
                         ("offered", "none", "plan"))
        self.assertEqual(tasks.free(self.juno["id"], sure=True), 1)
        self.assertIn("Plan 'Check", inbox.listing()["waiting"][0]["text"])

    def test_remind_on_a_free_agent_tells_him_at_once(self):
        task = self.offer()
        tasks.yes(task["id"], "remind")
        self.assertEqual(len(inbox.listing()["waiting"]), 1)

    def test_a_chat_turn_ending_is_the_moment(self):
        task = self.offer()
        self.working.add(self.juno["id"])
        tasks.yes(task["id"], "remind")
        broker._chat_frame(self.juno["id"], {"t": "state", "state": "idle"})
        self.assertEqual(len(inbox.listing()["waiting"]), 1)

    def test_the_feed_seeing_the_spinner_stop_is_the_moment(self):
        task = self.offer()
        self.working.add(self.juno["id"])
        tasks.yes(task["id"], "remind")
        self.working.clear()
        server._stopped_working({"agents": [{"id": self.juno["id"], "working": True}]},
                                {"agents": [{"id": self.juno["id"], "working": False}]})
        self.assertEqual(len(inbox.listing()["waiting"]), 1)


# -- the agent's own card --------------------------------------------------------

class Ownership(TaskCase):
    def setUp(self):
        super().setUp()
        self.other = self.agent("job-search")
        self.task = self.offer()

    def test_a_plan_may_come_before_he_asks(self):
        got = tasks.plan(self.task["id"], self.juno["id"], ["read it", "say what is missing"])
        self.assertEqual((got["plan_state"], got["plan"]), ("ready", ["read it", "say what is missing"]))

    def test_an_offer_can_carry_its_plan(self):
        got = tasks.offer(self.juno["id"], "Book the table", "Chat with Asha", "a booking",
                          ["find three places", "check Saturday 8 pm"])
        self.assertEqual(got["plan_state"], "ready")
        self.assertEqual(len(got["plan"]), 2)

    def test_only_the_owner_plans_or_closes(self):
        tasks.ask_plan(self.task["id"])
        with self.assertRaises(PermissionError):
            tasks.plan(self.task["id"], self.other["id"], ["a", "b"])
        tasks.yes(self.task["id"])
        with self.assertRaises(PermissionError):
            tasks.done(self.task["id"], self.other["id"], "done")

    def test_a_plan_is_two_to_eight_steps(self):
        tasks.ask_plan(self.task["id"])
        for steps in (["one"], [str(n) for n in range(9)], "not a list"):
            with self.assertRaises(ValueError):
                tasks.plan(self.task["id"], self.juno["id"], steps)
        got = tasks.plan(self.task["id"], self.juno["id"], ["a", "b"])
        self.assertEqual((got["plan"], got["plan_state"]), (["a", "b"], "ready"))

    def test_done_only_after_yes_and_with_a_real_file(self):
        with self.assertRaises(ValueError):
            tasks.done(self.task["id"], self.juno["id"], "found it")
        tasks.yes(self.task["id"])
        with self.assertRaises(ValueError):
            tasks.done(self.task["id"], self.juno["id"], "found it", "nothing.md")
        self.outbox(self.juno, "pages.md")
        got = tasks.done(self.task["id"], self.juno["id"], "Yes, pages 4 and 9.", "pages.md")
        self.assertEqual((got["state"], got["result"], got["result_file"]),
                         ("done", "Yes, pages 4 and 9.", "pages.md"))


# -- settings --------------------------------------------------------------------

class Settings(TaskCase):
    def test_the_default_prompt_and_putting_it_back(self):
        self.assertEqual(tasks.settings()["plan_prompt"], tasks.DEFAULT_PLAN_PROMPT)
        self.assertEqual(tasks.save_settings({"plan_prompt": "Short."})["plan_prompt"], "Short.")
        self.assertEqual(tasks.settings()["plan_prompt"], "Short.")
        self.assertEqual(tasks.save_settings({"plan_prompt": ""})["plan_prompt"],
                         tasks.DEFAULT_PLAN_PROMPT)
        with self.assertRaises(ValueError):
            tasks.save_settings({"plan_prompt": "x" * (tasks.PROMPT_CHARS + 1)})


# -- the routes and the tools ----------------------------------------------------

CONSOLE = {"X-Forwarded-For": "100.64.0.2"}


class Routes(TaskCase):
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

    def tool(self, name, args):
        return tool_ask(name, args, agent=self.juno["id"], api=self.base)

    def test_the_whole_life_of_a_card_through_the_tools_and_the_console(self):
        got = tool_payload(self.tool("offer_task", {
            "title": "Check if Sam's handout has a translation layer",
            "source": "Meeting with Sam, Thursday 15:00",
            "deliverable": "yes or no, with page references"}))
        task_id = got["task_id"]
        code, listing = self.hit("GET", "/api/tasks")
        self.assertEqual((code, listing["offered"]), (200, 1))
        self.assertEqual(listing["tasks"][0]["agent_name"], "Juno")

        code, _ = self.hit("POST", f"/api/tasks/{task_id}/plan", {}, CONSOLE)
        self.assertEqual(code, 200)
        got = tool_payload(self.tool("task_plan", {"task_id": task_id,
                                                   "steps": ["Read it", "List pages"]}))
        self.assertEqual(got["plan_state"], "ready")

        code, task = self.hit("POST", f"/api/tasks/{task_id}/yes", {}, CONSOLE)
        self.assertEqual((code, task["state"]), (200, "yes"))
        self.outbox(self.juno, "pages.md")
        got = tool_payload(self.tool("task_done", {"task_id": task_id, "result": "Yes, page 4.",
                                                   "file": "pages.md"}))
        self.assertEqual((got["state"], got["file"]), ("done", "pages.md"))

    def test_an_agent_on_the_laptop_cannot_answer_a_card(self):
        task = self.offer()
        for action in ("yes", "plan", "no", "why", "dismiss"):
            code, _ = self.hit("POST", f"/api/tasks/{task['id']}/{action}", {"why": "x"})
            self.assertEqual(code, 403, action)
        code, _ = self.hit("POST", "/api/tasks/settings", {"plan_prompt": "x"})
        self.assertEqual(code, 403)
        self.assertEqual(tasks.get(task["id"])["state"], "offered")

    def test_busy_then_remind_over_the_route(self):
        task = self.offer()
        self.working.add(self.juno["id"])
        code, got = self.hit("POST", f"/api/tasks/{task['id']}/yes", {}, CONSOLE)
        self.assertEqual((code, got["busy"], got["agent_name"]), (200, True, "Juno"))
        code, got = self.hit("POST", f"/api/tasks/{task['id']}/yes", {"mode": "remind"}, CONSOLE)
        self.assertEqual(got["state"], "waiting_remind")

    def test_no_why_and_dismiss_over_the_route(self):
        task = self.offer()
        code, got = self.hit("POST", f"/api/tasks/{task['id']}/no", {}, CONSOLE)
        self.assertEqual(got["state"], "no")
        code, got = self.hit("POST", f"/api/tasks/{task['id']}/why", {"why": "Not mine"}, CONSOLE)
        self.assertEqual(got["why"], "Not mine")
        code, got = self.hit("POST", f"/api/tasks/{task['id']}/dismiss", {}, CONSOLE)
        self.assertEqual(got["state"], "dismissed")

    def test_refusals_say_why(self):
        code, _ = self.hit("POST", "/api/tasks/abcdef1234/yes", {}, CONSOLE)
        self.assertEqual(code, 404)
        code, _ = self.hit("GET", "/api/tasks?side=demo")
        self.assertEqual(code, 400)
        for n in range(tasks.PER_DAY):
            self.offer(title=f"t{n}")
        answer = self.tool("offer_task", {"title": "t11", "source": "s", "deliverable": "d"})
        self.assertTrue(answer["result"]["isError"])
        self.assertIn("limit", answer["result"]["content"][0]["text"])

    def test_settings_over_the_route(self):
        code, got = self.hit("GET", "/api/tasks/settings")
        self.assertEqual(got["plan_prompt"], tasks.DEFAULT_PLAN_PROMPT)
        code, got = self.hit("POST", "/api/tasks/settings", {"plan_prompt": "Be short."}, CONSOLE)
        self.assertEqual((code, got["plan_prompt"]), (200, "Be short."))

    def test_a_change_is_published_on_its_own_topic(self):
        frames = []
        tasks.on_change(lambda event, payload: frames.append(payload))
        task = self.offer()
        tasks.dismiss(task["id"])
        self.assertEqual([f["event"] for f in frames], ["offered", "dismissed"])
        self.assertEqual(frames[-1]["task"]["state"], "dismissed")


# -- the task list: their own tasks, closing, reopening, the Done list -----------------

class TaskList(TaskCase):
    def finished(self):
        task = self.offer()
        tasks.yes(task["id"])
        return tasks.done(task["id"], self.juno["id"], "It has one, on page 4.")

    def test_his_own_task_is_listed_until_he_ticks_it(self):
        mine = tasks.add_mine("Shift to the new flat")
        self.assertEqual((mine["state"], mine["agent"], mine["by"]), ("mine", "owner", "owner"))
        self.assertIn(mine["id"], [t["id"] for t in tasks.listing()["tasks"]])
        self.assertEqual(tasks.listing()["offered"], 0, "their own tasks are never counted")
        tasks.tick(mine["id"])
        self.assertNotIn(mine["id"], [t["id"] for t in tasks.listing()["tasks"]])
        self.assertEqual([t["id"] for t in tasks.finished()["tasks"]], [mine["id"]])

    def test_an_agent_adds_one_for_him_and_the_same_one_twice_is_refused(self):
        mine = tasks.add_mine("Post the signed legal notice", by=self.juno["id"])
        self.assertEqual(mine["by_name"], "Juno")
        with self.assertRaises(ValueError):
            tasks.add_mine("post the signed legal notice")
        with self.assertRaises(KeyError):
            tasks.add_mine("Anything", by="nobody")

    def test_a_finished_task_waits_for_his_yes_however_old(self):
        task = self.finished()
        clock.freeze(clock.now() + 10 * 86400)
        self.assertIn(task["id"], [t["id"] for t in tasks.listing()["tasks"]])
        tasks.close(task["id"])
        self.assertNotIn(task["id"], [t["id"] for t in tasks.listing()["tasks"]])
        self.assertEqual(tasks.finished()["tasks"][0]["state"], "closed")

    def test_reopening_puts_it_back_with_the_agent(self):
        task = self.finished()
        self.assertEqual(tasks.reopen(task["id"])["state"], "yes")
        mine = tasks.add_mine("Call the Rapido driver")
        tasks.tick(mine["id"])
        self.assertEqual(tasks.reopen(mine["id"])["state"], "mine")
        with self.assertRaises(ValueError):
            tasks.tick(task["id"])

    def test_the_done_list_pages_ten_at_a_time(self):
        for n in range(13):
            clock.freeze(clock.now() + 1)
            tasks.tick(tasks.add_mine(f"Task {n}")["id"])
        first = tasks.finished()
        self.assertEqual((len(first["tasks"]), first["more"]), (10, True))
        self.assertEqual(first["tasks"][0]["title"], "Task 12")
        rest = tasks.finished(before=first["tasks"][-1]["closed_at"])
        self.assertEqual([t["title"] for t in rest["tasks"]], ["Task 2", "Task 1", "Task 0"])
        self.assertFalse(rest["more"])

    def test_the_agent_reads_its_tasks_and_his(self):
        task = self.offer()
        tasks.add_mine("Shift to the new flat")
        got = tasks.for_agent(self.juno["id"])
        self.assertTrue(got["your_tasks"][0].startswith(task["id"] + " | offered"))
        self.assertIn("Shift to the new flat", got["owner_tasks"][0])

    def test_an_alarm_with_a_label_is_his_task_and_a_bare_one_is_not(self):
        from commandcenter import phone
        phone.alarm(21, 0, "Call the Rapido driver", by=self.juno["id"])
        phone.alarm(7, 0)
        mine = [t for t in tasks.listing()["tasks"] if t["agent"] == "owner"]
        self.assertEqual([t["title"] for t in mine], ["Call the Rapido driver"])
        self.assertEqual(mine[0]["by"], self.juno["id"])
        self.assertTrue(mine[0]["due"] > clock.now())

    def test_what_others_owe_him_is_its_own_list_on_the_agents_side(self):
        owed = tasks.add_owed("Northwind tape", "Sam", self.juno["id"], due=clock.now() + 86400)
        self.assertEqual((owed["state"], owed["who"], owed["side"]), ("owed", "Sam", "personal"))
        with self.assertRaises(ValueError):
            tasks.add_owed("northwind tape", "Sam", self.juno["id"])
        got = tasks.for_agent(self.juno["id"])
        self.assertIn("from Sam", got["owed_to_owner"][0])
        self.assertEqual(got["owner_tasks"], [])
        tasks.tick(owed["id"])
        self.assertEqual(tasks.finished()["tasks"][0]["id"], owed["id"])
        self.assertEqual(tasks.reopen(owed["id"])["state"], "owed")
