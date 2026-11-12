"""pindocs.py: the text and links for LinuxCNC's pins in "Still to connect"."""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import integrate, pindocs  # noqa: E402


class TestPinDocs(unittest.TestCase):
    def test_plasmac_pin(self):
        d = pindocs.info("plasmac.breakaway")
        self.assertIn("breakaway", d.text)
        self.assertEqual(d.page_url, "https://linuxcnc.org/docs/html/plasma/qtplasmac.html")
        self.assertTrue(d.pin_url.startswith(d.page_url + "#"))
        self.assertEqual(d.title, "QtPlasmaC")

    def test_numbered_pin(self):
        self.assertIn("M66", pindocs.info("motion.digital-in-03").text)
        self.assertIn("home switch", pindocs.info("joint.2.home-sw-in").text)

    def test_own_component_has_none(self):
        self.assertIsNone(pindocs.info("mycomp.0.in"))
        self.assertIsNone(pindocs.info("plasmac.breakaway-extra"))

    def test_every_pattern_names_a_page(self):
        for _, key, _ in pindocs.PATTERNS:
            self.assertIn(key, pindocs.PAGES)

    def test_known_pins_with_a_text_are_in_known_pins(self):
        """Each documented pattern belongs to a pin the card can list."""
        for pat, _, _ in pindocs.PATTERNS:
            example = re.sub(r"\\d\+", "0", pat.replace("\\d\\d", "00")).replace("\\", "")
            if "(" in example:
                continue
            self.assertIsNotNone(integrate.known_pin(example), example)


if __name__ == "__main__":
    unittest.main()
