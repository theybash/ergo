"""Which agents exist. Identity and intent, and no liveness at all."""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

from commandcenter import broker, config, registry, server
from tests.harness import Case


class Creating(Case):
    def test_an_agent_gets_an_id_from_its_name(self):
        rec = registry.create(name="Job Search", cwd=str(self.dir))
        self.assertEqual(rec["id"], "job-search")

    def test_a_second_agent_with_the_same_name_gets_its_own_id(self):
        registry.create(name="box", cwd=str(self.dir))
        second = registry.create(name="box", cwd=str(self.dir))
        self.assertEqual(second["id"], "box-2")

    def test_the_session_id_is_decided_here_not_discovered_later(self):
        rec = registry.create(name="box", cwd=str(self.dir))
        self.assertEqual(len(rec["session_id"].split("-")), 5)

    def test_there_can_be_only_one_master(self):
        registry.create(name="one", cwd=str(self.dir), role="master")
        with self.assertRaises(ValueError):
            registry.create(name="two", cwd=str(self.dir), role="master")

    def test_a_record_survives_a_restart(self):
        rec = registry.create(name="box", cwd=str(self.dir), model="opus")
        registry.reset()
        back = registry.get("box")
        self.assertEqual(back["session_id"], rec["session_id"])
        self.assertEqual(back["model"], "opus")

    def test_an_unreadable_file_is_kept_and_the_console_still_starts(self):
        config.AGENTS_FILE.write_text("{ not json")
        registry.reset()
        self.assertEqual(registry.all(), [])
        self.assertTrue(config.AGENTS_FILE.with_suffix(".broken").exists(),
                        "losing the list quietly is worse than either")

    def test_a_new_field_in_an_old_file_gets_its_default(self):
        config.AGENTS_FILE.write_text(json.dumps(
            {"version": 1, "agents": [{"id": "box", "name": "box"}]}))
        registry.reset()
        rec = registry.get("box")
        self.assertIn("permission_mode", rec)
        self.assertFalse(rec["hidden"])


