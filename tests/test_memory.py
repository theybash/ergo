"""The voice's memory: the window in the briefing, and recall over the whole call log.

TypeSafe is never called. `memory.post_jev` is one module function for exactly this reason, so a
test replaces it, reads the request that would have gone out, and answers for Jev.
"""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

from commandcenter import config, memory, server, voice
from tests.harness import NINE_AM, Case

DAY = 86400.0
KEY = "ts-not-a-real-key-123456"
OPENAI = "sk-not-a-real-openai-key-98765"

# The fake embedding model. One dimension per topic word, plus a small constant so no vector is
# zero. Turns that share topic words are close; turns that share none are not.
TOPICS = ["contabo", "ram", "box", "northwind", "globex", "letter", "network", "swap", "support"]


def fake_vector(text):
    words = text.lower().replace(".", " ").replace(",", " ").split()
    vec = [0.0] * memory.DIMENSIONS
    for n, topic in enumerate(TOPICS):
        vec[n] = float(sum(1 for w in words if w.startswith(topic)))
    vec[memory.DIMENSIONS - 1] = 0.05
    return vec


class MemoryCase(Case):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.answer = None
        real = memory.post_jev

        def fake(payload, key, timeout=None):
            self.sent.append({"payload": payload, "key": key})
            if isinstance(self.answer, Exception):
                raise self.answer
            if callable(self.answer):
                return self.answer(payload)
            return self.answer or {"answers": {}}

        memory.post_jev = fake
        self.addCleanup(lambda: setattr(memory, "post_jev", real))

        config.OPENAI_KEY = OPENAI
        self.embeds = []
        self.embed_fails = None
        real_embed = memory.post_embed

        def fake_embed(payload, key, timeout=None):
            self.embeds.append({"payload": payload, "key": key})
            if self.embed_fails:
                raise self.embed_fails
            return {"data": [{"index": n, "embedding": fake_vector(t)}
                             for n, t in enumerate(payload["input"])],
                    "usage": {"total_tokens": 10 * len(payload["input"])}}

        memory.post_embed = fake_embed
        self.addCleanup(lambda: setattr(memory, "post_embed", real_embed))

    def log_rows(self, rows):
        """Append rows to the call log: (seconds before now, who, text)."""
        path = config.VOICE_DIR / "calls.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            for ago, who, text in rows:
                fh.write(json.dumps({"at": NINE_AM - ago, "who": who, "text": text}) + "\n")

    def old(self, texts, days=3):
        """Turns from an earlier call, `days` ago, alternating the owner and the front desk."""
        self.log_rows([(days * DAY - i, "you" if i % 2 == 0 else "voice", t)
                       for i, t in enumerate(texts)])

    def filler(self, count, ago=DAY):
        self.log_rows([(ago - i, "voice", f"filler turn number {i} about nothing much")
                       for i in range(count)])


# -- the setting -----------------------------------------------------------------

