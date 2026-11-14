"""Tests of what a slave can do with distributed clocks (dcinfo.py), the
chip's DC as the tool reports it, and the window's Sync row.

The slaves are those on turner's bus (2026-11-14): an EL2008 whose chip only
measures delays, an EL1008 with a 64-bit chip and no DC mode, an EL5101 with
DC modes 0x0320, an ECT60 drive with DC 0x0300 and free run.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
if "ECATSETUP_TEST_STATE" not in os.environ:
    # one folder for the whole run: the state, and every temporary file of the
    # tests and of what they start (TMPDIR); removed when the run ends
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = os.environ["TMPDIR"] = \
        __import__("tempfile").tempdir = __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
    __import__("atexit").register(__import__("shutil").rmtree, os.environ["ECATSETUP_TEST_STATE"], True)

from ecatsetup import dcinfo, esi, sii as sii_mod, tool  # noqa: E402

M = dcinfo.Mode
EL5101_ESI = [M("Synchron", "FreeRun/SM-Synchron", 0, 0, 0, "ESI"),
              M("DC", "DC-Synchron", 0x320, 1, 0, "ESI"),
              M("DCIN", "DC-Synchron (input based)", 0x320, 1, 0, "ESI")]
EL5101_SII = [M("Synchron", "", 0, 0, 0, "SII"), M("DC", "", 0x320, 1, 0, "SII"),
              M("DCIN", "", 0x320, 1, 0, "SII")]
ECT60_SII = [M("DC", "", 0x300, 1, 0, "SII"), M("Synchron", "", 0, 1, 0, "SII")]

VERBOSE_EL2008 = """=== Master 0, Slave 2 ===
Device: Main
State: PREOP
DL information:
  FMMU bit operation: no
  Distributed clocks: yes, delay measurement only
  DC system time transmission delay: 290 ns
General:
  Order number: EL2008
"""
VERBOSE_EL5101 = """=== Master 0, Slave 3 ===
State: PREOP
DL information:
  Distributed clocks: yes, 64 bit
Mailboxes:
  Bootstrap RX: 0x1000/244, TX: 0x10f4/244
  Supported protocols: CoE, FoE