class Changing(Case):
    def test_only_named_fields_can_be_set(self):
        registry.create(name="box", cwd=str(self.dir))
        with self.assertRaises(ValueError):
            registry.update("box", created=0, unix_user="someone")

    def test_an_agent_can_be_moved_to_another_folder(self):
        registry.create(name="box", cwd=str(self.dir))
        rec = registry.update("box", cwd="/home/dev/other/box")
        self.assertEqual(rec["cwd"], "/home/dev/other/box")
        registry.reset()
        self.assertEqual(registry.get("box")["cwd"], "/home/dev/other/box")

    def test_a_relative_folder_is_refused(self):
        """Claude Code finds the conversation from the folder path, so a relative one loses it."""
        registry.create(name="box", cwd=str(self.dir))
        for bad in ("other/box", "", None, "./box"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                registry.update("box", cwd=bad)

    def test_hiding_is_not_deleting(self):
        registry.create(name="box", cwd=str(self.dir))
        registry.hide("box")
        self.assertEqual(registry.all(), [])
        self.assertEqual(len(registry.all(include_hidden=True)), 1)
        registry.show("box")
        self.assertEqual(len(registry.all()), 1)

    def test_forgetting_drops_the_record(self):
        registry.create(name="box", cwd=str(self.dir))
        self.assertTrue(registry.forget("box"))
        self.assertIsNone(registry.get("box"))
        self.assertFalse(registry.forget("box"))

    def test_the_master_sorts_first(self):
        registry.create(name="one", cwd=str(self.dir))
        registry.create(name="two", cwd=str(self.dir), role="master")
        self.assertEqual(registry.all()[0]["role"], "master")

    def test_an_agent_shows_everywhere_until_it_is_tagged(self):
        rec = registry.create(name="box", cwd=str(self.dir))
        self.assertIsNone(rec["views"], "a new agent vanishing until tagged reads as a bug")

    def test_tagging_an_agent_into_two_views(self):
        registry.create(name="box", cwd=str(self.dir))
        rec = registry.update("box", views=["personal", "demo"])
        self.assertEqual(rec["views"], ["demo", "personal"])

    def test_all_three_is_recorded_as_none_because_it_means_the_same(self):
        registry.create(name="box", cwd=str(self.dir))
        rec = registry.update("box", views=["work", "demo", "personal"])
        self.assertIsNone(rec["views"])

    def test_choosing_nothing_shows_everywhere_rather_than_nowhere(self):
        registry.create(name="box", cwd=str(self.dir))
        self.assertIsNone(registry.update("box", views=[])["views"])

    def test_a_view_name_that_does_not_exist_is_refused(self):
        """It arrives from a browser, and an unknown name would hide the agent in every view."""
        registry.create(name="box", cwd=str(self.dir))
        for bad in (["nonsense"], "personal", ["work", "nope"], 7):
            with self.assertRaises(ValueError, msg=repr(bad)):
                registry.update("box", views=bad)

    def test_an_agent_can_be_promoted_to_master(self):
        registry.create(name="pa", cwd=str(self.dir))
        self.assertEqual(registry.update("pa", role="master")["role"], "master")

    def test_promoting_a_second_master_is_refused(self):
        """create always refused two. update did not, and which one Telegram talked to would have
        depended on the order the file happened to be written in."""
        registry.create(name="one", cwd=str(self.dir), role="master")
        registry.create(name="two", cwd=str(self.dir))
        with self.assertRaises(ValueError):
            registry.update("two", role="master")

    def test_promoting_the_agent_that_is_already_master_is_not_an_error(self):
        registry.create(name="one", cwd=str(self.dir), role="master")
        self.assertEqual(registry.update("one", role="master")["role"], "master")

    def test_finding_an_agent_by_its_conversation(self):
        rec = registry.create(name="box", cwd=str(self.dir))
        self.assertEqual(registry.by_session(rec["session_id"])["id"], "box")
        self.assertIsNone(registry.by_session("nothing"))


# -- moving an agent over the route ------------------------------------------------

class Moving(Case):
    """PATCH with a cwd is the daemon's half of a move. It records, it never copies a file."""

    def setUp(self):
        super().setUp()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]
        # Liveness belongs to the broker, and no test in here may start a real agent, so the
        # answer to "is it awake" is a switch the test sets.
        self.up = False
        real = broker.awake
        broker.awake = lambda agent_id: self.up
        self.addCleanup(lambda: setattr(broker, "awake", real))

    def patch(self, agent_id, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("PATCH", f"/api/agents/{agent_id}", body=json.dumps(body),
                     headers={"Content-Type": "application/json"})
        reply = conn.getresponse()
        got = json.loads(reply.read() or b"{}")
        conn.close()
        return reply.status, got

    def test_a_sleeping_agent_can_be_moved(self):
        self.agent("box")
        target = self.dir / "other" / "box"
        target.mkdir(parents=True)
        code, got = self.patch("box", {"cwd": str(target)})
        self.assertEqual(code, 200)
        self.assertEqual(got["cwd"], str(target))
        self.assertEqual(registry.get("box")["cwd"], str(target))

    def test_a_relative_folder_is_refused(self):
        self.agent("box")
        was = registry.get("box")["cwd"]
        code, got = self.patch("box", {"cwd": "other/box"})
        self.assertEqual(code, 400)
        self.assertIn("full path", got["error"])
        self.assertEqual(registry.get("box")["cwd"], was)

    def test_an_awake_agent_is_refused(self):
        """The engine holds the old folder open, so the record would disagree with the process."""
        self.agent("box")
        was = registry.get("box")["cwd"]
        self.up = True
        target = self.dir / "elsewhere"
        target.mkdir()
        code, got = self.patch("box", {"cwd": str(target)})
        self.assertEqual(code, 409)
        self.assertEqual(registry.get("box")["cwd"], was)

    def test_a_missing_folder_is_a_question_not_an_error(self):
        self.agent("box")
        was = registry.get("box")["cwd"]
        target = self.dir / "not there yet"
        code, got = self.patch("box", {"cwd": str(target)})
        self.assertEqual(code, 200)
        self.assertTrue(got["needs_dir"])
        self.assertEqual(got["path"], str(target))
        self.assertFalse(target.exists(), "the question must not make the folder")
        self.assertEqual(registry.get("box")["cwd"], was)

    def test_create_dir_answers_the_question(self):
        self.agent("box")
        target = self.dir / "not there yet"
        code, got = self.patch("box", {"cwd": str(target), "create_dir": True})
        self.assertEqual(code, 200)
        self.assertTrue(target.is_dir())
        self.assertEqual(got["cwd"], str(target))

    def test_the_daemon_moves_no_files(self):
        rec = self.agent("box")
        (self.dir / "box" / "CLAUDE.md").write_text("the brief")
        target = self.dir / "other" / "box"
        target.mkdir(parents=True)
        self.patch("box", {"cwd": str(target)})
        self.assertTrue((self.dir / "box" / "CLAUDE.md").exists())
        self.assertFalse((target / "CLAUDE.md").exists())
        self.assertEqual(registry.get("box")["session_id"], rec["session_id"])

    def test_every_other_field_is_still_dropped(self):
        rec = self.agent("box")
        code, got = self.patch("box", {"session_id": "nonsense", "created": 0,
                                       "hidden": True, "name": "renamed"})
        self.assertEqual(code, 200)
        self.assertEqual(got["name"], "renamed")
        self.assertEqual(got["session_id"], rec["session_id"])
        self.assertEqual(got["created"], rec["created"])
        self.assertFalse(got["hidden"])

    def test_moving_an_agent_that_does_not_exist(self):
        self.assertEqual(self.patch("ghost", {"cwd": str(self.dir)})[0], 404)
