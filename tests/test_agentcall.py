"""An agent on the phone: the ring, the session as the agent, its two tools, and the call record.

OpenAI is never called. `voice.post_json` and `agentcall.post_speech` are module functions for
exactly this reason, so a test replaces them and reads what would have gone out.
"""

import unittest
import unittest.mock
import base64
import json

from commandcenter import agentcall, broker, clock, config, cords, inbox, memory, voice
from tests.harness import Case

MP3 = b"ID3" + b"\x00" * 600
WAV = base64.b64encode(b"RIFF" + b"\x00" * 4000).decode()


class CallCase(Case):
    def setUp(self):
        super().setUp()
        config.OPENAI_KEY = "sk-not-a-real-key"
        config.VOICE_NAME = "vesper"
        config.VOICE_MODEL = "gpt-live-1"
        self.sent = []
        self.opened = {"session": {"id": "live_agent"},
                       "transport": {"type": "webrtc", "sdp": "v=0 answer"}}
        real = voice.post_json

        def fake(url, payload, key, timeout=None):
            self.sent.append({"url": url, "payload": payload})
            return self.opened

        voice.post_json = fake
        self.addCleanup(setattr, voice, "post_json", real)
        self.spoken = []

        def speech(payload, key, timeout=None):
            self.spoken.append(payload)
            return MP3

        agentcall.post_speech = speech
        self.sends = []
        real_send = broker.send
        broker.send = lambda agent_id, text, wake=False: self.sends.append(
            {"agent": agent_id, "text": text, "wake": wake}) or "sent"
        self.addCleanup(setattr, broker, "send", real_send)
        self.rec = self.agent("portfolio-manager")
        from commandcenter import registry
        registry.update(self.rec["id"], name="Juno")
        self.other = self.agent("job-search")

    def ring(self, agent=None, text="The payment failed and the order expires in 20 minutes."):
        inbox.add(agent or self.rec["id"], text, urgency="urgent")
        agentcall.wait()
        return inbox.ringing()

    def session(self):
        return self.sent[-1]["payload"]["session"]

    def backend(self):
        return self.session()["delegation"]["responses"]


class Ringing(CallCase):
    def test_a_ring_from_an_agent_is_written_down_with_an_opening_line(self):
        ring = self.ring()
        call = agentcall.get(ring["call"])
        self.assertEqual(call["agent"], "portfolio-manager")
        self.assertEqual(call["outcome"], "ringing")
        self.assertTrue(call["opening"].startswith("Hi, it's Juno. The payment failed"))

    def test_a_ring_records_nothing(self):
        self.ring()
        self.assertEqual(self.spoken, [], "no line is recorded per call any more")

    def test_a_ring_from_no_known_agent_is_desk_as_before(self):
        ring = self.ring(agent="schedule")
        self.assertIsNone(agentcall.get(ring["call"]))
        self.assertNotIn("caller", voice.start_session())

    def test_a_long_reason_is_cut_to_something_sayable(self):
        said = agentcall.reason_short("One. " + "word " * 60)
        self.assertEqual(said, "One.")
        said = agentcall.reason_short("word " * 60)
        self.assertEqual(len(said.split()), agentcall.OPENING_WORDS)
        self.assertTrue(said.endswith("."))