class Setting(MemoryCase):
    def test_the_default_comes_from_the_environment_setting(self):
        config.VOICE_MEMORY_TURNS = 30
        self.assertEqual(memory.memory_turns(), 30)

    def test_a_saved_number_wins_and_survives_a_restart(self):
        memory.set_memory_turns(42)
        memory.reset()
        self.assertEqual(memory.memory_turns(), 42)
        held = json.loads((config.VOICE_DIR / "settings.json").read_text())
        self.assertEqual(held["memory_turns"], 42)

    def test_it_is_clamped_to_one_and_a_hundred(self):
        self.assertEqual(memory.set_memory_turns(0)["memory_turns"], 1)
        self.assertEqual(memory.set_memory_turns(-5)["memory_turns"], 1)
        self.assertEqual(memory.set_memory_turns(500)["memory_turns"], 100)
        self.assertEqual(memory.set_memory_turns("17")["memory_turns"], 17)

    def test_something_that_is_not_a_number_is_refused(self):
        with self.assertRaises(ValueError):
            memory.set_memory_turns("lots")
        with self.assertRaises(ValueError):
            memory.set_memory_turns(None)

    def test_a_default_out_of_range_is_clamped_too(self):
        config.VOICE_MEMORY_TURNS = 400
        self.assertEqual(memory.memory_turns(), 100)

    def test_a_damaged_settings_file_falls_back_to_the_default(self):
        config.VOICE_DIR.mkdir(parents=True, exist_ok=True)
        (config.VOICE_DIR / "settings.json").write_text("{not json")
        self.assertEqual(memory.memory_turns(), 30)


    def test_recall_span_defaults_to_two_and_is_clamped_to_ten(self):
        self.assertEqual(memory.recall_span(), 2)
        self.assertEqual(memory.save_settings({"recall_span": 50})["recall_span"], 10)
        self.assertEqual(memory.save_settings({"recall_span": -3})["recall_span"], 0)
        config.VOICE_RECALL_SPAN = 99
        self.assertEqual(memory.settings()["limits"]["recall_span"]["default"], 10)

    def test_recall_span_survives_a_restart_and_leaves_the_window_alone(self):
        memory.save_settings({"memory_turns": 44})
        memory.save_settings({"recall_span": 4})
        memory.reset()
        self.assertEqual((memory.memory_turns(), memory.recall_span()), (44, 4))

    def test_a_bad_value_saves_nothing(self):
        memory.save_settings({"recall_span": 3})
        with self.assertRaises(ValueError):
            memory.save_settings({"memory_turns": 20, "recall_span": "wide"})
        self.assertEqual((memory.memory_turns(), memory.recall_span()), (30, 3))
        with self.assertRaises(ValueError):
            memory.save_settings({})


class Silence(MemoryCase):
    def test_it_defaults_to_eight_from_the_environment_setting(self):
        self.assertEqual(memory.silence_minutes(), 8)
        config.VOICE_SILENCE_MINUTES = 12
        self.assertEqual(memory.silence_minutes(), 12)

    def test_it_is_clamped_to_one_and_thirty(self):
        self.assertEqual(memory.save_settings({"silence_minutes": 0})["silence_minutes"], 1)
        self.assertEqual(memory.save_settings({"silence_minutes": 99})["silence_minutes"], 30)
        config.VOICE_SILENCE_MINUTES = 400
        self.assertEqual(memory.settings()["limits"]["silence_minutes"]["default"], 30)

    def test_it_survives_a_restart_and_leaves_the_other_two_alone(self):
        memory.save_settings({"memory_turns": 44, "recall_span": 4})
        memory.save_settings({"silence_minutes": 5})
        memory.reset()
        self.assertEqual((memory.memory_turns(), memory.recall_span(), memory.silence_minutes()),
                         (44, 4, 5))
        held = json.loads((config.VOICE_DIR / "settings.json").read_text())
        self.assertEqual(held["silence_minutes"], 5)

    def test_words_are_refused_and_nothing_is_saved(self):
        with self.assertRaises(ValueError):
            memory.save_settings({"silence_minutes": "ages"})
        self.assertEqual(memory.silence_minutes(), 8)


class BriefingWindow(MemoryCase):
    def test_the_briefing_reads_the_last_n_turns(self):
        self.log_rows([(1000 - i, "you", f"turn{i:03d} said") for i in range(60)])
        memory.set_memory_turns(20)
        said = voice.briefing()
        self.assertIn("turn059", said)
        self.assertIn("turn040", said)
        self.assertNotIn("turn039", said)

    def test_thirty_turns_fit_whole_by_default(self):
        self.log_rows([(1000 - i, "you", f"turn{i:03d} " + "word " * 25) for i in range(40)])
        said = voice.briefing()
        self.assertIn("turn010", said)
        self.assertNotIn("turn009", said)

    def test_when_it_overruns_the_oldest_turns_go_and_the_persona_stays(self):
        memory.set_memory_turns(100)
        voice.BRIEFING_WORDS, real = 0, voice.BRIEFING_WORDS
        voice.TURN_WORDS, real_turn = 5, voice.TURN_WORDS
        self.addCleanup(lambda: setattr(voice, "BRIEFING_WORDS", real))
        self.addCleanup(lambda: setattr(voice, "TURN_WORDS", real_turn))
        self.log_rows([(1000 - i, "you", f"turn{i:03d} " + "word " * 25) for i in range(100)])
        said = voice.briefing()
        self.assertIn(voice._persona()[:60], said)
        self.assertIn("turn099", said)
        self.assertNotIn("turn000", said)


