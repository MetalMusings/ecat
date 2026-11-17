"""LinuxCNC 2.9 and 2.10: the series in use (lcnc_env) and what depends on it
(config.check, propose, halcmd's output in testmode)."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
# the tests are written for 2.10 unless they say otherwise (lcnc_env.series)
os.environ.setdefault("ECATSETUP_LINUXCNC_SERIES", "2.10")

from ecatsetup import coe, config, esi, lcnc_env, propose, sii, testmode  # noqa: E402
from test_lcnc_env import fake_tree  # noqa: E402
from test_prototype import ENCODER_ESI  # noqa: E402


def generic(entry, kind="inputs"):
    return {"ecat": 1, "master": {}, "slaves": [{
        "position": 0, "name": "s", "identity": {"vendor": "0x2", "product": "0x1"},
        "profile": "generic",
        "process-data": {kind: [{"pdo": "0x1A00", "sm": 3, "entries": [dict(
            {"object": "0x6000:01", "bits": 32, "type": "UDINT", "pin": "c",
             "hal-type": "sint"}, **entry)]}]}}]}


class TestSeries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {k: v for k, v in os.environ.items()
               if k not in ("EMC2_HOME", "ECATSETUP_LINUXCNC", "ECATSETUP_LINUXCNC_SERIES")}
        env["ECATSETUP_SETTINGS"] = os.path.join(self.tmp.name, "settings.yaml")
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        lcnc_env._versions.clear()
        self.addCleanup(lcnc_env._versions.clear)

    def test_parse(self):
        self.assertEqual(lcnc_env.parse_series("2.9.4"), (2, 9))
        self.assertEqual(lcnc_env.parse_series("2.10.0~pre2"), (2, 10))
        self.assertEqual(lcnc_env.parse_series("2.11"), (2, 11))
        self.assertIsNone(lcnc_env.parse_series(""))
        self.assertIsNone(lcnc_env.parse_series(None))
        self.assertIsNone(lcnc_env.parse_series("master"))

    def test_from_the_run_in_place_tree(self):
        root = os.path.realpath(os.path.join(self.tmp.name, "rip"))
        fake_tree(root)
        with open(os.path.join(root, "VERSION"), "w") as f:
            f.write("2.9.7\n")
        with mock.patch.dict(os.environ, {"ECATSETUP_LINUXCNC": root}):
            self.assertEqual(lcnc_env.version(), "2.9.7")
            self.assertEqual(lcnc_env.series(), (2, 9))
            self.assertFalse(lcnc_env.wide_pins())
            self.assertIn("LinuxCNC 2.9.7", lcnc_env.describe())

    def test_from_the_installed_linuxcnc(self):
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "linuxcnc_var"), "w") as f:
            f.write('#!/bin/sh\n[ "$1" = LINUXCNCVERSION ] && echo 2.9.4\n')
        os.chmod(os.path.join(bindir, "linuxcnc_var"), 0o755)
        with mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ.get("PATH", "")}):
            self.assertEqual(lcnc_env.series(), (2, 9))
            self.assertEqual(lcnc_env.describe(), "installed LinuxCNC 2.9.4")

    def test_unknown_is_2_10_and_the_variable_wins(self):
        with mock.patch.dict(os.environ, {"PATH": self.tmp.name}):  # no linuxcnc_var
            self.assertIsNone(lcnc_env.version())
            self.assertEqual(lcnc_env.series(), (2, 10))
            self.assertTrue(lcnc_env.wide_pins())
            with mock.patch.dict(os.environ, {"ECATSETUP_LINUXCNC_SERIES": "2.9"}):
                self.assertEqual(lcnc_env.series(), (2, 9))
        self.assertTrue(lcnc_env.wide_pins((2, 10)))
        self.assertFalse(lcnc_env.wide_pins((2, 9)))


class TestCheck(unittest.TestCase):
    """The component's own rules for 2.9 (component/ecat_config.c), in step."""

    def test_64_bit_entries_take_real_on_2_9(self):
        for type_, hal in (("LINT", "sint"), ("ULINT", "uint")):
            cfg = generic({"bits": 64, "type": type_, "hal-type": hal})
            self.assertEqual(config.check(cfg, (2, 10)), [])
            errors = config.check(cfg, (2, 9))
            self.assertTrue(any("LinuxCNC 2.9 has no 64-bit pins: a %s entry takes hal-type real" % type_
                                in e for e in errors), errors)
            cfg = generic({"bits": 64, "type": type_, "hal-type": "real"})
            self.assertEqual(config.check(cfg, (2, 9)), [])

    def test_extend_is_for_16_bits_on_2_9(self):
        cfg = generic({"extend": True})
        self.assertEqual(config.check(cfg, (2, 10)), [])
        self.assertTrue(any("LinuxCNC 2.9's pins have 32 bits: extend is for an entry of 8 to 16 bits there"
                            in e for e in config.check(cfg, (2, 9))))
        cfg = generic({"bits": 16, "type": "UINT", "extend": True})
        self.assertEqual(config.check(cfg, (2, 9)), [])

    def test_the_series_in_use_by_default(self):
        cfg = generic({"bits": 64, "type": "LINT", "hal-type": "sint"})
        with mock.patch.dict(os.environ, {"ECATSETUP_LINUXCNC_SERIES": "2.9"}):
            self.assertTrue(any("2.9 has no 64-bit pins" in e for e in config.check(cfg)))
        with mock.patch.dict(os.environ, {"ECATSETUP_LINUXCNC_SERIES": "2.10"}):
            self.assertEqual(config.check(cfg), [])


