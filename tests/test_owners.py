"""Task owners: a change to a hot task wakes its owner with a report;
any other change waits for the owner's next message; the owner is not told of its own change."""

from commandcenter import clock, owners, tasks
from tests.harness import Case


class Reports(Case):
    def test_hot_wakes_far_waits_own_change_is_silent(self):
        self.agent("scout")
        sent = []
        real = owners._deliver
        owners._deliver = lambda agent, text, hot: sent.append((agent, text, hot))
        self.addCleanup(setattr, owners, "_deliver", real)
        self.addCleanup(owners.reset)
        tasks.on_change(owners.on_task)

        soon = tasks.add_mine("Send the HN comment", by="scout", due=clock.now() + 3600)
        later = tasks.add_mine("Renew the passport", by="scout", due=clock.now() + 86400 * 9)
        self.assertEqual(sent, [], "the owner made them, so it is not told")

        tasks.tick(soon["id"])
        agent, text, hot = sent[-1]
        self.assertEqual((agent, hot), ("scout", True))
        self.assertIn("ticked off by the owner", text)
        self.assertIn("Renew the passport", text, "the report carries its open slice")

        tasks.move(later["id"], clock.now() + 86400 * 10, "desk")
        self.assertEqual(sent[-1][2], False, "far off: it waits for the next message")

        tasks.dismiss(later["id"], "scout")
        self.assertEqual(len(sent), 2, "its own drop is not reported back to it")

    def test_he_picks_the_owner_when_he_adds_it(self):
        # A task the owner adds names an agent, and the home agent takes it if none.
        self.agent("scout")
        picked = tasks.add_mine("Book the dentist", due=clock.now() + 3600, owner="scout")
        plain = tasks.add_mine("Call the bank", due=clock.now() + 3600)
        self.assertEqual(picked["owner"], "scout")
        self.assertEqual(plain["owner"], owners.home())
