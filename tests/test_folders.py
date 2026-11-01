"""Turning what a person typed into an agent's folder, and the choice to create a missing one.

The path handling is the interesting half. A relative path must not be joined onto the daemon's own
directory, and a leading ~ ahead of an absolute path is how a folder nobody can find gets made.
"""

import os

from commandcenter import config
from tests.harness import Case


class ResolveDir(Case):
    def test_a_full_path_is_kept(self):
        self.assertEqual(config.resolve_dir("/home/user/work"), "/home/user/work")

    def test_a_leading_tilde_becomes_the_home_folder(self):
        self.assertEqual(config.resolve_dir("~/work"),
                         os.path.join(os.path.expanduser("~"), "work"))

    def test_empty_means_home(self):
        self.assertEqual(config.resolve_dir("  "), os.path.expanduser("~"))
        self.assertEqual(config.resolve_dir(None), os.path.expanduser("~"))

    def test_a_relative_path_is_refused_not_joined_to_the_daemon(self):
        """"home/user/x" is not "./home/user/x" from wherever the daemon runs; it is a mistake."""
        with self.assertRaises(ValueError):
            config.resolve_dir("home/user/ergo/dad")

    def test_the_doubled_path_a_tilde_prefix_makes_is_left_visible_not_hidden(self):
        """~ still in the field ahead of an absolute path is the bug the operator hit. resolve_dir
        does exactly what ~/... means; the doubled path is real, and the console shows it to be
        refused by eye rather than silently used."""
        got = config.resolve_dir("~/home/user/ergo/dad")
        self.assertEqual(got, os.path.join(os.path.expanduser("~"), "home/user/ergo/dad"))

    def test_dots_are_collapsed(self):
        self.assertEqual(config.resolve_dir("/home/user/work/../play"), "/home/user/play")
