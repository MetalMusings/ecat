"""The diagnosis names a cable that lost its link (diagnose.lost_link_notes)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import diagnose as d  # noqa: E402


def port(n, lost):
    return d.Port(n, True, {"rx": 0, "phy": 0, "fwd": 0, "lost": lost},
                  {"rx": 0, "phy": 0, "fwd": 0, "lost": 0})


def report(pos, ports):
    return d.Report(pos, "x", False, 8, False, 0, ports=ports)


class TestLostLinks(unittest.TestCase):
    def test_same_lost_count_at_both_ends_names_the_cable(self):
        notes = d.lost_link_notes([report(0, [port(0, 255), port(1, 1)]), report(1, [port(0, 1), port(1, 0)])])
        self.assertEqual(len(notes), 1)
        self.assertIn("slave 0 port 1 and slave 1 port 0", notes[0])

    def test_saturated_or_unequal_counts_say_nothing(self):
        self.assertEqual(d.lost_link_notes([report(0, [port(1, 255)]), report(1, [port(0, 255)])]), [])
        self.assertEqual(d.lost_link_notes([report(0, [port(1, 1)]), report(1, [port(0, 2)])]), [])


if __name__ == "__main__":
    unittest.main()