class Answering(CallCase):
    def turns(self, count, size=100):
        entries = []
        for n in range(count):
            entries.append(self.said(f"The owner {n} " + "x" * size, who="user"))
            entries.append(self.used_tool("Bash", command="secret-tool-input"))
            entries.append(self.tool_said("secret-tool-output"))
            entries.append(self.said(f"Agent {n} " + "y" * size))
        self.transcript_for(self.rec, entries)

    def test_the_door_says_who_is_calling_while_a_ring_is_up(self):
        ring = self.ring()
        got = voice.start_session()
        self.assertEqual(got["caller"]["call"], ring["call"])
        self.assertEqual(got["caller"]["name"], "Juno")
        # No greeting yet: the call takes one from its own first words.
        self.assertIsNone(got["caller"]["clip"])
        self.assertTrue(got["caller"]["capture"])
        self.assertEqual(got["caller"]["greeting"], "Hi, it's Juno.")
        # The door answers nothing: the ring is still up.
        self.assertIsNotNone(inbox.ringing())

    def test_an_offer_during_a_ring_is_the_agents_call(self):
        ring = self.ring()
        got = voice.open_call("v=0 offer")
        session = self.session()
        self.assertIn("You are Juno, one of the owner's agents", session["instructions"])
        self.assertIn("payment failed", session["instructions"])
        self.assertNotIn("Welcome back", session["instructions"])
        self.assertEqual(session["audio"]["output"]["voice"], config.AGENT_VOICE)
        self.assertIs(session["store"], False)
        self.assertEqual([t["name"] for t in self.backend()["tools"]], ["propose", "confirm", "cancel", "ask_agent", "end_call"])
        self.assertEqual(got["caller"]["agent"], "portfolio-manager")
        # Answered: the ring stops, and the front desk will not read the item again.
        self.assertIsNone(inbox.ringing())
        self.assertEqual(inbox.get(ring["id"])["status"], "told")
        self.assertEqual(agentcall.get(ring["call"])["outcome"], "answered")

    def test_the_backend_has_at_least_ten_real_turns_and_no_tools(self):
        self.turns(30, size=2000)
        self.ring()
        voice.open_call("v=0 offer")
        brief = self.backend()["instructions"]
        # Each turn is cut to TURN_CHARS, so fourteen of them fit the budget.
        self.assertIn("## Your last 14 turns with the owner", brief)
        self.assertNotIn("secret-tool", brief)
        self.assertIn("Working folder: " + self.rec["cwd"], brief)
        lines = [l for l in brief.splitlines() if l.startswith(("The owner: ", "You: "))]
        self.assertEqual(len(lines), 14)
        self.assertTrue(all(len(l) < agentcall.TURN_CHARS + 20 for l in lines))
        # Newest last.
        self.assertTrue(lines[-1].startswith("You: Agent 29"))
        # However tight the budget, ten real turns always go in.
        real = agentcall.TURNS_BUDGET
        agentcall.TURNS_BUDGET = 1000
        self.addCleanup(setattr, agentcall, "TURNS_BUDGET", real)
        self.assertEqual(len(agentcall.conversation(self.rec)), 10)

    def test_the_voice_speaks_as_the_agent_never_as_its_relay(self):
        # 20 Oct 2026: "That was the agent's proposed fix. I was just relaying it."
        self.ring()
        voice.open_call("v=0 offer")
        for said in (self.session()["instructions"], self.backend()["instructions"]):
            self.assertIn("Never say \"the agent\", never say you are relaying", said)
        self.assertIn("said as Juno.", self.backend()["instructions"])

    def test_up_to_sixteen_turns_when_they_fit(self):
        self.turns(30, size=100)
        self.ring()
        voice.open_call("v=0 offer")
        self.assertIn("## Your last 16 turns with the owner", self.backend()["instructions"])

    def test_a_named_caller_wins_and_a_resume_stays_the_agents_call(self):
        ring = self.ring()
        voice.open_call("v=0 offer")
        # Paused and resumed, with no ring up any more: the browser names the call.
        self.opened = {"session": {"id": "live_agent_2"},
                       "transport": {"type": "webrtc", "sdp": "v=0 answer"}}
        got = voice.open_call("v=0 offer", resume=[{"who": "you", "text": "go on"}],
                              caller=ring["call"])
        self.assertEqual(got["caller"]["agent"], "portfolio-manager")
        self.assertIn("resuming", self.session()["instructions"])
        self.assertEqual(self.session()["input"][0]["role"], "user")

    def test_the_item_id_names_the_call_too(self):
        ring = self.ring()
        got = voice.open_call("v=0 offer", caller=ring["id"])
        self.assertEqual(got["caller"]["call"], ring["call"])

    def test_a_desk_call_paused_during_a_ring_resumes_as_desk(self):
        self.ring()
        voice.open_call("v=0 offer", resume=[{"who": "you", "text": "and then"}])
        self.assertNotIn("one of the owner's agents. You are not the front desk", self.session()["instructions"])
        self.assertIsNotNone(inbox.ringing())

    def test_a_call_that_is_over_is_not_opened_again(self):
        ring = self.ring()
        got = voice.open_call("v=0 offer")
        agentcall.session_ended(got["session_id"], 30, "hangup")
        voice.open_call("v=0 offer", caller=ring["call"])
        self.assertNotIn("You are Juno", self.session()["instructions"])

    def test_with_no_ring_and_no_caller_desk_is_unchanged(self):
        voice.open_call("v=0 offer")
        self.assertEqual(self.session()["audio"]["output"]["voice"], "vesper")
        self.assertIn("propose", [t.get("name") for t in self.backend()["tools"]])