# -- the log and the vectors -----------------------------------------------------

class Vectors(MemoryCase):
    def test_every_spoken_turn_is_embedded_once_and_saved(self):
        self.old(["the Contabo box", "it has 8 GB of RAM"])
        self.log_rows([(3 * DAY - 5, "note", "Sent to personal-assistant")])
        self.assertEqual(memory.catch_up(memory.calls_log()), 2)
        saved = [json.loads(x) for x in
                 (config.VOICE_DIR / "embeddings.jsonl").read_text().splitlines()]
        self.assertEqual(len(saved), 2)
        self.assertEqual(set(saved[0]), {"at", "who", "key", "v"})
        self.assertEqual(len(memory._unpack(saved[0]["v"])), memory.DIMENSIONS)
        sent = self.embeds[-1]["payload"]
        self.assertEqual(sent["model"], "text-embedding-3-small")
        self.assertEqual(sent["input"], ["The owner: the Contabo box", "The front desk: it has 8 GB of RAM"])

    def test_only_new_rows_are_embedded_when_the_log_grows(self):
        self.old(["the Contabo box", "it has 8 GB of RAM"])
        memory.catch_up(memory.calls_log())
        self.old(["Northwind sent the terms"], days=2)
        self.assertEqual(memory.catch_up(memory.calls_log()), 1)
        self.assertEqual(self.embeds[-1]["payload"]["input"], ["The owner: Northwind sent the terms"])
        self.assertEqual(memory.catch_up(memory.calls_log()), 0)
        self.assertEqual(len(self.embeds), 2)

    def test_a_restart_reads_the_saved_vectors_and_embeds_nothing_again(self):
        self.old(["the Contabo box", "it has 8 GB of RAM"])
        memory.catch_up(memory.calls_log())
        memory.reset()
        self.assertEqual(memory.catch_up(memory.calls_log()), 0)
        self.assertEqual(len(self.embeds), 1)

    def test_embedding_goes_in_batches_of_a_hundred(self):
        self.old([f"turn {i} about the box" for i in range(250)])
        self.assertEqual(memory.catch_up(memory.calls_log()), 250)
        self.assertEqual([len(e["payload"]["input"]) for e in self.embeds], [100, 100, 50])

    def test_a_failure_halfway_keeps_the_batches_already_paid_for(self):
        self.old([f"turn {i} about the box" for i in range(150)])
        real = memory.post_embed
        calls = []

        def second_fails(payload, key, timeout=None):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("OpenAI could not be reached: timed out")
            return real(payload, key)

        memory.post_embed = second_fails
        with self.assertRaises(RuntimeError):
            memory.catch_up(memory.calls_log())
        memory.post_embed = real
        self.assertEqual(memory.catch_up(memory.calls_log()), 50)

    def test_the_log_grows_without_being_read_again(self):
        self.old(["first thing about Globex"])
        lg = memory.calls_log()
        offset = lg.offset
        self.old(["second thing about Globex"], days=2)
        again = memory.calls_log()
        self.assertIs(again, lg)
        self.assertEqual(len(again.rows), 2)
        self.assertGreater(again.offset, offset)

    def test_a_half_written_last_line_waits_for_the_next_read(self):
        path = config.VOICE_DIR / "calls.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": 1, "who": "you", "text": "whole"}) + "\n"
                        + '{"at": 2, "who": "you", "te')
        self.assertEqual(len(memory.calls_log().rows), 1)
        with open(path, "a") as fh:
            fh.write('xt": "finished"}\n')
        self.assertEqual([r["text"] for r in memory.calls_log().rows], ["whole", "finished"])

    def test_a_file_that_got_shorter_is_read_again_from_the_top(self):
        self.old(["one", "two", "three"])
        self.assertEqual(len(memory.calls_log().rows), 3)
        (config.VOICE_DIR / "calls.jsonl").write_text(
            json.dumps({"at": 1, "who": "you", "text": "fresh"}) + "\n")
        self.assertEqual([r["text"] for r in memory.calls_log().rows], ["fresh"])


