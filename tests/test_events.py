"""Tests of reading the component's flight recorder (events.py).

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
# state (test lock, drive limits): never the user's own
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = \
        __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")

from ecatsetup import events  # noqa: E402

N = 8


def write_log(path, records, nxt=None, pid=999999, n=N):
    """A recorder file: records [(index, type, slave, a, b)]; a record's
    seq is its index + 1, unless the index is negative (being written)."""
    data = bytearray(events.HEADER.size + n * events.RECORD.size)
    nxt = nxt if nxt is not None else max(i for i, *_ in records) + 1
    events.HEADER.pack_into(data, 0, events.MAGIC, events.VERSION, n, events.RECORD.size,
                            1_700_000_000_000_000_000, nxt, 1000000, pid, b"/c/ethercat.yaml")
    for i, typ, slave, a, b in records:
        seq = 0 if i < 0 else i + 1
        events.RECORD.pack_into(data, events.HEADER.size + (abs(i) % n) * events.RECORD.size,
                                seq, 1_700_000_001_000_000_000 + abs(i), typ, slave, a, b, 0)
    with open(path, "wb") as f:
        f.write(data)


class TestRead(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "ev")

    def test_read_in_order(self):
        write_log(self.path, [(0, events.START, -1, 4, 0), (1, events.DRIVE_FAULT, 2, 0x7500, 0x218)])
        log = events.read(self.path)
        self.assertEqual((log.config, log.period_ns, log.total, log.lost), ("/c/ethercat.yaml", 1000000, 2, 0))
        self.assertEqual([e.type for e in log.events], [events.START, events.DRIVE_FAULT])
        self.assertFalse(log.running)

    def test_wrapped_and_being_written(self):
        # 11 written into 8 places: 3..10 are there, 10 is being written
        recs = [(i, events.LATE, -1, i, 0) for i in range(3, 10)] + [(-10, events.LATE, -1, 10, 0)]
        write_log(self.path, recs, nxt=11)
        log = events.read(self.path)
        self.assertEqual([e.a for e in log.events], list(range(3, 10)))
        self.assertEqual(log.lost, 4)

    def test_not_a_log(self):
        with open(self.path, "wb") as f:
            f.write(b"x" * 400)
        with self.assertRaisesRegex(events.EventsError, "not an event log"):
            events.read(self.path)
        with self.assertRaisesRegex(events.EventsError, "no event log"):
            events.read(self.path + "-none")

    def test_texts(self):
        names = {2: "x", 3: "io"}
        E = events.Event
        cases = [
            (E(0, 0, events.LINK, -1, 0, 1), "error", "link DOWN"),
            (E(0, 0, events.WC, -1, 1, 2), "error", "working counter incomplete (was complete)"),
            (E(0, 0, events.SLAVE_AL, 3, 0x14, 0x08), "warn", "slave 3 (io): SAFEOP + ERROR (was OP)"),
            (E(0, 0, events.DRIVE_FAULT, 2, 0x7500, 0x218), "error", "slave 2 (x): drive fault 0x7500"),
            (E(0, 0, events.DRIVE_BLOCK, 2, 5, 0), "error", "fault-reason 5, watchdog"),
            (E(0, 0, events.GUARD, -1, 1, 0), "warn", "the servo thread stopped"),
            (E(0, 0, events.GUARD, -1, 2, 0x02), "info", "done, outputs zero, slaves in PREOP"),
            (E(0, 0, events.GUARD, -1, 2, 0x06), "error", "gave up, slaves still in PREOP+SAFEOP"),
            (E(0, 0, events.WC, -1, 1, 0), "info", "working counter incomplete (was zero)"),
            (E(0, 0, events.LATE, -1, 1800, 3), "warn", "1800 us since its last write, 3 more"),
            (E(0, 0, events.SHIFT, -1, 300000, 2), "info", "SYNC0 shift 300 us, turn-around 2"),
            (E(0, 0, events.MASTER_AL, -1, 0x0C, 0x08), "warn", "SAFEOP+OP (was OP)"),
        ]
        for e, sev, part in cases:
            s, msg = events.text(e, names)
            self.assertEqual(s, sev, msg)
            self.assertIn(part, msg)

    def test_context_calms_what_the_master_does_by_itself(self):
        E = events.Event
        # a cable broken after slave 1 of 4: the master reads the bus again and all slaves
        # are offline for a while; only 2 and 3 are really gone
        evs = [E(0, 0, events.LINK, -1, 1, 0), E(1, 0, events.RESPONDING, -1, 2, 4),
               E(2, 0, events.SLAVE_ONLINE, 0, 0, 0), E(3, 0, events.SLAVE_ONLINE, 1, 0, 0),
               E(4, 0, events.SLAVE_ONLINE, 2, 0, 0), E(5, 0, events.SLAVE_ONLINE, 3, 0, 0)]
        t = events.annotate(evs, {1: "x"})
        self.assertEqual([t[i][0] for i in range(2, 6)], ["info", "info", "error", "error"])
        self.assertIn("slave 1 (x) offline while the master reads the bus again", t[3][1])
        self.assertEqual(t[4][1], "slave 2 OFFLINE")
        # no slave answers (link lost): every one is gone
        evs = [E(0, 0, events.LINK, -1, 0, 1), E(1, 0, events.RESPONDING, -1, 0, 4),
               E(2, 0, events.SLAVE_ONLINE, 0, 0, 0)]
        self.assertEqual(events.annotate(evs)[2][0], "error")
        # the log starts after the context: the event keeps its own text
        self.assertEqual(events.annotate([E(0, 0, events.SLAVE_ONLINE, 0, 0, 0)])[0][0], "error")

    def test_the_working_counter_while_the_slaves_climb_to_op(self):
        E = events.Event
        up = [E(0, 0, events.LINK, -1, 1, 0), E(1, 0, events.MASTER_AL, -1, 0x0C, 0)]
        wc = [E(2, 0, events.WC, -1, 1, 2), E(3, 0, events.WC, -1, 0, 1)]
        t = events.annotate(up + wc)
        self.assertEqual([t[2][0], t[3][0]], ["info", "info"])
        self.assertIn("still coming up", t[2][1])
        # in OP the same events are faults
        run = [E(0, 0, events.LINK, -1, 1, 0), E(1, 0, events.MASTER_AL, -1, 0x08, 0)]
        t = events.annotate(run + wc)
        self.assertEqual([t[2][0], t[3][0]], ["error", "error"])
        # without a master state before it (an overwritten log) nothing is calmed
        t = events.annotate(wc)
        self.assertEqual([t[2][0], t[3][0]], ["error", "error"])

    def test_report(self):
        write_log(self.path, [(0, events.START, -1, 4, 0), (1, events.LINK, -1, 0, 1)])
        out = []
        events.report(events.read(self.path), out=out.append)
        self.assertIn("ecat stopped", out[0])
        self.assertTrue(out[2].endswith("ERROR link DOWN: cable, or the first slave's power"))


class TestReport(unittest.TestCase):
    def test_build(self):
        from ecatsetup import config, report, tool
        sys.path.insert(0, os.path.dirname(__file__))
        from test_integrate import INI, cfg
        with tempfile.TemporaryDirectory() as d:
            ini = os.path.join(d, "machine.ini")
            with open(ini, "w") as f:
                f.write(INI.replace("[HAL]", "[ETHERCAT]\nCONFIG = ethercat.yaml\n\n[HAL]"))
            config.write(cfg(), os.path.join(d, "ethercat.yaml"))
            write_log(os.path.join(d, "ev"), [(0, events.START, -1, 3, 0),
                                              (1, events.DRIVE_FAULT, 0, 0x7500, 0x218)])

            def no_master():
                raise tool.ToolError("no master")
            with mock.patch.object(events, "PATH", os.path.join(d, "ev")), \
                    mock.patch.object(tool, "slave_positions", no_master):
                text = report.build(None, ini)
        # the configuration found from the INI file, checked
        self.assertIn("## Configuration %s" % os.path.join(d, "ethercat.yaml"), text)
        self.assertIn("Check: no errors", text)
        self.assertIn("[EMCMOT]SERVO_PERIOD = 1000000", text)
        self.assertIn("[JOINT_0]SCALE = 100", text)
        self.assertNotIn("TWOPASS", text)
        self.assertIn("No master could be reached (no master)", text)
        # the event log, with the slaves' names
        self.assertIn("slave 0 (x): drive fault 0x7500", text)
        self.assertIn("## Event log, the run before", text)


try:
    from PyQt5.QtWidgets import QApplication
    from ecatsetup import gui, session
except ImportError:     # pragma: no cover
    gui = None


@unittest.skipIf(gui is None, "no PyQt5")
class TestCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_card(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev")
            write_log(path, [(0, events.START, -1, 4, 0), (1, events.DRIVE_FAULT, 2, 0x7500, 0x218),
                             (2, events.LINK, -1, 0, 1)])
            with mock.patch.object(events, "PATH", path):
                s = session.Session()
                w = gui.MainWindow(s)
                self.addCleanup(w.close)
                page = w.trouble_page if hasattr(w, "trouble_page") else next(
                    p for p in w.findChildren(gui.TroubleshootPage))
                page.events_run.setItemData(0, path)
                page.show_events()
                t = page.events_table
                self.assertEqual(t.rowCount(), 3)
                self.assertEqual(t.item(0, 2).text(), "link DOWN: cable, or the first slave's power")
                self.assertEqual(t.item(1, 1).text(), "ERROR")
                self.assertIn("ecat ran since", page.events_info.text())
                page.events_run.setItemData(1, path + ".none")
                page.events_run.setCurrentIndex(1)
                self.assertEqual(t.rowCount(), 0)
                self.assertIn("no event log", page.events_info.text())


if __name__ == "__main__":
    unittest.main()
