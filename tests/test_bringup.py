import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import bringup, coe, quirks  # noqa: E402

ECT60 = {"vendor": "0x00000A88", "product": "0x0A880002"}
OTHER = {"vendor": "0x00000002", "product": "0x00001234"}


def drive(identity=OTHER, outputs=((0x6040, 0), (0x607A, 0), (0x6060, 0)), inputs=((0x6041, 0), (0x6064, 0)),
          dc=True, **cia402):
    s = {"position": 1, "name": "x", "profile": "cia402", "identity": dict(identity),
         "cia402": dict({"mode": "csp"}, **cia402),
         "process-data": {"outputs": [{"pdo": "0x1600", "entries": [
             {"object": "0x%04X:%02X" % o} for o in outputs]}],
             "inputs": [{"pdo": "0x1A00", "entries": [{"object": "0x%04X:%02X" % o} for o in inputs]}]}}
    if dc:
        s["dc"] = {"assign-activate": "0x300"}
    return s


def proposal(esi=True, exact=True):
    return SimpleNamespace(esi_path="/esi/x.xml" if esi else None, esi_exact=exact, esi_revision=1, revision=2)


class Base(unittest.TestCase):
    def setUp(self):
        self.old = {k: os.environ.get(k) for k in ("ECATSETUP_DEVICES", "ECATSETUP_MEASURED")}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ECATSETUP_DEVICES"] = os.path.join(self.tmp.name, "devices")
        os.environ["ECATSETUP_MEASURED"] = os.path.join(self.tmp.name, "measured")
        quirks.load(reload=True)

    def tearDown(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()
        quirks.load(reload=True)

    def row(self, found, key):
        return next(r for r in found if r.key == key)


class TestRows(Base):
    def test_only_for_a_used_drive(self):
        self.assertEqual(bringup.rows({"profile": "generic"}), [])
        self.assertEqual(bringup.rows(dict(drive(), unused=True)), [])
        found = bringup.rows(drive(), proposal())
        self.assertEqual([r.key for r in found],
                         ["esi", "pdo", "mode", "sync", "ferror", "op", "enable", "follows", "start"])

    def test_what_is_known_from_data(self):
        found = bringup.rows(drive(), proposal())
        self.assertEqual(self.row(found, "esi").state, bringup.OK)
        self.assertEqual(self.row(found, "pdo").state, bringup.OK)
        self.assertIn("set every cycle", self.row(found, "mode").text)
        # nothing seen yet: the workbench rows wait for a try
        for key in ("op", "enable", "follows", "start"):
            self.assertEqual(self.row(found, key).state, bringup.TRY, key)
        self.assertIn("shown only when tried", bringup.summary(found))

    def test_gaps(self):
        found = bringup.rows(drive(outputs=((0x6040, 0), (0x607A, 0)), inputs=((0x6041, 0),), dc=False),
                             proposal(esi=False),
                             problems=[("error", "slave 1 (x): PDO 0x1600 lacks 0x607A")])
        self.assertEqual(self.row(found, "esi").state, bringup.MISSING)
        self.assertEqual(self.row(found, "esi").card, "source")
        self.assertEqual(self.row(found, "pdo").state, bringup.BAD)
        self.assertIn("lacks 0x607A", self.row(found, "pdo").text)
        self.assertEqual(self.row(found, "mode").state, bringup.BAD)
        self.assertEqual(self.row(found, "sync").state, bringup.MISSING)
        self.assertEqual(bringup.rows(drive(), proposal(exact=False))[0].state, bringup.MISSING)
        # the mode once at start, or only seen
        s = drive(outputs=((0x6040, 0),))
        s["settings"] = [{"object": "0x6060:00", "type": "SINT", "value": 8}]
        self.assertIn("once at start", self.row(bringup.rows(s, proposal()), "mode").text)
        s = drive(outputs=((0x6040, 0),), inputs=((0x6061, 0),))
        self.assertIn("seen in 0x6061", self.row(bringup.rows(s, proposal()), "mode").text)

    def test_the_following_error_window(self):
        self.assertEqual(self.row(bringup.rows(drive(identity=ECT60), proposal()), "ferror").text,
                         "the drive's own: 0x2022:00 (quirk file)")
        d = coe.Dictionary()
        d.entries[(0x6065, 0)] = object()
        self.assertEqual(self.row(bringup.rows(drive(), proposal(), dictionary=d), "ferror").state, bringup.OK)
        self.assertEqual(self.row(bringup.rows(drive(), proposal()), "ferror").state, bringup.TRY)

    def test_the_servo_period_against_what_was_measured(self):
        # the shipped ECT60 results: ok down to 125 us, refused at 100 us
        row = self.row(bringup.rows(drive(identity=ECT60), proposal(), period_us=500), "sync")
        self.assertEqual(row.state, bringup.OK)
        self.assertIn("runs at 125 us (refused 100 us)", row.text)
        self.assertEqual(self.row(bringup.rows(drive(identity=ECT60), proposal(), period_us=100),
                                  "sync").state, bringup.BAD)
        self.assertEqual(self.row(bringup.rows(drive(), proposal(), period_us=500), "sync").state, bringup.TRY)


class TestSeen(Base):
    def test_op_enable_follows(self):
        seen = bringup.Seen()
        seen.saw("run1", True, 1, clock=0)
        found = bringup.rows(drive(), proposal(), seen=seen)
        self.assertEqual(self.row(found, "op").state, bringup.OK)
        seen.saw("run1", True, 4, moved=0.5, clock=1)
        found = bringup.rows(drive(), proposal(), seen=seen)
        self.assertEqual(self.row(found, "enable").state, bringup.OK)
        self.assertEqual(self.row(found, "follows").text, "followed a move of 0.50 mm")

    def test_not_op_after_a_while(self):
        seen = bringup.Seen()
        seen.saw("r", False, clock=0)
        self.assertIsNone(seen.not_op)
        seen.saw("r", False, clock=bringup.Seen.WAIT)
        self.assertEqual(self.row(bringup.rows(drive(), proposal(), seen=seen), "op").state, bringup.BAD)
        self.assertEqual(self.row(bringup.rows(drive(), proposal(), seen=seen), "op").card, "diagnose")

    def test_a_fault_and_a_drive_held_by_its_hardware(self):
        seen = bringup.Seen()
        seen.saw("r", True, 7, error=0x8611)
        row = self.row(bringup.rows(drive(), proposal(), seen=seen), "enable")
        self.assertEqual(row.state, bringup.BAD)
        self.assertIn("fault 0x8611", row.text)
        seen = bringup.Seen()
        seen.saw("r", True, 1, enable_asked=True, clock=0)
        self.assertIsNone(seen.stuck)
        seen.saw("r", True, 1, enable_asked=True, clock=bringup.Seen.HELD)
        self.assertIn("hardware", self.row(bringup.rows(drive(), proposal(), seen=seen), "enable").text)
        # without enable asked it is only waiting
        seen = bringup.Seen()
        seen.saw("r", True, 1, clock=0)
        seen.saw("r", True, 1, clock=100)
        self.assertIsNone(seen.stuck)

    def test_the_start_position_over_two_runs(self):
        seen = bringup.Seen()
        seen.saw("r1", True, position=1000)
        seen.saw("r1", True, position=51234)       # moved in the first run
        self.assertEqual(self.row(bringup.rows(drive(), proposal(), seen=seen), "start").state, bringup.TRY)
        seen.saw("r2", True, position=51235)       # started where it ended: kept
        row = self.row(bringup.rows(drive(), proposal(), seen=seen), "start")
        self.assertEqual(row.state, bringup.MISSING)
        self.assertIn("kept its position", row.text)
        self.assertEqual(row.card, "operation")
        # ticked: known
        self.assertEqual(self.row(bringup.rows(drive(**{"zero-at-start": True}), proposal(), seen=seen),
                                  "start").state, bringup.OK)
        # a drive that starts from 0 by itself
        seen = bringup.Seen()
        seen.saw("r1", True, position=51234)
        seen.saw("r2", True, position=0)
        row = self.row(bringup.rows(drive(), proposal(), seen=seen), "start")
        self.assertEqual(row.state, bringup.OK)
        self.assertIn("starts from 0 by itself", row.text)

    def test_the_key_changes_only_with_what_the_card_shows(self):
        seen = bringup.Seen()
        seen.saw("r1", True, 4, position=10, moved=0.2, clock=0)
        k = seen.key()
        seen.saw("r1", True, 4, position=500, moved=3.0, clock=1)
        self.assertEqual(seen.key(), k)
        seen.saw("r2", True, 4, position=500, clock=2)
        self.assertNotEqual(seen.key(), k)

    def test_summary(self):
        self.assertEqual(bringup.summary([bringup.Row("a", bringup.OK, "A", "")]),
                         "Ready: everything for this goal is known.")
        self.assertEqual(bringup.summary([bringup.Row("a", bringup.BAD, "A", ""),
                                          bringup.Row("b", bringup.TRY, "B", "")]),
                         "1 to put right, 1 shown only when tried.")


if __name__ == "__main__":
    unittest.main()