class TestPropose(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "enc.xml")
            with open(path, "w") as f:
                f.write(ENCODER_ESI)
            self.dev = esi.load_file(path)[0]
        self.sii = sii.Sii(2, 0x13ED3052, 0x03FF0000, 0, 0, name="EL5101")

    def proposal(self, series, pdo_set=None):
        with mock.patch.dict(os.environ, {"ECATSETUP_LINUXCNC_SERIES": series}):
            p = propose.propose({"position": 3, "dc": False}, self.sii, coe.Dictionary(),
                                esi_dev=self.dev, esi_exact=True, pdo_set=pdo_set)
            return p, config.check({"ecat": 1, "master": {}, "slaves": [config.slave_from_proposal(p)]})

    def test_a_16_bit_counter_is_extended_to_32_bits_on_2_9(self):
        p, errors = self.proposal("2.9")
        counters = [e for pd in p.pdos for e in pd.entries if e.role == "pin" and e.extend]
        self.assertEqual([(e.bitlen, e.hal_type) for e in counters], [(16, "sint")])
        self.assertEqual(len([n for n in p.notes if "16-bit counter, extended to 32 bits" in n]), 1)
        self.assertEqual(errors, [])
        p, _ = self.proposal("2.10")
        self.assertEqual(len([n for n in p.notes if "16-bit counter, extended to 64 bits" in n]), 1)

    def test_64_bit_entries_are_real_pins_on_2_9(self):
        coe_dict = coe.Dictionary()
        s = sii.Sii(2, 0x2, 0x1, 0, 0, name="Big")
        s.pdos = [sii.Pdo(0x1A00, "tx", 3, "In", False, True, [sii.PdoEntry(0x6000, 1, "Position", "LINT", 64),
                                                   sii.PdoEntry(0x6000, 2, "Count", "ULINT", 64)])]
        for series, want in (("2.9", ["real", "real"]), ("2.10", ["sint", "uint"])):
            with mock.patch.dict(os.environ, {"ECATSETUP_LINUXCNC_SERIES": series}):
                p = propose.propose({"position": 0, "dc": False}, s, coe_dict)
                self.assertEqual([e.hal_type for pd in p.pdos for e in pd.entries], want)
                cfg = {"ecat": 1, "master": {}, "slaves": [config.slave_from_proposal(p)]}
                self.assertEqual([e for e in config.check(cfg) if "hal-type" in e], [])


    def test_a_32_bit_counter_wraps_as_the_pin_on_2_9(self):
        s = sii.Sii(2, 0x2, 0x1, 0, 0, name="Counter")
        s.pdos = [sii.Pdo(0x1A00, "tx", 3, "In", False, True,
                          [sii.PdoEntry(0x6000, 1, "Counter value", "UDINT", 32)])]
        for series, extend in (("2.9", False), ("2.10", True)):
            with mock.patch.dict(os.environ, {"ECATSETUP_LINUXCNC_SERIES": series}):
                p = propose.propose({"position": 0, "dc": False}, s, coe.Dictionary())
                e = p.pdos[0].entries[0]
                self.assertEqual((e.hal_type, e.extend), ("sint", extend), series)
                cfg = {"ecat": 1, "master": {}, "slaves": [config.slave_from_proposal(p)]}
                self.assertEqual(config.check(cfg), [])


class TestHalcmdOutput(unittest.TestCase):
    def test_2_9_names_and_hex(self):
        pins = testmode.parse_pins(
            "    4  bit   OUT          TRUE  ecat.0.all-op\n"
            "    4  u32   OUT    0x0007DF1B  ecat.0.shift-time\n"
            "    4  s32   OUT             -2  ecat.0.x\n"
            "    4  float RW              1  lcec.0.X.pos-scale\n")
        self.assertEqual((pins["ecat.0.all-op"].type, pins["ecat.0.all-op"].value), ("bool", "TRUE"))
        self.assertEqual((pins["ecat.0.shift-time"].type, pins["ecat.0.shift-time"].value), ("uint", "515867"))
        self.assertEqual((pins["ecat.0.x"].type, pins["ecat.0.x"].value), ("sint", "-2"))
        self.assertEqual(pins["lcec.0.X.pos-scale"].type, "real")

    def test_2_10_unchanged(self):
        pins = testmode.parse_pins("ecat uint OUT 515867 ecat.0.shift-time\necat bool IN TRUE a.b\n")
        self.assertEqual((pins["ecat.0.shift-time"].type, pins["ecat.0.shift-time"].value), ("uint", "515867"))
        self.assertEqual(pins["a.b"].type, "bool")


if __name__ == "__main__":
    unittest.main()
