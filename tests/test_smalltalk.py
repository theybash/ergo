"""Small talk while an agent works: the pack, the pick, and the words.

The network is never reached. `smalltalk.get` is one function for that reason, and each test that
fetches puts a stand in there.
"""

import json
import random

from commandcenter import clock, config, smalltalk
from tests.harness import Case

RSS = b"""<rss><channel>
<item><title>Octopuses dream in colour</title></item>
<item><title>STAT+: A paywalled story</title></item>
<item><title>A second  story\n spread over lines</title></item>
</channel></rss>"""

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>An Atom story</title></entry></feed>"""


class Pack(Case):
    def setUp(self):
        super().setUp()
        real = smalltalk.get
        self.addCleanup(lambda: setattr(smalltalk, "get", real))

    def serve(self, broken=()):
        def fake(url):
            if any(b in url for b in broken):
                raise OSError("down")
            if url == smalltalk.HN_TOP:
                return json.dumps([1, 2]).encode()
            if "item/" in url:
                return json.dumps({"title": "Show HN: a tiny compiler " + url[-6]}).encode()
            if "quanta" in url:
                return ATOM
            return RSS
        smalltalk.get = fake

    def test_a_refresh_gathers_every_source_and_skips_the_paywall(self):
        self.serve()
        pack = smalltalk.refresh(force=True)
        titles = [it["title"] for it in pack["items"]]
        self.assertIn("Octopuses dream in colour", titles)
        self.assertIn("A second story spread over lines", titles)
        self.assertIn("An Atom story", titles)
        self.assertFalse(any(t.startswith("STAT+") for t in titles))
        self.assertEqual(sum(1 for it in pack["items"] if it["kind"] == "hn"), 2)

    def test_a_source_that_fails_leaves_the_others(self):
        self.serve(broken=("arstechnica", "firebaseio"))
        pack = smalltalk.refresh(force=True)
        self.assertTrue(pack["items"])
        self.assertFalse(any(it["kind"] == "hn" for it in pack["items"]))

    def test_a_fresh_pack_is_not_fetched_again(self):
        self.serve()
        smalltalk.refresh(force=True)
        smalltalk.get = lambda url: self.fail("fetched a fresh pack")
        smalltalk.refresh()

    def test_the_switch_stops_the_background_fetch(self):
        config.SMALLTALK = False
        smalltalk.get = lambda url: self.fail("fetched with small talk off")
        smalltalk.refresh_soon()


class Pick(Case):
    def test_an_empty_pack_still_has_a_joke(self):
        got = smalltalk.line()
        self.assertEqual(got["kind"], "joke")
        self.assertIn("joke", got["say"])

    def test_a_story_is_said_with_its_source_and_not_twice_in_a_week(self):
        smalltalk._save({"at": clock.now(), "told": {},
                         "items": [{"kind": "hn", "source": "Hacker News", "title": "Rust in orbit"}]})
        rng = random.Random(1)
        first = smalltalk.pick(rng)
        self.assertEqual(first["title"], "Rust in orbit")
        again = smalltalk.pick(rng)
        self.assertEqual(again["kind"], "joke")

    def test_the_words_say_by_the_way_and_the_source(self):
        smalltalk._save({"at": clock.now(), "told": {},
                         "items": [{"kind": "tech", "source": "Ars Technica", "title": "A chip"}]})
        real = smalltalk.random.randrange
        smalltalk.random.randrange = lambda n: 1
        self.addCleanup(lambda: setattr(smalltalk.random, "randrange", real))
        got = smalltalk.line()
        self.assertIn("did you hear", got["say"])
        self.assertIn("Ars Technica", got["say"])
        self.assertIn("A chip", got["say"])
