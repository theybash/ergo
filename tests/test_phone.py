"""The phone as a place the fleet can reach: alarms it sets and notifications it keeps."""

import json

from commandcenter import clock, config, phone, voice
from tests import test_voice


class Phone(test_voice.Routes):
    def test_an_alarm_is_queued_fetched_and_done(self):
        code, got = self.hit("POST", "/api/phone/alarm", {"hour": 6, "minute": 30, "label": "gym"})
        self.assertEqual(code, 200)
        self.assertIn("06:30", got["said"])
        code, listed = self.hit("GET", "/api/phone/commands")
        self.assertEqual([c["id"] for c in listed["commands"]], [got["id"]])
        self.assertEqual(listed["commands"][0]["label"], "gym")
        code, _ = self.hit("POST", f"/api/phone/commands/{got['id']}/done", {"ok": True})
        self.assertEqual(code, 200)
        self.assertEqual(self.hit("GET", "/api/phone/commands")[1]["commands"], [])
        self.assertEqual(phone.last_command(got["id"])["state"], "done")

    def test_a_held_alarm_is_not_handed_out_and_is_done_later(self):
        got = phone.alarm(15, 25, "call")
        self.hit("POST", f"/api/phone/commands/{got['id']}/done", {"ok": True, "held": True})
        self.assertEqual(phone.last_command(got["id"])["state"], "held")
        self.assertEqual(phone.commands()["commands"], [])
        self.hit("POST", f"/api/phone/commands/{got['id']}/done", {"ok": True})
        self.assertEqual(phone.last_command(got["id"])["state"], "done")

    def test_a_bad_time_is_refused(self):
        for body in ({"hour": 24, "minute": 0}, {"hour": 7, "minute": 60}, {"hour": "x"}):
            code, _ = self.hit("POST", "/api/phone/alarm", body)
            self.assertEqual(code, 400)
        self.assertEqual(phone.commands()["commands"], [])

    def test_a_stale_alarm_is_never_handed_out(self):
        phone.alarm(7, 0)
        clock.freeze(clock.now() + phone.COMMAND_TTL + 1)
        self.assertEqual(phone.commands()["commands"], [])

    def test_desk_can_set_an_alarm(self):
        said = voice.call("set_alarm", {"hour": 21, "minute": 5, "label": "call mom"})
        self.assertTrue(said["ok"])
        self.assertEqual(phone.commands()["commands"][0]["hour"], 21)
        self.assertIn("set_alarm", [t["name"] for t in voice.TOOLS])

    def test_notifications_are_kept_once_each(self):
        batch = {"items": [
            {"key": "wa|1", "package": "com.whatsapp", "app": "WhatsApp", "title": "Ankur",
             "text": "  are you coming   tonight? ", "posted": 1000},
            {"key": "gm|2", "package": "com.google.android.gm", "app": "Gmail", "title": "",
             "text": "", "posted": 1001},
        ]}
        code, got = self.hit("POST", "/api/phone/notifications", batch)
        self.assertEqual(code, 200)
        self.assertEqual(got["kept"], 1, "a notification with no title or text is not kept")
        code, again = self.hit("POST", "/api/phone/notifications", batch)
        self.assertEqual(again["kept"], 0, "a resent batch is not written twice")
        lines = (config.PHONE_DIR / "notifications.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["text"], "are you coming tonight?")
        self.assertEqual(self.hit("GET", "/api/phone/notifications?n=5")[1]["items"][0]["app"],
                         "WhatsApp")

    def test_a_restart_still_knows_what_it_kept(self):
        item = {"key": "wa|9", "package": "com.whatsapp", "title": "A", "text": "hi", "posted": 5}
        phone.keep({"items": [item]})
        phone.reset()
        self.assertEqual(phone.keep({"items": [item]})["kept"], 0)

    def test_notifications_must_be_a_list(self):
        code, _ = self.hit("POST", "/api/phone/notifications", {"items": "nope"})
        self.assertEqual(code, 400)


class AlarmTask(test_voice.Routes):
    """Every alarm is linked to a task; the alarm screen talks to the agent that set it; half an
    hour after Stop an open task means the front desk calls (The owner, 17 October 2026)."""

    def setUp(self):
        super().setUp()
        from commandcenter import broker, inbox
        self.sent = []
        real = broker.send
        broker.send = lambda agent, text, wake=False: self.sent.append((agent, text, wake)) or "sent"
        self.addCleanup(setattr, broker, "send", real)
        self.urgent = []
        real_add = inbox.add
        inbox.add = lambda agent, text, **kw: self.urgent.append((agent, text, kw)) or {"id": "x"}
        self.addCleanup(setattr, inbox, "add", real_add)

    def test_an_alarm_with_no_task_makes_one_and_links_it(self):
        from commandcenter import tasks
        got = phone.alarm(7, 0, "gym")
        task = tasks.get(got["task"])
        self.assertEqual(task["title"], "gym")
        self.assertEqual(task["state"], "mine")
        row = phone.commands()["commands"][0]
        self.assertEqual((row["task"], row["title"]), (task["id"], "gym"))

    def test_a_bare_wake_up_has_no_task_and_no_call(self):
        got = phone.alarm(6, 15)
        self.assertEqual(got["task"], "")
        phone.stopped(got["id"])
        clock.freeze(clock.now() + phone.CHECK_AFTER)
        phone.stop_check(got["id"])
        self.assertEqual(self.urgent, [])

    def test_an_alarm_for_a_task_he_has_uses_it(self):
        from commandcenter import tasks
        task = tasks.add_mine("Install the jet shower")
        got = phone.alarm(18, 0, "shower", task_id=task["id"])
        self.assertEqual(got["task"], task["id"])
        self.assertEqual(phone.commands()["commands"][0]["title"], "Install the jet shower")

    def test_an_unknown_task_is_refused(self):
        with self.assertRaises(ValueError):
            phone.alarm(18, 0, "x", task_id="nope")

    def test_a_second_alarm_with_the_same_label_links_the_same_task(self):
        a = phone.alarm(7, 0, "gym")
        b = phone.alarm(19, 0, "Gym")
        self.assertEqual(a["task"], b["task"])

    def test_the_prompt_box_goes_to_the_agent_that_set_it(self):
        self.agent("box")
        got = phone.alarm(7, 0, "gym", by="box")
        said = phone.prompt(got["id"], "move it to 8")
        self.assertEqual(said["to"], "box")
        self.assertEqual(self.sent[0][0], "box")
        self.assertIn("move it to 8", self.sent[0][1])
        self.assertTrue(self.sent[0][2])

    def test_an_agent_on_loopback_cannot_speak_from_the_alarm_screen(self):
        got = phone.alarm(7, 0, "gym")
        for what in ("prompt", "stopped", "done"):
            code, _ = self.hit("POST", f"/api/phone/alarm/{got['id']}/{what}", {"text": "x"})
            self.assertEqual(code, 403)
        self.assertEqual(self.sent, [])

    def test_a_snoozed_alarm_is_still_the_same_alarm(self):
        got = phone.alarm(7, 0, "gym")
        self.assertTrue(phone.stopped(f"{got['id']}-snooze-1792200000000")["ok"])

    def test_stopped_and_not_ticked_wakes_the_owner(self):
        """18 Oct 2026: the owner is told and decides, so it can tick on his yes."""
        from commandcenter import owners
        sent = []
        real = owners._deliver
        owners._deliver = lambda agent, text, hot: sent.append((agent, text, hot))
        self.addCleanup(setattr, owners, "_deliver", real)
        got = phone.alarm(7, 0, "gym")
        phone.stopped(got["id"])
        clock.freeze(clock.now() + phone.CHECK_AFTER)
        phone.stop_check(got["id"])
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0][0], sent[0][2]), ("personal-assistant", True))
        self.assertIn("The owner stopped its alarm", sent[0][1])
        self.assertEqual(self.urgent, [], "no call from here")
        phone.stop_check(got["id"])
        self.assertEqual(len(sent), 1, "once only")

    def test_done_on_the_alarm_ticks_its_task(self):
        """28 Oct 2026: "In alarm give me option of done"."""
        from commandcenter import tasks
        got = phone.alarm(7, 0, "gym")
        self.assertEqual(phone.ticked(got["id"])["said"], "Ticked: gym")
        self.assertEqual(tasks.get(got["task"])["state"], "ticked")
        clock.freeze(clock.now() + phone.CHECK_AFTER)
        self.assertIsNone(phone.stop_check(got["id"]))
        with self.assertRaises(ValueError):
            phone.ticked(phone.alarm(6, 15)["id"])

    def test_ticked_in_time_means_no_call(self):
        from commandcenter import tasks
        got = phone.alarm(7, 0, "gym")
        phone.stopped(got["id"])
        tasks.tick(got["task"])
        clock.freeze(clock.now() + phone.CHECK_AFTER)
        phone.stop_check(got["id"])
        self.assertEqual(self.urgent, [])

    def test_a_tick_a_drop_or_a_move_takes_back_the_alarms_not_rung(self):
        """22 Oct 2026: he was woken for things already done."""
        from commandcenter import tasks
        phone.start()
        gym = phone.alarm(7, 0, "gym")
        again = phone.alarm(8, 0, task_id=gym["task"])
        tea = phone.alarm(9, 0, "tea")
        tasks.tick(gym["task"])
        sent = [c["alarm"] for c in phone.commands()["commands"] if c["kind"] == "cancel_alarm"]
        self.assertEqual(sorted(sent), sorted([gym["id"], again["id"]]))
        self.assertNotIn(gym["id"], [c["id"] for c in phone.commands()["commands"]],
                         "an alarm not fetched yet is never handed out")
        tasks.move(tea["task"], phone._alarms()[tea["id"]]["due"])
        self.assertEqual(phone.open_alarms(tea["task"])[0]["id"], tea["id"], "one at the new time stays")
        tasks.dismiss(tea["task"])
        self.assertEqual(phone.open_alarms(tea["task"]), [])

    def test_desk_ticks_it_from_his_answer(self):
        from commandcenter import tasks
        got = phone.alarm(7, 0, "gym")
        said = voice.call("task_tick", {"task": "gym"})
        self.assertTrue(said["ok"])
        self.assertEqual(tasks.get(got["task"])["state"], "ticked")

    def test_not_done_passes_his_words_to_the_agent_that_set_it(self):
        self.agent("box")
        phone.alarm(7, 0, "gym", by="box")
        said = voice.call("task_later", {"task": "gym", "words": "tomorrow at six"})
        self.assertEqual(said["agent"], "box")
        self.assertIn("tomorrow at six", self.sent[0][1])

    def test_not_done_on_an_alarm_desk_set_comes_back_to_desk(self):
        got = voice.call("set_alarm", {"hour": 7, "minute": 0, "label": "gym"})
        said = voice.call("task_later", {"task": "gym", "words": "at nine"})
        self.assertIn(got["task"], said["said"])
        self.assertEqual(self.sent, [])

    def test_a_prompt_with_no_agent_to_take_it_is_refused_in_words(self):
        got = phone.alarm(7, 0, "gym")
        with self.assertRaisesRegex(ValueError, "no agent called personal-assistant"):
            phone.prompt(got["id"], "hi")

    def test_a_check_missed_while_the_daemon_was_down_is_dropped(self):
        got = phone.alarm(7, 0, "gym")
        phone.stopped(got["id"])
        clock.freeze(clock.now() + phone.CHECK_AFTER + 7200)
        armed = []
        real = phone._arm
        phone._arm = lambda key, wait: armed.append(key)
        self.addCleanup(setattr, phone, "_arm", real)
        phone.rearm()
        self.assertEqual(armed, [])

    def test_an_agent_floats_itself_once_and_a_delegate_may_not(self):
        import tempfile
        from commandcenter import delegates, registry
        me = registry.create(name="floater", cwd=tempfile.mkdtemp())["id"]
        first = phone.float_agent(me)
        again = phone.float_agent(me)
        waiting = [c for c in phone.commands()["commands"] if c["kind"] == "float"]
        self.assertEqual([c["id"] for c in waiting], [again["id"]])
        self.assertNotEqual(first["id"], again["id"])
        with self.assertRaises(ValueError):
            phone.float_agent("nobody-here")
        self.assertIn("/api/phone/float", delegates._REACH)
