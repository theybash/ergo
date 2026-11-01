"""The feed (The owner, 21 October 2026): agents post typed parts, the app draws each part, and he
marks a post done or skipped."""

from commandcenter import clock, posts
from tests.harness import Case


class Posts(Case):
    def test_a_post_keeps_its_parts_drops_a_bad_link_and_takes_his_mark(self):
        self.agent("scout")
        self.addCleanup(posts.reset)
        heard = []
        posts.on_change(lambda event, payload: heard.append(payload["post"]["id"]))
        got = posts.post("scout", "Weak proof.", kind="HN brief",
                         link={"title": "Fake stats", "url": "javascript:alert(1)", "meta": "326"},
                         draft={"to": "m0rde", "text": "fair question."})
        self.assertEqual(got["link"], {"title": "Fake stats", "meta": "326"}, "no unsafe url")
        self.assertEqual(got["draft"]["text"], "fair question.")
        self.assertEqual(heard, [got["id"]], "pushed on the stream")
        later = posts.post("scout", "One pick.", card={"headline": "Founder's Office"})
        self.assertEqual([p["id"] for p in posts.listing()["posts"]], [later["id"], got["id"]])
        self.assertEqual(posts.mark(got["id"], "done")["state"], "done")
        posts.reset()
        self.assertEqual(posts.listing()["posts"][1]["state"], "done", "read back from the file")
        with self.assertRaises(ValueError):
            posts.post("nobody", "x")

    def test_the_feed_can_ask_for_only_the_last_three_days(self):
        """29 October 2026: "don't load the entire feed at once... you can load three days"."""
        self.agent("scout")
        self.addCleanup(posts.reset)
        self.addCleanup(clock.unfreeze)
        clock.freeze(1_000_000)
        old = posts.post("scout", "Four days old.")
        clock.advance(4 * 86400)
        new = posts.post("scout", "Today.")
        self.assertEqual([p["id"] for p in posts.listing(100, 3)["posts"]], [new["id"]])
        self.assertEqual(len(posts.listing()["posts"]), 2, "without days, all of them")
        self.assertNotEqual(old["id"], new["id"])

    def test_older_days_load_page_by_page(self):
        """30 October 2026: "I can't scroll farther than 3 days?" Each page says where the next
        starts, skips empty days, and says when there is nothing older."""
        self.agent("scout")
        self.addCleanup(posts.reset)
        self.addCleanup(clock.unfreeze)
        clock.freeze(1_000_000)
        oldest = posts.post("scout", "Ten days old.")
        clock.advance(10 * 86400)
        posts.post("scout", "Today.")
        first = posts.listing(100, 3)
        self.assertTrue(first["more"])
        last = posts.listing(100, 3, first["next"])
        self.assertEqual([p["id"] for p in last["posts"]], [oldest["id"]], "the empty week is skipped")
        self.assertFalse(last["more"])

    def test_media_is_kept_safe_and_a_link_gets_its_page_picture(self):
        self.agent("scout")
        self.addCleanup(posts.reset)
        real_fetch, real_spawn = posts._fetch_page, posts._spawn
        posts._fetch_page = lambda url: ('<head><meta content="/og/card.png?a=1&amp;b=2" '
                                         'property="og:image"></head>')
        posts._spawn = lambda fn, *a: fn(*a)
        self.addCleanup(setattr, posts, "_fetch_page", real_fetch)
        self.addCleanup(setattr, posts, "_spawn", real_spawn)
        got = posts.post("scout", "Look.", link={"title": "A page", "url": "https://ex.com/p"},
                         media=[{"url": "https://ex.com/a.jpg"}, {"file": "chart.png", "alt": "c"},
                                {"file": "../../etc/passwd.png"}, {"url": "javascript:x.mp4"},
                                {"file": "clip.mp4"}])
        self.assertEqual(got["media"], [{"kind": "image", "url": "https://ex.com/a.jpg"},
                                        {"kind": "image", "file": "chart.png", "alt": "c"},
                                        {"kind": "video", "file": "clip.mp4"}])
        self.assertEqual(posts.listing()["posts"][0]["link"]["image"],
                         "https://ex.com/og/card.png?a=1&b=2", "the page's own picture")

    def test_a_post_from_a_job_run_keeps_the_job_and_only_a_transcript_in_var(self):
        from commandcenter import config
        self.agent("scout")
        self.addCleanup(posts.reset)
        run = str(config.VAR / "job-runs" / "abc-20261021-090000.jsonl")
        got = posts.post("scout", "A pick.", job="abc", transcript=run)
        self.assertEqual((got["job"], got["transcript"]), ("abc", run))
        self.assertNotIn("transcript", posts.post("scout", "x", transcript="/etc/passwd.jsonl"))

    def test_a_link_file_is_kept_only_for_a_markdown_file_in_its_own_outbox(self):
        from commandcenter import files, registry
        self.agent("scout")
        self.agent("nova")
        self.addCleanup(posts.reset)
        box = files.outbox(registry.get("scout"))
        box.mkdir(parents=True, exist_ok=True)
        (box / "brief.md").write_text("# Brief\n")
        (box / "notes.txt").write_text("x")
        got = posts.post("scout", "Read.", link={"title": "Brief", "file": "brief.md"})
        self.assertEqual(got["link"], {"title": "Brief", "file": "brief.md"})
        for bad in ("notes.txt", "gone.md", "../nova/brief.md"):
            self.assertNotIn("file", posts.post("scout", "x", link={"title": "t", "file": bad})["link"])
        self.assertNotIn("link", posts.post("nova", "x", link={"file": "brief.md"}), "not its outbox")
