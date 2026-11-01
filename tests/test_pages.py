"""The screens an agent has written: how they are found, titled, ordered and capped."""

from commandcenter import config
from tests.harness import Case


class Pages(Case):
    def write(self, agent_id, *names):
        folder = config.VIEWS / agent_id
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_text("export function render(){}")
        return folder

    def test_no_folder_means_no_pages(self):
        self.assertEqual(config.pages("box"), [])
        self.assertIsNone(config.default_page("box"))

    def test_a_file_becomes_a_page(self):
        self.write("box", "positions.js")
        page = config.pages("box")[0]
        self.assertEqual(page["slug"], "positions")
        self.assertEqual(page["title"], "Positions")

    def test_the_agents_own_view_comes_first(self):
        self.write("box", "aaa.js", "view.js", "zzz.js")
        self.assertEqual([p["slug"] for p in config.pages("box")], ["view", "aaa", "zzz"])

    def test_a_number_prefix_orders_and_is_not_shown(self):
        self.write("box", "20-pnl.js", "10-positions.js")
        pages = config.pages("box")
        self.assertEqual([p["slug"] for p in pages], ["positions", "pnl"])
        self.assertEqual(pages[0]["title"], "Positions")

    def test_a_dash_becomes_a_space_in_the_title(self):
        self.write("box", "research-log.js")
        self.assertEqual(config.pages("box")[0]["title"], "Research log")

    def test_files_that_are_not_pages_are_ignored(self):
        self.write("box", "view.js", "notes.txt", "Broken Name.js", "UPPER.js")
        self.assertEqual([p["slug"] for p in config.pages("box")], ["view"])

    def test_too_many_pages_are_capped_and_the_cap_is_reported(self):
        """An agent writes files, so an agent in a loop can write five hundred."""
        self.write("box", *[f"p{n:03}.js" for n in range(config.MAX_PAGES + 6)])
        pages = config.pages("box")
        self.assertEqual(len(pages), config.MAX_PAGES)
        self.assertEqual(pages[-1]["truncated"], config.MAX_PAGES + 6,
                         "a dropped tab that nothing mentions reads as a page never written")

    def test_the_agent_chooses_which_page_opens(self):
        folder = self.write("box", "10-positions.js", "20-pnl.js")
        (folder / "default").write_text("pnl\n")
        self.assertEqual(config.default_page("box"), "pnl")

    def test_a_default_naming_nothing_falls_back_rather_than_breaking(self):
        folder = self.write("box", "positions.js")
        (folder / "default").write_text("nonsense")
        self.assertEqual(config.default_page("box"), "positions")

    def test_a_slug_is_resolved_against_the_listing_never_joined_onto_a_path(self):
        self.write("box", "view.js")
        self.assertIsNone(config.page_file("box", "../../../etc/passwd"))
        self.assertIsNotNone(config.page_file("box", "view"))
