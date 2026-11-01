"""The event stream sends what changed, when it changed, and nothing while nothing does."""

import http.client
import json
import socket
import threading
import time
import unittest
import zlib

from commandcenter import server
from tests import test_voice


class Diff(unittest.TestCase):
    def test_nothing_changed_is_none(self):
        s = {"agents": [{"id": "a", "working": False}], "jobs": [], "inbox": {"untold": 0}}
        self.assertIsNone(server.state_diff(s, json.loads(json.dumps(s))))

    def test_one_field_of_one_agent_goes_alone(self):
        old = {"agents": [{"id": "a", "working": False, "last_message": "x" * 500},
                          {"id": "b", "working": False}]}
        new = {"agents": [{"id": "a", "working": True, "last_message": "x" * 500},
                          {"id": "b", "working": False}]}
        self.assertEqual(server.state_diff(old, new), {"agents": {"a": {"working": True}}})

    def test_a_new_agent_goes_whole_and_a_gone_one_as_none_with_the_order(self):
        old = {"agents": [{"id": "a", "n": 1}, {"id": "b", "n": 2}]}
        new = {"agents": [{"id": "c", "n": 3}, {"id": "a", "n": 1}]}
        got = server.state_diff(old, new)
        self.assertEqual(got["agents"], {"c": {"id": "c", "n": 3}, "b": None})
        self.assertEqual(got["agents_order"], ["c", "a"])

    def test_a_field_that_went_away_is_sent_as_none(self):
        got = server.state_diff({"jobs": [{"id": "j", "x": 1}]}, {"jobs": [{"id": "j"}]})
        self.assertEqual(got, {"jobs": {"j": {"x": None}}})

    def test_whole_keys_go_whole_when_changed(self):
        got = server.state_diff({"inbox": {"untold": 0}, "hidden": []},
                                {"inbox": {"untold": 1}, "hidden": []})
        self.assertEqual(got, {"inbox": {"untold": 1}})


class Hub(unittest.TestCase):
    def test_a_client_gets_only_its_topics(self):
        hub = server.EventHub()
        phone = hub.add({"phone"})
        old = hub.add()
        hub.publish("agents")
        hub.publish("health", {"cpu": 1})
        hub.publish("phone", {"x": 1}, topic="phone")
        self.assertEqual(phone.get_nowait(), ("phone", {"x": 1}))
        self.assertTrue(phone.empty())
        self.assertEqual([old.get_nowait()[0] for _ in range(2)], ["agents", "health"])
        self.assertTrue(old.empty())

    def test_health_is_wanted_only_when_someone_asks(self):
        hub = server.EventHub()
        hub.add({"state"})
        self.assertFalse(hub.wants("health"))
        hub.add({"state", "health"})
        self.assertTrue(hub.wants("health"))


class Reader:
    """One open stream, read frame by frame, through one gzip decoder when it is gzipped."""

    def __init__(self, port, topics, gz):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=0.3)
        self.sock.sendall((f"GET /api/events?topics={topics} HTTP/1.1\r\nHost: x\r\n"
                           + ("Accept-Encoding: gzip\r\n" if gz else "") + "\r\n").encode())
        self.unpack = zlib.decompressobj(31) if gz else None
        self.head, self.body, self.text, self.wire = b"", False, "", 0

    def frames(self, want, seconds=3):
        end = time.time() + seconds
        while time.time() < end and (not self.body or self.text.count("\n\nevent:") + self.text.startswith("event:") < want):
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            if not self.body:
                self.head += chunk
                if b"\r\n\r\n" not in self.head:
                    continue
                self.head, chunk = self.head.split(b"\r\n\r\n", 1)
                self.body = True
            self.wire += len(chunk)
            self.text += self.unpack.decompress(chunk).decode() if self.unpack else chunk.decode()
        out = []
        for f in self.text.split("\n\n"):
            if f.startswith("event:"):
                name, data = f.split("\n", 1)
                out.append((name[7:], json.loads(data[6:])))
        self.text = ""
        return out

    def close(self):
        self.sock.close()


class Stream(test_voice.Routes):
    def reader(self, topics, gz=True):
        r = Reader(self.port, topics, gz)
        self.addCleanup(r.close)
        return r

    def test_a_state_client_gets_the_whole_state_first_then_nothing_while_nothing_changes(self):
        r = self.reader("state")
        got = r.frames(1)
        self.assertIn(b"Content-Encoding: gzip", r.head)
        self.assertEqual(got[0][0], "full")
        self.assertIn("agents", got[0][1])
        self.assertNotIn("health", got[0][1])
        server.feed.look()
        self.assertEqual(r.frames(1, seconds=0.5), [])

    def test_a_phone_client_hears_an_alarm_at_once(self):
        from commandcenter import phone
        r = self.reader("phone")
        first = r.frames(1)
        self.assertEqual(first[0], ("phone", first[0][1]))
        self.assertEqual(first[0][1]["commands"], [])
        phone.alarm(7, 15, "gym")
        server.feed.look()
        # The first digest can come twice, once from the look that opened the stream. The phone
        # compares digests, so a repeat changes nothing there.
        got = [d for e, d in r.frames(2, seconds=1) if e == "phone" and d["commands"]]
        self.assertEqual([c["hour"] for c in got[-1]["commands"]], [7])

    def test_an_old_console_still_gets_its_pings(self):
        r = self.reader("", gz=False)
        r.frames(0, seconds=0.3)
        server.hub.publish("agents")
        self.assertEqual(r.frames(1)[0][0], "agents")


class Json(test_voice.Routes):
    def test_a_large_answer_is_gzipped_for_a_client_that_takes_it(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/state", headers={"Accept-Encoding": "gzip"})
        reply = conn.getresponse()
        body = reply.read()
        conn.close()
        if reply.getheader("Content-Encoding") == "gzip":
            body = zlib.decompress(body, 31)
        else:
            self.assertLessEqual(len(body), 1024)
        self.assertIn("agents", json.loads(body))

    def test_a_client_that_does_not_take_gzip_gets_plain_json(self):
        code, got = self.hit("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertIn("agents", got)


if __name__ == "__main__":
    unittest.main()