class Search(MemoryCase):
    def query(self, text):
        return memory._unit(fake_vector(text))

    def ready(self):
        lg = memory.calls_log()
        memory.catch_up(lg)
        return lg

    def test_the_closest_by_cosine_come_first(self):
        self.old(["the weather is fine today",
                  "the Contabo box has 8 GB of RAM",
                  "RAM is cheap these days"])
        self.filler(40)
        lg = self.ready()
        ranked = [lg.rows[i]["text"] for _, i in memory.nearest(lg, self.query("RAM Contabo"))]
        self.assertEqual(ranked[0], "the Contabo box has 8 GB of RAM")
        self.assertEqual(ranked[1], "RAM is cheap these days")

    def test_k_counts_hits(self):
        self.old([f"the box number {i}" for i in range(80)])
        self.filler(30)
        lg = self.ready()
        self.assertEqual(len(memory.nearest(lg, self.query("box"))), 30)
        config.VOICE_RECALL_K = 7
        self.assertEqual(len(memory.nearest(lg, self.query("box"))), 7)

    def test_every_row_is_numbered_by_its_line_in_the_file(self):
        path = config.VOICE_DIR / "calls.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": 1, "who": "you", "text": "one"}) + "\n\n"
                        + "not json\n" + json.dumps({"at": 2, "who": "note", "text": "x"}) + "\n")
        self.old(["five"])
        self.assertEqual([(r["n"], r["text"]) for r in memory.calls_log().rows],
                         [(1, "one"), (4, "x"), (5, "five")])

    def test_a_hit_brings_two_turns_each_side_by_line_number(self):
        self.old([f"turn {i}" for i in range(10)])
        lg = self.ready()
        self.assertEqual([lg.rows[j]["n"] for j in memory.around(lg, 4)], [3, 4, 5, 6, 7])

    def test_the_range_is_clamped_to_the_file(self):
        self.old([f"turn {i}" for i in range(4)])
        lg = self.ready()
        self.assertEqual([lg.rows[j]["n"] for j in memory.around(lg, 0)], [1, 2, 3])
        self.assertEqual([lg.rows[j]["n"] for j in memory.around(lg, 3)], [2, 3, 4])

    def test_notes_are_skipped_in_the_range(self):
        self.old(["a", "b"])
        self.log_rows([(3 * DAY - 10, "note", "Sent to job-search")])
        self.old(["c", "d"], days=2)
        lg = self.ready()
        self.assertEqual([lg.rows[j]["text"] for j in memory.around(lg, 1)], ["a", "b", "c"])

    def test_the_span_is_the_setting(self):
        self.old([f"turn {i}" for i in range(20)])
        lg = self.ready()
        memory.save_settings({"recall_span": 0})
        self.assertEqual([lg.rows[j]["n"] for j in memory.around(lg, 9)], [10])
        memory.save_settings({"recall_span": 5})
        self.assertEqual(len(memory.around(lg, 9)), 11)

    def test_the_window_and_the_live_call_are_not_searched(self):
        self.old(["Globex said yes in the old call"])
        self.log_rows([(DAY - i, "you", f"Globex window turn {i}") for i in range(30)])
        self.log_rows([(60 - i, "you", f"Globex live turn {i}") for i in range(3)])
        lg = self.ready()
        found = [lg.rows[i]["text"] for _, i in memory.nearest(lg, self.query("Globex"))]
        self.assertEqual(found, ["Globex said yes in the old call"])

    def test_notes_are_never_searched(self):
        self.log_rows([(3 * DAY, "note", "Sent to job-search: find Contabo prices")])
        self.filler(30)
        lg = self.ready()
        self.assertEqual(memory.nearest(lg, self.query("Contabo")), [])


# -- Jev -------------------------------------------------------------------------

