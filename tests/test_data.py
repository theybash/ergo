"""How a screen gets its numbers. Answers 14 and 38.

The screen asks by name and never learns whether the answer came from a file or from the agent's own
server. That is what lets an agent move from one to the other without touching the screen it wrote.
"""

import json

from commandcenter import config, data, servers
from tests.harness import Case, json_route


class Files(Case):
    def test_a_published_file_is_found_by_name(self):
        rec = self.agent("box")
        self.publish(rec, "positions.json", '{"rows": [1, 2, 3]}')
        got = data.resolve(rec, "positions")
        self.assertEqual(got["source"], "file")
        self.assertEqual(got["value"], {"rows": [1, 2, 3]})

    def test_every_answer_says_how_old_it_is(self):
        """Answer 14: a screen showing yesterday's numbers as this morning's is worse than nothing."""
        rec = self.agent("box")
        path = self.publish(rec, "positions.json", "{}")
        import os
        os.utime(path, (0, 1787000000.0))
        got = data.resolve(rec, "positions")
        self.assertEqual(got["updated_at"], 1787000000.0)
        self.assertGreater(got["age"], 0)

    def test_text_csv_and_markdown_are_read_too(self):
        rec = self.agent("box")
        for name, body in (("notes.txt", "a note"), ("rows.csv", "a,b"), ("log.md", "# hi")):
            self.publish(rec, name, body)
            got = data.resolve(rec, name.split(".")[0])
            self.assertEqual(got["text"], body)

    def test_half_written_json_is_reported_rather_than_drawn_as_nothing(self):
        """The normal way this fails: the agent was writing the file as the screen read it."""
        rec = self.agent("box")
        self.publish(rec, "positions.json", '{"rows": [1,2,')
        got = data.resolve(rec, "positions")
        self.assertIn("not valid json yet", got["error"])
        self.assertIsNotNone(got["text"], "the raw text still comes back")

    def test_a_name_nothing_matches_is_a_missing_thing(self):
        rec = self.agent("box")
        with self.assertRaises(KeyError):
            data.resolve(rec, "nothing")

    def test_a_name_that_is_a_path_is_refused_before_anything_is_read(self):
        rec = self.agent("box")
        for bad in ("../../etc/passwd", "..", "Positions", "with space", "", "a" * 80):
            with self.assertRaises(ValueError, msg=bad):
                data.resolve(rec, bad)

    def test_a_file_too_big_for_a_screen_is_refused_with_its_size(self):
        rec = self.agent("box")
        self.publish(rec, "big.txt", "x" * (data.MAX_FILE + 10))
        with self.assertRaises(ValueError):
            data.resolve(rec, "big")

    def test_a_stray_file_in_the_folder_is_not_listed(self):
        rec = self.agent("box")
        self.publish(rec, "notes.txt", "kept")
        self.publish(rec, "picture.png", "ignored")
        self.publish(rec, "UPPER.json", "ignored")
        self.assertEqual([e["name"] for e in data.listing(rec)], ["notes"])


class Servers(Case):
    def _serve(self, rec, routes):
        """Stand an agent's own server up, without starting a process."""
        srv = self.http(routes)
        servers._running[rec["id"]] = {"proc": None, "pid": None, "port": srv.port,
                                       "started": 0}
        # `running()` asks whether the process is alive, and there is no process here.
        real = servers.running
        servers.running = lambda agent_id: agent_id == rec["id"]
        self.addCleanup(lambda: setattr(servers, "running", real))
        return srv

    def test_a_live_server_answers_before_the_file(self):
        rec = self.agent("box")
        self.publish(rec, "positions.json", '{"from": "the file"}')
        self._serve(rec, {"/data/positions": json_route({"from": "the server"})})
        got = data.resolve(rec, "positions")
        self.assertEqual(got["source"], "server")
        self.assertEqual(got["value"], {"from": "the server"})

    def test_a_name_the_server_does_not_have_falls_through_to_the_file(self):
        """Answer 38: the daemon resolves it, so an agent can have both."""
        rec = self.agent("box")
        self.publish(rec, "notes.txt", "only in the file")
        self._serve(rec, {"/data/positions": json_route({"live": True})})
        got = data.resolve(rec, "notes")
        self.assertEqual(got["source"], "file")

    def test_with_the_server_down_the_file_answers_and_says_its_age(self):
        rec = self.agent("box")
        self.publish(rec, "positions.json", '{"from": "the file"}')
        got = data.resolve(rec, "positions")
        self.assertEqual(got["source"], "file")
        self.assertIn("age", got)

    def test_the_server_is_believed_about_when_its_numbers_are_from(self):
        rec = self.agent("box")
        self._serve(rec, {"/data/p": json_route({"updated_at": 1787000000.0, "v": 1})})
        got = data.resolve(rec, "p")
        self.assertEqual(got["updated_at"], 1787000000.0)

    def test_a_server_error_is_not_an_answer(self):
        rec = self.agent("box")
        self.publish(rec, "p.json", '{"from": "the file"}')
        self._serve(rec, {"/data/p": lambda h, b: (500, "text/plain", "broken")})
        self.assertEqual(data.resolve(rec, "p")["source"], "file")

    def test_available_says_what_there_is_and_whether_a_server_is_up(self):
        rec = self.agent("box")
        self.publish(rec, "a.json", "{}")
        got = data.available(rec)
        self.assertEqual([f["name"] for f in got["files"]], ["a"])
        self.assertFalse(got["server"]["up"])
