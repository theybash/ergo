"""The voice front desk: the briefing, the tools, and the one rule that keeps it safe.

The rule is propose then confirm. The voice can say anything it likes and can be wrong about what
it heard; nothing reaches an agent until an id it was handed a moment ago comes back. So most of
this file is about that id: that it works once, that it expires, and that an id nobody issued does
nothing at all.

OpenAI is never called. `voice.post_json` is one module function for exactly this reason, so a
test replaces it and reads the payload that would have gone out.
"""

import http.client
import json
import shutil
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from commandcenter import broker, clock, config, cron, memory, registry, server, voice
from tests.harness import Case


class VoiceCase(Case):
    """A registered fleet, no key unless the test sets one, and nothing that can call out."""

    def setUp(self):
        super().setUp()
        config.OPENAI_KEY = "sk-not-a-real-key"
        config.VOICE_NAME = "vesper"
        config.VOICE_MODEL = "gpt-live-1"
        self.sent = []
        self.opened = {"session": {"id": "live_fake"},
                       "transport": {"type": "webrtc", "sdp": "v=0 answer"}}
        real = voice.post_json

        def fake(url, payload, key, timeout=None):
            self.sent.append({"url": url, "payload": payload, "key": key})
            return self.opened

        self.real_post = real
        voice.post_json = fake
        self.addCleanup(lambda: setattr(voice, "post_json", real))

    def open_call(self):
        """Open one call with a stand in for the browser's connection offer."""
        return voice.open_call("v=0 offer")

    def payload(self):
        """The session object of the one request that went out."""
        return self.sent[-1]["payload"]["session"]

    def backend(self):
        """The backend Responses model's half of that session."""
        return self.payload()["delegation"]["responses"]

    def awake(self, *agent_ids):
        """Say these agents are up, without starting anything."""
        up = set(agent_ids)
        real = broker.awake
        broker.awake = lambda agent_id: agent_id in up
        self.addCleanup(lambda: setattr(broker, "awake", real))

    def catch_sends(self):
        """Record every broker.send, and send nothing."""
        got = []
        real = broker.send
        broker.send = lambda agent_id, text, wake=False: got.append(
            {"agent": agent_id, "text": text, "wake": wake}) or "sent"
        self.addCleanup(lambda: setattr(broker, "send", real))
        return got


# -- minting the session ---------------------------------------------------------

class Session(VoiceCase):
    def test_a_call_cannot_start_without_a_key(self):
        config.OPENAI_KEY = ""
        with self.assertRaises(PermissionError) as caught:
            voice.start_session()
        self.assertIn("OPENAI_API_KEY", str(caught.exception))
        self.assertEqual(self.sent, [], "nothing should have been sent to OpenAI")

    def test_the_door_is_shut_on_the_offer_as_well_as_the_session(self):
        """The browser asks twice, so a budget that ran out between the two must still stop it."""
        config.OPENAI_KEY = ""
        with self.assertRaises(PermissionError):
            self.open_call()
        self.assertEqual(self.sent, [])

    def test_a_call_is_refused_once_the_day_is_spent(self):
        config.VOICE_DAILY_MINUTES = 10
        voice.spend(10 * 60)
        with self.assertRaises(RuntimeError) as caught:
            voice.start_session()
        self.assertIn("used its", str(caught.exception))
        self.assertEqual(self.sent, [])

    def test_opening_the_door_calls_openai_not_at_all(self):
        """It answers before the browser asks for the microphone, so it must cost nothing."""
        got = voice.start_session()
        self.assertEqual(self.sent, [])
        self.assertEqual(got["voice"], "vesper")
        self.assertEqual(got["session_minutes"], config.VOICE_SESSION_MINUTES)

    def test_the_payload_says_store_false(self):
        self.open_call()
        self.assertIs(self.payload()["store"], False)

    def test_the_payload_carries_the_voice_and_both_models(self):
        self.open_call()
        self.assertEqual(self.payload()["model"], "gpt-live-1")
        self.assertEqual(self.payload()["audio"]["output"]["voice"], "vesper")
        self.assertEqual(self.backend()["model"], config.VOICE_BACKEND)

    def test_the_payload_names_no_audio_format_because_webrtc_settles_that(self):
        self.open_call()
        self.assertNotIn("format", self.payload()["audio"])

    def test_the_payload_carries_every_tool_by_name(self):
        self.open_call()
        tools = self.backend()["tools"]
        named = [t["name"] for t in tools if "name" in t]
        self.assertIn("web_search", [t.get("type") for t in tools])
        for wanted in ("fleet_status", "agent_history", "agent_recap", "pending_asks",
                       "answer_ask", "insights", "agent_pages", "page_data", "calls",
                       "propose", "confirm", "cancel"):
            self.assertIn(wanted, named)
        # The hosted search tool is a type with no name of its own, which is what broke this
        # test. The docs are plain: delegation.responses "supports function definitions and
        # web_search entries in tools". So every other entry is a function, and that one is not.
        self.assertTrue(all(t["type"] == "function" for t in tools if "name" in t))

    def test_the_door_hands_the_browser_the_wait_pause_and_turn_gap_settings(self):
        config.VOICE_WAIT_PAUSE_SECONDS = 45.0
        config.VOICE_TURN_GAP_MS = 3000
        got = voice.start_session()
        self.assertEqual(got["wait_pause_seconds"], 45.0)
        self.assertEqual(got["turn_gap_ms"], 3000)

    def test_the_door_says_nothing_about_the_old_idle_number(self):
        # The old idle hang up counted seconds and is gone. The silence hang up is in minutes,
        # under its own name, and never ends a call that is waiting on an agent.
        self.assertNotIn("idle_seconds", voice.start_session())
        self.assertNotIn("idle_seconds", self.open_call())

    def test_the_door_and_the_call_hand_the_browser_the_silence_minutes(self):
        self.assertEqual(voice.start_session()["silence_minutes"], 8)
        self.assertEqual(self.open_call()["silence_minutes"], 8)

    def test_a_saved_silence_setting_is_what_the_browser_is_handed(self):
        memory.save_settings({"silence_minutes": 3})
        self.assertEqual(voice.start_session()["silence_minutes"], 3)
        self.assertEqual(self.open_call()["silence_minutes"], 3)

    def test_the_call_hands_them_back_as_well_so_a_resume_reads_them_again(self):
        config.VOICE_WAIT_PAUSE_SECONDS = 0.0
        config.VOICE_TURN_GAP_MS = 2500
        got = self.open_call()
        self.assertEqual(got["wait_pause_seconds"], 0.0)
        self.assertEqual(got["turn_gap_ms"], 2500)

    def test_an_ordinary_call_seeds_no_conversation_and_still_greets(self):
        self.open_call()
        self.assertNotIn("input", self.payload())
        self.assertNotIn(voice.NO_GREETING, self.payload()["instructions"])

    def test_a_resumed_call_carries_this_calls_turns_as_conversation_items(self):
        voice.open_call("v=0 offer", resume=[{"who": "you", "text": "what is alpha doing"},
                                             {"who": "voice", "text": "reading its own log"}])
        carried = self.payload()["input"]
        self.assertEqual([m["role"] for m in carried], ["user", "assistant"])
        self.assertEqual(carried[0]["content"][0]["type"], "input_text")
        self.assertEqual(carried[1]["content"][0]["type"], "output_text")
        self.assertIn("what is alpha doing", carried[0]["content"][0]["text"])

    def test_a_resumed_call_tells_both_models_not_to_greet_him_again(self):
        voice.open_call("v=0 offer", resume=[{"who": "you", "text": "carry on"}])
        self.assertIn(voice.NO_GREETING, self.payload()["instructions"])
        self.assertIn(voice.NO_GREETING, self.backend()["instructions"])

    def test_a_resume_is_not_counted_as_a_second_call(self):
        self.open_call()
        voice.open_call("v=0 offer", resume=[{"who": "you", "text": "carry on"}])
        self.assertEqual(voice.usage()["calls"], 1)

    def test_the_front_gets_the_short_prompt_and_the_backend_gets_the_briefing(self):
        self.agent("alpha")
        self.open_call()
        self.assertIn("alpha", self.payload()["instructions"])
        self.assertIn("propose", self.backend()["instructions"])

    def test_the_browsers_offer_is_what_goes_out_and_the_answer_comes_back(self):
        got = self.open_call()
        self.assertEqual(self.sent[-1]["payload"]["transport"],
                         {"type": "webrtc", "sdp": "v=0 offer\r\n"})
        self.assertEqual(got["sdp"], "v=0 answer")
        self.assertEqual(got["session_id"], "live_fake")

    def test_the_offer_keeps_its_last_line_ending(self):
        """An SDP whose last line has no terminator is refused as "failed to unmarshal SDP: EOF",
        which reads like a truncated body and is one missing newline."""
        voice.open_call("v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111")
        self.assertTrue(self.sent[-1]["payload"]["transport"]["sdp"].endswith("\r\n"))

    def test_a_call_with_no_offer_in_it_is_refused_before_openai(self):
        with self.assertRaises(ValueError):
            voice.open_call("  ")
        self.assertEqual(self.sent, [])

    def test_the_answer_holds_the_budget_and_never_the_key(self):
        got = self.open_call()
        self.assertEqual(got["budget"]["daily_minutes"], config.VOICE_DAILY_MINUTES)
        self.assertNotIn(config.OPENAI_KEY, json.dumps(got))

    def test_a_session_with_no_answer_in_it_is_a_failure_not_a_call(self):
        self.opened = {"session": {"id": "live_fake"}}
        with self.assertRaises(RuntimeError):
            self.open_call()

    def test_a_key_with_a_line_ending_in_it_is_refused_without_repeating_it(self):
        """This one happened. A key written with a Windows line ending cannot be a header value,
        urllib says so in a sentence containing the whole header, and that sentence reached a
        browser and put the key on the screen."""
        voice.post_json = self.real_post
        config.OPENAI_KEY = "sk-secret-value\r"
        with self.assertRaises(RuntimeError) as caught:
            self.open_call()
        self.assertNotIn("sk-secret-value", str(caught.exception))
        self.assertIn("var/secrets.env", str(caught.exception))

    def test_no_error_from_openai_can_carry_the_key_out(self):
        self.assertEqual(voice._safe("Bearer sk-secret in the text", "sk-secret"),
                         "Bearer the key in the text")

    def test_the_key_is_stripped_when_it_is_read(self):
        """A trailing newline in var/secrets.env is the ordinary way a key arrives unusable."""
        import os
        import importlib
        os.environ["OPENAI_API_KEY"] = " sk-with-space\n"
        try:
            self.assertEqual(importlib.reload(config).OPENAI_KEY, "sk-with-space")
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
            importlib.reload(config)
            config.use_var(self.dir / "var", views=self.dir / "views")