class Qualifying(MemoryCase):
    def setUp(self):
        super().setUp()
        config.TYPESAFE_KEY = KEY
        config.VOICE_RECALL_K = 3
        self.old(["tell me about the Contabo box",
                  "The Contabo box has 8 GB of RAM and swaps under load",
                  "noted",
                  "Contabo support never answered"])
        self.old(["Northwind wants a call on Friday"], days=5)
        self.filler(40)
        self.log_rows([(120, "you", "remind me what we said about the server"),
                       (100, "voice", "One moment.")])

    def yes(self, by_text):
        """Answer for Jev: a p(yes) per candidate, found by a word in its text."""
        def answer(payload):
            out = {}
            for turn in payload["state"]["remembered_turns"]:
                p = next((v for word, v in by_text.items() if word in turn["text"]), 0.05)
                out[turn["id"]] = {"type": "noul", "noul": p}
            return {"model": "jev-test", "answers": out}
        return answer

    def test_the_query_is_the_keywords_and_the_about_together(self):
        self.answer = self.yes({})
        memory.recall("Contabo RAM", "how much RAM the box has")
        self.assertEqual(self.embeds[-1]["payload"]["input"],
                         ["Contabo RAM how much RAM the box has"])

    def test_the_request_holds_the_live_call_the_about_and_the_candidates(self):
        self.answer = self.yes({})
        memory.recall("Contabo RAM", "how much RAM the Contabo box has")
        payload = self.sent[-1]["payload"]
        state = payload["state"]
        self.assertEqual(payload["model"], "jev-latest")
        self.assertEqual(state["trying_to_remember"], "how much RAM the Contabo box has")
        self.assertEqual([t["text"] for t in state["live_call"]],
                         ["remind me what we said about the server", "One moment."])
        for t in state["remembered_turns"]:
            self.assertEqual(set(t), {"id", "date", "speaker", "text"})

    def test_the_keys_go_to_their_transports_and_never_into_a_body(self):
        self.answer = self.yes({})
        memory.recall("Contabo", "the box")
        self.assertEqual(self.sent[-1]["key"], KEY)
        self.assertEqual(self.embeds[-1]["key"], OPENAI)
        for body in (self.sent[-1]["payload"], self.embeds[-1]["payload"]):
            self.assertNotIn(KEY, json.dumps(body))
            self.assertNotIn(OPENAI, json.dumps(body))

    def test_jev_judges_exactly_the_k_hits_and_nothing_else(self):
        config.VOICE_RECALL_K = 30
        self.filler(50, ago=2 * DAY)
        self.answer = self.yes({})
        memory.recall("Contabo", "the box")
        self.assertEqual(len(self.sent), 1)
        payload = self.sent[-1]["payload"]
        turns = payload["state"]["remembered_turns"]
        self.assertEqual(len(payload["questions"]), 30)
        self.assertEqual(sorted(payload["questions"]), sorted(t["id"] for t in turns))
        for n, t in enumerate(turns):
            q = payload["questions"][t["id"]]
            self.assertEqual(q["type"], "noul")
            self.assertEqual(set(q["criteria"]), {"true", "false"})
            self.assertIn("filler", q["criteria"]["false"])
            self.assertEqual(q["instructions"]["remembered_turn"]["text"], t["text"])
            self.assertIn(f"`remembered_turns[{n}]`", q["instructions"]["question"])

    def test_a_yes_brings_its_range_marked_numbered_and_in_order(self):
        # Lines 1 to 4 are the Contabo call, line 5 is Northwind, 6 onward is filler.
        self.answer = self.yes({"8 GB": 0.9})
        got = memory.recall("Contabo RAM", "how much RAM the box has")
        self.assertNotIn("note", got)
        self.assertEqual([m["n"] for m in got["memories"]], [1, 2, 3, 4])
        hit = [m for m in got["memories"] if m.get("hit")]
        self.assertEqual([m["n"] for m in hit], [2])
        self.assertEqual(hit[0]["text"], "The Contabo box has 8 GB of RAM and swaps under load")
        self.assertEqual(hit[0]["who"], "The front desk")
        self.assertEqual(hit[0]["date"], "Sun 16 Aug")
        self.assertEqual(set(got["memories"][0]), {"n", "date", "who", "text"})

    def test_two_yeses_that_overlap_are_merged(self):
        self.answer = self.yes({"8 GB": 0.9, "support": 0.8})
        got = memory.recall("Contabo", "the box")
        ns = [m["n"] for m in got["memories"]]
        self.assertEqual(ns, sorted(set(ns)))
        self.assertEqual([m["n"] for m in got["memories"] if m.get("hit")], [2, 4])

    def test_the_threshold_is_a_half_and_a_half_is_not_a_yes(self):
        self.assertEqual(memory.KEEP_ABOVE, 0.5)
        lg = memory.calls_log()
        kept, yes = memory.pick(lg, [0, 1], {"m1": {"noul": 0.5}, "m2": {"noul": 0.51}})
        self.assertEqual(yes, 1)
        self.assertEqual([t["n"] for t in kept if t.get("hit")], [2])

    def test_the_budget_keeps_whole_ranges_from_the_closest_hit_down(self):
        (config.VOICE_DIR / "calls.jsonl").write_text("")
        memory.reset()
        # Three separate stretches of five turns, each 1000 characters in all.
        for block in range(3):
            self.old([f"{block}" * 200 for _ in range(5)], days=10 - block)
        lg = memory.calls_log()
        hits = [12, 2, 7]          # closest first: the third stretch, then the first, the second
        answers = {f"m{lg.rows[i]['n']}": {"noul": 0.9} for i in hits}
        memory.BUDGET, real = 2500, memory.BUDGET
        self.addCleanup(lambda: setattr(memory, "BUDGET", real))
        kept, yes = memory.pick(lg, hits, answers)
        self.assertEqual(yes, 3)
        self.assertEqual([t["n"] for t in kept], [1, 2, 3, 4, 5, 11, 12, 13, 14, 15])

    def test_one_range_alone_over_the_budget_is_cut_not_dropped(self):
        (config.VOICE_DIR / "calls.jsonl").write_text("")
        memory.reset()
        self.old(["x" * 3000 for _ in range(5)])
        lg = memory.calls_log()
        kept, _ = memory.pick(lg, [2], {"m3": {"noul": 0.9}})
        self.assertEqual(len(kept), 5)
        self.assertLessEqual(sum(len(t["text"]) for t in kept), memory.BUDGET)

    def test_one_log_line_with_counts_and_timings_and_no_text(self):
        self.answer = self.yes({"8 GB": 0.9})
        with self.assertLogs("cc.memory", level="INFO") as caught:
            memory.recall("Contabo RAM secretword", "how much RAM")
        line = [x for x in caught.output if "voice recall:" in x]
        self.assertEqual(len(line), 1)
        self.assertIn("k 3, 1 yes, 4 turns returned, jev ran", line[0])
        self.assertIn("embed ", line[0])
        self.assertIn(" ms in all", line[0])
        self.assertNotIn("swaps under load", line[0])
        self.assertNotIn("secretword", line[0])

    def test_the_tool_route_reaches_it(self):
        self.answer = self.yes({"8 GB": 0.9})
        got = voice.call("recall", {"keywords": "Contabo RAM", "about": "the RAM"})
        self.assertEqual(len(got["memories"]), 4)
        self.assertIn("recall", [t["name"] for t in voice.TOOLS])

    def test_no_yes_says_nothing_relevant(self):
        self.answer = self.yes({})
        self.assertEqual(memory.recall("Contabo", "the box"),
                         {"memories": [], "note": "nothing relevant"})

    def test_a_jev_failure_falls_back_to_the_top_five_by_cosine_unranked(self):
        config.VOICE_RECALL_K = 30
        self.answer = RuntimeError("TypeSafe could not be reached: timed out")
        got = memory.recall("Contabo", "the box")
        self.assertIn("unranked", got["note"])
        self.assertEqual(len(got["memories"]), memory.FALLBACK)
        self.assertIn("Contabo support never answered", [m["text"] for m in got["memories"]])

    def test_no_typesafe_key_falls_back_without_calling_jev(self):
        config.TYPESAFE_KEY = ""
        got = memory.recall("Contabo", "the box")
        self.assertEqual(self.sent, [])
        self.assertIn("unranked", got["note"])
        self.assertTrue(got["memories"])

    def test_an_embedding_failure_says_search_is_unavailable(self):
        self.embed_fails = RuntimeError("OpenAI could not be reached: timed out")
        with self.assertLogs("cc.memory", level="INFO") as caught:
            got = memory.recall("Contabo", "the box")
        self.assertEqual(got, {"memories": [], "note": "memory search is unavailable right now"})
        self.assertEqual(self.sent, [])
        self.assertTrue(any("unavailable" in x for x in caught.output))

    def test_no_openai_key_says_search_is_unavailable(self):
        config.OPENAI_KEY = ""
        got = memory.recall("Contabo", "the box")
        self.assertEqual(got["note"], "memory search is unavailable right now")
        self.assertEqual(self.embeds, [])

    def test_a_broken_answer_from_jev_is_not_a_failure(self):
        self.answer = lambda payload: ["not", "a", "dict"]
        self.assertIn("memories", memory.recall("Contabo", "the box"))