"""


def el2008():
    return dcinfo.DcInfo(clock="delay", sii_modes=[], esi_modes=[], esi_exact=True,
                         esi_file="Beckhoff EL2xxx.xml")


def el1008():
    return dcinfo.DcInfo(clock="64", sii_modes=[], esi_modes=[], esi_exact=True,
                         esi_file="Beckhoff EL1xxx.xml")


def el5101(**kw):
    d = dcinfo.DcInfo(clock="64", sii_modes=list(EL5101_SII), esi_modes=list(EL5101_ESI),
                      esi_exact=True, esi_file="Beckhoff EL5xxx.xml",
                      sync=[dcinfo.SyncParams(0x1C32, 0xC807, 66000),
                            dcinfo.SyncParams(0x1C33, 0xC807, 66000)])
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def ect60():
    # the ECT60's 0x1C32:05 is 0xE8480000 (3.9 s): not a minimum cycle time
    return dcinfo.DcInfo(clock="64", sii_modes=list(ECT60_SII),
                         sync=[dcinfo.SyncParams(0x1C32, 0x401F, 0xE8480000)])


def slave(dc=None, device="EL2008 8K. Dig. Ausgang 24V, 0.5A"):
    s = {"position": 2, "name": "slave-2", "device": device}
    if dc is not None:
        s["dc"] = {"assign-activate": dc, "sync0-cycles": 1, "sync0-shift-ns": 0}
    return s


class TestChip(unittest.TestCase):
    def test_dc_clock(self):
        for text, want in ((VERBOSE_EL2008, "delay"), (VERBOSE_EL5101, "64"),
                           ("  Distributed clocks: yes, 32 bit\n", "32"),
                           ("  Distributed clocks: no\n", "none"), ("State: OP\n", None)):
            self.assertEqual(tool.dc_clock(text), want, text)

    def test_slave_info(self):
        foe_only = VERBOSE_EL5101.replace("CoE, FoE", "FoE")
        for text, dc, coe in ((VERBOSE_EL2008, False, False), (VERBOSE_EL5101, True, True),
                              (foe_only, True, False)):
            with mock.patch.object(tool, "run", lambda *a, t=text, **k: t):
                info = tool.slave_info(2)
            # "yes, delay measurement only" is not a DC clock
            self.assertEqual((info["dc"], info["coe"]), (dc, coe))


class TestModes(unittest.TestCase):
    def test_the_best_source(self):
        d = el5101()
        self.assertEqual([m.source for m in d.modes()], ["ESI"] * 3)   # ESI of the revision
        d.esi_exact = False
        self.assertEqual([m.source for m in d.modes()], ["SII"] * 3)   # the slave's own
        d.sii_modes = []
        self.assertEqual([m.source for m in d.modes()], ["ESI"] * 3)   # another revision's
        self.assertIn("another revision", d.source())
        self.assertEqual(dcinfo.DcInfo().modes(), [])

    def test_dc_modes_once(self):
        self.assertEqual([m.label() for m in el5101().dc_modes()],
                         ["DC (DC-Synchron) 0x0320", "DCIN (DC-Synchron (input based)) 0x0320"])
        self.assertEqual(el2008().dc_modes(), [])

    def test_build_from_what_was_read(self):
        sii = sii_mod.Sii(2, 0x13ED3052, 0x03FF0000, 0, 4,
                          dc_modes=[sii_mod.DcMode("DC", 0x320, 0, 0, 1)])
        dev = esi.EsiDevice("/esi/Beckhoff EL5xxx.xml", 2, 0x13ED3052, 0x03FF0000, "EL5101", "x",
                            dc_modes=[esi.EsiDcMode("DC", "DC-Synchron", 0x320, 1, 0)])
        d = dcinfo.build({"dc": True, "dc_clock": "64"}, sii, dev, True)
        self.assertEqual((d.clock, d.esi_file, d.esi_exact), ("64", "Beckhoff EL5xxx.xml", True))
        self.assertEqual([(m.name, m.assign, m.source) for m in d.modes()], [("DC", 0x320, "ESI")])
        # an older info without dc_clock
        self.assertEqual(dcinfo.build({"dc": True}).clock, "64")

    def test_lines(self):
        text = "\n".join(el2008().lines())
        self.assertIn("Chip: delay measurement only", text)
        self.assertIn("SII: no DC category", text)
        self.assertIn("ESI Beckhoff EL2xxx.xml: no DC modes", text)
        text = "\n".join(ect60().lines())
        self.assertIn("CoE 0x1C3x:04 = 0x401F: free run, SM synchronous, DC", text)
        self.assertIn("minimum cycle 3.9 s (not plausible, ignored)", text)
        self.assertIn("minimum cycle 66 us", "\n".join(el5101().lines()))


class TestProblems(unittest.TestCase):
    def found(self, s, d, period=None):
        return dcinfo.problems(s, d, period)

    def test_a_chip_without_a_dc_clock(self):
        [(level, text)] = self.found(slave("0x0300"), el2008())
        self.assertEqual(level, "warning")
        self.assertIn("no DC clock, only delay measurement: it will not reach OP with DC", text)
        self.assertEqual(self.found(slave(), el2008()), [])          # free run: nothing to say
        self.assertEqual(self.found(slave("0x0000"), el2008()), [])  # 0: free run as well

    def test_no_dc_mode(self):
        [(level, text)] = self.found(slave("0x0300", "EL1008"), el1008())
        self.assertEqual(level, "warning")
        self.assertIn("the slave describes no DC mode (ESI file Beckhoff EL1xxx.xml)", text)
        # CoE says it can: no warning from the missing modes
        d = el1008()
        d.sync = [dcinfo.SyncParams(0x1C32, 0x0007)]
        self.assertEqual(self.found(slave("0x0300", "EL1008"), d), [])

    def test_assign_activate_not_among_the_modes(self):
        [(level, text)] = self.found(slave("0x0300", "EL5101"), el5101())
        self.assertIn("assign-activate 0x0300 is not one of the slave's DC modes: DC (DC-Synchron) "
                      "0x0320", text)
        self.assertEqual(self.found(slave("0x320", "EL5101"), el5101()), [])

    def test_coe_lists_no_dc(self):
        d = ect60()
        d.sync = [dcinfo.SyncParams(0x1C32, 0x0003)]
        [(level, text)] = self.found(slave("0x0300", "ECT60"), d)
        self.assertIn("CoE 0x1C32/0x1C33:04 lists no DC mode", text)

    def test_only_dc_modes_and_dc_off(self):
        d = dcinfo.DcInfo(clock="64", sii_modes=[M("DC", "", 0x300, 1, 0, "SII")])
        [(level, text)] = self.found(slave(None, "Drive"), d)
        self.assertIn("lists only DC modes (DC 0x0300)", text)
        self.assertIn("AL 0x0027", text)
        self.assertEqual(self.found(slave(None, "ECT60"), ect60()), [])   # it has Synchron
        d = dcinfo.DcInfo(clock="64", sync=[dcinfo.SyncParams(0x1C32, 0x0004)])
        self.assertIn("CoE 0x1C3x:04", self.found(slave(None, "Drive"), d)[0][1])

    def test_minimum_cycle_time(self):
        [(level, text)] = self.found(slave("0x320", "EL5101"), el5101(), period=50000)
        self.assertIn("minimum cycle time is 66 us", text)
        self.assertIn("every 50 us", text)
        self.assertEqual(self.found(slave("0x320", "EL5101"), el5101(), period=1000000), [])
        self.assertEqual(self.found(slave("0x0300", "ECT60"), ect60(), period=1000000), [])

    def test_sii_and_esi_differ(self):
        d = el5101(sii_modes=[M("DC", "", 0x300, 1, 0, "SII")])
        notes = [t for lv, t in self.found(slave("0x320", "EL5101"), d) if lv == "note"]
        self.assertEqual(len(notes), 1)
        self.assertIn("differ in their DC modes", notes[0])

    def test_nothing_known(self):
        self.assertEqual(self.found(slave("0x0300"), None), [])


class TestPropose(unittest.TestCase):
    def test_no_dc_for_a_chip_without_a_clock(self):
        from ecatsetup import coe, propose
        s = sii_mod.Sii(2, 0x07D83052, 0x00120000, 0, 0, dc_modes=[sii_mod.DcMode("DC", 0x300, 0, 0, 1)])
        p = propose.propose({"position": 2, "dc": False, "dc_clock": "delay"}, s, coe.Dictionary())
        self.assertIsNone(p.dc_assign)
        self.assertTrue(any("no DC clock" in n for n in p.notes))
        p = propose.propose({"position": 2, "dc": True, "dc_clock": "64"}, s, coe.Dictionary())
        self.assertEqual(p.dc_assign, 0x300)
        self.assertEqual(p.dc_info.clock, "64")


class TestSession(unittest.TestCase):
    def test_problems_of_the_slaves_read(self):
        from ecatsetup import propose, session
        ses = session.Session()
        ses.cfg = {"slaves": [dict(slave("0x0300"), identity={"vendor": "0x00000002",
                                                               "product": "0x07D83052"})]}
        p = propose.Proposal(2, 2, 0x07D83052, 0x00120000, 0, "EL2008", dc_info=el2008())
        info = {"position": 2, "vendor_id": 2, "product_code": 0x07D83052}
        ses.bus = {2: session.BusSlave(info, None, p)}
        [(level, text)] = ses.dc_problems()
        self.assertTrue(text.startswith("slave 2 (slave-2): DC SYNC0 is on"), text)
        # another kind of slave there: what it can do says nothing about the file's
        info["product_code"] = 0x03F03052
        self.assertEqual(ses.dc_problems(), [])
        self.assertIsNone(ses.dc_info(2))


try:
    from PyQt5.QtWidgets import QApplication
    from ecatsetup import gui
except ImportError:  # pragma: no cover
    gui = None


@unittest.skipIf(gui is None, "PyQt5 is not installed")
class TestSyncRow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def window(self, d, dc=None):
        from ecatsetup import propose, session
        ses = session.Session()
        ses.cfg = {"ecat": 1, "master": {"interface": "eth0"},
                   "slaves": [dict(slave(dc, "EL5101"), profile="generic", **{"configure-pdos": True},
                                   identity={"vendor": "0x00000002", "product": "0x13ED3052",
                                             "revision": "0x03FF0000"})]}
        p = propose.Proposal(2, 2, 0x13ED3052, 0x03FF0000, 0, "EL5101", dc_info=d)
        info = {"position": 2, "vendor_id": 2, "product_code": 0x13ED3052, "revision": 0x03FF0000,
                "state": "PREOP", "dc": d.can_sync, "dc_clock": d.clock}
        ses.bus = {2: session.BusSlave(info, None, p)}
        w = gui.MainWindow(ses)
        self.addCleanup(lambda: (ses.__setattr__("saved", ses.cfg), w.close()))
        w.select_slave(2)
        return w, w.slave_page, ses

    def choose(self, page, index):
        page.dc_mode.setCurrentIndex(index)
        page.dc_mode_chosen()

    def test_the_slaves_modes_and_user_settings(self):
        w, page, ses = self.window(el5101())
        texts = [page.dc_mode.itemText(i) for i in range(page.dc_mode.count())]
        self.assertEqual(texts, ["Free run (no DC)", "DC mode DC (DC-Synchron) 0x0320 · ESI",
                                 "DC mode DCIN (DC-Synchron (input based)) 0x0320 · ESI",
                                 "DC mode with user settings"])
        self.assertEqual(page.dc_mode.currentIndex(), 0)
        self.assertFalse(page.assign.isEnabled())
        self.assertEqual(page.fields["Clock"].text(), "DC, 64 bit")
        self.assertIn("Chip: DC clock, 64 bit", page.dc_hint.text())
        # a mode sets AssignActivate
        self.choose(page, 1)
        self.assertEqual(ses.slave(2)["dc"]["assign-activate"], "0x0320")
        self.assertFalse(page.assign.isEnabled())
        self.assertTrue(page.dc_warn.isHidden())
        # DC mode with user settings: any value, with a warning when the slave does not describe it
        self.choose(page, 3)
        self.assertTrue(page.assign.isEnabled())
        page.assign.setText("0x0300")
        page.general_edited()
        self.assertEqual(ses.slave(2)["dc"]["assign-activate"], "0x0300")
        self.assertFalse(page.dc_warn.isHidden())
        self.assertIn("0x0300 is not one of", page.dc_warn.text())
        w.check()
        self.assertTrue(any("0x0300 is not one of" in w.messages.item(i).text()
                            for i in range(w.messages.count())))
        # free run removes it
        self.choose(page, 0)
        self.assertNotIn("dc", ses.slave(2))

    def test_a_file_value_is_shown_as_its_mode_or_as_its_own(self):
        _, page, _ = self.window(el5101(), dc="0x320")
        self.assertEqual(page.dc_mode.currentIndex(), 1)
        _, page, _ = self.window(el5101(), dc="0x0300")
        self.assertEqual(page.dc_mode.currentIndex(), page.dc_mode.count() - 1)
        self.assertEqual(page.assign.text(), "0x0300")

    def test_a_slave_without_dc_warns_but_lets_it_be(self):
        w, page, ses = self.window(el2008(), dc="0x0300")
        self.assertEqual([page.dc_mode.itemText(i) for i in range(page.dc_mode.count())],
                         ["Free run (no DC)", "DC mode with user settings"])
        self.assertEqual(page.dc_mode.currentIndex(), 1)      # kept, not removed
        self.assertEqual(page.fields["Clock"].text(), "delay measurement only (no SYNC0)")
        self.assertIn("it will not reach OP with DC", page.dc_warn.text())
        self.assertEqual(ses.slave(2)["dc"]["assign-activate"], "0x0300")
        w.check()
        warned = [w.messages.item(i).text() for i in range(w.messages.count())]
        self.assertTrue(any(t.startswith("Warning: slave 2 (slave-2): DC SYNC0 is on") for t in warned),
                        warned)
        self.assertNotIn(2, w.messages_by_slave)               # not an error of the slave


if __name__ == "__main__":
    unittest.main()