# -- the briefing ----------------------------------------------------------------

class Briefing(VoiceCase):
    def test_the_persona_file_is_the_top_of_it(self):
        said = voice.briefing()
        self.assertIn("front desk", said)
        self.assertIn("British", said)

    def test_there_is_one_line_per_agent_saying_awake_or_asleep(self):
        self.agent("alpha")
        self.agent("beta")
        self.awake("alpha")
        said = voice.briefing()
        self.assertIn("alpha, awake", said)
        self.assertIn("beta, asleep", said)

    def test_an_awake_agent_with_a_recap_has_it_read_in(self):
        rec = self.agent("alpha")
        self.transcript_for(rec, [
            self.said("hello"),
            {"type": "system", "subtype": "away_summary", "timestamp": "2026-08-20T10:00:00Z",
             "content": "Read the filings and wrote the summary."},
        ])
        self.awake("alpha")
        self.assertIn("Read the filings", voice.briefing())

    def test_a_sleeping_agent_recap_is_left_out(self):
        rec = self.agent("alpha")
        self.transcript_for(rec, [
            {"type": "system", "subtype": "away_summary", "timestamp": "2026-08-20T10:00:00Z",
             "content": "Read the filings and wrote the summary."}])
        self.awake()
        self.assertNotIn("Read the filings", voice.briefing())

    def test_a_recap_is_cut_to_sixty_words(self):
        rec = self.agent("alpha")
        self.transcript_for(rec, [
            {"type": "system", "subtype": "away_summary", "timestamp": "2026-08-20T10:00:00Z",
             "content": " ".join(["word"] * 200)}])
        self.awake("alpha")
        said = voice.briefing()
        self.assertIn("...", said)
        self.assertLess(said.count("word"), 70)

    def test_the_previous_call_is_in_the_briefing(self):
        voice.say_down("you", "what is job search up to")
        voice.say_down("voice", "It is reading the listings.")
        said = voice.briefing()
        self.assertIn("what is job search up to", said)
        self.assertIn("It is reading the listings.", said)

    def test_the_time_is_in_it_because_a_front_desk_knows_what_day_it_is(self):
        self.assertIn("The time is", voice.briefing())


# -- resuming a paused call ------------------------------------------------------
# A pause closes the session at OpenAI, so going again is a new session with a new briefing. The
# whole job of these is that the second half reads as the same conversation as the first.

class Resuming(VoiceCase):
    TURNS = [{"who": "you", "text": "what is alpha doing"},
             {"who": "voice", "text": "It is reading its own log."},
             {"who": "you", "text": "leave it then"}]

    def test_the_resume_briefing_holds_this_calls_own_turns(self):
        said = voice.resume_brief(self.TURNS)
        self.assertIn("## This call so far", said)
        self.assertIn("what is alpha doing", said)
        self.assertIn("It is reading its own log.", said)
        self.assertIn("leave it then", said)

    def test_the_resume_briefing_names_who_said_which(self):
        said = voice.resume_brief(self.TURNS)
        self.assertIn("The owner: what is alpha doing", said)
        self.assertIn("You: It is reading its own log.", said)

    def test_the_resume_briefing_is_still_the_whole_briefing(self):
        self.agent("alpha")
        said = voice.resume_brief(self.TURNS)
        self.assertIn("alpha", said)
        self.assertIn("The time is", said)

    def test_the_resume_briefing_tells_him_not_to_greet_him_again(self):
        said = voice.resume_brief(self.TURNS)
        self.assertIn(voice.NO_GREETING, said)
        for wanted in ("Do not greet them", "Do not say welcome back"):
            self.assertIn(wanted, said)

    def test_the_ordinary_briefing_says_nothing_about_resuming(self):
        self.assertNotIn(voice.NO_GREETING, voice.briefing())
        self.assertNotIn("## This call so far", voice.briefing())

    def test_the_front_of_house_prompt_is_where_the_greeting_is_taken_back(self):
        # The persona tells them to open with "Welcome back", and that instruction is in the
        # live prompt rather than the briefing, because the live model is the one that talks.
        self.assertNotIn(voice.NO_GREETING, voice.live_prompt())
        self.assertIn(voice.NO_GREETING, voice.live_prompt(resuming=True))

    def test_a_punctuation_only_turn_is_not_carried_over(self):
        said = voice.resume_brief([{"who": "you", "text": "."}, {"who": "you", "text": "right"}])
        self.assertNotIn("The owner: .", said)
        self.assertIn("right", said)
        self.assertEqual(len(voice.resume_input([{"who": "you", "text": ","}])), 0)

    def test_only_the_last_few_turns_are_carried(self):
        many = [{"who": "you", "text": f"turn {i}"} for i in range(40)]
        self.assertEqual(len(voice.resume_input(many)), voice.RESUME_TURNS)
        self.assertIn("turn 39", voice.resume_input(many)[-1]["content"][0]["text"])

    def test_this_calls_turns_are_not_read_out_twice(self):
        # The browser posts every turn to the log as it happens, so a resume would otherwise find
        # this call's own turns in both the previous calls section and this one.
        voice.say_down("you", "what is alpha doing")
        said = voice.resume_brief([{"who": "you", "text": "what is alpha doing"}])
        self.assertEqual(said.count("The owner: what is alpha doing"), 1)

    def test_nothing_said_yet_leaves_the_section_out_and_seeds_nothing(self):
        self.assertNotIn("## This call so far", voice.resume_brief([]))
        self.assertEqual(voice.resume_input([]), [])

# -- what came back while the call was paused ------------------------------------
#
# A call that waits on an agent pauses itself, so the agent finishing is what opens it again. The
# browser sends the agent and which of the two things happened; every word is written here.