class Tools(CallCase):
    def open(self):
        self.ring()
        return voice.open_call("v=0 offer")["session_id"]

    def wait_send(self):
        agentcall.wait()
        import time
        for _ in range(50):
            if self.sends:
                break
            time.sleep(0.02)

    def test_a_confirmed_ask_waits_for_the_call_to_end_and_goes_once_with_it(self):
        sid = self.open()
        call_id = agentcall.session_call(sid)["id"]
        made = agentcall.tool(sid, "propose", {"text": "Pay it from the second card.",
                                               "agent": "job-search"})
        self.assertIn("Pay it from the second card", made["readback"])
        got = agentcall.tool(sid, "confirm", {"id": made["id"]})
        self.assertTrue(got["ok"])
        self.assertNotIn("agent", got, "the call must not wait on its own agent")
        self.assertNotIn("Pay it", got["said"], "the voice is not handed their words to repeat")
        import time
        time.sleep(0.1)
        self.assertEqual(self.sends, [], "nothing breaks into the agent while the call is on")
        self.assertFalse(agentcall.tool(sid, "confirm", {"id": made["id"]})["ok"], "once only")

        agentcall.finish(call_id)
        self.wait_send()
        self.assertEqual(len(self.sends), 1, "one message, after the call")
        self.assertEqual(self.sends[0]["agent"], "portfolio-manager")
        self.assertIn("What they asked you to do on the call:\n- Pay it from the second card.",
                      self.sends[0]["text"])

    def test_mid_call_the_agent_can_read_the_call_and_the_voice_can_hang_up(self):
        sid = self.open()
        made = agentcall.tool(sid, "propose", {"text": "Add an end call tool."})
        agentcall.tool(sid, "confirm", {"id": made["id"]})
        import time
        for _ in range(50):
            live = agentcall.listing("portfolio-manager")[-1]
            if live["asks"]:
                break
            time.sleep(0.02)
        self.assertIsNone(live["ended_at"])
        self.assertEqual(live["asks"], ["Add an end call tool."], "what call_transcript reads")
        self.assertTrue(agentcall.tool(sid, "end_call", {})["hangup"])

    def test_now_goes_at_once_but_into_a_busy_agent_he_chooses(self):
        sid = self.open()
        busy = [True]
        real = agentcall._busy
        agentcall._busy = lambda agent: busy[0]
        self.addCleanup(setattr, agentcall, "_busy", real)
        made = agentcall.tool(sid, "propose", {"text": "Ship it.", "now": True})
        self.assertTrue(made["choose"], "into its work is their to choose, never Auto's")
        self.assertIn("in the middle of work", made["readback"])
        agentcall.tool(sid, "confirm", {"id": made["id"]})
        self.wait_send()
        self.assertEqual(self.sends[0]["text"], "[From the call with the owner] Ship it.",
                         "sent during the call")
        busy[0] = False
        later = agentcall.tool(sid, "propose", {"text": "Then tidy up.", "now": True})
        self.assertFalse(later["choose"])
        agentcall.tool(sid, "confirm", {"id": later["id"], "later": True})
        import time
        time.sleep(0.1)
        self.assertEqual(len(self.sends), 1, "kept for the end, as they chose")

    def test_cancel_drops_it_and_nothing_goes(self):
        sid = self.open()
        made = agentcall.tool(sid, "propose", {"text": "Sell it all."})
        self.assertTrue(agentcall.tool(sid, "cancel", {"id": made["id"]})["ok"])
        self.assertFalse(agentcall.tool(sid, "confirm", {"id": made["id"]})["ok"])
        import time
        time.sleep(0.05)
        self.assertEqual(self.sends, [])

    def test_a_call_cannot_confirm_the_desks_proposal(self):
        sid = self.open()
        theirs = voice.propose("job-search", "apply to the bank")
        got = agentcall.tool(sid, "confirm", {"id": theirs["id"]})
        self.assertFalse(got["ok"])
        self.assertIsNotNone(voice.pending_proposal(theirs["id"]), "left for the front desk")

    def test_ask_agent_waits_for_the_agent_and_hands_back_its_reply(self):
        sid = self.open()
        asked = []
        real = cords.ask
        cords.ask = lambda agent, text, timeout=0: asked.append((agent, timeout)) or "It is card two."
        self.addCleanup(setattr, cords, "ask", real)
        got = agentcall.tool(sid, "ask_agent", {"question": "which card?"})
        self.assertEqual(got["reply"], "It is card two.")
        self.assertEqual(asked, [("portfolio-manager", agentcall.ASK_SECONDS)])

    def test_now_into_a_free_agent_waits_for_its_answer_and_hands_it_to_the_voice(self):
        sid = self.open()
        real_busy, real_ask = agentcall._busy, cords.ask
        agentcall._busy = lambda agent: False
        asked = []
        cords.ask = lambda agent, text, timeout=0: asked.append(text) or "Next is the first model."
        self.addCleanup(setattr, agentcall, "_busy", real_busy)
        self.addCleanup(setattr, cords, "ask", real_ask)
        made = agentcall.tool(sid, "propose", {"text": "What are the next steps?", "now": True})
        got = agentcall.tool(sid, "confirm", {"id": made["id"]})
        self.assertEqual(got["reply"], "Next is the first model.", "read back on the call")
        self.assertTrue(asked[0].startswith("[From the call with the owner] What are the next steps?"))
        self.assertNotIn("agent", got, "the call must not wait on its own agent")

    def test_a_session_that_is_not_an_agents_reaches_no_agent_tool(self):
        self.assertEqual(agentcall.tool("live_unknown", "propose", {"text": "x"}),
                         "This is not an agent's call.")
        self.assertIn("no such tool", voice.call("tell_agent", {"text": "x"}))


