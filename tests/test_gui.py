"""Tests of the session behind EtherCAT Setup's window, and of the window
itself (offscreen, skipped without PyQt5), against a fake bus.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ecatsetup import config, session, tool  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data")


class FakeTool:
    """The ethercat tool for a bus with EaserCAT 7000 slaves at the given positions."""

    def __init__(self, positions=(0,)):
        self.positions = list(positions)
        with open(os.path.join(DATA, "easercat7000.sii"), "rb") as f:
            self.sii_data = f.read()
        with open(os.path.join(DATA, "easercat7000-sdos.txt")) as f:
            self.sdo_text = f.read()

    def patches(self):
        return [mock.patch.object(tool, name, getattr(self, name))
                for name in ("slave_positions", "slave_info", "sii", "sdos", "upload")]

    def slave_positions(self):
        return list(self.positions)

    def slave_info(self, pos):
        return {"position": pos, "dc": False, "vendor_id": 0xD0BED1D0, "product_code": 0x7000,
                "revision": 2, "serial": 0, "state": "PREOP", "al_status_code": None}

    def sii(self, pos):
        return self.sii_data

    def sdos(self, pos):
        return self.sdo_text

    def upload(self, pos, index, subindex, dtype):
        return {0x1000: 5001}.get(index)


class BusTest(unittest.TestCase):
    positions = (0, 1)

    def setUp(self):
        self.tool = FakeTool(self.positions)
        for p in self.tool.patches():
            p.start()
            self.addCleanup(p.stop)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)


class TestSession(BusTest):
    def test_read_bus_propose_and_save(self):
        s = session.Session()
        s.read_bus()
        self.assertEqual(sorted(s.bus), [0, 1])
        s.new(interface="enp2s0")
        self.assertEqual([x["name"] for x in s.cfg["slaves"]], ["io-0", "io-1"])
        self.assertEqual(s.check(), ([], [], []))
        self.assertTrue(s.dirty)
        path = os.path.join(self.dir.name, "ethercat.yaml")
        s.save(path)
        self.assertFalse(s.dirty)
        self.assertEqual(config.load(path), s.cfg)
        s.cfg["slaves"][0]["name"] = "thc"
        s.save()
        self.assertTrue(os.path.exists(path + ".bak"))
        self.assertEqual(config.load(path)["slaves"][0]["name"], "thc")

    def test_errors_are_not_saved(self):
        s = session.Session()
        s.read_bus()
        s.new()
        s.cfg["slaves"][1]["name"] = s.cfg["slaves"][0]["name"]
        errors, _, _ = s.check()
        self.assertTrue(any("used twice" in e for e in errors))
        path = os.path.join(self.dir.name, "ethercat.yaml")
        with self.assertRaises(ValueError):
            s.save(path)
        self.assertFalse(os.path.exists(path))

    def test_bus_compared_with_the_file(self):
        s = session.Session()
        s.read_bus()
        s.new()
        s.cfg["slaves"].append(dict(s.cfg["slaves"][1], position=2, name="gone"))
        s.cfg["slaves"][0]["identity"]["revision"] = "0x00000001"
        _, problems, notes = s.check()
        self.assertEqual(problems, ["slave 2 (gone) is missing"])
        self.assertEqual(notes, ["slave 0 has revision 0x00000002, the file expects 0x00000001"])

    def test_update_keeps_choices_and_adds_new_slaves(self):
        s = session.Session()
        s.read_bus(positions=[0])
        s.new()
        s.cfg["slaves"][0]["name"] = "thc"
        s.read_bus()
        notes = s.update_from_bus()
        self.assertEqual([x["name"] for x in s.cfg["slaves"]], ["thc", "io-1"])
        self.assertTrue(any("slave 1: new" in n for n in notes))

    def test_propose_again_keeps_the_name(self):
        s = session.Session()
        s.read_bus()
        s.new()
        s.cfg["slaves"][1]["name"] = "thc"
        del s.cfg["slaves"][1]["process-data"]["inputs"]
        s.propose_again(1)
        self.assertEqual(s.cfg["slaves"][1]["name"], "thc")
        self.assertIn("inputs", s.cfg["slaves"][1]["process-data"])

    def test_no_master(self):
        s = session.Session()
        with mock.patch.object(tool, "slave_positions", side_effect=tool.ToolError("no socket")):
            with self.assertRaises(tool.ToolError):
                s.read_bus()


try:
    from PyQt5.QtWidgets import QApplication
    from ecatsetup import gui
except ImportError:  # pragma: no cover
    gui = None


@unittest.skipIf(gui is None, "PyQt5 is not installed")
class TestWindow(BusTest):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def window(self):
        s = session.Session()
        s.read_bus()
        s.new()
        w = gui.MainWindow(s)
        w.check()
        w.refresh()
        self.addCleanup(lambda: (s.__setattr__("saved", s.cfg), w.close()))
        return w

    def items(self, w):
        return [[w.slaves.topLevelItem(i).text(c) for c in range(5)]
                for i in range(w.slaves.topLevelItemCount())]

    def test_list_and_pages(self):
        w = self.window()
        self.assertEqual(self.items(w)[0][1], "Master")
        self.assertEqual(self.items(w)[1], ["0", "io-0", "PREOP", "ok", "MetalMusings EaserCAT 7000"])
        w.select_slave(1)
        self.assertIs(w.pages.currentWidget(), w.slave_page)
        self.assertGreater(w.slave_page.pd.rowCount(), 0)
        self.assertIn("io-1", w.yaml.toPlainText())

    def test_edit_a_pin_and_see_an_error(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        row = next(r for r in range(page.pd.rowCount())
                   if page.pd.item(r, page.PIN).flags() & gui.Qt.ItemIsEditable
                   and page.pd.item(r, page.PIN).text())
        page.pd.item(row, page.PIN).setText("bad pin")
        self.assertEqual(page._entry(row)["pin"], "bad pin")
        self.assertIn("bad pin", w.yaml.toPlainText())
        self.assertEqual(self.items(w)[1][3], "1 problem")
        self.assertTrue(w.session.dirty)
        page.pd.item(row, page.PIN).setText("")
        self.assertNotIn("pin", page._entry(row))
        self.assertEqual(self.items(w)[1][3], "ok")

    def test_rename_slave(self):
        w = self.window()
        w.select_slave(1)
        w.slave_page.name.setText("thc")
        w.slave_page.general_edited()
        self.assertEqual(w.session.slave(1)["name"], "thc")
        self.assertEqual(self.items(w)[2][1], "thc")
        self.assertIs(w.pages.currentWidget(), w.slave_page)

    def test_settings(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        n = len(w.session.slave(0).get("settings") or [])
        page.add_setting()
        row = page.st.rowCount() - 1
        page.st.item(row, 0).setText("0x2000:02")
        st = w.session.slave(0)["settings"][n]
        self.assertEqual(st["type"], "USINT")    # from the slave's dictionary
        self.assertIn("I2C", st["name"])
        page.st.item(row, 3).setText("0x48")
        self.assertEqual(st["value"], 72)
        page.st.selectRow(row)
        page.remove_setting()
        self.assertEqual(len(w.session.slave(0)["settings"]), n)

    def test_master_page(self):
        w = self.window()
        w.select_slave(gui.MASTER_ITEM)
        m = w.master_page
        m.interface.setEditText("enp9s0")
        m.transport.setCurrentText("xdp-native")
        self.assertEqual(w.session.cfg["master"]["interface"], "enp9s0")
        self.assertEqual(w.session.cfg["master"]["transport"], "xdp-native")

    def test_message_selects_slave(self):
        w = self.window()
        w.session.cfg["slaves"][1]["name"] = "io-0"
        w.check()
        item = next(w.messages.item(i) for i in range(w.messages.count())
                    if "used twice" in w.messages.item(i).text())
        w.message_activated(item)
        self.assertEqual(w.slave_page.position, 1)


    def test_diagnosis_findings(self):
        block = ["Slave 0: ECT60V202(COE)", "  State: PREOP (not OP)",
                 "  Port 0: link; since power-on: rx 1", "  Port 1: link",
                 "  CiA 402: Fault (status word 0x0A08), mode CSP",
                 "    -> see the error code; reset with fault-reset",
                 "  Error code 0x603F: 0x7500 communication"]
        self.assertEqual(gui.diagnosis_findings(block), [
            "State: PREOP (not OP)",
            "CiA 402: Fault (status word 0x0A08), mode CSP -> see the error code; reset with "
            "fault-reset",
            "Error code 0x603F: 0x7500 communication"])


if __name__ == "__main__":
    unittest.main()