class Reading(MemoryCase):
    def setUp(self):
        super().setUp()
        self.old([f"turn {i}" for i in range(100)])
        self.log_rows([(3 * DAY - 200, "note", "Sent to personal-assistant")])

    def test_a_range_comes_back_inclusive_and_numbered(self):
        got = memory.turns(10, 14)
        self.assertEqual([t["n"] for t in got["turns"]], [10, 11, 12, 13, 14])
        self.assertEqual(set(got["turns"][0]), {"n", "date", "who", "text"})
        self.assertEqual(got["turns"][0]["text"], "turn 9")
        self.assertNotIn("note", got)

    def test_a_note_is_included_and_marked(self):
        got = memory.turns(100, 101)
        self.assertEqual([t["who"] for t in got["turns"]], ["The front desk", "note"])

    def test_sixty_at_most_and_it_says_where_to_go_on(self):
        got = memory.turns(1, 100)
        self.assertEqual(len(got["turns"]), 60)
        self.assertEqual(got["note"], "capped, ask for n=61 onward")

    def test_numbers_out_of_range_are_clamped(self):
        got = memory.turns(-5, 3)
        self.assertEqual([t["n"] for t in got["turns"]], [1, 2, 3])
        got = memory.turns(99, 5000)
        self.assertEqual([t["n"] for t in got["turns"]], [99, 100, 101])
        self.assertEqual([t["n"] for t in memory.turns(5000, 9000)["turns"]], [101])

    def test_a_range_given_backwards_is_read_forwards(self):
        self.assertEqual([t["n"] for t in memory.turns(5, 3)["turns"]], [3, 4, 5])

    def test_the_tool_route_reaches_it_and_logs_no_text(self):
        with self.assertLogs("cc", level="INFO") as caught:
            got = voice.call("turns", {"from": 1, "to": 2})
        self.assertEqual(len(got["turns"]), 2)
        self.assertIn("turns", [t["name"] for t in voice.TOOLS])
        self.assertTrue(any("voice turns: 1 to 2, 2 turns" in x for x in caught.output))
        self.assertFalse(any("turn 0" in x for x in caught.output))


