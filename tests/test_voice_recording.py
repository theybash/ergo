"""Call recordings: the Config switch, and the file the app sends when a session closes."""

import io

from tests.harness import Case

from commandcenter import memory, voice


class Recording(Case):
    def test_recording_is_on_by_default_and_config_turns_it_off(self):
        self.assertTrue(memory.record_calls())
        self.assertTrue(memory.settings()["record_calls"])
        memory.save_settings({"record_calls": False})
        self.assertFalse(memory.record_calls())
        self.assertFalse(memory.settings()["record_calls"])

    def test_the_switch_takes_only_true_or_false(self):
        with self.assertRaises(ValueError):
            memory.save_settings({"record_calls": "yes"})

    def test_a_recording_is_kept_by_date_and_who(self):
        body = b"webm" * 1000
        path = voice.save_recording("sess_abcdef123456", io.BytesIO(body), len(body))
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(path.parent.parent, voice.recordings_dir())
        self.assertTrue(path.name.endswith("-desk-ef123456.webm"))

    def test_pieces_sent_during_the_call_make_one_file(self):
        """A call that drops must still keep its recording. Pieces go up as they are made;
        a piece sent twice is kept once, and agent calls carry the switch too."""
        a = voice.save_recording("sess_pieces12345", io.BytesIO(b"head"), 4, part=0)
        b = voice.save_recording("sess_pieces12345", io.BytesIO(b"more"), 4, part=1)
        voice.save_recording("sess_pieces12345", io.BytesIO(b"more"), 4, part=1)
        self.assertEqual(a, b)
        self.assertEqual(a.read_bytes(), b"headmore")
        with self.assertRaises(ValueError):
            voice.save_recording("sess_neverbegan1", io.BytesIO(b"x"), 1, part=3)
        from commandcenter import agentcall
        self.assertIn('"record": memory.record_calls()', open(agentcall.__file__).read())

    def test_nothing_is_kept_when_recording_is_off(self):
        memory.save_settings({"record_calls": False})
        with self.assertRaises(ValueError):
            voice.save_recording("sess_abcdef123456", io.BytesIO(b"x"), 1)
        self.assertFalse(voice.recordings_dir().exists())

    def test_a_cut_short_upload_leaves_no_file(self):
        with self.assertRaises(ValueError):
            voice.save_recording("sess_abcdef123456", io.BytesIO(b"abc"), 10)
        files = [p for p in voice.recordings_dir().rglob("*") if p.is_file()]
        self.assertEqual(files, [])

    def test_a_strange_session_name_is_refused(self):
        with self.assertRaises(ValueError):
            voice.save_recording("../../etc", io.BytesIO(b"x"), 1)

    def test_the_call_offer_carries_the_switch(self):
        self.assertIn('"record": memory.record_calls()', open(voice.__file__).read())