class CameBack(VoiceCase):
    def test_an_agent_that_finished_is_one_sentence_naming_it(self):
        said = voice.came_back({"agent": "alpha", "kind": "settled"})
        self.assertIn("alpha", said)
        self.assertIn("finished", said)

    def test_an_agent_that_stopped_to_ask_says_what_it_is_asking(self):
        said = voice.came_back({"agent": "alpha", "kind": "ask",
                                "asking": "permission to run Bash"})
        self.assertIn("alpha", said)
        self.assertIn("waiting", said)
        self.assertIn("permission to run Bash", said)

    def test_an_ask_with_nothing_said_still_says_he_is_wanted(self):
        said = voice.came_back({"agent": "alpha", "kind": "ask"})
        self.assertIn("alpha", said)
        self.assertIn("waiting", said)

    def test_a_kind_nobody_sent_reads_as_the_agent_having_finished(self):
        self.assertIn("finished", voice.came_back({"agent": "alpha", "kind": "banana"}))

    def test_anything_that_is_not_an_agent_says_nothing(self):
        for rubbish in (None, "alpha", 7, {}, {"kind": "settled"}, {"agent": ""}):
            self.assertEqual(voice.came_back(rubbish), "")

    def test_a_call_woken_by_an_agent_tells_the_front_of_house_what_arrived(self):
        voice.open_call("v=0 offer", resume=[{"who": "you", "text": "leave it then"}],
                        back={"agent": "alpha", "kind": "settled"})
        said = self.payload()["instructions"]
        self.assertIn(voice.WAIT_REPORT, said)
        self.assertIn("alpha has finished the turn you gave it.", said)

    def test_the_backend_is_told_the_same_thing(self):
        voice.open_call("v=0 offer", resume=[{"who": "you", "text": "leave it then"}],
                        back={"agent": "alpha", "kind": "ask", "asking": "a question of its own"})
        said = self.backend()["instructions"]
        self.assertIn(voice.WAIT_REPORT, said)
        self.assertIn("a question of its own", said)

    def test_she_is_told_to_report_it_without_being_asked(self):
        said = voice.resume_brief([{"who": "you", "text": "leave it then"}],
                                  {"agent": "alpha", "kind": "settled"})
        self.assertIn("without being asked", said)
        self.assertIn("Do not greet them first.", said)

    def test_a_resume_that_no_agent_woke_says_nothing_about_waiting(self):
        voice.open_call("v=0 offer", resume=[{"who": "you", "text": "carry on"}])
        self.assertNotIn(voice.WAIT_REPORT, self.payload()["instructions"])
        self.assertNotIn(voice.WAIT_REPORT, self.backend()["instructions"])

    def test_an_ordinary_call_says_nothing_about_waiting_either(self):
        self.open_call()
        self.assertNotIn(voice.WAIT_REPORT, self.payload()["instructions"])
        self.assertNotIn(voice.WAIT_REPORT, self.backend()["instructions"])

    # The night of 23 September: the call woke on a settle and they said nothing, because the
    # prompt told them both to report at once and to wait in silence.
    def test_a_resume_with_news_does_not_tell_him_to_wait_in_silence(self):
        back = {"agent": "alpha", "kind": "settled"}
        for said in (voice.live_prompt(resuming=True, back=back),
                     voice.resume_brief([{"who": "you", "text": "go on"}], back)):
            self.assertNotIn(voice.WAIT_SILENT, said)
            self.assertIn("Do not greet them.", said)
            self.assertIn("without being asked", said)

    def test_an_ordinary_resume_still_waits_in_silence(self):
        for said in (voice.live_prompt(resuming=True),
                     voice.resume_brief([{"who": "you", "text": "go on"}])):
            self.assertIn(voice.NO_GREETING, said)
            self.assertIn(voice.WAIT_SILENT, said)

    def test_a_list_is_one_short_sentence_per_agent(self):
        said = voice.came_back([{"agent": "alpha", "kind": "settled"},
                                {"agent": "beta", "kind": "ask", "asking": "a question"}])
        self.assertEqual(said, "alpha has finished the turn you gave it. "
                               "beta has stopped and is waiting on them: a question")

    def test_one_agent_twice_is_said_once_with_the_newest_fact(self):
        said = voice.came_back([{"agent": "alpha", "kind": "settled"}, "rubbish",
                                {"agent": "alpha", "kind": "ask"}])
        self.assertEqual(said.count("alpha"), 1)
        self.assertIn("waiting", said)
        self.assertEqual(voice.came_back([]), "")
        self.assertEqual(voice.came_back([None, {}]), "")

    def test_a_list_reaches_both_prompts(self):
        voice.open_call("v=0 offer", resume=[{"who": "you", "text": "leave it then"}],
                        back=[{"agent": "alpha", "kind": "settled"},
                              {"agent": "beta", "kind": "settled"}])
        for said in (self.payload()["instructions"], self.backend()["instructions"]):
            self.assertIn("alpha has finished", said)
            self.assertIn("beta has finished", said)

    def test_the_offer_answer_carries_the_report(self):
        got = voice.open_call("v=0 offer", resume=[{"who": "you", "text": "leave it then"}],
                              back=[{"agent": "alpha", "kind": "settled"}])
        self.assertEqual(got["report"], "alpha has finished the turn you gave it.")

    def test_an_offer_with_nothing_back_carries_an_empty_report(self):
        self.assertEqual(self.open_call()["report"], "")
        got = voice.open_call("v=0 offer", resume=[{"who": "you", "text": "carry on"}])
        self.assertEqual(got["report"], "")

    def test_news_with_no_turns_yet_is_still_a_resume(self):
        voice.open_call("v=0 offer", back={"agent": "alpha", "kind": "settled"})
        self.assertIn(voice.NO_GREETING_BASE, self.payload()["instructions"])
        self.assertIn("alpha has finished", self.backend()["instructions"])


# -- what each delegated agent is doing, read on every resume ----------------------
# Asked "did they not come back yet", they said "Not yet" from memory. Now every resume carries each
# agent given work on this call, with its state as the daemon reads it at that moment.

class DelegatedState(VoiceCase):
    def with_reply(self, name="alpha"):
        rec = self.agent(name)
        self.transcript_for(rec, [self.said("the task", who="user"),
                                  self.said("done, here it is")])
        return rec

    def test_an_agent_that_answered_after_the_task_is_finished(self):
        self.with_reply()
        # The task went out a day before the reply in the fake transcript.
        lines = voice.delegated_lines([{"agent": "alpha", "at": clock.now()}])
        self.assertEqual(len(lines), 1)
        self.assertIn("alpha: finished since the task went out", lines[0])

    def test_an_agent_whose_last_word_is_older_than_the_task_is_still_working(self):
        self.with_reply()
        later = voice._epoch("2026-08-21T10:00:00Z")
        lines = voice.delegated_lines([{"agent": "alpha", "at": later * 1000}])
        self.assertIn("alpha: still working", lines[0])

    def test_an_agent_with_no_transcript_is_still_working(self):
        # A name no other test uses: the transcript readers are kept per agent id.
        self.agent("quiet")
        lines = voice.delegated_lines([{"agent": "quiet", "at": clock.now()}])
        self.assertIn("quiet: still working", lines[0])

    def test_an_agent_stopped_on_an_ask_says_so(self):
        self.with_reply()
        self.awake("alpha")
        real = broker.pending_asks
        broker.pending_asks = lambda agent_id: [{"id": "a1", "kind": "permission"}]
        self.addCleanup(lambda: setattr(broker, "pending_asks", real))
        lines = voice.delegated_lines([{"agent": "alpha", "at": clock.now()}])
        self.assertIn("alpha: stopped, waiting on an answer from them.", lines[0])

    def test_unknown_agents_and_repeats_are_left_out(self):
        self.with_reply()
        lines = voice.delegated_lines([{"agent": "nobody"}, {"agent": "alpha"},
                                       {"agent": "alpha"}, "rubbish"])
        self.assertEqual(len(lines), 1)
        self.assertEqual(voice.delegated_lines(None), [])

    def test_both_prompts_carry_the_section_on_a_resume(self):
        self.with_reply()
        voice.open_call("v=0 offer", resume=[{"who": "you", "text": "did they not come back"}],
                        sent=[{"agent": "alpha", "at": clock.now()}])
        for said in (self.payload()["instructions"], self.backend()["instructions"]):
            self.assertIn("## Delegated on this call, as it stands now", said)
            self.assertIn("alpha: finished since the task went out", said)
            self.assertIn("check with a tool", said)

    def test_a_first_call_carries_no_section(self):
        self.open_call()
        self.assertNotIn("Delegated on this call", self.payload()["instructions"])


class PersonaLines(VoiceCase):
    def test_the_persona_says_to_check_before_saying_an_agent_has_not_replied(self):
        said = voice.PERSONA.read_text()
        self.assertIn("Before you say an agent has not replied, check with a tool.", said)
        self.assertIn("Never answer that from memory.", said)


# -- the call log ----------------------------------------------------------------