class Record(CallCase):
    def test_lines_go_to_the_call_and_not_to_the_desks_memory(self):
        ring = self.ring()
        voice.open_call("v=0 offer")
        self.assertTrue(agentcall.turn(ring["call"], "you", "Use the second card."))
        self.assertTrue(agentcall.turn(ring["call"], "voice", "Done, noted."))
        self.assertEqual(voice.calls(10), [])
        self.assertEqual(len(agentcall.get(ring["call"])["turns"]), 2)

    def test_a_line_for_a_call_not_answered_is_refused(self):
        ring = self.ring()
        self.assertIsNone(agentcall.turn(ring["call"], "you", "hello"))

    def test_a_pause_keeps_the_call_and_a_hang_up_sends_the_transcript(self):
        ring = self.ring()
        sid = voice.open_call("v=0 offer")["session_id"]
        agentcall.turn(ring["call"], "you", "Use the second card.")
        agentcall.turn(ring["call"], "voice", "Noted, I will retry with it.")
        agentcall.session_ended(sid, 50, "pause")
        self.assertIsNone(agentcall.get(ring["call"])["ended_at"])
        self.assertEqual(self.sends, [])
        agentcall.session_ended(sid, 190, "hangup")
        agentcall.wait()
        call = agentcall.get(ring["call"])
        self.assertTrue(call["ended_at"])
        self.assertTrue(call["told"])
        text = self.sends[-1]["text"]
        self.assertTrue(text.startswith("[Your call with the owner, 09:00, 3 min]"))
        self.assertIn("The owner: Use the second card.\nYou: Noted, I will retry with it.", text)
        self.assertTrue(self.sends[-1]["wake"])

    def test_a_call_where_owner_said_nothing_sends_no_transcript(self):
        ring = self.ring()
        sid = voice.open_call("v=0 offer")["session_id"]
        agentcall.turn(ring["call"], "voice", "Hey, the owner. What do you need?")
        agentcall.session_ended(sid, 20, "hangup")
        agentcall.wait()
        self.assertTrue(agentcall.get(ring["call"])["ended_at"])
        self.assertEqual(self.sends, [])

    def test_an_unanswered_ring_becomes_a_missed_call_with_no_voicemail(self):
        ring = self.ring()
        clock.freeze(clock.now() + inbox.RING_SECONDS + 1)
        agentcall.sweep()
        calls = agentcall.listing("portfolio-manager")
        self.assertEqual(calls[-1]["outcome"], "missed")
        self.assertEqual(calls[-1]["how"], "unanswered")
        self.assertIsNone(calls[-1]["audio"], "no voicemail: a missed call is a text")

    def test_declined_and_call_me_back_are_missed(self):
        ring = self.ring()
        inbox.decline(ring["id"])
        self.assertIsNone(inbox.ringing())
        self.assertEqual(agentcall.get(ring["call"])["how"], "declined")
        other = self.ring(text="A second urgent thing.")
        inbox.callback(other["id"])
        self.assertEqual(agentcall.get(other["call"])["how"], "callback")

    def test_a_newer_ring_makes_the_older_one_missed(self):
        first = self.ring()
        clock.freeze(clock.now() + 5)
        self.ring(agent="job-search", text="The offer must be signed now.")
        self.assertEqual(agentcall.get(first["call"])["outcome"], "missed")

    def test_a_greeting_is_kept_once_and_played_on_the_next_call(self):
        self.assertIsNone(agentcall.greeting("portfolio-manager"))
        agentcall.save_greeting("portfolio-manager", {"audio": WAV})
        ring = self.ring()
        got = voice.start_session()["caller"]
        self.assertEqual(got["clip"], "/api/agents/portfolio-manager/greeting")
        self.assertFalse(got["capture"])
        voice.open_call("v=0 offer")
        # The session opens knowing the clip played, so no note is appended mid-call for the
        # voice to answer aloud ("Okay, noted.", 22 October 2026).
        line = self.session()["instructions"]
        self.assertIn("plays to them in your voice", line)
        self.assertNotIn("Open the call with exactly", line)
        self.assertIn("The payment failed", line)
        # Another agent, a new voice or a new name has none until its own call takes one.
        self.assertIsNone(agentcall.greeting("job-search"))
        memory.save_settings({"voices": {"portfolio-manager": "ash"}})
        self.assertIsNone(agentcall.greeting("portfolio-manager"))

    def test_a_greeting_must_be_a_short_wav(self):
        with self.assertRaises(ValueError):
            agentcall.save_greeting("portfolio-manager", {"audio": "bm90IGEgd2F2"})
        with self.assertRaises(ValueError):
            agentcall.save_greeting("nobody", {"audio": WAV})

    def test_a_missed_agent_call_is_a_text_in_its_name(self):
        from commandcenter import telegram
        said = []
        real = telegram.notify
        telegram.notify = lambda text, **kw: said.append(text) or True
        self.addCleanup(setattr, telegram, "notify", real)
        ring = self.ring()
        inbox._telegram_if_unanswered(ring["id"], ring)
        self.assertEqual(said, ["Missed call from Juno: The payment failed and the order "
                                "expires in 20 minutes."])

    def test_the_record_survives_a_restart(self):
        ring = self.ring()
        agentcall.reset()
        self.assertEqual(agentcall.get(ring["call"])["agent"], "portfolio-manager")


