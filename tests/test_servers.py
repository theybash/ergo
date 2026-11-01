"""An agent's own server: ports, starting, giving up, and outliving its agent.

These use real processes, because the interesting failures are all about processes: one that exits at
once, one that runs but never listens, one that has to be adopted after an unclean stop. A fake
process would test the fake.
"""

import json
import os
import time
from pathlib import Path

from commandcenter import config, servers, status, telegram
from tests.harness import Case

LISTENS = ("python3", "-c",
           "import os,socket,time\n"
           "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
           "s.bind(('127.0.0.1',int(os.environ['PORT'])));s.listen(5)\n"
           "time.sleep(120)")
DIES = ("python3", "-c", "raise SystemExit('no such module')")
QUIET = ("python3", "-c", "import time; time.sleep(120)")


class Declaring(Case):
    def test_no_declaration_means_no_server(self):
        rec = self.agent("box")
        self.assertIsNone(servers.declaration(rec))
        self.assertFalse(servers.ensure("box"))

    def test_a_command_written_as_one_string_is_split(self):
        rec = self.agent("box")
        self.declare(rec, [])
        (Path(rec["cwd"]) / ".platform" / "server.json").write_text(
            '{"command": "python3 api.py --port 1"}')
        self.assertEqual(servers.declaration(rec)["command"],
                         ["python3", "api.py", "--port", "1"])

    def test_a_broken_declaration_is_a_message_not_a_crash(self):
        rec = self.agent("box")
        (Path(rec["cwd"]) / ".platform").mkdir(parents=True)
        (Path(rec["cwd"]) / ".platform" / "server.json").write_text("{ nonsense")
        self.assertIsNone(servers.declaration(rec))

    def test_a_declaration_with_no_command_is_refused(self):
        rec = self.agent("box")
        (Path(rec["cwd"]) / ".platform").mkdir(parents=True)
        (Path(rec["cwd"]) / ".platform" / "server.json").write_text('{"note": "hello"}')
        self.assertIsNone(servers.declaration(rec))

    def test_a_disabled_declaration_is_not_started(self):
        rec = self.agent("box")
        self.declare(rec, LISTENS, enabled=False)
        self.assertFalse(servers.ensure("box"))


class Ports(Case):
    def test_a_port_is_assigned_once_and_kept(self):
        self.agent("box")
        first = servers.port_for("box")
        self.assertEqual(servers.port_for("box"), first, "a port that moves breaks every copy of it")
        low, high = config.PORT_RANGE
        self.assertTrue(low <= first <= high)

    def test_two_agents_get_different_ports(self):
        self.agent("one")
        self.agent("two")
        self.assertNotEqual(servers.port_for("one"), servers.port_for("two"))

    def test_an_agent_may_insist_on_a_port(self):
        self.agent("box")
        self.assertEqual(servers.port_for("box", 8177), 8177)

    def test_it_will_not_hand_out_a_port_another_agent_holds(self):
        self.agent("one")
        self.agent("two")
        servers.port_for("one", 8177)
        with self.assertRaises(ValueError):
            servers.port_for("two", 8177)

    def test_the_port_is_written_where_the_agent_can_read_it(self):
        """Answer 18: a file, because the agent outlives the process that read a variable."""
        rec = self.agent("box")
        servers._tell_agent(rec, 8123)
        written = json.loads(servers.port_path(rec).read_text())
        self.assertEqual(written["port"], 8123)
        self.assertEqual(written["url"], "http://127.0.0.1:8123")

    def test_releasing_gives_the_number_back(self):
        self.agent("box")
        port = servers.port_for("box")
        self.assertTrue(servers.release("box"))
        self.assertNotIn("box", json.loads(config.PORTS_FILE.read_text())["ports"])
        self.agent("other")
        self.assertEqual(servers.port_for("other"), port, "the number is free again")