class CallLog(VoiceCase):
    def test_a_turn_is_appended_and_read_back(self):
        voice.say_down("you", "morning")
        voice.say_down("voice", "Morning, the owner.")
        got = voice.calls(10)
        self.assertEqual([r["who"] for r in got], ["you", "voice"])
        self.assertEqual(got[-1]["text"], "Morning, the owner.")

    def test_only_the_last_n_come_back(self):
        for i in range(20):
            voice.say_down("you", f"turn {i}")
        got = voice.calls(5)
        self.assertEqual(len(got), 5)
        self.assertEqual(got[-1]["text"], "turn 19")

    def test_an_empty_turn_is_not_written_down(self):
        self.assertIsNone(voice.say_down("you", "   "))
        self.assertEqual(voice.calls(5), [])

    def test_a_turn_of_only_punctuation_is_never_logged(self):
        # The log used to hold rows that were one full stop, because a fragment arrived carrying
        # the punctuation of the sentence before it and nothing else. Those are not turns.
        for noise in (".", ",", "...", " ? ", "-", "!?"):
            self.assertIsNone(voice.say_down("you", noise))
        self.assertEqual(voice.calls(20), [])

    def test_a_note_is_written_under_its_own_name(self):
        voice.say_down("note", "Sent to boxer.")
        row = voice.calls(1)[0]
        self.assertEqual(row["who"], "note")
        self.assertEqual(row["text"], "Sent to boxer.")

    def test_a_who_nobody_recognises_is_filed_as_the_voice(self):
        voice.say_down("stranger", "something")
        self.assertEqual(voice.calls(1)[0]["who"], "voice")

    def test_the_briefing_reads_a_note_as_a_note_and_not_as_something_she_said(self):
        voice.say_down("note", "Sent to boxer.")
        said = voice.briefing()
        self.assertIn("Note: Sent to boxer.", said)
        self.assertNotIn("You: Sent to boxer.", said)

    def test_a_note_seeds_no_conversation_item_on_a_resume(self):
        # It belongs in the briefing as a fact. In the conversation it would be one of them
        # saying it out loud, and neither of them did.
        carried = voice.resume_input([{"who": "note", "text": "Sent to boxer."},
                                      {"who": "you", "text": "carry on"}])
        self.assertEqual(len(carried), 1)
        self.assertEqual(carried[0]["role"], "user")

    def test_a_resumed_call_still_reads_its_own_notes_back(self):
        said = voice.resume_brief([{"who": "note", "text": "Sent to boxer."}])
        self.assertIn("Note: Sent to boxer.", said)

    def test_a_turn_with_one_word_in_it_is_kept(self):
        self.assertIsNotNone(voice.say_down("you", "no."))
        self.assertIsNotNone(voice.say_down("voice", "4."))
        self.assertEqual(len(voice.calls(20)), 2)


# -- the budget ------------------------------------------------------------------

class Budget(VoiceCase):
    def test_minutes_add_up_and_the_rest_is_what_is_left(self):
        config.VOICE_DAILY_MINUTES = 60
        voice.end_session("sess", 600)
        self.assertEqual(voice.budget()["used_minutes"], 10.0)
        self.assertEqual(voice.budget()["left_minutes"], 50.0)

    def test_yesterdays_minutes_are_not_charged_to_today(self):
        voice.spend(600)
        clock.advance(60 * 60 * 26)
        self.assertEqual(voice.usage()["seconds"], 0.0)

    def test_ending_the_same_session_twice_does_not_charge_it_twice(self):
        # A pause ends a session and the overlay ends it again when it unmounts. The seconds are
        # charged to the session, not to the post, so the second one adds nothing.
        voice.end_session("live_one", 600)
        voice.end_session("live_one", 600)
        self.assertEqual(voice.usage()["seconds"], 600.0)

    def test_a_later_larger_number_for_the_same_session_tops_it_up(self):
        voice.end_session("live_one", 600)
        voice.end_session("live_one", 900)
        self.assertEqual(voice.usage()["seconds"], 900.0)

    def test_two_sessions_on_one_call_are_both_charged(self):
        # Pause and resume: two sessions, two ids, and the call cost the sum of them.
        voice.end_session("live_one", 300)
        voice.end_session("live_two", 200)
        self.assertEqual(voice.usage()["seconds"], 500.0)

    def test_a_session_with_no_id_is_charged_as_it_comes(self):
        voice.end_session(None, 60)
        voice.end_session("", 60)
        self.assertEqual(voice.usage()["seconds"], 120.0)

    def test_how_the_call_ended_is_in_the_log_line(self):
        with self.assertLogs("cc.voice", level="INFO") as caught:
            voice.end_session("live_one", 60, "back")
        self.assertIn("voice session live_one ended by back after 60 seconds", caught.output[-1])

    def test_a_why_nobody_knows_is_left_out_of_the_log(self):
        with self.assertLogs("cc.voice", level="INFO") as caught:
            voice.end_session("live_one", 60, "rm -rf\nforged line")
        self.assertIn("voice session live_one ended after 60 seconds", caught.output[-1])
        self.assertNotIn("forged", caught.output[-1])

    def test_a_second_end_for_one_session_is_ignored_and_says_so(self):
        # Hang up posts, then the tab closes and the beacon posts again.
        voice.end_session("live_one", 60, "hangup")
        with self.assertLogs("cc.voice", level="INFO") as caught:
            left = voice.end_session("live_one", 60, "pagehide")
        self.assertEqual(voice.usage()["seconds"], 60.0)
        self.assertEqual(left["used_minutes"], 1.0)
        self.assertIn("voice session live_one ended again by pagehide, ignored", caught.output[-1])

    def test_seconds_that_are_not_a_number_charge_nothing(self):
        voice.end_session("live_one", "lots")
        self.assertEqual(voice.usage()["seconds"], 0.0)


# -- reading a file ----------------------------------------------------------------
# The fence, twice over. A path has to resolve inside a root the owner named, and no part of it
# may be one of the names that hold a credential. Both checks run on the resolved path, so a
# symlink cannot walk out of a root and cannot launder a name either.