class CallOwner(CallCase):
    @unittest.mock.patch.object(agentcall, 'CALLS_PER_HOUR', 3)
    def test_three_calls_an_hour_and_the_fourth_is_told_to_write(self):
        for n in range(3):
            got = agentcall.call_owner("job-search", f"Urgent thing number {n}.")
            self.assertFalse(got["duplicate"])
            clock.freeze(clock.now() + 60)
        with self.assertRaises(PermissionError) as caught:
            agentcall.call_owner("job-search", "A fourth thing.")
        self.assertIn("brief", str(caught.exception))
        clock.freeze(clock.now() + 3600)
        self.assertFalse(agentcall.call_owner("job-search", "Next hour.")["duplicate"])

    def test_it_rings_like_brief_urgent(self):
        agentcall.call_owner("job-search", "Sign the offer now.")
        self.assertEqual(inbox.ringing()["agent"], "job-search")


class Settings(CallCase):
    def test_voices_default_and_save(self):
        self.assertEqual(memory.voice_for(), "vesper")
        self.assertEqual(memory.voice_for("job-search"), config.AGENT_VOICE)
        got = memory.save_settings({"voices": {"job-search": "marin", "desk": "ripple"}})
        self.assertEqual(got["voices"]["job-search"], "marin")
        self.assertEqual(memory.voice_for(), "ripple")
        memory.save_settings({"voices": {"job-search": ""}})
        self.assertEqual(memory.voice_for("job-search"), config.AGENT_VOICE)

    def test_an_agent_cannot_have_a_voice_that_cannot_be_recorded(self):
        with self.assertRaises(ValueError):
            memory.save_settings({"voices": {"job-search": "vesper"}})
        with self.assertRaises(ValueError):
            memory.save_settings({"voices": {"desk": "nobody"}})

    def test_a_new_desk_voice_drops_the_old_greeting(self):
        voice._greeting_file().write_bytes(b"RIFF" + b"\x00" * 3000)
        memory.save_settings({"voices": {"desk": "cedar"}})
        self.assertIsNone(voice.greeting())

    def test_prewarm_is_on_until_turned_off(self):
        self.assertIs(memory.settings()["prewarm"], True)
        self.assertIs(memory.save_settings({"prewarm": False})["prewarm"], False)
        with self.assertRaises(ValueError):
            memory.save_settings({"prewarm": "yes"})

    def test_the_agents_voice_reaches_its_call(self):
        memory.save_settings({"voices": {"portfolio-manager": "ash"}})
        self.ring()
        voice.open_call("v=0 offer")
        self.assertEqual(self.session()["audio"]["output"]["voice"], "ash")

    def test_a_preview_is_made_once_and_kept(self):
        self.assertEqual(agentcall.preview("marin"), MP3)
        self.assertEqual(agentcall.preview("marin"), MP3)
        self.assertEqual(len(self.spoken), 1)
        with self.assertRaises(ValueError):
            agentcall.preview("vesper")

    def test_settings_list_the_choices(self):
        got = memory.settings()
        self.assertIn("vesper", got["voice_choices"]["desk"])
        self.assertNotIn("vesper", got["voice_choices"]["agent"])
        self.assertEqual(json.loads(json.dumps(got))["agent_voice"], config.AGENT_VOICE)