class Starting(Case):
    def setUp(self):
        super().setUp()
        self.alerts = self.catch_alerts()
        self.addCleanup(servers.stop_all)

    def test_a_server_starts_and_is_recorded(self):
        rec = self.agent("box")
        self.declare(rec, LISTENS)
        self.assertTrue(servers.ensure("box"))
        state = servers.state("box")
        self.assertTrue(state["up"])
        self.assertIsNotNone(state["pid"])
        self.assertIsNone(state["error"])

    def test_a_command_that_exits_at_once_says_so_and_fast(self):
        rec = self.agent("box")
        self.declare(rec, DIES)
        started = time.time()
        self.assertFalse(servers.ensure("box"))
        self.assertLess(time.time() - started, 2.0,
                        "waiting the full readiness time for a dead process delays every other one")
        self.assertIn("exited at once", servers.state("box")["error"])

    def quick_ready(self):
        """A command that never listens fails the same way after half a second as after five."""
        real = servers.READY_SECONDS
        servers.READY_SECONDS = 0.5
        self.addCleanup(setattr, servers, "READY_SECONDS", real)

    def test_a_command_that_runs_but_never_listens_is_a_different_fault(self):
        self.quick_ready()
        rec = self.agent("box")
        self.declare(rec, QUIET)
        self.assertFalse(servers.ensure("box"))
        self.assertIn("nothing is listening", servers.state("box")["error"])

    def test_a_command_that_never_listens_is_stopped_and_not_left_running(self):
        """Five restart attempts must not leave five processes behind.

        The marker makes this about this test's own process and nothing else. The first version
        matched every `time.sleep(120)` on the machine and failed because an earlier run had left
        three behind, which is what the fix was for: a test that reads the whole process table is a
        test that reports other people's mess as its own.
        """
        self.quick_ready()
        rec = self.agent("box")
        token = f"cc-never-listens-{os.getpid()}"
        self.declare(rec, ("python3", "-c", f"# {token}\nimport time; time.sleep(120)"))
        servers.ensure("box")
        left = [line for line in os.popen("ps -eo args").read().splitlines() if token in line]
        self.assertEqual(left, [], "the process that would not listen is still running")

    def test_the_port_it_tried_is_visible_even_when_it_failed(self):
        rec = self.agent("box")
        self.declare(rec, DIES)
        servers.ensure("box")
        self.assertIsNotNone(servers.state("box")["port"])

    def test_a_failure_reaches_the_phone(self):
        """Answer 20: a server that will not start sends a Telegram and is diagnosed by hand."""
        rec = self.agent("box")
        self.declare(rec, DIES)
        servers.ensure("box")
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("server-box.log", self.alerts[0], "it should say where to look")

    def test_its_own_output_goes_to_its_own_log(self):
        rec = self.agent("box")
        self.declare(rec, DIES)
        servers.ensure("box")
        time.sleep(0.3)
        self.assertIn("no such module", (config.VAR / "logs" / "server-box.log").read_text())

    def test_the_command_is_given_its_port_and_its_name(self):
        rec = self.agent("box")
        marker = Path(rec["cwd"]) / "env.txt"
        self.declare(rec, ("python3", "-c",
                           f"import os,socket,time\n"
                           f"open({str(marker)!r},'w').write(os.environ['PORT']+' '"
                           f"+os.environ['PLATFORM_AGENT'])\n"
                           "s=socket.socket();s.bind(('127.0.0.1',int(os.environ['PORT'])))\n"
                           "s.listen(1);time.sleep(60)"))
        self.assertTrue(servers.ensure("box"))
        port, name = marker.read_text().split()
        self.assertEqual(int(port), servers.state("box")["port"])
        self.assertEqual(name, "box")


