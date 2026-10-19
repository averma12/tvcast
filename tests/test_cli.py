import unittest

from tvcast.cli import pick

VIDEOS = [(label, label) for label in ("Alien (1979)", "Aliens (1986)", "Dune (1984)", "Dune (2021)")]


class PickTest(unittest.TestCase):
    def test_exact_label_beats_partial_matches(self):
        self.assertEqual(pick("alien", VIDEOS, "video"), "Alien (1979)")
        self.assertEqual(pick("Dune (2021)", VIDEOS, "video"), "Dune (2021)")
        self.assertEqual(pick("aliens", VIDEOS, "video"), "Aliens (1986)")

    def test_ambiguous_or_missing_exits_with_the_choices(self):
        with self.assertRaises(SystemExit) as cm:
            pick("dune", VIDEOS, "video")
        self.assertIn("Dune (1984)\n  Dune (2021)", str(cm.exception))
        with self.assertRaises(SystemExit):
            pick("arrival", VIDEOS, "video")


if __name__ == "__main__":
    unittest.main()