class Routes(CallCase):
    """The daemon's own doors for all of this, over HTTP, as the console and the tools use them."""

    def setUp(self):
        super().setUp()
        import threading
        from http.server import ThreadingHTTPServer
        from commandcenter import server
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]

    def hit(self, method, path, body=None, raw=False):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body or {}) if method == "POST" else None
        conn.request(method, path, body=payload,
                     headers={"Content-Type": "application/json"} if payload else {})
        reply = conn.getresponse()
        data = reply.read()
        conn.close()
        return reply.status, (data if raw else json.loads(data or b"{}"))

    def test_the_whole_call_over_the_routes(self):
        ring = self.ring()
        code, door = self.hit("POST", "/api/voice/session", {})
        self.assertEqual(door["caller"]["call"], ring["call"])
        code, got = self.hit("POST", "/api/voice/offer", {"sdp": "v=0 offer",
                                                          "caller": ring["call"]})
        self.assertEqual(code, 200)
        sid = got["session_id"]
        code, got = self.hit("POST", "/api/voice/log", {"who": "you", "text": "Use card two.",
                                                        "caller": ring["call"]})
        self.assertTrue(got["ok"])
        code, got = self.hit("POST", "/api/voice/tool", {"name": "propose", "session_id": sid,
                                                         "args": {"text": "Use card two."}})
        made = got["result"]
        code, got = self.hit("POST", "/api/voice/tool", {"name": "confirm", "session_id": sid,
                                                         "args": {"id": made["id"]}})
        self.assertTrue(got["result"]["ok"])
        code, got = self.hit("POST", "/api/voice/end", {"session_id": sid, "seconds": 75,
                                                        "why": "hangup"})
        agentcall.wait()
        code, got = self.hit("GET", "/api/agents/portfolio-manager/calls")
        call = got["calls"][-1]
        self.assertEqual(call["outcome"], "answered")
        self.assertEqual(call["seconds"], 75)
        self.assertEqual(call["turns"][0]["text"], "Use card two.")
        code, _ = self.hit("GET", "/api/agents/portfolio-manager/greeting", raw=True)
        self.assertEqual(code, 404)
        code, got = self.hit("POST", "/api/voice/greeting", {"agent": "portfolio-manager",
                                                              "audio": WAV})
        self.assertTrue(got["ok"])
        code, audio = self.hit("GET", "/api/agents/portfolio-manager/greeting", raw=True)
        self.assertEqual(code, 200)
        self.assertTrue(audio.startswith(b"RIFF"))

    def test_a_desk_call_names_itself_and_takes_no_ring(self):
        self.ring()
        code, got = self.hit("POST", "/api/voice/offer", {"sdp": "v=0 offer", "caller": "desk"})
        self.assertEqual(code, 200)
        self.assertNotIn("caller", got)
        self.assertIsNotNone(inbox.ringing())

    @unittest.mock.patch.object(agentcall, 'CALLS_PER_HOUR', 3)
    def test_call_owner_is_refused_with_429_after_three(self):
        for n in range(3):
            code, _ = self.hit("POST", "/api/agents/job-search/call", {"reason": f"Thing {n}."})
            self.assertEqual(code, 200)
        code, got = self.hit("POST", "/api/agents/job-search/call", {"reason": "Thing 4."})
        self.assertEqual(code, 429)
        self.assertIn("brief", got["error"])

    def test_decline_from_the_phone(self):
        ring = self.ring()
        code, _ = self.hit("POST", "/api/phone/decline", {"id": ring["id"]})
        self.assertEqual(code, 200)
        self.assertEqual(agentcall.get(ring["call"])["outcome"], "missed")

    def test_a_voice_preview_is_served_as_audio(self):
        code, audio = self.hit("GET", "/api/voice/preview/cedar", raw=True)
        self.assertEqual((code, audio), (200, MP3))
        code, _ = self.hit("GET", "/api/voice/preview/vesper", raw=True)
        self.assertEqual(code, 404)


