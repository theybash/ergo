"""The Memory screen's route: read only, and a view sees only the agents it shows."""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

from commandcenter import config, memories, registry, server
from tests.harness import Case


class Route(Case):
    def test_the_personal_view_sees_its_own_memory_and_never_an_agent_tagged_for_work(self):
        home = self.agent("home")
        work_dir = self.dir / "ledger"
        work_dir.mkdir(parents=True)
        work = registry.create(name="ledger", cwd=str(work_dir))
        work = registry.update(work["id"], views=["work"])
        for rec, fact in ((home, "likes tea"), (work, "client secret plan")):
            box = memories.folder(rec)
            box.mkdir(parents=True)
            (box / "MEMORY.md").write_text("- [Tea](tea.md) \u2014 they like tea\n")
            (box / "tea.md").write_text(fact)
        (self.dir / "outside.md").write_text("not a memory")

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        def get(path):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            conn.request("GET", path)
            reply = conn.getresponse()
            got = json.loads(reply.read() or b"{}")
            conn.close()
            return reply.status, got

        code, got = get("/api/memory?view=personal")
        self.assertEqual(code, 200)
        ids = [a["id"] for a in got["agents"]]
        self.assertIn(home["id"], ids)
        self.assertNotIn(work["id"], ids)
        mine = next(a for a in got["agents"] if a["id"] == home["id"])
        self.assertEqual(mine["memory"][0]["title"], "Tea")
        self.assertEqual(mine["memory"][0]["hook"], "they like tea")

        code, got = get(f"/api/memory?view=personal&agent={home['id']}&file=tea.md")
        self.assertEqual((code, got["text"]), (200, "likes tea"))
        code, _ = get(f"/api/memory?view=personal&agent={work['id']}&file=tea.md")
        self.assertEqual(code, 404)
        code, _ = get(f"/api/memory?view=personal&agent={home['id']}&file=../../../outside.md")
        self.assertEqual(code, 404)
        code, got = get("/api/memory?view=demo")
        self.assertEqual(got["agents"], [])
        self.assertTrue(str(config.PROJECTS) in str(memories.folder(home)))