class ReadFile(VoiceCase):
    def setUp(self):
        super().setUp()
        # The test's own folder is the only root, resolved, because a temporary directory can
        # sit behind a symlink and then nothing inside it would be under its own root.
        self.root = Path(self.dir).resolve()
        config.VOICE_READ_ROOTS = (str(self.root),)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_a_file_inside_a_root_is_read(self):
        self.write("notes.md", "one\ntwo\nthree\n")
        got = voice.read_file(str(self.root / "notes.md"))
        self.assertEqual(got["start"], 1)
        self.assertEqual(got["end"], 3)
        self.assertEqual(got["total_lines"], 3)
        self.assertEqual(got["text"], "one\ntwo\nthree\n")

    def test_a_file_outside_every_root_is_refused_with_no_content(self):
        outside = Path(tempfile.mkdtemp(prefix="cc-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "secret.txt").write_text("the interesting part")
        got = voice.read_file(str(outside / "secret.txt"))
        self.assertEqual(got, voice.NOT_ALLOWED)
        self.assertNotIn("interesting", got)

    def test_a_relative_path_cannot_climb_out_of_a_root(self):
        got = voice.read_file(str(self.root / ".." / ".." / "etc" / "hosts"))
        self.assertEqual(got, voice.NOT_ALLOWED)

    def test_every_denied_name_is_refused_inside_the_root(self):
        for name in ("secrets.env", ".env", ".creds/token", ".ssh/id_rsa", ".gnupg/key"):
            self.write(name, "a credential")
            got = voice.read_file(str(self.root / name))
            self.assertEqual(got, voice.NOT_ALLOWED, name)

    def test_anything_under_var_secrets_is_refused_by_its_folder(self):
        self.write("var/secrets/openai", "a key")
        got = voice.read_file(str(self.root / "var" / "secrets" / "openai"))
        self.assertEqual(got, voice.NOT_ALLOWED)

    def test_a_symlink_out_of_the_root_is_refused(self):
        outside = Path(tempfile.mkdtemp(prefix="cc-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "elsewhere.txt").write_text("not yours")
        link = self.root / "innocent.txt"
        link.symlink_to(outside / "elsewhere.txt")
        got = voice.read_file(str(link))
        self.assertEqual(got, voice.NOT_ALLOWED)

    def test_a_symlink_cannot_launder_a_denied_name(self):
        self.write("secrets.env", "OPENAI_API_KEY=nope")
        link = self.root / "notes.txt"
        link.symlink_to(self.root / "secrets.env")
        got = voice.read_file(str(link))
        self.assertEqual(got, voice.NOT_ALLOWED)

    def test_a_file_with_a_nul_byte_in_it_is_not_read_out(self):
        (self.root / "picture.bin").write_bytes(b"PNG\x00\x01\x02 and more")
        got = voice.read_file(str(self.root / "picture.bin"))
        self.assertIn("not a text file", got)

    def test_a_file_that_is_not_there_says_so(self):
        got = voice.read_file(str(self.root / "nothing.md"))
        self.assertIn("no file", got)

    def test_a_page_starts_where_it_is_asked_to(self):
        self.write("long.md", "".join(f"line {n}\n" for n in range(1, 51)))
        got = voice.read_file(str(self.root / "long.md"), start=20, lines=5)
        self.assertEqual(got["start"], 20)
        self.assertEqual(got["end"], 24)
        self.assertEqual(got["total_lines"], 50)
        self.assertEqual(got["text"], "line 20\nline 21\nline 22\nline 23\nline 24\n")

    def test_a_page_says_how_many_lines_are_left_and_where_to_start_again(self):
        self.write("long.md", "".join(f"line {n}\n" for n in range(1, 51)))
        got = voice.read_file(str(self.root / "long.md"), start=1, lines=10)
        self.assertIn("40 more lines", got["note"])
        self.assertIn("line 11", got["note"])

    def test_the_last_page_of_a_file_offers_nothing_more(self):
        self.write("short.md", "one\ntwo\n")
        got = voice.read_file(str(self.root / "short.md"))
        self.assertIsNone(got.get("note"))

    def test_two_hundred_lines_is_the_most_one_call_reads(self):
        self.write("huge.md", "".join(f"line {n}\n" for n in range(1, 1001)))
        got = voice.read_file(str(self.root / "huge.md"), lines=900)
        self.assertEqual(got["end"], voice.READ_LINES)
        self.assertEqual(got["total_lines"], 1000)

    def test_the_text_is_capped_in_bytes_however_few_the_lines(self):
        # Ten lines of four thousand characters each. The line count allows all ten and the byte
        # cap does not, because a long line is still a long line.
        self.write("wide.md", "".join("x" * 4000 + "\n" for _ in range(10)))
        got = voice.read_file(str(self.root / "wide.md"), lines=10)
        self.assertLessEqual(len(got["text"]), voice.READ_BYTES)
        self.assertIn("cut at", got["note"])

    def test_the_tool_route_reaches_it_and_reports_what_it_read(self):
        self.write("notes.md", "one\ntwo\n")
        got = voice.call("read_file", {"path": str(self.root / "notes.md")})
        self.assertEqual(got["total_lines"], 2)

    def test_the_tool_is_offered_to_the_model_by_name(self):
        names = [tool["name"] for tool in voice.TOOLS]
        self.assertIn("read_file", names)
        said = [t for t in voice.TOOLS if t["name"] == "read_file"][0]["description"]
        self.assertIn("shape", said)


# -- what the tools did ------------------------------------------------------------
# One line per tool call in the daemon log. Twice in one night the voice said "sent" without
# calling confirm, and the log could not settle it either way, because nothing wrote a tool
# call down at all.

class ToolLog(VoiceCase):
    def setUp(self):
        super().setUp()
        self.rec = self.agent("boxer")

    def lines(self, name, args=None):
        """Run one tool call and hand back what it wrote to the log."""
        with self.assertLogs("cc.voice", level="INFO") as caught:
            result = voice.call(name, args)
        return result, caught.output

    def test_every_tool_call_leaves_one_line(self):
        _, said = self.lines("fleet_status")
        self.assertEqual(len(said), 1)
        self.assertIn("voice tool fleet_status", said[0])
        self.assertIn("ok", said[0])

    def test_the_line_names_the_agent_the_call_landed_on(self):
        _, said = self.lines("agent_recap", {"agent": "Boxer"})
        self.assertIn("-> boxer", said[0])

    def test_an_agent_nobody_has_is_written_down_as_failed(self):
        _, said = self.lines("agent_recap", {"agent": "nobody"})
        self.assertIn("failed", said[0])

    def test_a_tool_nobody_has_is_written_down_as_such(self):
        _, said = self.lines("wave_hands")
        self.assertIn("no such tool", said[0])

    def test_a_proposal_writes_the_task_down_cut_short(self):
        long_task = "read the filings " * 20
        _, said = self.lines("propose", {"agent": "boxer", "task": long_task})
        self.assertIn("voice tool propose", said[0])
        self.assertNotIn(long_task.strip(), said[0])

    def test_a_confirm_that_sends_says_where_it_went(self):
        got = self.catch_sends()
        made = voice.propose("boxer", "read the filings")
        with self.assertLogs("cc.voice", level="INFO") as caught:
            voice.confirm(made["id"])
        self.assertEqual(len(got), 1)
        self.assertTrue(any("sent to boxer" in line for line in caught.output))

    def test_a_confirm_that_sends_nothing_says_that_instead(self):
        real = broker.send
        broker.send = lambda agent_id, text, wake=False: None
        self.addCleanup(lambda: setattr(broker, "send", real))
        made = voice.propose("boxer", "read the filings")
        with self.assertLogs("cc.voice", level="INFO") as caught:
            voice.confirm(made["id"])
        self.assertTrue(any("nothing sent to boxer" in line for line in caught.output))

    def test_a_confirm_for_an_id_nobody_issued_is_written_down_too(self):
        with self.assertLogs("cc.voice", level="INFO") as caught:
            voice.confirm("deadbeef")
        self.assertTrue(any("no such proposal" in line for line in caught.output))

    def test_a_read_that_is_refused_is_written_down_as_failed(self):
        _, said = self.lines("read_file", {"path": "/etc/shadow"})
        self.assertIn("voice tool read_file", said[0])
        self.assertIn("failed", said[0])

    def test_nothing_read_out_of_a_file_reaches_the_log(self):
        root = Path(self.dir).resolve()
        config.VOICE_READ_ROOTS = (str(root),)
        (root / "notes.md").write_text("the interesting part\n")
        _, said = self.lines("read_file", {"path": str(root / "notes.md")})
        self.assertNotIn("interesting", said[0])
        self.assertIn("lines 1 to 1 of 1", said[0])


# -- every tool ------------------------------------------------------------------

class Tools(VoiceCase):
    def setUp(self):
        super().setUp()
        self.rec = self.agent("boxer")
        self.transcript_for(self.rec, [
            self.said("I looked at three of them.", who="user"),
            self.used_tool("Bash", command="ls -la"),
            self.said("Two are worth reading."),
            {"type": "system", "subtype": "away_summary", "timestamp": "2026-08-20T10:00:00Z",
             "content": "Read three filings, two worth keeping."},
        ])
        self.publish(self.rec, "positions.json", '{"open": 3}')

    def test_fleet_status_names_the_agent_and_says_what_it_said(self):
        said = voice.call("fleet_status")
        self.assertIn("boxer", said)
        self.assertIn("asleep", said)
        self.assertIn("Two are worth reading.", said)

    def test_agent_history_gives_the_final_answer_and_what_was_asked(self):
        got = voice.call("agent_history", {"agent": "boxer"})
        self.assertEqual(got["resolved_to"], "boxer")
        answers = got["history"]["answers"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["asked"], "I looked at three of them.")
        self.assertEqual(answers[0]["answer"], "Two are worth reading.")
        self.assertNotIn("still_running", got["history"])

    def test_text_before_a_tool_is_not_the_final_answer(self):
        rec = self.agent("talker")
        self.transcript_for(rec, [
            self.said("check the box", who="user"),
            self.said("Let me look first."),
            self.used_tool("Bash", command="uptime"),
            self.tool_said("up 3 days"),
            self.said("It has been up three days."),
        ])
        answers = voice.call("agent_history", {"agent": "talker"})["history"]["answers"]
        self.assertEqual([a["answer"] for a in answers], ["It has been up three days."])

    def test_the_last_n_answers_are_whole(self):
        rec = self.agent("long")
        long_answer = " ".join(["word"] * 400)
        rows = []
        for i in range(5):
            rows += [self.said(f"question {i}", who="user"), self.said(f"{long_answer} {i}")]
        self.transcript_for(rec, rows)
        answers = voice.call("agent_history", {"agent": "long", "n": 2})["history"]["answers"]
        self.assertEqual([a["asked"] for a in answers], ["question 3", "question 4"])
        self.assertTrue(answers[1]["answer"].endswith(" 4"))
        self.assertEqual(len(answers[1]["answer"].split()), 401)

    def test_a_running_agent_shows_the_turn_it_is_on(self):
        rec = self.agent("busy")
        self.transcript_for(rec, [
            self.said("first", who="user"), self.said("First answer."),
            self.said("measure the network", who="user"),
            self.said("Let me measure one thing first."),
            self.used_tool("Bash", command="ping -c 120 1.1.1.1"),
        ])
        with mock.patch.object(voice.live, "working", return_value=True):
            got = voice.call("agent_history", {"agent": "busy"})["history"]
        self.assertEqual([a["answer"] for a in got["answers"]], ["First answer."])
        now = got["still_running"]
        self.assertEqual(now["asked"], "measure the network")
        self.assertIn("ping", now["running_tool"])
        self.assertEqual(now["said_so_far"], "Let me measure one thing first.")

    def test_a_stop_is_proposed_then_confirmed_like_a_task(self):
        made = voice.call("propose", {"agent": "boxer", "action": "stop"})
        self.assertIn("stop what boxer is doing", made["readback"])
        with mock.patch.object(voice.broker, "stop", return_value=True) as stop, \
                mock.patch.object(voice.broker, "send") as send:
            done = voice.call("confirm", {"id": made["id"]})
        stop.assert_called_once_with("boxer")
        send.assert_not_called()
        self.assertTrue(done["ok"])
        self.assertEqual(done["stopped"], "boxer")
        self.assertNotIn("agent", done)

    def test_a_stop_with_nothing_running_says_so(self):
        made = voice.call("propose", {"agent": "boxer", "action": "stop"})
        with mock.patch.object(voice.broker, "stop", return_value=False):
            done = voice.call("confirm", {"id": made["id"]})
        self.assertFalse(done["ok"])
        self.assertIn("nothing running", done["said"])

    def test_agent_recap_reads_the_newest_away_summary(self):
        got = voice.call("agent_recap", {"agent": "boxer"})
        self.assertIn("Read three filings", got["recap"])

    def test_pending_asks_is_empty_when_nobody_is_waiting(self):
        self.assertEqual(voice.call("pending_asks"), "Nobody is waiting on you.")

    def test_pending_asks_names_the_agent_and_the_ask_id(self):
        self.awake("boxer")
        real = broker.pending_asks
        broker.pending_asks = lambda agent_id: [
            {"t": "ask", "id": "ask-7", "kind": "perm", "tool": "Bash",
             "summary": "command: rm -rf build"}]
        self.addCleanup(lambda: setattr(broker, "pending_asks", real))
        got = voice.call("pending_asks")
        self.assertEqual(got[0]["agent"], "boxer")
        self.assertEqual(got[0]["ask_id"], "ask-7")
        self.assertIn("Bash", got[0]["asking"])

    def test_answer_ask_carries_the_yes_to_the_broker(self):
        seen = []
        real = broker.answer
        broker.answer = lambda agent_id, request_id, allow=None, answers=None: seen.append(
            (agent_id, request_id, allow)) or True
        self.addCleanup(lambda: setattr(broker, "answer", real))
        got = voice.call("answer_ask", {"agent": "boxer", "ask_id": "ask-7", "allow": True})
        self.assertEqual(seen, [("boxer", "ask-7", True)])
        self.assertTrue(got["ok"])

    def test_insights_reports_the_agent_and_the_schedule(self):
        cron.add(agent="boxer", task="read the filings", at="07:00")
        got = voice.call("insights")
        self.assertEqual(got["agents"][0]["agent"], "boxer")
        self.assertEqual(got["jobs"][0]["agent"], "boxer")
        self.assertEqual(got["jobs"][0]["last"], "never run")
        self.assertIn("network", got)

    def test_agent_pages_lists_the_data_the_agent_publishes(self):
        got = voice.call("agent_pages", {"agent": "boxer"})
        self.assertIn("positions", got["data"])

    def test_page_data_reads_one_published_name(self):
        got = voice.call("page_data", {"agent": "boxer", "name": "positions"})
        self.assertIn('"open":3', got["data"])
        self.assertIsNone(got["note"])

    def test_page_data_is_cut_and_says_it_was_cut(self):
        self.publish(self.rec, "big.txt", "x" * 9000)
        got = voice.call("page_data", {"agent": "boxer", "name": "big"})
        self.assertIn("cut", got["data"])
        self.assertIn("9000 in all", got["note"])

    def test_calls_hands_back_previous_turns(self):
        voice.say_down("voice", "It is reading the listings.")
        self.assertEqual(voice.call("calls", {"n": 3})[-1]["text"], "It is reading the listings.")

    def test_an_agent_name_said_loosely_still_lands(self):
        self.agent("job-search")
        got = voice.call("agent_history", {"agent": "Job Search"})
        self.assertEqual(got["resolved_to"], "job-search")

    def test_an_agent_nobody_has_is_said_plainly(self):
        self.assertIn("no agent called", voice.call("agent_history", {"agent": "gideon"}).lower())

    def test_a_tool_nobody_has_is_said_plainly(self):
        self.assertEqual(voice.call("delete_everything"), "no such tool: delete_everything")


# -- propose, then confirm -------------------------------------------------------

class ProposeThenConfirm(VoiceCase):
    def setUp(self):
        super().setUp()
        self.agent("boxer")
        self.done = self.catch_sends()

    def test_propose_runs_nothing_and_hands_back_a_sentence_to_read(self):
        got = voice.call("propose", {"agent": "boxer", "task": "read the filings"})
        self.assertIn("I will ask boxer to read the filings", got["readback"])
        self.assertEqual(self.done, [], "propose must send nothing")

    def test_confirm_sends_it_once(self):
        got = voice.call("propose", {"agent": "boxer", "task": "read the filings"})
        said = voice.call("confirm", {"id": got["id"]})
        self.assertTrue(said["ok"])
        self.assertEqual(self.done, [{"agent": "boxer", "text": "read the filings", "wake": True}])

    def test_the_same_yes_twice_does_not_send_it_twice(self):
        got = voice.call("propose", {"agent": "boxer", "task": "read the filings"})
        voice.call("confirm", {"id": got["id"]})
        again = voice.call("confirm", {"id": got["id"]})
        self.assertFalse(again["ok"])
        self.assertEqual(len(self.done), 1)

    def test_an_id_nobody_issued_sends_nothing(self):
        said = voice.call("confirm", {"id": "deadbeef"})
        self.assertFalse(said["ok"])
        self.assertIn("expired or was never made", said["said"])
        self.assertEqual(self.done, [])

    def test_a_stale_yes_sends_nothing(self):
        got = voice.call("propose", {"agent": "boxer", "task": "read the filings"})
        clock.advance(config.VOICE_CONFIRM_SECONDS + 1)
        said = voice.call("confirm", {"id": got["id"]})
        self.assertFalse(said["ok"])
        self.assertEqual(self.done, [])

    def test_cancel_drops_it_and_confirm_then_does_nothing(self):
        got = voice.call("propose", {"agent": "boxer", "task": "read the filings"})
        self.assertTrue(voice.call("cancel", {"id": got["id"]})["ok"])
        self.assertFalse(voice.call("confirm", {"id": got["id"]})["ok"])
        self.assertEqual(self.done, [])

    def test_a_proposal_for_an_agent_nobody_has_is_refused_at_the_proposing(self):
        self.assertIn("no agent called", voice.call("propose", {"agent": "gideon", "task": "go"}))

    def test_a_new_agent_is_proposed_by_name_and_folder(self):
        got = voice.call("propose", {"agent": "new", "task": "watch the feed",
                                     "name": "watcher", "cwd": "~/watch"})
        self.assertIn("make a new agent called watcher", got["readback"])
        self.assertIsNone(registry.get("watcher"), "nothing is made until the yes")
        voice.call("confirm", {"id": got["id"]})
        self.assertIsNotNone(registry.get("watcher"))
        self.assertEqual(self.done, [{"agent": "watcher", "text": "watch the feed", "wake": True}])


# -- the turns that go with the task ---------------------------------------------
# A task alone loses the call that led to it. The voice says how many of this call's turns the
# agent should read, and the turns are gathered at the confirm rather than at the proposal.

class TurnsWithTheTask(VoiceCase):
    def setUp(self):
        super().setUp()
        self.agent("boxer")
        self.done = self.catch_sends()

    def call_so_far(self):
        """Three turns of a call, the way the browser posts them as they happen."""
        voice.say_down("you", "Look at the two filings from this morning.")
        voice.say_down("voice", "The second one is the larger of the two.")
        voice.say_down("you", "Do that one.")

    def sent_text(self):
        """The one message that reached the broker."""
        self.assertEqual(len(self.done), 1)
        return self.done[0]["text"]

    # -- the count ----------------------------------------------------------------

    def test_no_turns_are_asked_for_by_default(self):
        got = voice.call("propose", {"agent": "boxer", "task": "read the filings"})
        self.assertEqual(got["turns"], 0)
        self.assertEqual(voice.pending_proposal(got["id"])["turns"], 0)

    def test_the_count_is_held_with_the_proposal(self):
        got = voice.call("propose", {"agent": "boxer", "task": "go", "turns": 5})
        self.assertEqual(got["turns"], 5)
        self.assertEqual(voice.pending_proposal(got["id"])["turns"], 5)

    def test_the_ceiling_itself_is_kept(self):
        self.assertEqual(voice.propose("boxer", "go", turns=100)["turns"], 100)

    def test_more_than_the_ceiling_is_cut_down_to_it(self):
        self.assertEqual(voice.propose("boxer", "go", turns=250)["turns"], voice.MAX_TURNS)

    def test_a_negative_count_is_none(self):
        self.assertEqual(voice.propose("boxer", "go", turns=-4)["turns"], 0)

    def test_a_count_said_as_a_string_still_counts(self):
        self.assertEqual(voice.propose("boxer", "go", turns="5")["turns"], 5)

    def test_a_count_that_is_not_a_number_is_none(self):
        self.assertEqual(voice.propose("boxer", "go", turns="a few")["turns"], 0)
        self.assertEqual(voice.propose("boxer", "go", turns=None)["turns"], 0)

    # -- the readback --------------------------------------------------------------

    def test_the_readback_says_how_many_turns_go_with_it(self):
        got = voice.propose("boxer", "read the filings", turns=3)
        self.assertEqual(got["readback"],
                         "I will ask boxer to read the filings with the last 3 turns of this "
                         "call attached. Shall I?")

    def test_the_readback_says_nothing_about_turns_when_there_are_none(self):
        got = voice.propose("boxer", "read the filings")
        self.assertEqual(got["readback"], "I will ask boxer to read the filings. Shall I?")

    def test_the_readback_says_the_count_after_it_was_cut_down(self):
        got = voice.propose("boxer", "go", turns=250)
        self.assertIn("the last 100 turns", got["readback"])

    # -- what the agent is given ----------------------------------------------------

    def test_the_agent_is_given_the_call_under_a_heading(self):
        self.call_so_far()
        made = voice.propose("boxer", "read the second filing", turns=3)
        voice.confirm(made["id"])
        self.assertEqual(self.sent_text(),
                         "read the second filing\n\n"
                         "From the call with the owner, last 3 turns\n"
                         "The owner: Look at the two filings from this morning.\n"
                         "The front desk: The second one is the larger of the two.\n"
                         "The owner: Do that one.")

    def test_only_the_last_turns_asked_for_go_with_it(self):
        self.call_so_far()
        made = voice.propose("boxer", "go", turns=1)
        voice.confirm(made["id"])
        text = self.sent_text()
        self.assertIn("last 1 turns", text)
        self.assertIn("The owner: Do that one.", text)
        self.assertNotIn("two filings", text)

    def test_a_note_is_not_a_turn_and_is_left_out(self):
        voice.say_down("you", "Ask boxer to read it.")
        voice.say_down("note", "Sent to boxer.")
        voice.say_down("voice", "It is reading it now.")
        made = voice.propose("boxer", "carry on", turns=5)
        voice.confirm(made["id"])
        text = self.sent_text()
        self.assertNotIn("Sent to boxer.", text)
        self.assertIn("The owner: Ask boxer to read it.", text)
        self.assertIn("The front desk: It is reading it now.", text)
        self.assertIn("last 2 turns", text)

    def test_fewer_turns_than_asked_for_says_the_real_count(self):
        voice.say_down("you", "Only this one thing was said.")
        made = voice.propose("boxer", "go", turns=20)
        voice.confirm(made["id"])
        text = self.sent_text()
        self.assertIn("From the call with the owner, last 1 turns", text)
        self.assertNotIn("last 20 turns", text)

    def test_a_previous_call_is_not_this_call(self):
        voice.say_down("you", "That was last night.")
        clock.advance(config.VOICE_SESSION_MINUTES * 60 + 60)
        voice.say_down("you", "This is now.")
        made = voice.propose("boxer", "go", turns=10)
        voice.confirm(made["id"])
        text = self.sent_text()
        self.assertIn("The owner: This is now.", text)
        self.assertNotIn("last night", text)
        self.assertIn("last 1 turns", text)

    def test_none_of_the_call_goes_out_when_none_was_asked_for(self):
        self.call_so_far()
        made = voice.propose("boxer", "read the second filing")
        voice.confirm(made["id"])
        self.assertEqual(self.sent_text(), "read the second filing")

    def test_nothing_is_attached_when_the_call_has_said_nothing(self):
        made = voice.propose("boxer", "go", turns=5)
        voice.confirm(made["id"])
        self.assertEqual(self.sent_text(), "go")

    def test_a_new_agent_is_given_the_call_too(self):
        self.call_so_far()
        made = voice.propose("new", "watch the feed", turns=1, name="watcher", cwd="~/watch")
        voice.confirm(made["id"])
        self.assertIn("The owner: Do that one.", self.sent_text())

    # -- what the console says and writes down ---------------------------------------

    def test_the_sent_row_says_how_many_turns_went_with_it(self):
        self.call_so_far()
        made = voice.propose("boxer", "go", turns=2)
        self.assertEqual(voice.confirm(made["id"])["said"], "Sent to boxer with 2 turns.")

    def test_the_sent_row_is_unchanged_when_no_turns_went_with_it(self):
        made = voice.propose("boxer", "go")
        self.assertEqual(voice.confirm(made["id"])["said"], "Sent to boxer.")

    def test_the_log_carries_the_count_and_never_the_turns(self):
        voice.say_down("you", "The interesting part of the call.")
        with self.assertLogs("cc.voice", level="INFO") as caught:
            made = voice.call("propose", {"agent": "boxer", "task": "go", "turns": 4})
            voice.confirm(made["id"])
        said = "\n".join(caught.output)
        self.assertIn("4 turns", said)
        self.assertIn("1 turns", said)
        self.assertNotIn("interesting", said)



# -- the routes ------------------------------------------------------------------

class Routes(VoiceCase):
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
        payload = json.dumps(body or {}) if method == "POST" else None
        conn.request(method, path, body=payload,
                     headers={"Content-Type": "application/json"} if payload else {})
        reply = conn.getresponse()
        got = json.loads(reply.read() or b"{}")
        conn.close()
        return reply.status, got

    def test_the_session_route_says_503_when_there_is_no_key(self):
        config.OPENAI_KEY = ""
        code, got = self.hit("POST", "/api/voice/session")
        self.assertEqual(code, 503)
        self.assertIn("OPENAI_API_KEY", got["error"])

    def test_the_session_route_says_402_when_the_day_is_spent(self):
        config.VOICE_DAILY_MINUTES = 5
        voice.spend(5 * 60)
        code, got = self.hit("POST", "/api/voice/session")
        self.assertEqual(code, 402)
        self.assertIn("minutes", got["error"])

    def test_the_session_route_opens_the_door_without_calling_openai(self):
        code, got = self.hit("POST", "/api/voice/session")
        self.assertEqual(code, 200)
        self.assertEqual(got["voice"], "vesper")
        self.assertEqual(self.sent, [])

    def test_the_offer_route_hands_the_browser_the_answer(self):
        code, got = self.hit("POST", "/api/voice/offer", {"sdp": "v=0 offer"})
        self.assertEqual(code, 200)
        self.assertEqual(got["sdp"], "v=0 answer")

    def test_the_offer_route_carries_a_resume_through_to_the_briefing(self):
        code, _ = self.hit("POST", "/api/voice/offer",
                           {"sdp": "v=0 offer",
                            "resume": [{"who": "you", "text": "carry on where we were"}]})
        self.assertEqual(code, 200)
        self.assertIn("carry on where we were", self.backend()["instructions"])
        self.assertIn(voice.NO_GREETING, self.payload()["instructions"])

    def test_the_log_route_never_writes_a_turn_of_only_punctuation(self):
        code, got = self.hit("POST", "/api/voice/log", {"who": "you", "text": "."})
        self.assertEqual(code, 200)
        self.assertFalse(got["ok"])
        self.assertEqual(voice.calls(5), [])

    def beacon(self, path, raw, ctype):
        """A post the way navigator.sendBeacon sends one: the body and its type, nothing else."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", path, body=raw, headers={"Content-Type": ctype})
        reply = conn.getresponse()
        got = json.loads(reply.read() or b"{}")
        conn.close()
        return reply.status, got

    def test_the_end_route_takes_a_beacon_with_a_json_blob(self):
        raw = json.dumps({"session_id": "live_b", "seconds": 90, "why": "pagehide"}).encode()
        with self.assertLogs("cc.voice", level="INFO") as caught:
            code, got = self.beacon("/api/voice/end", raw, "application/json")
        self.assertEqual(code, 200)
        self.assertEqual(got["used_minutes"], 1.5)
        self.assertIn("ended by pagehide after 90 seconds", caught.output[-1])

    def test_the_end_route_takes_a_beacon_sent_as_plain_text_too(self):
        # A string beacon goes as text/plain. The body is still read as JSON.
        raw = json.dumps({"session_id": "live_t", "seconds": 30}).encode()
        code, got = self.beacon("/api/voice/end", raw, "text/plain;charset=UTF-8")
        self.assertEqual(code, 200)
        self.assertEqual(got["used_minutes"], 0.5)

    def test_the_end_route_ignores_a_second_end_for_one_session(self):
        self.hit("POST", "/api/voice/end",
                 {"session_id": "live_one", "seconds": 120, "why": "hangup"})
        raw = json.dumps({"session_id": "live_one", "seconds": 120, "why": "pagehide"}).encode()
        _, got = self.beacon("/api/voice/end", raw, "application/json")
        self.assertEqual(got["used_minutes"], 2.0)

    def test_the_end_route_charges_one_session_once(self):
        self.hit("POST", "/api/voice/end", {"session_id": "live_one", "seconds": 120})
        _, got = self.hit("POST", "/api/voice/end", {"session_id": "live_one", "seconds": 120})
        self.assertEqual(got["used_minutes"], 2.0)

    def test_the_offer_route_says_400_with_no_offer_in_it(self):
        self.assertEqual(self.hit("POST", "/api/voice/offer", {})[0], 400)

    def test_the_tool_route_carries_any_tool(self):
        self.agent("boxer")
        code, got = self.hit("POST", "/api/voice/tool", {"name": "fleet_status", "args": {}})
        self.assertEqual(code, 200)
        self.assertIn("boxer", got["result"])

    def test_the_tool_route_is_the_only_door_to_propose_and_confirm(self):
        self.agent("boxer")
        done = self.catch_sends()
        _, got = self.hit("POST", "/api/voice/tool",
                          {"name": "propose", "args": {"agent": "boxer", "task": "go"}})
        self.assertEqual(done, [])
        _, said = self.hit("POST", "/api/voice/tool",
                           {"name": "confirm", "args": {"id": got["result"]["id"]}})
        self.assertTrue(said["result"]["ok"])
        self.assertEqual(len(done), 1)

    def test_the_log_route_writes_a_note_under_its_own_name(self):
        code, got = self.hit("POST", "/api/voice/log",
                             {"who": "note", "text": "Sent to boxer."})
        self.assertEqual(code, 200)
        self.assertEqual(got["row"]["who"], "note")

    def test_the_tool_route_reaches_read_file(self):
        root = Path(self.dir).resolve()
        config.VOICE_READ_ROOTS = (str(root),)
        (root / "notes.md").write_text("one\ntwo\n")
        code, got = self.hit("POST", "/api/voice/tool",
                             {"name": "read_file", "args": {"path": str(root / "notes.md")}})
        self.assertEqual(code, 200)
        self.assertEqual(got["result"]["total_lines"], 2)

    def test_the_tool_route_refuses_a_path_outside_every_root_with_no_content(self):
        config.VOICE_READ_ROOTS = (str(Path(self.dir).resolve()),)
        code, got = self.hit("POST", "/api/voice/tool",
                             {"name": "read_file", "args": {"path": "/etc/hosts"}})
        self.assertEqual(code, 200)
        self.assertEqual(got["result"], voice.NOT_ALLOWED)

    def test_the_log_route_appends_and_reads_back(self):
        self.hit("POST", "/api/voice/log", {"who": "you", "text": "morning"})
        code, got = self.hit("GET", "/api/voice/log?n=5")
        self.assertEqual(code, 200)
        self.assertEqual(got["calls"][-1]["text"], "morning")
        self.assertIn("left_minutes", got["budget"])

    def test_the_end_route_charges_the_minutes(self):
        code, got = self.hit("POST", "/api/voice/end", {"session_id": "s", "seconds": 120})
        self.assertEqual(code, 200)
        self.assertEqual(got["used_minutes"], 2.0)

    def test_an_unknown_voice_route_is_a_404(self):
        self.assertEqual(self.hit("POST", "/api/voice/nonsense")[0], 404)


# -- an image they paste ----------------------------------------------------------

import base64 as _b64  # noqa: E402

PNG = "data:image/png;base64," + _b64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 40).decode()


class Image(VoiceCase):
    """The live voice cannot see, so a small model looks and the voice is handed the words."""

    hit = Routes.hit

    def setUp(self):
        super().setUp()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]
        self.opened = {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "A receipt for 12 pounds."}]}]}

    def test_a_data_url_is_read_for_its_decoded_size(self):
        self.assertEqual(voice.image_bytes(PNG), 48)

    def test_something_that_is_not_a_data_url_is_refused_with_a_sentence(self):
        with self.assertRaises(ValueError) as caught:
            voice.image_bytes("https://example.com/a.png")
        self.assertIn("data URL", str(caught.exception))

    def test_a_file_that_is_not_an_image_is_refused(self):
        with self.assertRaises(ValueError):
            voice.image_bytes("data:text/plain;base64,aGVsbG8=")

    def test_damaged_base64_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            voice.image_bytes("data:image/png;base64,@@@@")
        self.assertIn("damaged", str(caught.exception))

    def test_an_image_over_eight_megabytes_is_refused_before_openai(self):
        big = "data:image/jpeg;base64," + _b64.b64encode(
            b"\0" * (voice.IMAGE_MAX_BYTES + 1)).decode()
        code, got = self.hit("POST", "/api/voice/image", {"image": big})
        self.assertEqual(code, 413)
        self.assertIn("8 MB", got["error"])
        self.assertEqual(self.sent, [])

    def test_no_image_is_a_400_with_a_sentence(self):
        code, got = self.hit("POST", "/api/voice/image", {"text": "what is this"})
        self.assertEqual(code, 400)
        self.assertIn("no image", got["error"])
        self.assertEqual(self.sent, [])

    def test_the_route_answers_with_what_the_model_said(self):
        code, got = self.hit("POST", "/api/voice/image", {"image": PNG, "text": "how much"})
        self.assertEqual(code, 200)
        self.assertEqual(got, {"said": "A receipt for 12 pounds."})

    def test_the_request_names_the_vision_model_and_keeps_nothing(self):
        config.VOICE_VISION_MODEL = "gpt-6-luna"
        self.hit("POST", "/api/voice/image", {"image": PNG, "text": "how much"})
        sent = self.sent[-1]
        self.assertEqual(sent["url"], voice.RESPONSES_URL)
        body = sent["payload"]
        self.assertEqual(body["model"], "gpt-6-luna")
        self.assertIs(body["store"], False)
        parts = body["input"][0]["content"]
        self.assertEqual(body["input"][0]["role"], "user")
        self.assertEqual([p["type"] for p in parts], ["input_text", "input_image"])
        self.assertEqual(parts[1]["image_url"], PNG)
        self.assertIn("how much", parts[0]["text"])
        self.assertIn("120 words", parts[0]["text"])

    def test_an_image_alone_is_allowed(self):
        code, _ = self.hit("POST", "/api/voice/image", {"image": PNG})
        self.assertEqual(code, 200)
        self.assertIn("sent this image", self.sent[-1]["payload"]["input"][0]["content"][0]["text"])

    def test_an_sdk_style_answer_is_read_as_well(self):
        self.opened = {"output_text": "A cat."}
        self.assertEqual(voice.see_image(PNG), {"said": "A cat."})

    def test_an_answer_with_no_words_is_a_failure(self):
        self.opened = {"output": []}
        code, _ = self.hit("POST", "/api/voice/image", {"image": PNG})
        self.assertEqual(code, 502)

    def test_no_key_is_a_503(self):
        config.OPENAI_KEY = ""
        code, got = self.hit("POST", "/api/voice/image", {"image": PNG})
        self.assertEqual(code, 503)
        self.assertEqual(self.sent, [])

    def test_the_key_is_scrubbed_from_an_error(self):
        def boom(url, payload, key, timeout=None):
            raise RuntimeError(f"OpenAI refused the session (401): bad key {key}")
        voice.post_json = boom
        code, got = self.hit("POST", "/api/voice/image", {"image": PNG})
        self.assertEqual(code, 502)
        self.assertNotIn(config.OPENAI_KEY, got["error"])
        self.assertIn("the key", got["error"])
        self.assertIn("refused the image", got["error"])

    def test_the_log_line_says_size_model_and_outcome_and_nothing_else(self):
        with self.assertLogs("cc.voice", level="INFO") as seen:
            voice.see_image(PNG, "secret words")
        line = "\n".join(seen.output)
        self.assertIn("voice image 1 kB, model", line)
        self.assertIn("ok", line)
        self.assertNotIn("secret words", line)
        self.assertNotIn("receipt", line)
        self.assertNotIn(PNG[30:], line)

    def test_the_vision_call_waits_sixty_seconds(self):
        got = {}

        def keep(url, payload, key, timeout=None):
            got["timeout"] = timeout
            return self.opened
        voice.post_json = keep
        voice.see_image(PNG)
        self.assertEqual(got["timeout"], 60.0)

    def test_the_setting_defaults_to_gpt_6_luna_and_can_be_changed(self):
        import os
        import importlib
        os.environ["PLATFORM_VOICE_VISION_MODEL"] = "gpt-6-astra"
        try:
            self.assertEqual(importlib.reload(config).VOICE_VISION_MODEL, "gpt-6-astra")
        finally:
            os.environ.pop("PLATFORM_VOICE_VISION_MODEL", None)
            self.assertEqual(importlib.reload(config).VOICE_VISION_MODEL, "gpt-6-luna")
            config.use_var(self.dir / "var", views=self.dir / "views")