Qualifying._original = staticmethod(memory.post_jev)
Qualifying._embed = staticmethod(memory.post_embed)


class Scrubbing(MemoryCase):
    def break_urlopen(self, key):
        import urllib.error
        import urllib.request
        real = urllib.request.urlopen

        def boom(req, timeout=None):
            raise urllib.error.URLError(f"bad header Bearer {key}")

        urllib.request.urlopen = boom
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", real))

    def test_the_typesafe_key_is_scrubbed_from_a_transport_error(self):
        config.TYPESAFE_KEY = KEY
        self.break_urlopen(KEY)
        with self.assertRaises(RuntimeError) as caught:
            Qualifying._original({"state": "x"}, KEY)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertIn("the key", str(caught.exception))

    def test_the_openai_key_is_scrubbed_from_an_embedding_error(self):
        self.break_urlopen(OPENAI)
        with self.assertRaises(RuntimeError) as caught:
            Qualifying._embed({"model": "m", "input": ["x"]}, OPENAI)
        self.assertNotIn(OPENAI, str(caught.exception))
        self.assertIn("the key", str(caught.exception))

    def test_the_real_transport_puts_the_key_in_the_bearer_header(self):
        seen = {}

        class Answer:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"answers": {}}'

        import urllib.request
        real = urllib.request.urlopen

        def fake_open(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.get_header("Authorization")
            seen["body"] = req.data.decode()
            return Answer()

        urllib.request.urlopen = fake_open
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", real))
        Qualifying._original({"state": "x", "model": "jev-latest", "questions": {}}, KEY)
        self.assertEqual(seen["url"], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(seen["auth"], f"Bearer {KEY}")
        self.assertNotIn(KEY, seen["body"])

    def test_a_key_that_is_not_a_header_value_is_named_without_repeating_it(self):
        with self.assertRaises(RuntimeError) as caught:
            Qualifying._original({"state": "x"}, KEY + "\r\n")
        self.assertNotIn(KEY, str(caught.exception))

    def test_the_key_is_stripped_when_it_is_read(self):
        import importlib
        import os
        before = os.environ.get("TYPESAFE_API_KEY")
        os.environ["TYPESAFE_API_KEY"] = KEY + "\r\n"
        try:
            self.assertEqual(importlib.reload(config).TYPESAFE_KEY, KEY)
        finally:
            if before is None:
                os.environ.pop("TYPESAFE_API_KEY", None)
            else:
                os.environ["TYPESAFE_API_KEY"] = before
            importlib.reload(config)
            config.use_var(self.dir / "var", views=self.dir / "views")


# -- the route -------------------------------------------------------------------

class Route(MemoryCase):
    def setUp(self):
        super().setUp()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def ask(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json"})
        try:
            got = conn.getresponse()
            return got.status, json.loads(got.read() or b"{}")
        finally:
            conn.close()

    def test_the_settings_are_read_and_saved(self):
        code, got = self.ask("GET", "/api/voice/settings")
        self.assertEqual(code, 200)
        self.assertEqual((got["memory_turns"], got["recall_span"]), (30, 2))
        self.assertEqual(got["limits"]["memory_turns"], {"min": 1, "max": 100, "default": 30})
        self.assertEqual(got["limits"]["recall_span"], {"min": 0, "max": 10, "default": 2})
        code, got = self.ask("POST", "/api/voice/settings", {"memory_turns": 45})
        self.assertEqual((code, got["memory_turns"], got["recall_span"]), (200, 45, 2))
        code, got = self.ask("POST", "/api/voice/settings", {"recall_span": 5})
        self.assertEqual((code, got["memory_turns"], got["recall_span"]), (200, 45, 5))

    def test_the_silence_setting_is_read_and_saved_through_the_route(self):
        code, got = self.ask("GET", "/api/voice/settings")
        self.assertEqual((code, got["silence_minutes"]), (200, 8))
        self.assertEqual(got["limits"]["silence_minutes"], {"min": 1, "max": 30, "default": 8})
        code, got = self.ask("POST", "/api/voice/settings", {"silence_minutes": 45})
        self.assertEqual((code, got["silence_minutes"], got["memory_turns"]), (200, 30, 30))
        code, got = self.ask("POST", "/api/voice/settings", {"silence_minutes": "soon"})
        self.assertEqual(code, 400)
        self.assertIn("1 to 30", got["error"])

    def test_a_number_out_of_range_is_pulled_in_and_words_are_refused(self):
        self.assertEqual(self.ask("POST", "/api/voice/settings",
                                  {"memory_turns": 1000})[1]["memory_turns"], 100)
        self.assertEqual(self.ask("POST", "/api/voice/settings",
                                  {"recall_span": 11})[1]["recall_span"], 10)
        code, got = self.ask("POST", "/api/voice/settings", {"memory_turns": "many"})
        self.assertEqual(code, 400)
        self.assertIn("1 to 100", got["error"])