class Outgoing(CallCase):
    """The owner calling an agent from its chat: no ring, no item, the same session."""

    def test_the_door_names_the_agent_and_writes_nothing(self):
        got = voice.start_session("agent:portfolio-manager")
        self.assertEqual(got["caller"]["agent"], "portfolio-manager")
        self.assertEqual(got["caller"]["name"], "Juno")
        self.assertEqual(got["caller"]["direction"], "outgoing")
        self.assertIsNone(got["caller"]["call"])
        self.assertIsNone(got["caller"]["clip"])
        self.assertEqual(agentcall.listing("portfolio-manager"), [])

    def test_the_offer_opens_the_agents_session_as_a_call_he_made(self):
        got = voice.open_call("v=0 offer", caller="agent:portfolio-manager")
        session = self.session()
        self.assertIn("The owner has called you themselves", session["instructions"])
        self.assertIn("ask what they need", session["instructions"])
        self.assertNotIn("because of this", session["instructions"])
        self.assertIn("## Why this call", self.backend()["instructions"])
        self.assertEqual(session["audio"]["output"]["voice"], config.AGENT_VOICE)
        self.assertEqual([t["name"] for t in self.backend()["tools"]], ["propose", "confirm", "cancel", "ask_agent", "end_call"])
        call = agentcall.get(got["caller"]["call"])
        self.assertEqual(call["outcome"], "outgoing")
        self.assertIn("id", agentcall.tool(got["session_id"], "propose", {"text": "x"}))

    def test_the_voice_prompt_he_saves_on_config_is_what_the_next_call_hears(self):
        import tempfile
        from pathlib import Path
        real = agentcall.VOICE_PROMPT
        agentcall.VOICE_PROMPT = Path(tempfile.mkdtemp()) / "agent_voice.md"
        self.addCleanup(setattr, agentcall, "VOICE_PROMPT", real)
        self.assertIn("Speak as yourself", agentcall.voice_prompt_doc()["text"])
        agentcall.save_voice_prompt("Talk like a pirate.")
        voice.open_call("v=0 offer", caller="agent:portfolio-manager")
        self.assertIn("Talk like a pirate.", self.session()["instructions"])
        self.assertNotIn("Speak as yourself", self.session()["instructions"])

    def test_an_unknown_agent_is_desk(self):
        voice.open_call("v=0 offer", caller="agent:nobody")
        self.assertNotIn("The owner has called you", self.session()["instructions"])
        voice.open_call("v=0 offer", caller="agent:../etc")
        self.assertNotIn("The owner has called you", self.session()["instructions"])

    def test_a_ring_that_is_up_does_not_take_the_call_over(self):
        self.ring(agent="job-search", text="Sign the offer now.")
        got = voice.open_call("v=0 offer", caller="agent:portfolio-manager")
        self.assertEqual(got["caller"]["agent"], "portfolio-manager")
        self.assertIsNotNone(inbox.ringing())

    def test_lines_the_end_and_the_transcript(self):
        got = voice.open_call("v=0 offer", caller="agent:portfolio-manager")
        call_id = got["caller"]["call"]
        self.assertTrue(agentcall.turn(call_id, "you", "How is the rebalance going?"))
        self.assertTrue(agentcall.turn(call_id, "voice", "Done, two trades left."))
        agentcall.session_ended(got["session_id"], 125, "hangup")
        agentcall.wait()
        text = self.sends[-1]["text"]
        self.assertTrue(text.startswith("[the owner called you, 09:00, 2 min]"))
        self.assertIn("The owner: How is the rebalance going?", text)
        row = agentcall.listing("portfolio-manager")[-1]
        self.assertEqual((row["outcome"], row["seconds"], row["audio"]), ("outgoing", 125, None))

    def test_a_resume_names_the_call_it_made(self):
        got = voice.open_call("v=0 offer", caller="agent:portfolio-manager")
        call_id = got["caller"]["call"]
        agentcall.session_ended(got["session_id"], 20, "pause")
        self.opened = {"session": {"id": "live_agent_2"},
                       "transport": {"type": "webrtc", "sdp": "v=0 answer"}}
        again = voice.open_call("v=0 offer", resume=[{"who": "you", "text": "go on"}],
                                caller=call_id)
        self.assertEqual(again["caller"]["call"], call_id)
        self.assertEqual(len(agentcall.listing("portfolio-manager")), 1)

    def test_his_own_calls_do_not_count_against_the_agent(self):
        for _ in range(4):
            got = voice.open_call("v=0 offer", caller="agent:job-search")
            agentcall.session_ended(got["session_id"], 10, "hangup")
        agentcall.wait()
        self.assertFalse(agentcall.call_owner("job-search", "Urgent now.")["duplicate"])


class Opening(unittest.TestCase):
    REC = {"id": "personal-assistant", "name": "Ada"}

    def test_the_agents_own_greeting_is_not_said_twice(self):
        for text in ("Ada here. Did you call the driver?", "Ada again. Did you call the driver?",
                     "personal-assistant: did you call the driver?",
                     "Hi the owner, this is Ada. Did you call the driver?"):
            self.assertEqual(agentcall.opening_line(self.REC, text),
                             "Hi, it's Ada. Did you call the driver?", text)

    def test_a_word_that_only_starts_with_the_name_stays(self):
        self.assertEqual(agentcall.opening_line(self.REC, "Adaptive rules apply."),
                         "Hi, it's Ada. Adaptive rules apply.")
