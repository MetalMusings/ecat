import os
import tempfile
import unittest
from datetime import date

from ecatsetup import measured

ECT = {"name": "x", "device": "ECT60", "identity": {"vendor": "0x00000A88", "product": "0x0A880002"}}
OTHER = {"name": "y", "identity": {"vendor": "0x00000002", "product": "0x03F03052"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("ECATSETUP_MEASURED")
        os.environ["ECATSETUP_MEASURED"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ECATSETUP_MEASURED", None)
        else:
            os.environ["ECATSETUP_MEASURED"] = self.old
        self.tmp.cleanup()


class TestMeasured(Base):
    def test_shipped_results(self):
        r = measured.results(ECT)
        self.assertEqual([x["period-us"] for x in r], [100, 125, 150, 175, 200, 250])
        self.assertEqual(r[0]["result"], "refused")
        self.assertEqual(r[0]["al-status"], "0x0036")
        self.assertEqual(measured.results(OTHER), [])
        self.assertEqual(measured.results({"name": "z"}), [])
        self.assertEqual(measured.results((0xA88, 0x0A880002)), r)       # a (vendor, product) too

    def test_verdict(self):
        r = measured.results(ECT)
        v = lambda us: measured.verdict(r, us * 1000)
        self.assertEqual(v(80)[0], "bad")
        self.assertEqual(v(100)[0], "bad")                 # at the period that was refused
        self.assertEqual(v(110)[0], "unknown")             # between: not claimed
        self.assertIn("between 100 and 125", v(110)[1])
        self.assertEqual(v(125), ("ok", "tried ok at this period"))
        self.assertEqual(v(150)[0], "ok")
        self.assertEqual(v(1000)[0], "ok")
        self.assertIsNone(measured.verdict([], 125000))
        self.assertIsNone(measured.verdict(r, 0))
        # only a refusal: nothing is known to work
        only = [{"period-us": 100.0, "result": "refused"}]
        self.assertEqual(measured.verdict(only, 90000)[0], "bad")
        self.assertEqual(measured.verdict(only, 200000)[0], "unknown")

    def test_describe(self):
        text = measured.describe(measured.results(ECT))
        self.assertEqual(text, "250 us ok, 200 us ok, 175 us ok, 150 us ok, 125 us ok, "
                               "100 us refused (AL 0x0036); measured 2026-11-16")
        self.assertEqual(measured.describe([]), "")

    def test_record_and_merge(self):
        path = measured.record(ECT, 110000, "ok", note="halrun", today=date(2026, 12, 1))
        self.assertTrue(path.startswith(self.tmp.name))
        r = measured.results(ECT)
        self.assertEqual([x["period-us"] for x in r], [100, 110, 125, 150, 175, 200, 250])
        self.assertEqual(measured.verdict(r, 110000)[0], "ok")
        self.assertEqual(measured.verdict(r, 105000)[0], "unknown")
        # the same period again: the user's result replaces, and the shipped one is kept under it
        measured.record(ECT, 125000, "refused", al_status=0x36, today=date(2026, 12, 2))
        r = {x["period-us"]: x for x in measured.results(ECT)}
        self.assertEqual(r[125]["result"], "refused")
        self.assertEqual(r[125]["al-status"], "0x0036")
        self.assertEqual(r[125]["date"], "2026-12-02")
        self.assertEqual(r[150]["result"], "ok")
        # a second device is a second entry; the file still reads
        measured.record(OTHER, 250000, "ok", today=date(2026, 12, 3))
        self.assertEqual([x["period-us"] for x in measured.results(OTHER)], [250])
        self.assertEqual(measured.results(OTHER)[0]["date"], "2026-12-03")
        # recording twice the same period does not duplicate it
        measured.record(OTHER, 250000, "ok", today=date(2026, 12, 4))
        self.assertEqual(len(measured.results(OTHER)), 1)

    def test_record_errors(self):
        with self.assertRaises(ValueError):
            measured.record({"name": "n"}, 125000, "ok")
        with self.assertRaises(ValueError):
            measured.record(ECT, 125000, "maybe")

    def test_a_broken_user_file_is_ignored(self):
        with open(measured.user_file(), "w") as f:
            f.write("not: [a list")
        self.assertEqual(len(measured.results(ECT)), 6)


if __name__ == "__main__":
    unittest.main()