class Supervision(Case):
    def setUp(self):
        super().setUp()
        self.alerts = self.catch_alerts()
        self.addCleanup(servers.stop_all)

    def test_a_server_that_dies_is_restarted(self):
        rec = self.agent("box")
        self.declare(rec, LISTENS)
        servers.ensure("box")
        first = servers.state("box")["pid"]
        os.kill(first, 9)
        # Wait for the death to be observable first. A scan in the microsecond after a kill still
        # sees the process as alive, and the first version of this test broke out of its loop on
        # exactly that, then asserted against the pid it had never replaced.
        for _ in range(60):
            if not servers.running("box"):
                break
            time.sleep(0.05)
        self.assertFalse(servers.running("box"), "the kill did not take")
        for _ in range(60):
            servers._scan()
            if servers.running("box"):
                break
            time.sleep(0.1)
        self.assertTrue(servers.running("box"))
        self.assertNotEqual(servers.state("box")["pid"], first)

    def test_it_stops_trying_after_the_cap_and_says_why(self):
        rec = self.agent("box")
        self.declare(rec, DIES)
        servers.ensure("box")
        for _ in range(config.SERVER_RESTARTS + 3):
            servers._scan()
        self.assertFalse(servers.state("box")["wanted"], "it should have given up")
        self.assertIn("not restarting again", servers.state("box")["error"])
        self.assertTrue(any("not restarting again" in a for a in self.alerts))

    def test_a_deliberate_stop_is_not_restarted(self):
        rec = self.agent("box")
        self.declare(rec, LISTENS)
        servers.ensure("box")
        servers.stop("box")
        servers._scan()
        self.assertFalse(servers.running("box"))

    def test_the_daemon_stopping_keeps_the_intent_so_a_restart_brings_it_back(self):
        """Answer 19: the last configuration comes back."""
        rec = self.agent("box")
        self.declare(rec, LISTENS)
        servers.ensure("box")
        servers.stop_all()
        status.reset()
        self.assertEqual(status.servers_wanted(), ["box"])

    def test_restore_starts_what_was_wanted(self):
        rec = self.agent("box")
        self.declare(rec, LISTENS)
        servers.ensure("box")
        servers.stop_all()
        servers.reset()
        status.reset()
        self.assertEqual(servers.restore(), ["box"])
        self.assertTrue(servers.running("box"))

    def test_an_orphan_from_an_unclean_stop_is_adopted_not_fought_with(self):
        """Without this the new daemon cannot bind the port and every screen goes blank."""
        rec = self.agent("box")
        self.declare(rec, LISTENS)
        servers.ensure("box")
        pid, port = servers.state("box")["pid"], servers.state("box")["port"]
        servers.reset()                      # the daemon died without stopping anything
        self.assertTrue(servers.ensure("box"))
        self.assertEqual(servers.state("box")["pid"], pid, "it should have taken over the orphan")
        self.assertEqual(servers.state("box")["port"], port)

    def test_a_program_this_daemon_did_not_start_is_not_adopted(self):
        """Adoption has to be exact. Matching on the program name alone means any python3 qualifies.

        The bug this pins is quiet and bad: the foreign program answers on the port, so the readiness
        check would call the server up, and every screen would be reading whatever that program
        serves.
        """
        import subprocess
        rec = self.agent("box")
        self.declare(rec, LISTENS)
        port = servers.port_for("box")
        other = subprocess.Popen(
            ["python3", "-c", "import socket,time\n"
             "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
             f"s.bind(('127.0.0.1',{port}));s.listen(1);time.sleep(30)"])
        self.addCleanup(other.kill)
        for _ in range(60):
            if not servers._free(port):
                break
            time.sleep(0.05)
        # The record names the foreign pid, which is the situation adoption has to be careful about.
        status.server("box", port=port, pid=other.pid)
        self.assertFalse(servers.ensure("box"), "it must not report a stranger's program as up")
        self.assertNotEqual(servers.state("box")["pid"], other.pid)
        self.assertIn("did not start", servers.state("box")["error"])

    def test_a_server_is_not_stopped_when_its_agent_sleeps(self):
        """Answer 7, and the reason this is not simply part of waking up."""
        from commandcenter import broker
        rec = self.agent("box")
        self.declare(rec, LISTENS)
        servers.ensure("box")
        broker.sleep("box")                  # no terminal exists; nothing should touch the server
        servers._scan()
        self.assertTrue(servers.running("box"))
