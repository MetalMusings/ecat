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

    def test_the_spindle_encoder_pins(self):
        self.assertIn("index-enable pin of the spindle encoder", pindocs.info("spindle.0.index-enable").text)
        self.assertIn("revolutions", pindocs.info("spindle.1.revs").text)
        self.assertIn("handshake", pindocs.info("joint.2.index-enable").text)

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


class TestInvertPin(unittest.TestCase):
    def cfg(self):
        return {"slaves": [{"name": "io", "process-data": {"inputs": [{"pdo": "0x1A00", "entries": [
            {"object": "0x6000:00", "type": "UINT8", "bits": 8,
             "split": ["a", "b", None, "d"]},
            {"object": "0x6001:00", "type": "BOOL", "bits": 1, "pin": "single",
             "hal-type": "bool"},
            {"object": "0x6002:00", "type": "UINT32", "bits": 32, "pin": "n",
             "hal-type": "uint"}]}]}}]}

    def test_a_bit_of_a_field(self):
        c = self.cfg()
        self.assertIs(integrate.pin_inverted(c, "io.b"), False)
        self.assertTrue(integrate.set_pin_inverted(c, "io.d", True))
        self.assertTrue(integrate.set_pin_inverted(c, "io.b", True))
        e = c["slaves"][0]["process-data"]["inputs"][0]["entries"][0]
        self.assertEqual(e["invert"], ["b", "d"])               # in bit order
        self.assertIs(integrate.pin_inverted(c, "io.b"), True)
        self.assertIs(integrate.pin_inverted(c, "io.a"), False)
        integrate.set_pin_inverted(c, "io.b", False)
        integrate.set_pin_inverted(c, "io.d", False)
        self.assertNotIn("invert", e)

    def test_a_single_bool_pin(self):
        c = self.cfg()
        integrate.set_pin_inverted(c, "io.single", True)
        e = c["slaves"][0]["process-data"]["inputs"][0]["entries"][1]
        self.assertIs(e["invert"], True)
        self.assertIs(integrate.pin_inverted(c, "io.single"), True)
        integrate.set_pin_inverted(c, "io.single", False)
        self.assertNotIn("invert", e)

    def test_not_a_bit_pin(self):
        c = self.cfg()
        self.assertIsNone(integrate.pin_inverted(c, "io.n"))
        self.assertFalse(integrate.set_pin_inverted(c, "io.n", True))
        self.assertIsNone(integrate.pin_inverted(c, "io.nothing"))
