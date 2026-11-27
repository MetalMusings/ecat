"""Tests of the session behind EtherCAT Setup's window, and of the window
itself (offscreen, skipped without PyQt5), against a fake bus.

Run with: python3 -m unittest discover -s tests
"""

import copy
import ipaddress
import os
import time
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# the tests are written for 2.10 unless they say otherwise (lcnc_env.series)
os.environ.setdefault("ECATSETUP_LINUXCNC_SERIES", "2.10")
# quirk files: only those that come with the program
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_MEASURED"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
# state (test lock, drive limits): never the user's own
if "ECATSETUP_TEST_STATE" not in os.environ:
    # one folder for the whole run: the state, and every temporary file of the
    # tests and of what they start (TMPDIR); removed when the run ends
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = os.environ["TMPDIR"] = \
        __import__("tempfile").tempdir = __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
    __import__("atexit").register(__import__("shutil").rmtree, os.environ["ECATSETUP_TEST_STATE"], True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ecatsetup import coe, config, guidance, session, tool  # noqa: E402

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
                for name in ("slave_positions", "slave_info", "sii", "sdos", "upload", "master_mac",
                             "pdos")] + [
            mock.patch.object(tool, "sdos_settled", lambda pos, **kw: self.sdos(pos))]

    mac = None  # the master's MAC address: no interface found for it unless a test says so

    def master_mac(self):
        return self.mac

    def slave_positions(self):
        return list(self.positions)

    def slave_info(self, pos):
        return {"position": pos, "dc": False, "vendor_id": 0xD0BED1D0, "product_code": 0x7000,
                "revision": 2, "serial": 0, "state": "PREOP", "al_status_code": None}

    def sii(self, pos):
        return self.sii_data

    def sdos(self, pos):
        return self.sdo_text

    own = {}  # position -> the slave's own mapping (tool.pdos); none unless a test gives one

    def pdos(self, pos):
        if pos not in self.own:
            raise tool.ToolError("pdos: not in this fake bus")
        return self.own[pos]

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


class TestTool(unittest.TestCase):
    def test_signed_upload(self):
        with mock.patch.object(tool, "run", lambda *a, **k: "0xffffffff -1\n"):
            self.assertEqual(tool.upload(0, 0x607D, 1, "int32"), -1)
        with mock.patch.object(tool, "run", lambda *a, **k: "0x00000fa0 4000\n"):
            self.assertEqual(tool.upload(0, 0x2022, 0, "uint32"), 4000)

    def test_master_mac(self):
        out = ("Master0\n  Phase: Idle\n  Active: no\n  Slaves: 6\n  Ethernet devices:\n"
               "    Main: E8:EA:6A:09:88:2B (attached)\n      Link: UP\n")
        with mock.patch.object(tool, "run", lambda *a, **k: out):
            self.assertEqual(tool.master_mac(), "e8:ea:6a:09:88:2b")
        with mock.patch.object(tool, "run", lambda *a, **k: "Master0\n"):
            self.assertIsNone(tool.master_mac())

    def test_negative_download(self):
        calls = []
        with mock.patch.object(tool, "run", lambda *a, **k: calls.append(a) or ""):
            tool.download(0, 0x607D, 1, "-2000", "int32")
        # "--" keeps the value from being read as an option
        self.assertEqual(calls[0], ("download", "-p", 0, "-t", "int32", "--", "0x607d", 1, "-2000"))

    def test_dictionary_read_once_settled(self):
        # after a start the master lists the dictionary bit by bit
        answers = iter(["", "", "SDO 0x1000\n", "SDO 0x1000\nSDO 0x1008\n",
                        "SDO 0x1000\nSDO 0x1008\n"])
        told = []
        with mock.patch.object(tool, "sdos", lambda pos: next(answers)):
            text = tool.sdos_settled(0, interval=0, progress=told.append)
        self.assertEqual(text, "SDO 0x1000\nSDO 0x1008\n")
        self.assertEqual(len(told), 1)


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

    def test_a_new_configuration_uses_the_interface_the_bus_was_read_through(self):
        from ecatsetup import netif
        self.tool.mac = "e8:ea:6a:09:88:2b"
        for native, transport in ((True, "xdp-native"), (False, "raw"), (None, "raw")):
            with mock.patch.object(netif, "by_mac", {"e8:ea:6a:09:88:2b": "enp1s0"}.get), \
                    mock.patch.object(netif, "xdp_native", lambda name: native), \
                    mock.patch.object(netif, "driver", lambda name: "igb"):
                s = session.Session()
                s.read_bus()
                self.assertEqual(s.bus_interface, "enp1s0")
                notes = s.new()
                self.assertEqual((s.cfg["master"]["interface"], s.cfg["master"]["transport"]),
                                 ("enp1s0", transport))
                self.assertIn("interface enp1s0", notes[0])
                self.assertEqual("xdp-native, as igb can do it" in notes[0], native is True)
                # given in the file: kept
                s.cfg["master"].update(interface="eth7", transport="raw")
                self.assertEqual(s.update_from_bus(), [])
                self.assertEqual(s.cfg["master"]["interface"], "eth7")
                # a file without one gets it, as a new one
                del s.cfg["master"]["interface"]
                s.update_from_bus()
                self.assertEqual((s.cfg["master"]["interface"], s.cfg["master"]["transport"]),
                                 ("enp1s0", transport))

    def test_import_without_an_interface_uses_the_bus_one(self):
        from ecatsetup import netif
        self.tool.mac = "e8:ea:6a:09:88:2b"
        path = os.path.join(self.dir.name, "ethercat-conf.xml")
        for transport_attr, transport in (("", "xdp-native"), (' transport="raw"', "raw")):
            with open(path, "w") as f:
                f.write('<masters><master idx="0" appTimePeriod="1000000"%s/></masters>' % transport_attr)
            with mock.patch.object(netif, "by_mac", lambda mac: "enp1s0"), \
                    mock.patch.object(netif, "xdp_native", lambda name: True):
                s = session.Session()
                s.read_bus()
                notes = s.import_xml(path)
            self.assertEqual((s.cfg["master"]["interface"], s.cfg["master"]["transport"]),
                             ("enp1s0", transport))
            self.assertFalse([n for n in notes if "no interface" in n])
            self.assertEqual(bool([n for n in notes if "no transport" in n]), False)

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
    from PyQt5.QtCore import QEvent, QUrl
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication, QDialog, QFormLayout
    from ecatsetup import gui
except ImportError:  # pragma: no cover
    gui = None


RED_NAME = gui.RED.name() if gui is not None else ""
AMBER_NAME = gui.AMBER.name() if gui is not None else ""


@unittest.skipIf(gui is None, "PyQt5 is not installed")
class WindowBase(BusTest):
    """A window against the fake bus; the tests are in the classes that use it."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def window(self):
        # never follow a real free master's journal from a test
        from ecatsetup import masterlog
        p = mock.patch.object(masterlog, "free_master_pid", lambda proc="/proc": None)
        p.start()
        self.addCleanup(p.stop)
        s = session.Session()
        s.read_bus()
        s.new()
        w = gui.MainWindow(s)
        w.check()
        w.refresh()
        self.addCleanup(self.dispose, s, w)
        return w

    @staticmethod
    def dispose(s, w):
        """Close the window and delete it now: windows left alive make every
        later style change (setStyle restyles each widget) slow, 350 s in
        test_dark_and_back after the other tests' 70 windows."""
        s.saved = s.cfg
        w.close()
        w.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    @staticmethod
    def hal(w):
        """The HAL & LinuxCNC page, shown: it is built only while shown."""
        w.tabs.setCurrentWidget(w.hal_page)
        return w.hal_page

    def rows(self, w):
        """(key, texts) of the bus list's entries."""
        lst = w.bus_page.list.list
        out = []
        for i in range(lst.count()):
            it = lst.item(i)
            texts = [lab.text() for lab in lst.itemWidget(it).findChildren(gui.QLabel)]
            out.append((it.data(gui.Qt.UserRole), texts))
        return out



class TestTheme(WindowBase):
    def test_dark_and_back(self):
        """The mode changes the style sheet, the shared colours and the palette
        at once, is kept in settings.yaml and read at the next start."""
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"ECATSETUP_SETTINGS": os.path.join(d, "s.yaml")}):
            w = self.window()
            self.addCleanup(gui.apply_theme, "light")
            self.assertEqual(w.theme, "light")
            self.assertIn("#eef1f4", w.styleSheet())
            w.a_theme["dark"].trigger()
            self.assertEqual(w.theme, "dark")
            self.assertIn(gui.THEMES["dark"]["bg"], w.styleSheet())
            self.assertNotIn("#eef1f4", w.styleSheet())
            self.assertEqual(gui.RED.name(), gui.THEMES["dark"]["red"])
            self.assertEqual(self.app.palette().color(gui.QPalette.Window).name(),
                             gui.THEMES["dark"]["bg"])
            self.assertEqual(gui.settings.load()["theme"], "dark")
            # check boxes are drawn so that they show on the dark cards
            self.assertIsInstance(self.app.style(), gui.IndicatorStyle)
            box = gui.QCheckBox("x")
            box.resize(60, 24)
            img = box.grab().toImage()
            colours = {img.pixelColor(x, y).name() for x in range(16) for y in range(img.height())}
            self.assertTrue(any(gui.QColor(c).lightness() > 100 for c in colours), colours)
            w2 = self.window()
            self.assertEqual(w2.theme, "dark")
            self.assertTrue(w2.a_theme["dark"].isChecked())
            w2.a_theme["light"].trigger()
            self.assertEqual(gui.RED.name(), gui.THEMES["light"]["red"])
            self.assertEqual(gui.settings.load()["theme"], "light")
            self.assertNotIsInstance(self.app.style(), gui.IndicatorStyle)

    def test_every_token_in_both(self):
        self.assertEqual(set(gui.THEMES["light"]), set(gui.THEMES["dark"]))
        for t in gui.THEMES:
            gui.style(t)    # every name in the template has a colour


class TestWindow(WindowBase):
    def test_start_page(self):
        from ecatsetup import tasks
        w = self.window()
        pages = {"bus": w.bus_page, "status": w.trouble_page, "test": w.test_page,
                 "live": w.live_page, "hal": w.hal_page, "timing": w.timing_page}
        known = set(pages) | {"scan", "open", "update", "esi", "watch", "ini"}
        targets = {t for _, _, steps in tasks.TASKS for _, t, _ in steps}
        helps = {t for t in targets if t.startswith("help:")}
        self.assertEqual(targets - known - helps, set())
        # a guide's step opens the Help tab at a topic that exists
        from ecatsetup import helpdocs
        self.assertTrue(helps)
        for t in helps:
            self.assertIn(t[len("help:"):], helpdocs.TOPICS)
        with mock.patch.object(w, "show_help") as show:
            w.go("help:guide:homing")
            show.assert_called_once_with("guide:homing")
        self.assertEqual(len(w.start_page.buttons), sum(len(x[2]) for x in tasks.TASKS))
        self.assertIs(w.tabs.currentWidget(), w.bus_page)       # a configuration is open
        for target, page in pages.items():
            w.go(target)
            self.assertIs(w.tabs.currentWidget(), page)
        w.show_tab(w.start_page)
        self.assertIn("configuration: open", w.start_page.state.text())
        with mock.patch.object(w, "start_watch") as watch:
            w.go("watch")
            watch.assert_called_once()
        # nothing open yet: it starts there
        s = session.Session()
        w2 = gui.MainWindow(s)
        self.addCleanup(w2.close)
        self.assertIs(w2.tabs.currentWidget(), w2.start_page)

    def test_list_and_pages(self):
        w = self.window()
        rows = self.rows(w)
        self.assertEqual(rows[0][0], gui.MASTER_ITEM)
        self.assertEqual(rows[1], (0, ["0", "MetalMusings EaserCAT 7000", "io-0 · Modular device",
                                       "PREOP"]))
        self.assertIn("matches", w.bus_page.list.summary.text())
        w.select_slave(1)
        self.assertIs(w.bus_page.stack.currentWidget(), w.slave_page)
        self.assertGreater(w.slave_page.pd.rowCount(), 0)
        self.assertEqual(w.slave_page.title.text(), "Slave 1 · MetalMusings EaserCAT 7000")
        self.assertIn("io-1", self.hal(w).yaml.toPlainText())
        self.assertIn("Bus read · 2 slaves", w.header.status.text())

    def test_pdo_assignment_box(self):
        # the box shows the ESI's predefined assignment the file uses, and choosing
        # another one proposes the slave again with it (the user's EL5101 on turner)
        w = self.window()
        p = w.session.bus[1].proposal
        pd = w.session.slave(1)["process-data"]
        used = sorted((pdo["sm"], int(pdo["pdo"], 16))
                      for pdo in pd.get("outputs", []) + pd.get("inputs", []))
        p.pdo_sets = ["Standard 16 Bit", "Standard 32 Bit"]
        p.pdo_set_pdos = {"Standard 16 Bit": [(3, 0x1A03)], "Standard 32 Bit": used}
        w.select_slave(1)
        box = w.slave_page.pdo_set
        self.assertEqual(box.currentData(), "Standard 32 Bit")
        self.assertEqual(box.currentText(), "PDO assignment: Standard 32 Bit")
        with mock.patch.object(w, "propose_again") as again:
            box.setCurrentIndex(box.findData("Standard 32 Bit"))
            box.activated.emit(box.currentIndex())
            again.assert_not_called()                       # the one in use: nothing to do
            box.setCurrentIndex(box.findData("Standard 16 Bit"))
            box.activated.emit(box.currentIndex())
            again.assert_called_once()
            self.assertEqual(again.call_args[0][0], 1)
            self.assertEqual(again.call_args[0][2], "Standard 16 Bit")
        # an edited mapping is none of them
        pd.setdefault("inputs", []).append({"pdo": "0x1A07", "sm": 3, "entries": []})
        self.assertIsNone(gui.pdo_set_in_use(w.session.slave(1), p))
        self.assertIsNone(gui.pdo_set_in_use(w.session.slave(1), None))

    def test_edit_a_pin_and_see_an_error(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        row = next(r for r in range(page.pd.rowCount())
                   if page.pd.item(r, page.PIN).flags() & gui.Qt.ItemIsEditable
                   and page.pd.item(r, page.PIN).text())
        page.pd.item(row, page.PIN).setText("bad pin")
        self.assertEqual(page._entry(row)["pin"], "bad pin")
        self.assertIn("bad pin", self.hal(w).yaml.toPlainText())
        self.assertIn("1 problem", self.rows(w)[1][1])
        self.assertIn("except slave 0", w.bus_page.list.summary.text())
        self.assertTrue(w.session.dirty)
        self.assertIn(" *", w.header.info.text())
        page.pd.item(row, page.PIN).setText("")
        self.assertNotIn("pin", page._entry(row))
        self.assertNotIn("1 problem", self.rows(w)[1][1])

    def test_a_32_bit_pin_becomes_a_bit_field_and_back(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        row = next(r for r in range(page.pd.rowCount())
                   if page.pd.item(r, page.PIN).flags() & gui.Qt.ItemIsEditable
                   and page.pd.item(r, page.PIN).text()
                   and (page._entry(r).get("bits") or 0) >= 8)
        e = page._entry(row)
        base, bits = e["pin"], e["bits"]
        page.pd.cellWidget(row, page.HALTYPE).setCurrentText(page.BIT_FIELD)
        self.assertNotIn("pin", e)
        self.assertNotIn("hal-type", e)
        self.assertEqual(e["split"], ["%s-%d" % (base, i) for i in range(min(bits, 32))])
        self.assertEqual(page.pd.item(row, page.USE).text(), "split")
        page.pd.cellWidget(row, page.HALTYPE).setCurrentText("uint")
        self.assertEqual(e["pin"], base)
        self.assertEqual(e["hal-type"], "uint")
        self.assertNotIn("split", e)

    def test_the_mouse_wheel_over_a_pin_box_connects_nothing(self):
        from PyQt5.QtCore import QPoint, QPointF
        from PyQt5.QtGui import QWheelEvent
        from PyQt5.QtWidgets import QApplication
        from ecatsetup import integrate
        w = self.window()
        w.select_slave(0)
        self.hal(w)
        boxes = [w.hal_page.pins.cellWidget(r, 5) for r in range(w.hal_page.pins.rowCount())]
        boxes = [b for b in boxes if b is not None and b.count() > 1]
        self.assertTrue(boxes)
        for b in boxes:
            before = b.currentText()
            QApplication.sendEvent(b, QWheelEvent(
                QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120), 0,
                gui.Qt.Vertical, gui.Qt.NoButton, gui.Qt.NoModifier))
            self.assertEqual(b.currentText(), before)
        self.assertEqual(integrate.nets(w.session.cfg), [])

    def test_bit_pins_are_edited_in_a_column_of_sane_width(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        row = next(r for r in range(page.pd.rowCount())
                   if page.pd.item(r, page.PIN).flags() & gui.Qt.ItemIsEditable
                   and page.pd.item(r, page.PIN).text()
                   and (page._entry(r).get("bits") or 0) >= 8)
        page.pd.cellWidget(row, page.HALTYPE).setCurrentText(page.BIT_FIELD)
        split = page.pd.item(row, page.SPLIT)
        self.assertTrue(split.flags() & gui.Qt.ItemIsEditable)
        self.assertFalse(page.pd.item(row, page.PIN).flags() & gui.Qt.ItemIsEditable)
        self.assertLess(page.pd.columnWidth(page.SPLIT), 600)    # not as wide as all the names
        hdr = page.pd.horizontalHeader()
        self.assertEqual(hdr.sectionResizeMode(page.SPLIT), gui.QHeaderView.Interactive)  # draggable
        self.assertTrue(all(page.pd.columnWidth(c) <= 280 for c in range(page.pd.columnCount())
                            if c != page.PIN))
        page.pd_double_clicked(row, page.PIN)                    # takes you to the bit pins
        self.assertEqual(page.pd.currentColumn(), page.SPLIT)
        split.setText("lamp, valve, torch, spare")
        self.assertEqual(page._entry(row)["split"], ["lamp", "valve", "torch", "spare"])

    def test_still_to_connect_can_invert_a_bit_pin(self):
        from PyQt5.QtWidgets import QCheckBox
        from ecatsetup import integrate, wiring
        w = self.window()
        page = self.hal(w)
        s = w.session
        pin = next(p for p, (ht, way) in sorted(integrate.slave_pins(s.cfg).items())
                   if ht == "bool" and way == "out" and integrate.pin_inverted(s.cfg, p) is False)
        page.wiring_rows = [wiring.Row("motion.digital-in-00", "bool", "in", "connected",
                                       target="motion.digital-in-00", slave=pin)]
        page.wiring.setRowCount(1)
        page.wiring.setCellWidget(0, 5, QCheckBox())
        page.wiring_inverted(pin, True)
        self.assertIs(integrate.pin_inverted(s.cfg, pin), True)
        page.wiring_inverted(pin, False)
        self.assertIs(integrate.pin_inverted(s.cfg, pin), False)

    def test_show_value_in_hex(self):
        self.assertEqual(gui.show_value(255, "UINT", True), "0x00FF")
        self.assertEqual(gui.show_value(255, "UINT", False), 255)
        self.assertEqual(gui.show_value(-1, "INT", True), "0xFFFF")
        self.assertEqual(gui.show_value(-1, "", True), -1)           # no type: stays decimal
        self.assertEqual(gui.show_value(5, "USINT", True), "0x05")
        self.assertEqual(gui.show_value(1.5, "REAL", True), 1.5)
        self.assertEqual(gui.show_value(True, "BOOL", True), True)
        self.assertEqual(gui.show_value(70000, None, True), "0x11170")
        self.assertEqual(gui.show_value(None, None, True), "")

    def test_settings_values_in_hex_and_back(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        s = w.session.slave(0)
        s["settings"] = [{"object": "0x2000:03", "name": "f", "type": "UDINT", "value": 255}]
        page.present[(0, "0x2000:03")] = "16"
        page.show_settings(s)
        self.assertEqual(page.st.item(0, 3).text(), "255")
        w.set_hex_values(True)
        self.assertEqual(page.st.item(0, 3).text(), "0x000000FF")
        self.assertEqual(page.st.item(0, 4).text(), "0x00000010")
        self.assertTrue(w.test_page.hex_box.isChecked())          # one setting for both places
        page.st.item(0, 3).setText("0x10")                         # typed in hex: stored as number
        self.assertEqual(s["settings"][0]["value"], 16)
        w.set_hex_values(False)
        self.assertEqual(page.st.item(0, 3).text(), "16")
        self.assertFalse(page.hex_box.isChecked())

    def test_the_save_button_shows_unsaved_changes(self):
        w = self.window()
        w.select_slave(0)
        w.session.cfg["slaves"][0]["name"] = "changed"
        w.changed()
        self.assertIn("Save changes", w.header.save.text())
        self.assertEqual(w.header.save.objectName(), "savedirty")
        w.session.save(os.path.join(self.dir.name, "x.yaml"))
        w.refresh()
        self.assertEqual(w.header.save.text(), "Saved")
        self.assertEqual(w.header.save.objectName(), "saveclean")

    def test_save_offers_to_create_a_missing_folder(self):
        from unittest import mock
        w = self.window()
        path = os.path.join(self.dir.name, "new", "deeper", "ethercat.yaml")
        with mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Cancel):
            self.assertFalse(w._save(path))
        self.assertFalse(os.path.exists(os.path.dirname(path)))
        with mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Yes):
            self.assertTrue(w._save(path))
        self.assertTrue(os.path.exists(path))

    def test_write_into_linuxcnc_own_files_asks_first(self):
        from unittest import mock
        w = self.window()
        page = self.hal(w)
        page.plan = mock.Mock(changes=[], edited=[], notes=[], warnings=["lathe.ini is in LinuxCNC's own files"])
        ini = os.path.join(self.dir.name, "m.ini")
        open(ini, "w").write("[EMC]\n")
        w.session.ini_path = ini
        answers = []

        def question(*a, **k):
            return gui.QMessageBox.Yes
        asked = []

        def warning(parent, title, text, *rest):
            asked.append(text)
            return gui.QMessageBox.Cancel
        with mock.patch.object(gui.QMessageBox, "question", question), \
                mock.patch.object(gui.QMessageBox, "warning", warning):
            page.write()
        self.assertEqual(len(asked), 1)
        self.assertIn("Write here anyway?", asked[0])
        self.assertIn("LinuxCNC's own files", asked[0])
        self.assertFalse(os.path.exists(os.path.join(self.dir.name, "ethercat.yaml")))   # nothing written

    def test_write_question_says_what_happens_to_each_file(self):
        from unittest import mock
        from ecatsetup import integrate
        w = self.window()
        page = self.hal(w)
        page.plan = integrate.Plan(
            changes=[integrate.FileChange(os.path.join(self.dir.name, "sim.hal"), "new", "", "x",
                                          "sim.tcl as HAL, 2 lines commented out")],
            notes=["sim.tcl is turned into sim.hal: the Tcl run once"])
        ini = os.path.join(self.dir.name, "m.ini")
        open(ini, "w").write("[EMC]\n")
        w.session.ini_path = ini
        asked = []

        def question(parent, title, text, *rest):
            asked.append(text)
            return gui.QMessageBox.Cancel
        with mock.patch.object(gui.QMessageBox, "question", question):
            page.write()
        self.assertEqual(len(asked), 1)
        self.assertIn("sim.hal: sim.tcl as HAL, 2 lines commented out", asked[0])
        self.assertIn("- sim.tcl is turned into sim.hal", asked[0])

    def test_the_ini_file_can_be_chosen_again(self):
        from unittest import mock
        w = self.window()
        page = self.hal(w)
        w.tabs.setCurrentWidget(page)
        page.refresh()
        self.assertIn("Choose the machine's INI file", page.ini_text.text())
        self.assertEqual(page.ini_button.text(), "INI file...")
        ini = os.path.join(self.dir.name, "m.ini")
        with open(ini, "w") as f:
            f.write("[EMCMOT]\nSERVO_PERIOD = 500000\n")
        with mock.patch.object(gui.QFileDialog, "getOpenFileName", lambda *a, **k: (ini, "")):
            w.a_ini.trigger()
        self.assertEqual(w.session.ini_path, ini)
        self.assertEqual(w.session.period, 500000)
        self.assertFalse(page.ini_row.isHidden())
        self.assertIn(ini, page.ini_text.text())
        self.assertIn("read again", page.ini_button.text())
        # the same file again: read again
        with open(ini, "w") as f:
            f.write("[EMCMOT]\nSERVO_PERIOD = 1000000\n")
        with mock.patch.object(gui.QFileDialog, "getOpenFileName", lambda *a, **k: (ini, "")):
            page.ini_button.click()
        self.assertEqual(w.session.period, 1000000)

    def test_the_ini_files_configuration_is_opened(self):
        """With no file open, choosing the INI file opens the configuration it
        names ([ETHERCAT]CONFIG); a new one with changes only when the user says
        so. (Watching LinuxCNC on a new configuration showed nothing, 2026-11-26.)"""
        from ecatsetup import config
        w = self.window()
        d = self.dir.name
        with open(os.path.join(d, "ethercat.yaml"), "w") as f:
            f.write(config.dumps(w.session.cfg))
        ini = os.path.join(d, "m.ini")
        with open(ini, "w") as f:
            f.write("[ETHERCAT]\nCONFIG = ethercat.yaml\n")
        # the new configuration has changes: asked, Cancel keeps it
        self.assertTrue(w.session.dirty)
        with mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Cancel) as q:
            w.set_ini(ini)
        q.assert_called_once()
        self.assertIsNone(w.session.path)
        with mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Open):
            w.set_ini(ini)
        self.assertEqual(w.session.path, os.path.join(d, "ethercat.yaml"))
        # a file open: left alone
        with open(ini, "w") as f:
            f.write("[ETHERCAT]\nCONFIG = other.yaml\n")
        with open(os.path.join(d, "other.yaml"), "w") as f:
            f.write(config.dumps(w.session.cfg))
        w.set_ini(ini)
        self.assertEqual(w.session.path, os.path.join(d, "ethercat.yaml"))
        # an INI naming no file, or one that is not there: nothing
        from ecatsetup import integrate
        self.assertIsNone(integrate.ini_config(os.path.join(d, "none.ini")))
        with open(ini, "w") as f:
            f.write("[EMC]\n")
        self.assertIsNone(integrate.ini_config(ini))

    def test_servo_period_typed_without_an_ini_file(self):
        from unittest import mock
        w = self.window()
        w.select_slave(gui.MASTER_ITEM)
        m = w.master_page
        self.assertTrue(m.period.isEnabled())
        self.assertEqual(m.period.value(), 1000)
        self.assertIn("cycle 1 ms (default)", w.header.info.text())
        m.period.setValue(500)
        self.assertEqual(w.session.period, 500000)
        self.assertIn("cycle 0.5 ms", w.header.info.text())
        self.assertNotIn("default", w.header.info.text())
        # an INI file with SERVO_PERIOD: its value, not editable
        ini = os.path.join(self.dir.name, "m.ini")
        with open(ini, "w") as f:
            f.write("[EMCMOT]\nSERVO_PERIOD = 2000000\n")
        w.set_ini(ini)
        self.assertEqual(w.session.period, 2000000)
        self.assertFalse(m.period.isEnabled())
        self.assertEqual(m.period.value(), 2000)
        self.assertIn("(from INI)", w.header.info.text())
        # one without: the typed period stays and can be changed
        with open(ini, "w") as f:
            f.write("[EMC]\n")
        w.set_ini(ini)
        self.assertEqual(w.session.period, 2000000)
        self.assertTrue(m.period.isEnabled())
        self.assertIn("no SERVO_PERIOD", m.ini_label.text())
        m.period.setValue(250)
        self.assertEqual(w.session.period, 250000)
        # the I/O test gets it
        from ecatsetup import testmode
        got = []

        class Run:
            def __init__(self, cfg, period):
                got.append(period)
                raise testmode.TestError("stop here")
        with mock.patch.object(testmode, "TestRun", Run), \
                mock.patch.object(gui.QMessageBox, "question", lambda *a, **k: gui.QMessageBox.Yes), \
                mock.patch.object(gui.QMessageBox, "warning", lambda *a, **k: None):
            w.start_test()
        self.assertEqual(got, [250000])

    def test_a_handshake_row_offers_the_handshake_pins(self):
        from unittest import mock
        from ecatsetup import integrate, wiring
        w = self.window()
        page = self.hal(w)
        ini = os.path.join(self.dir.name, "m.ini")
        open(ini, "w").write("[EMC]\n")
        w.session.ini_path = ini
        rows = [wiring.Row("spindle.0.index-enable", "bool", "io", "simulated",
                           target="spindle.0.index-enable"),
                wiring.Row("spindle.0.revs", "real", "in", "simulated", target="spindle.0.revs")]
        own = {"io.enc.index-enable": ("bool", "io"), "io.enc.pos": ("real", "out"),
               "io.enc.reset": ("bool", "in"), "io.in-1": ("bool", "out")}
        with mock.patch.object(wiring, "rows", return_value=rows), \
                mock.patch.object(integrate, "slave_pins", return_value=own):
            page.show_wiring()
        items = lambda r: [page.wiring.cellWidget(r, 4).itemText(i)
                           for i in range(page.wiring.cellWidget(r, 4).count())]
        self.assertEqual(items(0), ["", "io.enc.index-enable"])    # both ways, a bool
        self.assertEqual(items(1), ["", "io.enc.pos"])             # written by the slave, a real
        self.assertIn("\u2194", page.wiring.item(0, 0).toolTip())

    def test_an_encoder_channels_pins_are_rows_to_connect(self):
        from ecatsetup import integrate
        w = self.window()
        cfg = w.session.cfg
        io = cfg["slaves"][0]
        io["encoders"] = [{"name": "enc", "index": "0x6010"}]
        io["process-data"] = {
            "outputs": [{"pdo": "0x1603", "sm": 2, "entries": [
                {"object": "0x7010:01", "bits": 1, "type": "BOOL", "use": "profile"},
                {"object": "0x7010:11", "bits": 32, "type": "UDINT", "use": "profile"}]}],
            "inputs": [{"pdo": "0x1A04", "sm": 3, "entries": [
                {"object": "0x6010:01", "bits": 1, "type": "BOOL", "use": "profile"},
                {"object": "0x6010:11", "bits": 32, "type": "UDINT", "use": "profile"},
                {"object": "0x6010:12", "bits": 32, "type": "UDINT", "use": "profile"}]}]}
        name = io["name"]
        rows = {r[2]: r for r in gui.config_pins(cfg)}
        self.assertEqual(rows["%s.enc.pos" % name][3:], ("real", "from the slave"))
        self.assertEqual(rows["%s.enc.index-enable" % name][3:], ("bool", "both ways"))
        self.assertIn("%s.enc.index-enable" % name, integrate.slave_pins(cfg))

    def test_bit_pins_can_be_inverted(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        row = next(r for r in range(page.pd.rowCount()) if "split" in page._entry(r)
                   and "in-4" in page._entry(r)["split"])
        e = page._entry(row)
        page.pd.item(row, page.INVERT).setText("in-4, in-7, nonsense")
        self.assertEqual(e["invert"], ["in-4", "in-7"])         # only names that are there
        self.assertEqual(page.pd.item(row, page.INVERT).text(), "in-4, in-7")
        page.pd.item(row, page.SPLIT).setText(", ".join(
            n for n in e["split"] if n != "in-4"))
        self.assertEqual(e["invert"], ["in-7"])                  # a removed pin is not inverted
        page.pd.item(row, page.INVERT).setText("")
        self.assertNotIn("invert", e)
        # a single bit pin: yes, and only with the type bool
        row = next(r for r in range(page.pd.rowCount()) if "pin" in page._entry(r)
                   and not page._entry(r).get("use"))
        e = page._entry(row)
        page.pd.cellWidget(row, page.HALTYPE).setCurrentText("uint")
        page.pd.item(row, page.INVERT).setText("yes")
        self.assertNotIn("invert", e)
        page.pd.cellWidget(row, page.HALTYPE).setCurrentText("bool")
        page.pd.item(row, page.INVERT).setText("yes")
        self.assertIs(e["invert"], True)
        page.pd.cellWidget(row, page.HALTYPE).setCurrentText("uint")
        self.assertNotIn("invert", e)

    def test_a_one_bit_entry_cannot_become_a_bit_field(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        row = next(r for r in range(page.pd.rowCount())
                   if page.pd.item(r, page.PIN).flags() & gui.Qt.ItemIsEditable
                   and page.pd.item(r, page.PIN).text()
                   and (page._entry(r).get("bits") or 0) >= 8)
        e = page._entry(row)
        e["bits"] = 1      # the fake bus has no one-bit entry with a pin
        before = dict(e)
        page.pd.cellWidget(row, page.HALTYPE).setCurrentText(page.BIT_FIELD)
        self.assertEqual(e, before)
        self.assertEqual(page.pd.cellWidget(row, page.HALTYPE).currentText(), before.get("hal-type", ""))

    def test_rename_slave(self):
        w = self.window()
        w.select_slave(1)
        w.slave_page.name.setText("thc")
        w.slave_page.general_edited()
        self.assertEqual(w.session.slave(1)["name"], "thc")
        self.assertIn("thc · Modular device", self.rows(w)[2][1])
        self.assertIs(w.bus_page.stack.currentWidget(), w.slave_page)
        self.assertTrue(any(r[2] == "thc.out-1" for r in gui.config_pins(w.session.cfg)))

    def test_update_from_bus_shows_the_changes_first(self):
        w = self.window()
        s = w.session
        # the file has one slave less: the second becomes new
        del s.cfg["slaves"][1]
        self.assertIn("The bus differs from the configuration: 1 new", w.bus_summary())
        before = copy.deepcopy(s.cfg)
        with mock.patch.object(gui.QMessageBox, "question",
                               return_value=gui.QMessageBox.Cancel) as q:
            w.update_from_bus()
        self.assertIn("new: position 1", q.call_args[0][2])
        self.assertEqual(s.cfg, before)
        with mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Yes):
            w.update_from_bus()
        self.assertEqual(len(s.cfg["slaves"]), 2)
        self.assertIn("matches", w.bus_summary())

    def test_a_test_that_cannot_start_says_why_in_a_dialog(self):
        w = self.window()
        w.worker = mock.Mock()
        with mock.patch.object(gui.QMessageBox, "warning") as warn:
            w._failed("Workbench: The kernel EtherCAT master is loaded.\nStop it first")
        self.assertIn("did not start", warn.call_args[0][2])
        self.assertIn("kernel EtherCAT master", warn.call_args[0][2])

    def test_troubleshoot_page_has_the_flow(self):
        w = self.window()
        page = w.trouble_page
        with mock.patch.object(w, "start_watch") as watch:
            page.watch_button.click()
        watch.assert_called_once()
        self.assertIn("Save a report", "".join(
            lab.text() for lab in page.findChildren(gui.QLabel)))

    def test_watch_while_the_io_test_runs_says_where_to_look(self):
        w = self.window()
        w.test = mock.Mock(running=True, watching=False)
        try:
            with mock.patch.object(w, "watch_busy_choice", return_value="live") as ask, \
                    mock.patch.object(gui.testmode, "WatchRun") as watch, \
                    mock.patch.object(w, "show_tab") as show:
                w.start_watch()
            ask.assert_called_once_with()
            watch.assert_not_called()
            show.assert_called_once_with(w.live_page)
            with mock.patch.object(w, "watch_busy_choice", return_value="stop"), \
                    mock.patch.object(w, "stop_test") as stop:
                w.start_watch()
            stop.assert_called_once()
            # already watching: no question, the Live tab
            w.test.watching = True
            with mock.patch.object(w, "watch_busy_choice") as ask, \
                    mock.patch.object(w, "show_tab") as show:
                w.start_watch()
            ask.assert_not_called()
            show.assert_called_once_with(w.live_page)
        finally:
            w.test = None

    def test_watch_busy_box_names_the_live_tab(self):
        w = self.window()
        seen = {}

        def fake_exec(box):
            seen["text"] = box.text() + " " + box.informativeText()
            seen["buttons"] = [b.text() for b in box.buttons()]
            return 0
        with mock.patch.object(gui.QMessageBox, "exec_", fake_exec):
            self.assertIsNone(w.watch_busy_choice())
        self.assertIn("Live tab", seen["text"])
        self.assertIn("cannot run while the workbench", seen["text"])
        self.assertIn("Stop the workbench", seen["buttons"])
        self.assertIn("Go to the Live tab", seen["buttons"])

    def test_live_tab_says_what_runs(self):
        w = self.window()
        live = w.live_page
        live.show_source()
        self.assertIn("No master runs here", live.source_text.text())
        self.assertFalse(live.source_watch.isHidden())
        self.assertTrue(live.source_stop.isHidden())
        w.master_log.pid = 7
        live.show_source()
        self.assertIn("The free master runs", live.source_text.text())
        w.test = mock.Mock(running=True, watching=False)
        try:
            live.show_source()
            self.assertIn("The workbench runs", live.source_text.text())
            self.assertTrue(live.source_watch.isHidden())
            self.assertEqual(live.source_stop.text(), "Stop the workbench")
            w.test.watching = True
            live.show_source()
            self.assertIn("Watching LinuxCNC", live.source_text.text())
            self.assertEqual(live.source_stop.text(), "Stop watching")
            self.assertTrue(live.source_bench.isHidden())
        finally:
            w.test = None
            w.master_log.pid = None

    def test_live_pins_are_read_only(self):
        w = self.window()
        s = w.session.slave(0)
        s["profile"], s["name"] = "cia402", "x"
        s["cia402"] = {"mode": "csp"}
        P = gui.testmode.Pin
        pins = {"x.cia-state": P("x.cia-state", "uint", "OUT", "4"),
                "x.statusword": P("x.statusword", "uint", "OUT", "0x1237"),
                "x.error-code": P("x.error-code", "uint", "OUT", "0"),
                "x.enable": P("x.enable", "bool", "IN", "FALSE")}
        run = mock.Mock(running=True, watching=False)
        run.pins = lambda prefix: {n: p for n, p in pins.items() if n.startswith(prefix)}
        run.writable = lambda pin: True              # the Workbench could set it: Live does not
        w.test, w.current = run, 0
        try:
            live = w.live_page
            live.update_pins()
            self.assertFalse(live.pins_card.isHidden())
            self.assertEqual(live.pins.rowCount(), 4)
            self.assertTrue(live.pins.isColumnHidden(4))
            self.assertTrue(all(live.pins.cellWidget(r, 4) is None for r in range(4)))
            self.assertIn("Operation enabled", live.pins_drive.text())
            self.assertIn("0x1237", live.pins_drive.text())
            self.assertNotIn("error", live.pins_drive.text())
            pins["x.cia-state"].value, pins["x.error-code"].value = "6", "0x7500"
            live.update_pins()
            self.assertIn("Fault", live.pins_drive.text())
            self.assertIn("error 0x7500", live.pins_drive.text())
            r = live.pins.rows["x.cia-state"]
            self.assertEqual(live.pins.item(r, live.pins.VALUE).text(), "6")
        finally:
            w.test = None
        w.live_page.update_pins()
        self.assertTrue(w.live_page.pins_card.isHidden())

    def test_live_overview_of_the_slaves(self):
        w = self.window()
        s0, s1 = w.session.slave(0), w.session.slave(1)
        s0["profile"], s0["name"] = "cia402", "x"
        s0["cia402"] = {"mode": "csp"}
        s1["name"] = "io"
        every = {"x.slave-online": "TRUE", "x.slave-state-op": "TRUE", "x.cia-state": "4",
                 "io.slave-online": "TRUE", "io.slave-state-op": "FALSE",
                 "io.slave-state-preop": "TRUE"}
        live = w.live_page
        live.update_overview(every)
        t = live.overview_table
        self.assertFalse(live.overview.isHidden())
        self.assertEqual([[t.item(r, c).text() for c in range(4)] for r in range(t.rowCount())],
                         [["0", "x", "OP", "Operation enabled"], ["1", "io", "PREOP", ""]])
        every["io.slave-online"] = "FALSE"
        every["x.cia-state"] = "7"
        live.update_overview(every)
        self.assertEqual((t.item(0, 3).text(), t.item(1, 2).text()), ("Fault", "offline"))
        live.update_overview({})
        self.assertTrue(live.overview.isHidden())

    def test_a_click_in_the_overview_chooses_the_slave(self):
        """The slave chooser sits in the Pins card, below the fold on a small
        screen (the user, 2026-11-26): a row of Slaves chooses the slave, and
        the chosen one's row is marked."""
        w = self.window()
        s0, s1 = w.session.slave(0), w.session.slave(1)
        s0["name"], s1["name"] = "x", "io"
        every = {"x.slave-online": "TRUE", "x.slave-state-op": "TRUE",
                 "io.slave-online": "TRUE", "io.slave-state-op": "TRUE"}
        P = gui.testmode.Pin
        pins = {"io.in-1": P("io.in-1", "bool", "OUT", "TRUE")}
        run = mock.Mock(running=True, watching=True)
        run.pins = lambda prefix: {n: p for n, p in pins.items() if n.startswith(prefix)}
        w.test, w.current = run, 0
        try:
            live = w.live_page
            live.update_overview(every)
            t = live.overview_table
            self.assertEqual(t.currentRow(), 0)
            with mock.patch.object(live, "scroll_to_pins") as scroll:
                t.cellClicked.emit(1, 1)
                self.app.processEvents()
            self.assertEqual(w.current, 1)
            self.assertFalse(live.pins_card.isHidden())
            self.assertEqual(live.pins.rowCount(), 1)
            self.assertEqual(live.slave.currentData(), 1)
            scroll.assert_called_once()
            live.update_overview(every)
            self.assertEqual(t.currentRow(), 1)
            # as tall as its header and rows: no empty row under the last
            self.assertEqual(t.height(), t.horizontalHeader().sizeHint().height() + 4
                             + sum(t.rowHeight(r) for r in range(t.rowCount())))
        finally:
            w.test = None

    def test_workbench_does_nothing_while_watching(self):
        w = self.window()
        s = w.session.slave(0)
        s["profile"], s["name"] = "cia402", "x"
        s["cia402"] = {"mode": "csp"}
        run = mock.Mock(running=True, watching=True, period=1000000, log_path="")
        run.pins = lambda prefix: {}
        w.test, w.current = run, 0
        try:
            w.test_page.show_slave()
            self.assertTrue(w.test_page.drive.isHidden())
            self.assertTrue(w.test_page.all_card.isHidden())
            self.assertIn("Live tab", w.test_page.banner_text.text())
            run.watching = False
            w.test_page.show_slave()
            self.assertFalse(w.test_page.drive.isHidden())
        finally:
            w.test = None

    def test_tab_names(self):
        w = self.window()
        names = [w.tabs.bar.tabText(i) for i in range(w.tabs.count())]
        self.assertEqual(names[2:5], ["Workbench", "Live", "Diagnose"])
        self.assertNotIn("Test", names)

    def test_timer_tables_are_not_filled_again_unchanged(self):
        # the Live tab's event log and master's messages are refreshed every 2 s:
        # filling them again each time held the window up and made scrolling slow
        w = self.window()
        t = gui.table(["A", "B"], 1)
        rows = [(("1", None, "mono"), ("x", gui.RED, None))]
        self.assertTrue(gui.put_rows(t, rows))
        self.assertFalse(gui.put_rows(t, list(rows)))
        self.assertEqual((t.rowCount(), t.item(0, 1).text()), (1, "x"))
        self.assertTrue(gui.put_rows(t, rows + [(("2", None, None), ("y", None, None))]))
        self.assertEqual(t.rowCount(), 2)
        self.assertTrue(gui.put_rows(t, []))
        self.assertEqual(t.rowCount(), 0)
        live = w.trouble_page
        live.show_events()
        with mock.patch.object(live.events_table, "setItem") as put:
            live.show_events()
        put.assert_not_called()
        w.set_theme("dark", save=False)
        w.set_theme("light", save=False)

    def test_sdo_card_is_off_while_watching(self):
        w = self.window()
        run = mock.Mock(running=True, watching=True)
        w.test = run
        try:
            w.test_page.show_slave()
            self.assertFalse(w.test_page.sdo_card.isEnabled())
            self.assertIn("Not while watching", w.test_page.sdo_card.hint.text())
        finally:
            w.test = None

    def test_progress_shows_while_a_test_starts(self):
        w = self.window()
        page = w.test_page
        w.status("ignored")
        self.assertEqual(page.starting_text.text(), "")
        page.starting.show()
        w.status("3/5 Starting halrun")
        self.assertEqual(page.starting_text.text(), "3/5 Starting halrun")

    def test_zero_at_start(self):
        w = self.window()
        s = w.session.slave(0)
        s["profile"] = "cia402"
        s["cia402"] = {"mode": "csp", "zero-at-start": True}
        w.select_slave(0)
        page = w.slave_page
        self.assertTrue(page.zero_at_start.isChecked())
        page.zero_at_start.setChecked(False)
        self.assertNotIn("zero-at-start", s["cia402"])
        page.zero_at_start.setChecked(True)
        self.assertIs(s["cia402"]["zero-at-start"], True)
        self.assertIn("zero-at-start: true", self.hal(w).yaml.toPlainText())

    def test_master_start_asks_and_runs_only_on_yes(self):
        from ecatsetup import masterstart
        w = self.window()
        ran = []
        w.run = lambda what, fn, done: done(fn(lambda text: None))
        w.read_bus = lambda: ran.append("read")
        with mock.patch.object(masterstart, "linuxcnc_running", lambda: False), \
                mock.patch.object(masterstart, "conf_interface", lambda path=None: "enp1s0"), \
                mock.patch.object(masterstart, "run",
                                  lambda p, noninteractive=True: ran.append(("run", noninteractive)) or (True, "ok")):
            with mock.patch.object(gui.QMessageBox, "exec_", lambda self: gui.QMessageBox.No):
                w.offer_master_start()
            self.assertEqual(ran, [])
            with mock.patch.object(gui.QMessageBox, "exec_", lambda self: gui.QMessageBox.Yes):
                w.offer_master_start()
            self.assertEqual(ran, [("run", True), "read"])      # sudo -n, then the bus is read
            self.assertTrue(w.master_just_started)
        ran.clear()
        with mock.patch.object(masterstart, "linuxcnc_running", lambda: True), \
                mock.patch.object(gui.QMessageBox, "exec_", lambda self: ran.append("asked")):
            w.offer_master_start(force=True)
        self.assertEqual(ran, [])                               # LinuxCNC has the bus

    def test_master_start_asks_for_the_card_when_nothing_names_it(self):
        from ecatsetup import masterstart
        w = self.window()
        w.session.cfg = None            # no file yet
        plans = []
        w.run = lambda what, fn, done: None
        with mock.patch.object(masterstart, "linuxcnc_running", lambda: False), \
                mock.patch.object(masterstart, "conf_interface", lambda path=None: None), \
                mock.patch.object(masterstart, "wired", lambda *a: [("lo0", True), ("enp9s0", False)]), \
                mock.patch.object(masterstart, "plan", wraps=masterstart.plan) as plan, \
                mock.patch.object(gui.QMessageBox, "exec_", lambda self: plans.append(
                    self.informativeText()) or gui.QMessageBox.No):
            with mock.patch.object(gui.QInputDialog, "getItem", return_value=("", False)):
                w.offer_master_start()
            self.assertEqual(plans, [])                         # cancelled: nothing more
            with mock.patch.object(gui.QInputDialog, "getItem",
                                   return_value=("enp9s0  (no link)", True)) as ask:
                w.offer_master_start()
            self.assertIn("enp9s0  (no link)", ask.call_args[0][3])
            self.assertEqual(plan.call_args.kwargs.get("interface"), "enp9s0")
            self.assertEqual(len(plans), 1)                     # then the command is shown

    def test_bus_rows_fit_their_text(self):
        w = self.window()
        w.session.slave(0)["name"] = "x-axis-drive-with-a-long-name"   # a subtitle that wraps
        w.refresh()
        w.show()
        self.addCleanup(w.hide)
        gui.QApplication.processEvents()
        lst = w.bus_page.list.list
        for i in range(lst.count()):
            row = lst.itemWidget(lst.item(i))
            need = row.layout().totalHeightForWidth(row.width())
            self.assertGreaterEqual(lst.item(i).sizeHint().height(), need, "row %d" % i)

    def test_hal_check_shows_a_long_message_whole(self):
        from ecatsetup import halcheck
        w = self.window()
        ini = os.path.join(self.dir.name, "m.ini")
        open(ini, "w").write("[EMC]\n")
        w.session.ini_path = ini
        long_text = "pin io-0.some-pin-that-does-not-exist is used by a net " + "and so on " * 40
        found = [halcheck.Finding("error", "ethercat.hal", 12, long_text)]
        w.show()
        self.addCleanup(w.hide)
        w.tabs.setCurrentWidget(w.hal_page)
        with mock.patch.object(halcheck, "check", lambda *a, **k: found):
            w.hal_page.show_check()
        gui.QApplication.processEvents()
        t = w.hal_page.check_table
        self.assertTrue(t.wordWrap())
        self.assertEqual(t.item(0, 2).toolTip(), long_text)
        one_line = t.fontMetrics().height()
        self.assertGreater(t.rowHeight(0), 3 * one_line)       # wrapped, not cut off
        self.assertGreaterEqual(t.height(), t.rowHeight(0))

    def test_the_expanded_ini_is_warned_about_on_the_hal_page(self):
        w = self.window()
        src = os.path.join(self.dir.name, "m.ini")
        open(src, "w").write("[EMC]\n")
        open(src + ".expanded", "w").write("[EMC]\n")
        w.session.ini_path = src
        w.hal_page.show_joints()
        self.assertTrue(w.hal_page.expanded_warning.isHidden())
        w.session.ini_path = src + ".expanded"
        w.hal_page.show_joints()
        self.assertFalse(w.hal_page.expanded_warning.isHidden())
        self.assertIn("m.ini", w.hal_page.expanded_warning.text())
        self.assertIn("writes from", w.hal_page.expanded_warning.text())

    def test_a_selected_pin_shows_what_it_does_and_links(self):
        from ecatsetup import wiring
        w = self.window()
        page = self.hal(w)
        page.wiring_rows = [wiring.Row("plasmac.breakaway", "bool", "in", "open"),
                            wiring.Row("mycomp.0.in", "bool", "in", "open")]
        page.wiring.setRowCount(2)
        page.wiring.selectRow(0)
        text = page.wiring_info.text()
        self.assertIn("breakaway", text)
        self.assertIn("https://linuxcnc.org/docs/html/plasma/qtplasmac.html", text)
        self.assertIn("The whole QtPlasmaC page", text)
        page.wiring.selectRow(1)
        self.assertIn("no documentation known", page.wiring_info.text())

    def test_mode_switching(self):
        w = self.window()
        s = w.session.slave(0)
        s["profile"] = "cia402"
        s["cia402"] = {"mode": "csp"}
        s["identity"] = {"vendor": "0x00000A88", "product": "0x0A880002"}
        w.select_slave(0)
        page = w.slave_page
        self.assertEqual(page.opmode_hint.text(), "")
        self.assertFalse(page.other_modes["csp"].isEnabled())    # the mode it starts in
        self.assertTrue(page.other_modes["hm"].isEnabled())
        page.other_modes["pp"].setChecked(True)
        self.assertEqual(s["cia402"]["modes"], ["pp"])
        self.assertIn("0 = CSP", page.opmode_hint.text())
        self.assertIn("1 = PP", page.opmode_hint.text())
        page.other_modes["hm"].setChecked(True)                  # homing has no number
        self.assertEqual(s["cia402"]["modes"], ["pp", "hm"])
        self.assertNotIn("HM", page.opmode_hint.text())

    def test_serial_card(self):
        w = self.window()
        s = w.session.slave(0)
        page = w.slave_page
        w.select_slave(0)
        self.assertTrue(page.serial_card.isHidden())          # no serial channels
        s["serial"] = [{"name": "port", "output-pdo": "0x1602", "input-pdo": "0x1A02",
                        "port": "/dev/ttyECAT0", "baud": 9600, "frame": "8N1"}]
        with mock.patch.object(w.session, "object_name",
                               lambda pos, i, sub: "Enable half duplex" if (i, sub) == (0x8000, 6) else ""):
            w.select_slave(0)
            self.assertFalse(page.serial_card.isHidden())
            row = page.serial_widgets[0]
            self.assertEqual(row["baud"].currentData(), 9600)
            row["baud"].setCurrentIndex(row["baud"].findData(19200))
            page.serial_edited(0, "baud")
            self.assertEqual(s["serial"][0]["baud"], 19200)
            row["frame"].setCurrentIndex(row["frame"].findData("8E1"))
            page.serial_edited(0, "frame")
            self.assertEqual(s["serial"][0]["frame"], "8E1")
            row["port"].setText("/dev/ttyVFD")
            page.serial_edited(0, "port")
            self.assertEqual(s["serial"][0]["port"], "/dev/ttyVFD")
            row["continuous"].setChecked(True)
            self.assertTrue(s["serial"][0]["send-continuous"])
            row["continuous"].setChecked(False)
            self.assertNotIn("send-continuous", s["serial"][0])
            # half duplex: a setting of the channel's settings object
            self.assertTrue(row["half"].isEnabled())
            row["half"].setChecked(True)
            self.assertIn({"object": "0x8000:06", "type": "BOOL", "value": 1}, s["settings"])
            row["half"].setChecked(False)
            self.assertNotIn("0x8000:06", [x["object"] for x in s["settings"]])
            # the lines for mb2hal follow
            lines = page.serial_lines.toPlainText()
            self.assertIn("SERIAL_PORT=/dev/ttyVFD", lines)
            self.assertIn("SERIAL_BAUD=19200", lines)
            self.assertIn("SERIAL_PARITY=even", lines)
            # shown again from the file
            w.select_slave(0)
            self.assertEqual(page.serial_widgets[0]["frame"].currentData(), "8E1")
        # a terminal without half duplex (EL6001): the box is off
        with mock.patch.object(w.session, "object_name", lambda pos, i, sub: ""):
            w.select_slave(0)
            self.assertFalse(page.serial_widgets[0]["half"].isEnabled())

    def test_used_checkbox(self):
        w = self.window()
        s = w.session.slave(1)
        page = w.slave_page
        w.select_slave(1)
        self.assertTrue(page.used.isChecked())
        self.assertNotIn("unused", s)
        page.used.setChecked(False)
        self.assertIs(s["unused"], True)
        self.assertEqual(config.check(w.session.cfg), [])
        self.assertTrue(config.yaml.safe_load(config.dumps(w.session.cfg))["slaves"][1]["unused"])
        w.select_slave(0)                                  # another slave, then back: kept
        w.select_slave(1)
        self.assertFalse(page.used.isChecked())
        page.used.setChecked(True)
        self.assertNotIn("unused", s)                      # the default is not written

    def test_eoe_card(self):
        w = self.window()
        s, other = w.session.slave(0), w.session.slave(1)
        page = w.slave_page
        w.select_slave(0)
        self.assertTrue(page.eoe_card.isHidden())             # the slave has no EoE
        w.session.bus[0].proposal.has_eoe = True
        nets = [("enp1s0", ipaddress.IPv4Network("192.168.10.0/24"))]
        with mock.patch.object(gui.config, "host_networks", lambda: nets):
            w.select_slave(0)
            self.assertFalse(page.eoe_card.isHidden())
            self.assertFalse(page.eoe_on.isChecked())
            self.assertFalse(page.eoe_fields["ip"].isEnabled())
            self.assertNotIn("eoe", s)
            # switched on: a subnet nobody has (the computer is in 192.168.10.x, slave 1 in .11.x)
            other["eoe"] = {"ip": "192.168.11.2/24"}
            page.eoe_on.setChecked(True)
            self.assertEqual(s["eoe"], {"ip": "192.168.12.2/24", "host-ip": "192.168.12.1"})
            self.assertEqual(page.eoe_fields["ip"].text(), "192.168.12.2/24")
            self.assertTrue(page.eoe_fields["ip"].isEnabled())
            self.assertTrue(page.eoe_fields["mask"].isHidden())
            self.assertIn("eoe0s0", page.eoe_hint.text())
            self.assertIn("ping 192.168.12.2,", page.eoe_hint.text())
            self.assertTrue(page.eoe_errors.isHidden())
            self.assertTrue(page.eoe_warn.isHidden())
            self.assertEqual(config.check(w.session.cfg), [])

            def typed(key, text):
                page.eoe_fields[key].setText(text)
                page.eoe_edited(key)
            typed("hostname", "drive-x")
            typed("mac", "12:00:00:00:10:21")
            self.assertEqual(s["eoe"]["hostname"], "drive-x")
            self.assertEqual(config.yaml.safe_load(config.dumps(w.session.cfg))["slaves"][0]["eoe"]["mac"],
                             "12:00:00:00:10:21")
            typed("hostname", "")                             # emptied: the key goes
            self.assertNotIn("hostname", s["eoe"])
            # an error and a warning are shown on the card
            typed("host-ip", "192.168.13.1")
            self.assertIn("not in the slave's subnet", page.eoe_errors.text())
            self.assertTrue(page.eoe_hint.isHidden())
            typed("host-ip", "192.168.12.1")
            self.assertTrue(page.eoe_errors.isHidden())
            typed("ip", "192.168.10.2/24")
            typed("host-ip", "192.168.10.1")
            self.assertIn("also the computer's own on enp1s0", page.eoe_warn.text())
            self.assertNotIn("slave 0", page.eoe_warn.text())
            # without the computer's address: the device is left down
            typed("host-ip", "")
            self.assertNotIn("host-ip", s["eoe"])
            self.assertIn("left down", page.eoe_hint.text())
            # switched off and on again: the block comes back as it was
            kept = dict(s["eoe"])
            page.eoe_on.setChecked(False)
            self.assertNotIn("eoe", s)
            page.eoe_on.setChecked(True)
            self.assertEqual(s["eoe"], kept)
            # a mask in the file has its own field
            s["eoe"] = {"ip": "192.168.12.2", "mask": "255.255.255.0"}
            w.select_slave(0)
            self.assertFalse(page.eoe_fields["mask"].isHidden())
            self.assertEqual(page.eoe_fields["mask"].text(), "255.255.255.0")
            # a block on a slave whose SII has no EoE: shown, with a warning
            w.session.bus[0].proposal.has_eoe = False
            w.select_slave(0)
            self.assertFalse(page.eoe_card.isHidden())
            self.assertIn("does not list the EoE", page.eoe_warn.text())

    def test_homing_card(self):
        w = self.window()
        s = w.session.slave(0)
        s["profile"] = "cia402"
        s["cia402"] = {"mode": "csp"}
        s["identity"] = {"vendor": "0x00000A88", "product": "0x0A880002"}
        w.select_slave(0)
        page = w.slave_page
        self.assertTrue(page.homing_card.isHidden())        # no hm in the modes
        page.other_modes["hm"].setChecked(True)
        self.assertFalse(page.homing_card.isHidden())
        self.assertEqual(page.home_method.itemData(0), None)
        i = page.home_method.findData(1)
        # the ECT60's quirk file does not list 1: advice, so it can still be chosen, marked
        self.assertTrue(page.home_method.model().item(i).isEnabled())
        self.assertIn("not in the quirk file's list", page.home_method.itemText(i))
        page.home_method.setCurrentIndex(page.home_method.findData(17))
        page.homing_edited()
        self.assertEqual(s["homing"]["method"], 17)
        self.assertIn("0x6098:00", [x["object"] for x in s["settings"]])
        page.home_fields["fast"].setValue(4000)
        self.assertEqual(s["homing"]["fast"], 4000)
        # each field names its object; the INI lines only when asked for (the user, 2026-11-22)
        form = page.home_hform
        self.assertEqual(form.labelForField(page.home_method).text(), "Method (SDO 0x6098:00)")
        self.assertIn("(SDO 0x6099:01)", form.labelForField(page.home_fields["fast"]).text())
        self.assertFalse(page.home_ini_title.isHidden())
        self.assertFalse(page.home_ini_title.isChecked())
        self.assertTrue(page.home_ini.isHidden() and page.home_copy.isHidden() and page.home_hal.isHidden())
        page.home_ini_title.setChecked(True)
        self.assertFalse(page.home_ini.isHidden() or page.home_copy.isHidden())
        self.assertFalse(page.home_hal.isHidden() or page.home_hal_copy.isHidden())
        self.assertIn("HOME_USE_INDEX", page.home_ini.toPlainText())
        # the HAL lines with it: the net Apply writes, and the guards
        self.assertIn("index-enable <=>", page.home_hal.toPlainText())
        self.assertIn(".home-max-travel", page.home_hal.toPlainText())
        page.home_ini_title.setChecked(False)
        self.assertTrue(page.home_ini.isHidden() and page.home_hal.isHidden())
        s["process-data"]["outputs"][0]["entries"].append({"object": "0x6098:00", "bits": 8, "type": "SINT"})
        page.show_homing(s)
        self.assertEqual(form.labelForField(page.home_method).text(), "Method (PDO 0x6098:00)")
        s["process-data"]["outputs"][0]["entries"].pop()
        page.home_method.setCurrentIndex(page.home_method.findData(36))
        page.homing_edited()
        self.assertIn("0x2030:01", [x["object"] for x in s["settings"]])
        # the ECT60 follows its target after torque homing only when enabled
        # again (its quirk file); the profile is told, and how fast it homes
        self.assertTrue(page.home_again.isChecked() and not page.home_again.isHidden())
        self.assertEqual((s["cia402"].get("home-enable-again"), s["cia402"].get("home-speed")),
                         (True, 4000))
        page.home_again.setChecked(False)                 # the user's word wins
        self.assertNotIn("home-enable-again", s["cia402"])
        self.assertFalse(s["homing"]["enable-again"])
        page.show_homing(s)
        self.assertFalse(page.home_again.isChecked())
        page.home_method.setCurrentIndex(page.home_method.findData(35))
        page.homing_edited()
        self.assertFalse(page.home_again.isChecked())
        self.assertEqual(s["cia402"].get("home-speed"), 1)   # does not move: any step is a set
        # ticking homing off takes the choice and its settings out (0x2022 for torque
        # homing would stay a setting otherwise) and hides the card; on again: back
        page.home_method.setCurrentIndex(page.home_method.findData(36))
        page.homing_edited()
        page.other_modes["hm"].setChecked(False)
        self.assertTrue(page.homing_card.isHidden())
        self.assertNotIn("homing", s)
        self.assertNotIn("0x2022:00", [x["object"] for x in s.get("settings") or []])
        self.assertNotIn("home-enable-again", s["cia402"])
        page.other_modes["hm"].setChecked(True)
        self.assertFalse(page.homing_card.isHidden())
        self.assertEqual(s["homing"]["method"], 36)
        self.assertIn("0x2022:00", [x["object"] for x in s["settings"]])
        page.home_method.setCurrentIndex(0)
        page.homing_edited()
        self.assertTrue(page.home_again.isHidden())
        self.assertNotIn("home-speed", s["cia402"])
        self.assertNotIn("homing", s)
        self.assertNotIn("0x6098:00", [x["object"] for x in s.get("settings") or []])

    def test_homing_card_started_by(self):
        # the joint's choice between index-enable and cia402_homecomp: only for a
        # joint, and only when LinuxCNC has the module (or the joint uses it)
        from ecatsetup import integrate
        w = self.window()
        s = w.session.slave(0)
        s["profile"] = "cia402"
        s["cia402"] = {"mode": "csp", "modes": ["hm"]}
        s["homing"] = {"method": 35}
        w.select_slave(0)
        page = w.slave_page
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"ECATSETUP_MODULE_DIR": d}):
            page.show_homing(s)
            self.assertTrue(page.home_way.isHidden())         # not a joint
            integrate.set_joint(w.session.cfg, 0, s["name"], 100.0)
            page.show_homing(s)
            self.assertTrue(page.home_way.isHidden())         # no module
            open(os.path.join(d, "cia402_homecomp.so"), "w").close()
            page.show_homing(s)
            self.assertFalse(page.home_way.isHidden())
            self.assertEqual(page.home_way.currentData(), "index")
            page.home_way.setCurrentIndex(page.home_way.findData("homecomp"))
            page.home_way_edited()
            self.assertEqual(integrate.joint_homing(w.session.cfg, 0), "homecomp")
            self.assertIn("HOMEMOD = cia402_homecomp", page.home_ini.toPlainText())
            self.assertIn("request-custom-homing", page.home_hal.toPlainText())
            os.remove(os.path.join(d, "cia402_homecomp.so"))
            page.show_homing(s)
            self.assertFalse(page.home_way.isHidden())        # in use: still shown
            page.home_way.setCurrentIndex(page.home_way.findData("index"))
            page.home_way_edited()
            self.assertEqual(integrate.joint_homing(w.session.cfg, 0), "index")

    def test_homing_card_switches(self):
        w = self.window()
        s = w.session.slave(0)
        s["profile"] = "cia402"
        s["cia402"] = {"mode": "csp", "modes": ["hm"]}
        s["identity"] = {"vendor": "0x00000A88", "product": "0x0A880002"}
        w.select_slave(0)
        page = w.slave_page
        page.home_method.setCurrentIndex(page.home_method.findData(35))
        page.homing_edited()
        self.assertTrue(page.home_switch_text.isHidden() and page.home_switch_look.isHidden())
        page.home_method.setCurrentIndex(page.home_method.findData(21))
        page.homing_edited()
        self.assertFalse(page.home_switch_text.isHidden())
        self.assertIn("needs the home switch", page.home_switch_text.text())
        # the ECT60's quirk file says how: rows below, not the general advice
        self.assertIn("Set which input is which below", page.home_switch_text.text())
        d = coe.Dictionary('SDO 0x2007, "Inputs Function"\n'
                           '  0x2007:05, rwrwrw, uint16, 16 bit, "Input 5 Function"\n')
        with mock.patch.object(w.session, "dictionary", lambda pos: d), \
                mock.patch.object(gui.tool, "upload", lambda *a: 3):
            page.home_switch_look.click()
        self.assertTrue(page.home_switch_look.isChecked())
        self.assertFalse(page.home_switch_found.isHidden())
        self.assertIn("0x2007:05", page.home_switch_found.text())
        page.home_switch_look.click()                   # a toggle: the list goes
        self.assertTrue(page.home_switch_found.isHidden())
        self.assertEqual(page.home_switch_found.text(), "")
        with mock.patch.object(w.session, "dictionary", lambda pos: coe.Dictionary()):
            page.look_for_input_settings()
        self.assertIn("Nothing found", page.home_switch_found.text())

    def test_homing_card_input_rows(self):
        w = self.window()
        s = w.session.slave(0)
        s["profile"] = "cia402"
        s["cia402"] = {"mode": "csp", "modes": ["hm"]}
        s["identity"] = {"vendor": "0x00000A88", "product": "0x0A880002"}
        w.select_slave(0)
        page = w.slave_page
        page.home_method.setCurrentIndex(page.home_method.findData(21))
        page.homing_edited()
        form = page.home_inputs_form
        self.assertFalse(page.home_inputs.isHidden())
        self.assertEqual(form.rowCount(), 1)                 # 21 needs the home switch only
        self.assertEqual(form.itemAt(0, QFormLayout.LabelRole).widget().text(), "Home switch")
        # the ECT60 from the factory: IN5, normally open
        row = form.itemAt(0, QFormLayout.FieldRole).layout()
        box, pbox = row.itemAt(0).widget(), row.itemAt(1).widget()
        self.assertEqual((box.currentData(), pbox.currentData()), ("IN5", "normally open"))
        page.input_row_changed("home", "IN3", "normally closed")
        objs = {x["object"]: x["value"] for x in s["settings"]}
        self.assertEqual((objs["0x2007:03"], objs["0x2007:05"], objs["0x2008:00"]), (3, 0, 0x3B))
        self.assertIn("IN3 was the negative limit switch", w.statusBar().currentMessage())
        # the drive has another choice now (a test wrote one that was not saved):
        # said, with a button that writes the configuration's choice
        s["settings"] = [x for x in s["settings"] if not x["object"].startswith(("0x2007", "0x2008"))]
        drive = {(0x2007, 3): 1, (0x2007, 4): 3, (0x2007, 5): 0, (0x2007, 6): 6, (0x2008, 0): 0x3F}
        w.session.bus[0] = w.session.bus.get(0) or object()
        with mock.patch.object(gui.tool, "upload", lambda pos, i, sub, t: drive.get((i, sub))):
            page.show_homing(s)
            self.assertEqual(form.rowCount(), 2)
            warn = form.itemAt(1, QFormLayout.FieldRole).layout()
            self.assertIn("The drive has IN4 (normally open) now", warn.itemAt(0).widget().text())
            self.assertIn("the configuration IN5 (normally open)", warn.itemAt(0).widget().text())
            warn.itemAt(1).widget().click()
        objs = {x["object"]: x["value"] for x in s["settings"]}
        self.assertEqual((objs["0x2007:04"], objs["0x2007:05"]), (0, 3))
        # a method with limits gets their rows too; a drive without data none
        page.home_method.setCurrentIndex(page.home_method.findData(23))
        page.homing_edited()
        self.assertEqual(form.rowCount(), 2)
        s["identity"] = {"vendor": "0x00000001", "product": "0x00000001"}
        page.show_homing(s)
        self.assertTrue(page.home_inputs.isHidden())
        self.assertIn("normally open or closed", page.home_switch_text.text())

    def test_pin_table_objects(self):
        import tempfile
        from ecatsetup import testmode
        w = self.window()

        class Run:
            def writable(self, pin):
                return pin.dir == "IN"
        pins = {"x.digital-inputs": testmode.Pin("x.digital-inputs", "uint", "OUT", "0x00130004"),
                "x.enable": testmode.Pin("x.enable", "bool", "IN", "FALSE")}
        src = {"x.digital-inputs": (0x60FD, 0, None, 0x1A00)}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"ECATSETUP_SETTINGS": os.path.join(d, "s.yaml")}):
            t = gui.PinTable(w)
            self.assertTrue(t.isColumnHidden(1))                 # off unless asked for
            t.fill(pins, "x.", Run(), src, lambda i, s: "Digital inputs")
            r = t.rows["x.digital-inputs"]
            self.assertEqual(t.item(r, 1).text(), "0x60FD:00")
            self.assertIn("0x60FD:00  Digital inputs", t.item(r, 0).toolTip())
            self.assertIn("PDO 0x1A00", t.item(r, 0).toolTip())
            self.assertEqual(t.item(t.rows["x.enable"], 1).text(), "")
            self.assertIsNotNone(t.cellWidget(t.rows["x.enable"], 4))      # the setter
            pins["x.digital-inputs"].value = "0x00030000"
            t.update_values(pins)
            self.assertEqual(t.item(r, t.VALUE).text(), "0x00030000")
            # one box for the three tables, kept in settings
            page = w.test_page
            page.objects_boxes[0].setChecked(True)
            self.assertTrue(all(b.isChecked() for b in page.objects_boxes))
            self.assertFalse(page.all_pins.isColumnHidden(1) or page.inputs.isColumnHidden(1))
            self.assertTrue(gui.PinTable.objects_wanted())
            self.assertFalse(gui.PinTable(w).isColumnHidden(1))
            page.objects_boxes[2].setChecked(False)
            self.assertTrue(page.outputs.isColumnHidden(1))
            self.assertFalse(gui.PinTable.objects_wanted())

    def test_switch_card(self):
        w = self.window()
        card = w.test_page.switches
        slave = {"name": "x", "profile": "cia402", "identity": {"vendor": "0x00000A88", "product": "0x0A880002"},
                 "cia402": {"mode": "csp", "modes": ["hm"], }, "homing": {"method": 21},
                 "process-data": {"inputs": [{"pdo": "0x1A00", "entries": [
                     {"object": "0x60FD:00", "bits": 32, "type": "UDINT", "pin": "digital-inputs"}]}]}}
        card.show_drive(slave)
        self.assertTrue(card.missing.isHidden())
        pins = {"digital-inputs": "0x00030000"}
        card.update_values(lambda n: pins.get(n))
        # a box per switch and per input, grey when off and green when on, live
        self.assertEqual(card.lamps["home"].property("box"), "off")
        self.assertEqual([b.text() for b in card.input_boxes.values()],
                         ["IN1 (encoder A)", "IN2 (encoder B)", "IN3", "IN4", "IN5", "IN6"])
        self.assertEqual([card.input_boxes[b].property("box") for b in (16, 17, 20)], ["on", "on", "off"])
        self.assertIn("needs the home switch", card.inputs.text())
        pins["digital-inputs"] = "0x00130004"           # the home switch on IN5, pressed
        card.update_values(lambda n: pins.get(n))
        self.assertEqual(card.lamps["home"].property("box"), "on")
        self.assertEqual(card.lamps["negative"].property("box"), "off")
        self.assertEqual(card.input_boxes[20].property("box"), "on")
        # a drive without input names: bits 16 to 23, and any other that is on
        other = dict(slave, identity={"vendor": "0x00000001", "product": "0x00000001"})
        card.show_drive(other)
        pins["digital-inputs"] = "0x01000000"
        card.update_values(lambda n: pins.get(n))
        self.assertEqual(sorted(card.input_boxes), list(range(16, 25)))
        self.assertEqual(card.input_boxes[24].property("box"), "on")
        self.assertIn("see the drive's manual", card.inputs.text())
        card.show_drive(slave)
        pins["digital-inputs"] = "0x00030000"
        # the test, started by clicking the switch's box: released (IN1 flickers
        # on its own: left out), then pressed
        self.assertFalse(hasattr(card, "buttons"))
        card.lamps["home"].clicked.emit()
        self.assertEqual(card.test["kind"], "home")
        t = 100.0
        for value in (0x30000, 0x20000, 0x30000):
            card.step_test(value, t)
            t += 0.5
        self.assertIn("press and hold", card.step.text())
        card.step_test(0x130004, t)
        card.step_test(0x130004, t + 0.1)
        self.assertEqual(card.result.text(), "")          # held 0.3 s first
        card.step_test(0x120004, t + 0.4)                 # IN1 left out: the same
        self.assertIn("Set up right", card.result.text())
        self.assertIsNone(card.test)
        # not pressed within the time: nothing reached the drive
        card.start("home")
        card.step_test(0x30000, 200.0)
        card.step_test(0x30000, 201.1)
        card.step_test(0x30000, 216.2)
        self.assertIn("Nothing changed", card.result.text())
        # without 0x60FD in the process data: what to do
        card.show_drive(dict(slave, **{"process-data": {}}))
        self.assertFalse(card.missing.isHidden())
        self.assertIn("0x60FD", card.missing.text())

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

    def test_suggest_following_error_window(self):
        def page_visible(w, pos):
            w.select_slave(pos)
            w.slave_page.show_settings(w.session.slave(pos))
            return w.slave_page.ferror_button.isVisibleTo(w.slave_page)

        w = self.window()
        s = w.session.slave(0)
        self.assertFalse(page_visible(w, 0))          # not a drive: no button
        s["profile"] = "cia402"
        s["cia402"] = {"mode": "csp"}
        s["identity"] = {"vendor": "0x00000A88", "product": "0x0A880002"}
        self.assertTrue(page_visible(w, 0))
        page = w.slave_page
        ini = os.path.join(self.dir.name, "m.ini")
        open(ini, "w").write("[EMCMOT]\nSERVO_PERIOD = 1000000\n[JOINT_0]\nFERROR = 0.5\n")
        w.session.ini_path = ini
        page.suggest_following_error()                # no joint yet: says so, adds nothing
        self.assertFalse(any(x["object"] in ("0x6065:00", "0x2022:00") for x in s.get("settings") or []))
        from ecatsetup import integrate
        integrate.set_joint(w.session.cfg, 0, config.axes(s)[0], 100.0)
        page.suggest_following_error()
        page.suggest_following_error()                # again: updates, no duplicate
        found = [x for x in s["settings"] if x["object"] in ("0x6065:00", "0x2022:00")]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["value"], 100)

    def test_settings_picker_and_guidance(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        s = w.session.slave(0)
        n = len(s.get("settings") or [])
        dlg = gui.SettingPicker(page, s, w.session.dictionary(0), set())
        objects = [dlg.table.item(r, 0).text() for r in range(dlg.table.rowCount())]
        self.assertIn("0x2000:02", objects)
        self.assertNotIn("0x1000:00", objects)          # read-only: not offered
        self.assertFalse([o for o in objects if o.startswith(("0x16", "0x1A"))])   # PDO mapping
        dlg.filter.setText("i2c_address")
        self.assertEqual([dlg.table.item(r, 0).text() for r in range(dlg.table.rowCount())],
                         ["0x2000:02", "0x2001:02"])
        dlg.table.selectRow(0)
        e, = dlg.chosen()
        self.assertEqual((e.index, e.subindex), (0x2000, 2))
        dlg.only_writable.setChecked(False)
        dlg.filter.setText("0x1000")
        self.assertEqual(dlg.table.rowCount(), 1)
        # added with the slave's present value, else 0
        with mock.patch.object(tool, "upload", lambda pos, i, sub, t: 0x48 if i == 0x2000 else None):
            page.add_entries(s, [e], True)
            page.add_entries(s, [w.session.dictionary(0).entries[(0x2001, 2)]], True)
        a, b = s["settings"][n:]
        self.assertEqual((a["object"], a["type"], a["value"]), ("0x2000:02", "USINT", 0x48))
        self.assertEqual((b["object"], b["value"]), ("0x2001:02", 0))
        self.assertEqual(page.st.rowCount(), n + 2)
        # a value that does not fit the type is marked and explained
        page.st.item(n, 3).setText("300")
        self.assertIn("does not fit USINT", page.st_problems.text())
        self.assertIn("does not fit", page.st.item(n, 3).toolTip())
        page.st.item(n, 3).setText("5")
        self.assertEqual(page.st_problems.text(), "")
        # an object that is not in the dictionary is a warning
        page.st.item(n, 0).setText("0x2FFF:01")
        self.assertIn("not in the object dictionary", page.st_problems.text())
        # completion text is accepted
        page.st.item(n, 0).setText("0x2000:03  LowpassFilterPoleFrequency")
        self.assertEqual(s["settings"][n]["object"], "0x2000:03")
        self.assertEqual(s["settings"][n]["type"], "UDINT")
        # present values
        with mock.patch.object(tool, "upload", lambda pos, i, sub, t: 7):
            page.read_present_values()
        self.assertEqual(page.st.item(n, 4).text(), "7")

    def test_commands_are_offered_grey_and_can_be_added(self):
        # a quirk file's command is advice, not a rule (the user, 2026-11-27)
        w = self.window()
        w.select_slave(0)
        s = w.session.slave(0)
        with mock.patch.object(guidance, "command_reason",
                               lambda slave, index, entry=None: ("warning", "a command")
                               if index == 0x2000 else None):
            dlg = gui.SettingPicker(w.slave_page, s, w.session.dictionary(0), set())
            row = [dlg.table.item(r, 0).text() for r in range(dlg.table.rowCount())].index("0x2000:02")
            self.assertTrue(dlg.table.item(row, 0).flags() & gui.Qt.ItemIsSelectable)
            self.assertEqual(dlg.table.item(row, 0).toolTip(), "a command")
            n = len(s.get("settings") or [])
            w.slave_page.add_entries(s, [w.session.dictionary(0).entries[(0x2000, 2)]])
            self.assertEqual(len(s.get("settings") or []), n + 1)   # the user's choice

    def test_run_command_is_confirmed_and_not_with_a_drive_enabled(self):
        from ecatsetup import testmode
        w = self.window()
        w.select_slave(0)
        page = w.test_page
        page.sdo_obj.setText("0x2000:02")
        page.sdo_value.setText("1")
        QMB = gui.QMessageBox
        calls = []
        with mock.patch.object(tool, "download", lambda *a, **k: calls.append(a)), \
                mock.patch.object(QMB, "information", lambda *a, **k: calls.append("info")), \
                mock.patch.object(QMB, "warning", lambda *a, **k: calls.append("warning")), \
                mock.patch.object(QMB, "question", lambda *a, **k: QMB.Yes):
            page.sdo_run_command()                  # an ordinary object: not a command
            self.assertEqual(calls, ["info"])
            del calls[:]
            reason = ("warning", "Zero position: a command")
            with mock.patch.object(guidance, "command_reason", lambda *a, **k: reason), \
                    mock.patch.object(testmode, "drives_enabled", lambda cfg, up: ["x"]):
                page.sdo_run_command()
            self.assertEqual(calls, ["warning"])    # a drive is enabled: not run
            del calls[:]
            with mock.patch.object(guidance, "command_reason", lambda *a, **k: reason), \
                    mock.patch.object(testmode, "drives_enabled", lambda cfg, up: []):
                page.sdo_run_command()
            self.assertEqual(calls, [(0, 0x2000, 2, "1", None)])

    def test_master_page(self):
        w = self.window()
        w.select_slave(gui.MASTER_ITEM)
        self.assertIs(w.bus_page.stack.currentWidget(), w.master_page)
        m = w.master_page
        m.interface.setEditText("enp9s0")
        m.transport.setCurrentText("xdp-native")
        self.assertEqual(w.session.cfg["master"]["interface"], "enp9s0")
        self.assertEqual(w.session.cfg["master"]["transport"], "xdp-native")
        self.assertEqual(w.bus_page.list.title.text(), "BUS · ENP9S0")
        self.assertIn("enp9s0", w.header.info.text())
        self.assertIn("Not known whether enp9s0", m.xdp_hint.text())  # no such interface
        from ecatsetup import netif
        with mock.patch.object(netif, "interfaces", lambda: ["enp1s0", "enp2s0"]), \
                mock.patch.object(netif, "xdp_native", lambda name: name == "enp1s0"), \
                mock.patch.object(netif, "driver", {"enp1s0": "igb", "enp2s0": "r8169"}.get):
            m.interface.setEditText("enp1s0")
            self.assertEqual(m.xdp_hint.text(), "enp1s0 (igb) can do xdp-native")
            m.interface.setEditText("enp2s0")
            self.assertEqual(m.xdp_hint.text(), "enp2s0 (r8169) cannot do xdp-native: use raw or xdp-skb")
        labels = [lab.text() for lab in m.findChildren(gui.QLabel)]
        self.assertIn("Global shift time", labels)
        self.assertNotIn("SYNC0 shift", labels)
        # the slaves follow the servo thread: the reference clock is synchronised often
        m.dc_on.setChecked(True)
        m.clock.setCurrentIndex(1)
        m.ref_cycles.setValue(1000)
        self.assertTrue(w.session.cfg["master"]["dc"]["servo-thread-follows-ref-clock"])
        self.assertEqual(w.session.cfg["master"]["dc"]["ref-clock-sync-cycles"], 1000)
        self.assertEqual(config.dc_warnings(w.session.cfg), [])
        m.clock.setCurrentIndex(0)
        self.assertFalse(w.session.cfg["master"]["dc"]["servo-thread-follows-ref-clock"])
        self.assertEqual(m.ref_cycles.value(), 100)
        self.assertEqual(w.session.cfg["master"]["dc"]["ref-clock-sync-cycles"], 100)
        # chosen by the user afterwards: kept, and warned about
        m.ref_cycles.setValue(1000)
        self.assertEqual(w.session.cfg["master"]["dc"]["ref-clock-sync-cycles"], 1000)
        self.assertTrue(any("every 1 to 100 cycles" in t for lv, t in w.session.dc_problems()))
        m.ref_cycles.setValue(10)
        self.assertEqual(config.dc_warnings(w.session.cfg), [])

    def test_master_cpu_fields(self):
        import tempfile
        w = self.window()
        w.select_slave(gui.MASTER_ITEM)
        m = w.master_page
        self.assertNotIn("irq-cpu", w.session.cfg.get("master", {}))
        m.irq_cpu.setValue(3)
        m.cpu.setValue(2)
        self.assertEqual(w.session.cfg["master"]["irq-cpu"], 3)
        self.assertEqual(w.session.cfg["master"]["cpu"], 2)
        self.assertEqual([e for e in config.check(w.session.cfg) if "master" in e], [])
        m.irq_cpu.setValue(-1)          # "not pinned": the key goes away
        self.assertNotIn("irq-cpu", w.session.cfg["master"])
        self.assertEqual(w.session.cfg["master"]["cpu"], 2)
        # a file's values are shown
        w.session.cfg["master"]["irq-cpu"] = 1
        m.show_config()
        self.assertEqual(m.irq_cpu.value(), 1)
        # a value that is not a CPU of this computer is kept
        w.session.cfg["master"]["irq-cpu"] = 77
        m.show_config()
        self.assertEqual(m.irq_cpu.value(), 77)
        self.assertIn("not on this computer", m.irq_cpu.currentText())
        # the recommendation follows the isolated CPUs
        with tempfile.TemporaryDirectory() as d:
            cpu = os.path.join(d, "devices", "system", "cpu")
            os.makedirs(cpu)
            with open(os.path.join(cpu, "possible"), "w") as f:
                f.write("0-3\n")
            self.assertIn("Recommended: not pinned, not bound", m.cpu_text(d))
            self.assertEqual(m.cpu_advice(set()), (-1, -1))
            with open(os.path.join(cpu, "isolated"), "w") as f:
                f.write("3\n")
            self.assertIn("the interrupts on CPU 3", m.cpu_text(d))
            self.assertIn("threads not bound", m.cpu_text(d))
            self.assertEqual(m.cpu_advice({3}), (3, -1))
            m.fill_cpus(d)
            self.assertEqual(m.irq_cpu.count(), 1 + 4 + 1)     # none, 0-3, and the kept 77
            self.assertIn("recommended", m.irq_cpu.itemText(m.irq_cpu.findData(3)))
            self.assertNotIn("recommended", m.irq_cpu.itemText(m.irq_cpu.findData(2)))
            self.assertIn("recommended", m.cpu.itemText(m.cpu.findData(-1)))
            m.show_cpu_advice(d)
            self.assertFalse(m.cpu_use.isHidden())
            # the button takes the recommendation (here: the real /sys's)
            m.isolated_cpus = lambda sys="/sys": {3}
            m.use_cpu_advice()
            self.assertEqual((w.session.cfg["master"].get("irq-cpu"), w.session.cfg["master"].get("cpu")),
                             (3, None))
            m.show_cpu_advice(d)
            self.assertTrue(m.cpu_use.isHidden())

    def test_sync0_cycle_and_measured_lines(self):
        w = self.window()
        s = w.session.slave(0)
        s["identity"] = {"vendor": "0x00000A88", "product": "0x0A880002"}   # an ECT60: results ship
        s["dc"] = {"assign-activate": "0x0300", "sync0-cycles": 2, "sync0-shift-ns": 0}
        page = w.slave_page
        w.session.period = 125000
        w.select_slave(0)
        text = page.dc_cycle.text()
        self.assertIn("SYNC0 every 250 us: the servo period 125 us times cycle 2.", text)
        self.assertIn("Measured on the bus: 250 us ok", text)
        self.assertIn("100 us refused (AL 0x0036)", text)
        self.assertIn("tried ok at this period", text)
        self.assertTrue(page.dc_warn.isHidden() or "measured" not in page.dc_warn.text())
        # a period that was refused: a warning here and in the session's list
        w.session.period = 100000
        page.show_dc_warnings(s)
        self.assertIn("this device was measured: refused at 100 us (AL 0x0036)", page.dc_warn.text())
        self.assertTrue(any("measured" in t and "refused at 100 us" in t for lv, t in w.session.dc_problems()))
        # between what was tried: a note, not a warning
        w.session.period = 110000
        page.show_dc_warnings(s)
        self.assertTrue(page.dc_warn.text().startswith("Note: this device was measured: not tried between 100 and 125"))
        # without SYNC0, and without a period
        s.pop("dc")
        w.session.period = 125000
        page.show_dc_warnings(s)
        self.assertEqual(page.dc_cycle.text().splitlines()[0], "Servo period 125 us; this slave runs without SYNC0.")
        self.assertTrue(all("measured" not in t for lv, t in w.session.dc_problems()))     # no DC: not about SYNC0
        w.session.period = None
        page.show_dc_warnings(s)
        self.assertIn("Servo period 1 ms (the default: no INI file chosen", page.dc_cycle.text())
        self.assertNotIn("Measured", page.dc_cycle.text())              # nothing to compare with
        # a device nothing was measured on
        s["identity"] = {"vendor": "0x00000002", "product": "0x03F03052"}
        w.session.period = 125000
        s["dc"] = {"assign-activate": "0x0300"}
        page.show_dc_warnings(s)
        self.assertNotIn("Measured", page.dc_cycle.text())

    def test_remember_period(self):
        import tempfile
        w = self.window()
        s = w.session.slave(0)
        s["identity"] = {"vendor": "0x00000A88", "product": "0x0A880002"}
        s["device"], s["name"] = "ECT60", "x"
        s["dc"] = {"assign-activate": "0x0300"}
        tp = w.live_page.sync
        w.session.period = 110000                   # a period nothing was measured at, between 100 and 125
        asked, told = [], []
        yes = lambda parent, title, text, *a: (asked.append(text), gui.QMessageBox.Yes)[1]
        info = lambda parent, title, text, *a: told.append(text)
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"ECATSETUP_MEASURED": d}), \
                mock.patch.object(gui.QMessageBox, "question", yes), \
                mock.patch.object(gui.QMessageBox, "information", info):
            # all in OP, but for ten seconds only: not enough
            tp.ok_since = time.monotonic() - 10
            with mock.patch.object(gui.diagnose, "al_status", lambda pos: (8, False, 0)):
                tp.remember_period()
            self.assertTrue(told and "Let it run for a minute or more" in told[0], told)
            self.assertEqual(asked, [])
            self.assertEqual([r for r in gui.measured.results(s) if r["period-us"] == 110], [])
            # after two minutes: asked, kept
            tp.ok_since = time.monotonic() - 120
            with mock.patch.object(gui.diagnose, "al_status", lambda pos: (8, False, 0)):
                tp.remember_period()
            self.assertIn("ECT60 (x) works at the servo period 110 us: all slaves in OP for 2 min", asked[-1])
            r = [r for r in gui.measured.results(s) if r["period-us"] == 110][0]
            self.assertEqual(r["result"], "ok")
            self.assertIn("2 min", r["note"])
            self.assertTrue(os.path.exists(os.path.join(d, "sync0.yaml")))
            # a drive that is not in OP with the SYNC0 cycle time code: kept as refused
            w.session.period = 90000
            with mock.patch.object(gui.diagnose, "al_status", lambda pos: (2, True, 0x36)):
                tp.remember_period()
            self.assertIn("refused the servo period 90 us", asked[-1])
            self.assertIn("AL 0x0036", asked[-1])
            r = [r for r in gui.measured.results(s) if r["period-us"] == 90][0]
            self.assertEqual((r["result"], r["al-status"]), ("refused", "0x0036"))
            # the slave's own code cleared by the master's acknowledgement: the master's kept code is used
            w.session.period = 85000
            with mock.patch.object(gui.diagnose, "al_status", lambda pos: (2, False, 0)), \
                    mock.patch.object(gui.tool, "slave_info", lambda pos: {"al_status_code": 0x36}):
                tp.remember_period()
            self.assertIn("refused the servo period 85 us", asked[-1])
            self.assertEqual([r["al-status"] for r in gui.measured.results(s) if r["period-us"] == 85], ["0x0036"])
            # no code anywhere (or the master cannot say): nothing is kept
            w.session.period = 82000
            n = len(asked)
            with mock.patch.object(gui.diagnose, "al_status", lambda pos: (2, False, 0)), \
                    mock.patch.object(gui.tool, "slave_info", mock.Mock(side_effect=RuntimeError("no master"))):
                tp.remember_period()
            self.assertEqual(len(asked), n)
            self.assertIn("AL status 0x0000", told[-1])
            # not in OP for another reason: nothing is kept
            w.session.period = 80000
            n = len(asked)
            with mock.patch.object(gui.diagnose, "al_status", lambda pos: (2, True, 0x1D)):
                tp.remember_period()
            self.assertEqual(len(asked), n)
            self.assertIn("not a SYNC0 cycle problem", told[-1])
            self.assertEqual([r for r in gui.measured.results(s) if r["period-us"] == 80], [])
            # answering no keeps nothing
            w.session.period = 70000
            with mock.patch.object(gui.QMessageBox, "question", lambda *a, **k: gui.QMessageBox.No), \
                    mock.patch.object(gui.diagnose, "al_status", lambda pos: (2, True, 0x35)):
                tp.remember_period()
            self.assertEqual([r for r in gui.measured.results(s) if r["period-us"] == 70], [])
            # no period: told
            w.session.period = None
            tp.remember_period()
            self.assertIn("no servo period", told[-1])

    def test_remember_text_follows_the_run(self):
        w = self.window()
        tp = w.live_page.sync
        pins = {"all-op": "TRUE", "dc-sync-failed": "FALSE", "dc-phased": "TRUE", "pll-err": "1",
                "shift-time": "70000", "turnaround-cycles": "2"}
        w.session.cfg["master"] = {"dc": {"servo-thread-follows-ref-clock": True, "sync0-shift-ns": "auto"}}
        tp.ok_since = None
        tp.show_sync(pins.get, 125000)
        self.assertIsNotNone(tp.ok_since)
        self.assertIn("Running well for 0 s at 125 us", tp.remember_text.text())
        tp.ok_since -= 200
        tp.show_sync(pins.get, 125000)
        self.assertIn("Running well for 3 min 20 s at 125 us", tp.remember_text.text())
        pins["turnaround-cycles"] = "3"                     # three cycles with the automatic shift: not well
        tp.show_sync(pins.get, 125000)
        self.assertIsNone(tp.ok_since)
        self.assertIn("Not running well yet", tp.remember_text.text())
        pins.update({"turnaround-cycles": "2", "all-op": "FALSE"})
        tp.show_sync(pins.get, 125000)
        self.assertIsNone(tp.ok_since)

    def test_message_selects_slave(self):
        w = self.window()
        w.session.cfg["slaves"][1]["name"] = "io-0"
        w.check()
        it = next(w.messages.item(i) for i in range(w.messages.count())
                  if "used twice" in w.messages.item(i).text())
        w.message_activated(it)
        self.assertEqual(w.slave_page.position, 1)
        self.assertFalse(w.slave_page.problems.isHidden())

    def test_troubleshoot_page(self):
        from ecatsetup import diagnose
        w = self.window()
        w.select_slave(0)
        w.tabs.setCurrentWidget(w.trouble_page)
        page = w.trouble_page
        self.assertFalse(page.waiting.isHidden())
        port = diagnose.Port(0, True, {"rx": 1, "phy": 9, "fwd": 0, "lost": 0},
                             {"rx": 0, "phy": 6, "fwd": 0, "lost": 0})
        w.reports[0] = diagnose.Report(0, "io", True, 4, True, 0x1D, ports=[port], seconds=2)
        w.show_current()
        self.assertTrue(page.waiting.isHidden())
        self.assertEqual(page.title.text(), "Slave 0 · MetalMusings EaserCAT 7000 does not reach OP")
        self.assertEqual([c.tag.text() for c in page.steps],
                         ["✓ reached", "✓ reached", "✓ reached", "✕ refused"])
        self.assertIn("0x001D Invalid output configuration", page.steps[3].text.text())
        self.assertIn("Invalid output configuration", page.headline.text())
        self.assertIn("phy", page.ports.horizontalHeaderItem(3).text().lower())
        self.assertEqual(page.ports.item(0, 3).text(), "9  (+6)")
        self.assertIn("grew on port 0", page.cable_verdict.text())
        self.assertIn("no diagnosis history", page.history_none.text())
        self.assertTrue(page.cpu.isHidden())        # nothing read about the processor
        # the processor stopped, and the slave's cycle does not fit
        r = w.reports[0]
        r.processor = diagnose.Processor(operational=True, watchdog_ok=False, pdi_ms=100.0, pdi_expired=3)
        r.sync = [diagnose.SyncInfo(0x1C32, {"type": 2, "cycle_ns": 1000000, "calc_copy_ns": 663000},
                                    missed_delta=2)]
        w.show_current()
        text = page.cpu_text.text()
        self.assertFalse(page.cpu.isHidden())
        self.assertIn("PDI running; PDI watchdog 100 ms, expired.", text)
        self.assertIn("the slave's processor has stopped talking to its EtherCAT chip", text)
        self.assertIn("Sync (0x1C32, outputs): DC SYNC0, cycle 1 ms, its own work 663 us", text)
        self.assertIn("2 cycles without new process data", text)

    def test_what_the_event_log_means(self):
        """Frames lost while running: the event log card says what it means and
        judges the causes on this computer (a kernel master loaded here), with a
        button to the Timing tab; a clean log shows nothing."""
        from ecatsetup import events, report, rtcheck
        sys.path.insert(0, os.path.dirname(__file__))
        from test_events import write_log
        d = tempfile.mkdtemp()
        path = os.path.join(d, "ev")
        start = [(0, events.START, -1, 3, 0), (1, events.LINK, -1, 1, 0), (2, events.MASTER_AL, -1, 8, 2),
                 (3, events.WC, -1, 2, 1)]
        write_log(path, start)
        facts = {"device": False, "modules": ["ec_master"], "enabled": "enabled", "active": "active",
                 "free": [], "rtapi": False, "linuxcnc": [], "kernel_master": True,
                 "kernel_in_way": "The kernel EtherCAT master is loaded and uses lo.", "kernel_at_boot": None}
        with mock.patch.object(events, "PATH", path), \
                mock.patch.object(report, "master_facts", lambda **k: facts), \
                mock.patch.object(rtcheck, "inventory", lambda roots=None, ini=None: rtcheck.Inventory()):
            w = self.window()
            page = w.trouble_page
            page.show_events()
            self.assertTrue(page.meaning.isHidden())
            write_log(path, start + [(4, events.WC, -1, 0, 2), (5, events.DRIVE_BLOCK, 0, 2, 0)])
            page.show_events()
            self.assertFalse(page.meaning.isHidden())
            text = page.meaning_text.text()
            self.assertIn("Frames were lost or came back too late", text)
            self.assertIn("working counter zero 1 times while running", text)
            self.assertIn("The IgH kernel master is loaded beside the userspace master", text)
            self.assertLess(text.index("IgH kernel master is loaded"), text.index("servo thread runs late"))
            self.assertFalse(page.meaning_timing.isHidden())
            page.meaning_timing.click()
            self.assertIs(w.tabs.currentWidget(), w.timing_page)

    class FakeMasterLog:
        """The free master's messages without a journal."""

        def __init__(self, texts=(), status="", note=""):
            self.items, self.line, self.note = list(texts), status, note
            self.pid = None         # no free master: its frame counts are not read

        def poll(self):
            pass

        def texts(self):
            return list(self.items)

        def status(self):
            return self.line

        def close(self):
            pass

    def test_free_master_messages_on_slave_status(self):
        """The free master's messages (from the journal) are in the event log
        and, for the chosen slave, under Messages from the master, marked as
        the free master's; a journal that cannot be read is explained."""
        from ecatsetup import diagnose, events
        T = events.Text
        now = time.time()
        free = [T(0, now, 3, "EtherCAT ERROR 0-0: Failed to set SAFEOP state.", "free master"),
                T(1, now, 6, "EtherCAT 0-1: other slave", "free master")]
        d = tempfile.mkdtemp()
        with mock.patch.object(events, "PATH", os.path.join(d, "none")):
            w = self.window()
            w.master_log = self.FakeMasterLog(free, "the free master (ec_master, pid 7) runs; its "
                                              "messages come from the system journal")
            w.select_slave(0)
            w.reports[0] = diagnose.Report(0, "io", True, 2, True, 0)
            w.tabs.setCurrentWidget(w.trouble_page)
            w.show_current()
            page = w.trouble_page
            page.show_events()
            self.assertIn("pid 7", page.events_info.text())
            rows = [page.events_table.item(r, 2).text() for r in range(page.events_table.rowCount())]
            self.assertIn("free master: EtherCAT ERROR 0-0: Failed to set SAFEOP state.", rows)
            self.assertEqual(page.events_table.item(rows.index(
                "free master: EtherCAT ERROR 0-0: Failed to set SAFEOP state."), 1).text(), "ERROR")
            said = [page.master_table.item(r, 1).text() for r in range(page.master_table.rowCount())]
            self.assertEqual(said, ["free master: EtherCAT ERROR 0-0: Failed to set SAFEOP state."])
            # the journal cannot be read: said under the card
            w.master_log = self.FakeMasterLog(note="free master's messages are in the system journal, "
                                                   "which this user cannot read")
            page.show_events()
            self.assertIn("cannot read", page.master_none.text())
            self.assertFalse(page.master_none.isHidden())

    def test_slave_status_tab_marks_new_errors(self):
        """Errors or warnings that come while another tab is shown put a red or
        amber dot on the Live tab; showing that tab clears it."""
        from ecatsetup import events
        T = events.Text
        d = tempfile.mkdtemp()
        with mock.patch.object(events, "PATH", os.path.join(d, "none")):
            w = self.window()
            w.tabs.setCurrentWidget(w.test_page)
            i = w.tabs.indexOf(w.trouble_page)
            w.news_seen -= 60       # the window has been open for a minute
            w.master_log = self.FakeMasterLog([T(0, w.news_seen - 10, 3, "EtherCAT ERROR 0-0: old")])
            w.check_news()
            self.assertIsNone(w.news_mark)          # from before: not new
            w.master_log.items.append(T(1, time.time() - 1, 4, "EtherCAT WARNING 0-0: w"))
            w.check_news()
            self.assertIsNotNone(w.news_mark)
            self.assertIn(AMBER_NAME, w.news_mark.styleSheet())
            self.assertIn("1 warning", w.tabs.bar.tabToolTip(i))
            w.master_log.items.append(T(2, time.time() - 1, 3, "EtherCAT ERROR 0-0: e"))
            w.check_news()
            self.assertIn(RED_NAME, w.news_mark.styleSheet())
            self.assertIn("1 error and 1 warning", w.tabs.bar.tabToolTip(i))
            w.tabs.setCurrentWidget(w.trouble_page)
            self.assertIsNone(w.news_mark)
            self.assertIsNone(w.tabs.bar.tabButton(i, gui.QTabBar.RightSide))
            w.tabs.setCurrentWidget(w.test_page)
            w.check_news()
            self.assertIsNone(w.news_mark)          # seen

    def test_a_stop_is_no_news_and_news_is_shown_in_bold(self):
        """The workbench's stop (slaves to PREOP, the thread ending, the master
        closing) puts no dot on Diagnose; an error does, the tooltip names it,
        and opening the tab shows it in bold with the event log card on top."""
        from ecatsetup import events
        E, T = events.Event, events.Text
        w = self.window()
        w.master_log = self.FakeMasterLog()
        w.tabs.setCurrentWidget(w.test_page)
        now = time.time()
        w.news_seen = now - 60
        run = [E(0, now - 100, events.START, -1, 5, 0), E(1, now - 99, events.LINK, -1, 1, 0),
               E(2, now - 98, events.MASTER_AL, -1, 0x08, 0x02), E(3, now - 97.9, events.WC, -1, 2, 1)]
        stop = [E(4, now - 10, events.TAKE_DOWN, -1, 1, 0), E(5, now - 9.9, events.WC, -1, 0, 2),
                E(6, now - 9.5, events.SLAVE_AL, 0, 0x02, 0x08), E(7, now - 9.4, events.LATE, -1, 779, 0),
                E(8, now - 9.4, events.GUARD, -1, 1, 0), E(9, now - 9.3, events.STOP, -1, 0, 0)]
        said = [T(0, now - 9.2, 3, "EtherCAT ERROR 0-2: Failed to receive AL state datagram")]
        logs = {}

        def read(path=events.PATH):
            return logs[path]
        logs[events.PATH] = events.Log(events.PATH, now - 100, 1000000, 1, "", 10, run + stop, said, 1)
        with mock.patch.object(events, "read", side_effect=read):
            w.check_news()
            self.assertIsNone(w.news_mark)              # the stop: no news
            fault = E(10, now - 30, events.DRIVE_FAULT, 0, 0x7500, 0x218)
            logs[events.PATH] = events.Log(events.PATH, now - 100, 1000000, 1, "", 11,
                                           run + [fault] + stop, said, 1)
            w.check_news()
            self.assertIsNotNone(w.news_mark)
            tip = w.tabs.bar.tabToolTip(w.tabs.indexOf(w.trouble_page))
            self.assertIn("1 error", tip)
            self.assertIn("drive fault 0x7500", tip)
            page = w.trouble_page
            w.tabs.setCurrentWidget(page)
            self.assertIsNone(w.news_mark)
            self.assertIn("1 new since you last looked", page.events_info.text())
            t = page.events_table
            bold = [t.item(r, 2).text() for r in range(t.rowCount()) if t.item(r, 2).font().bold()]
            self.assertEqual(len(bold), 1)
            self.assertIn("drive fault", bold[0])
            stopped = [t.item(r, 2).text() for r in range(t.rowCount())
                       if t.item(r, 2).text().endswith("(at the stop)")]
            self.assertTrue(any("Failed to receive" in x for x in stopped))
            # leaving the tab: seen, no longer bold
            w.tabs.setCurrentWidget(w.test_page)
            page.show_events()
            self.assertFalse(any(t.item(r, 2).font().bold() for r in range(t.rowCount())))

    def test_long_term_history_of_a_slave(self):
        """The Live page shows the chosen slave's long-term history
        from the run in the event log: its DC curve and the counters."""
        from ecatsetup import events
        sys.path.insert(0, os.path.dirname(__file__))
        from test_events import write_log
        d = tempfile.mkdtemp()
        path = os.path.join(d, "ev")
        write_log(path, [(0, events.START, -1, 1, 0), (1, events.MON_DC, 0, 30, 600),
                         (2, events.MON_DC, 0, 45, 600), (3, events.MON_ONSET, 0, 7 << 8, 2)])
        with mock.patch.object(events, "PATH", path):
            w = self.window()
            w.select_slave(0)
            page = w.trouble_page
            page.show_events()
            self.assertIn("at most 45 ns off the reference clock", page.longterm_dc.text())
            self.assertFalse(page.longterm_curve.isHidden())
            self.assertEqual(page.longterm_curve.values, [30, 45])
            self.assertIn("PDI watchdog expired", page.longterm_counts.text())
            self.assertIn("Began to grow", page.longterm_counts.text())

    def test_test_page_without_a_test(self):
        w = self.window()
        w.select_slave(0)
        w.tabs.setCurrentWidget(w.test_page)
        self.assertFalse(w.test_page.start_card.isHidden())
        self.assertTrue(w.test_page.io.isHidden())
        self.assertTrue(w.header.stop.isHidden())


@unittest.skipIf(gui is None, "PyQt5 is not installed")
class TestMonitorRows(unittest.TestCase):
    """The Synchronisation card's rows from the long-term monitoring pins."""

    CFG = {"slaves": [
        {"position": 0, "name": "drive-0", "dc": {"assign-activate": "0x0300"}},
        {"position": 1, "name": "drive-1", "dc": {"assign-activate": "0x0300"}},
        {"position": 2, "name": "io", "dc": {"assign-activate": "0x0000"}},
        {"position": 3, "name": "drive-3", "dc": {"assign-activate": "0x0300"}, "unused": True}]}

    def test_reference_clock_is_the_first_with_dc(self):
        self.assertEqual(gui.dc_slaves(self.CFG), ("drive-0", ["drive-1"]))
        self.assertEqual(gui.dc_slaves({}), (None, []))

    def test_rows(self):
        seen = {}
        pins = {"drive-1.dc-diff-ns": "-7", "drive-1.dc-diff-max-ns": "12", "drive-0.error-count": "0",
                "drive-1.error-count": "0", "io.error-count": "0", "ecat.0.wc-bad-cycles": "0"}
        rows = gui.monitor_rows(self.CFG, pins, seen)
        self.assertEqual(rows[0], (True, "DC clocks against the reference clock (drive-0): drive-1 -7 ns; "
                                         "at most 12 ns since LinuxCNC loaded, 7 ns since you began looking"))
        self.assertEqual(rows[1], (True, "No error counter of a slave grew since LinuxCNC loaded"))
        self.assertEqual(len(rows), 2)
        pins.update({"drive-1.dc-diff-ns": "3", "drive-1.dc-diff-max-ns": "4000", "io.error-count": "5",
                     "ecat.0.wc-bad-cycles": "9"})
        rows = gui.monitor_rows(self.CFG, pins, seen)
        self.assertFalse(rows[0][0])
        self.assertIn("7 ns since you began looking", rows[0][1])     # the largest seen is kept
        self.assertEqual(rows[1], (False, "Error counters grew since LinuxCNC loaded: io 5 (Diagnose tab: "
                                          "the event log says which, and when)"))
        self.assertIn("9 cycles in OP with the working counter not complete", rows[2][1])
        # an older ecat without the pins: nothing
        self.assertEqual(gui.monitor_rows(self.CFG, {}, {}), [])
        # the master's frame statistics (master 1.7)
        self.assertEqual(gui.monitor_rows(self.CFG, {"ecat.0.frames-lost": "0"}, {}),
                         [(True, "0 frames lost since the bus started (sent, never came back)")])
        self.assertFalse(gui.monitor_rows(self.CFG, {"ecat.0.frames-lost": "3"}, {})[0][0])


@unittest.skipIf(gui is None, "PyQt5 is not installed")
class TestFreeMasterLine(unittest.TestCase):
    def test_line(self):
        from ecatsetup import events
        self.assertEqual(gui.free_master_line("", {"tx": 5}, []), "")
        st = "the free master (ec_master, pid 7) runs; its messages come from the system journal"
        line = gui.free_master_line(st, {"tx": 1000, "rx": 997, "lost": 3, "loss_rate": 1}, [])
        self.assertEqual(line, st + "; it has lost 3 of 1000 frames since it started, 1 in the last second")
        late = [events.Text(0, 0, 4, "EtherCAT WARNING 0: 2 datagrams UNMATCHED!", "free master")]
        self.assertIn("without real-time priority", gui.free_master_line(st, None, late))


class TestPdoEditing(WindowBase):
    """Process data editing on the slave page (docs/development.md "Own PDO mapping", step 3)."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_pdoreq import ESI_PDOS, MAPPABLE, fslave, make
        self.dev = make(MAPPABLE, ESI_PDOS)
        self.fslave = fslave

    def page(self, outputs, inputs):
        from ecatsetup import coe
        w = self.window()
        s = w.session.slave(1)
        s["profile"] = "cia402"
        s["cia402"] = {"mode": "csp"}
        s["identity"] = dict(s["identity"], revision="0x00000001")     # the ESI file's device is exact
        s["process-data"] = self.fslave(outputs, inputs)["process-data"]
        w.session.bus[1].proposal.recognised = None     # the fake bus's slave is an I/O terminal
        for p in (mock.patch.object(w.session, "esi_device", return_value=self.dev),
                  mock.patch.object(w.session, "dictionary", return_value=coe.Dictionary())):
            p.start()
            self.addCleanup(p.stop)
        w.select_slave(0)
        w.select_slave(1)
        return w, w.slave_page, s

    def test_without_the_esi_file_the_card_says_so(self):
        w = self.window()                        # the fake bus: no ESI file for its slaves
        w.select_slave(0)
        page = w.slave_page
        self.assertFalse(page.pa_no_esi.isHidden())
        self.assertIn("No ESI file for this device", page.pa_no_esi_text.text())
        with mock.patch.object(w, "add_esi") as add:
            next(b for b in page.pa_no_esi.findChildren(gui.QPushButton)).click()
        add.assert_called_once()
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        self.assertTrue(page.pa_no_esi.isHidden())          # with it: the boxes, no line

    def test_status_lines_and_add_what_is_missing(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        self.assertIn("cannot run as CiA 402 in CSP", page.pd_errors.text())
        self.assertIn("0x607A Target position", page.pd_errors.text())
        self.assertFalse(page.pd_add_missing.isHidden())
        page.add_pd_missing()
        self.assertEqual([e["object"] for e in s["process-data"]["outputs"][0]["entries"]],
                         ["0x6040:00", "0x607A:00"])
        self.assertTrue(page.pd_errors.isHidden())
        self.assertTrue(page.pd_add_missing.isHidden())
        self.assertEqual(page.pd.rowCount(), 4)

    def test_a_fixed_pdo_refuses_and_says_why(self):
        w, page, s = self.page([(0x1701, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        with mock.patch.object(gui.QMessageBox, "warning") as warn:
            page.add_pd_missing()
        self.assertIn("fixed", warn.call_args[0][2])
        self.assertEqual(len(s["process-data"]["outputs"][0]["entries"]), 1)

    def test_remove_the_selected_entry(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16), (0x607A, 32)])], [(0x1A00, 3, [(0x6041, 16)])])
        page.pd.setCurrentCell(1, 0)
        page.remove_pd_entry()
        self.assertEqual([e["object"] for e in s["process-data"]["outputs"][0]["entries"]], ["0x6040:00"])
        self.assertEqual(page.pd.rowCount(), 2)
        self.assertIn("0x607A", page.pd_errors.text())

    def test_remove_from_a_fixed_pdo_is_refused(self):
        w, page, s = self.page([(0x1701, 2, [(0x6040, 16), (0x607A, 32)])], [(0x1A00, 3, [(0x6041, 16)])])
        page.pd.setCurrentCell(0, 0)
        with mock.patch.object(gui.QMessageBox, "warning") as warn:
            page.remove_pd_entry()
        self.assertTrue(warn.called)
        self.assertEqual(page.pd.rowCount(), 3)

    def test_add_object_and_add_pdo_from_the_choice(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])

        def pick(prefix):
            return lambda parent, title, label, items, *a: (next(i for i in items if i.startswith(prefix)), True)
        with mock.patch.object(gui.QInputDialog, "getItem", side_effect=pick("0x6064")):
            page.add_pd_object()
        self.assertEqual([e["object"] for e in s["process-data"]["inputs"][0]["entries"]],
                         ["0x6041:00", "0x6064:00"])
        s["process-data"]["outputs"] = []
        with mock.patch.object(gui.QInputDialog, "getItem", side_effect=pick("0x1701")):
            page.add_pd_pdo()
        self.assertEqual([p["pdo"] for p in s["process-data"]["outputs"]], ["0x1701"])

    def test_the_esi_check_shows_under_the_table(self):
        w, page, s = self.page([(0x1701, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16), (0x6064, 32)])])
        self.assertIn("0x1701 is fixed in the ESI file", page.pd_errors.text())

    def test_esi_objections_are_apart_from_the_files_errors(self):
        w, page, s = self.page([(0x1701, 2, [(0x6040, 16), (0x607A, 32), (0x60FF, 32)])],
                               [(0x1A00, 3, [(0x6041, 16), (0x6064, 32)])])
        w.check()
        texts = [w.messages.item(i).text() for i in range(w.messages.count())]
        self.assertTrue(any(t.startswith("ESI file: slave 1") and "0x1701 is fixed" in t for t in texts), texts)
        self.assertFalse(any(t.startswith("Error: ") and "0x1701" in t for t in texts))

    def test_mode_marks_in_the_choices(self):
        # what the drive reports and what the PDOs can give, in the mode choices themselves
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        # the ESI file's 0x6502 has CSP and CSV only
        texts = {page.mode.itemData(n): page.mode.itemText(n) for n in range(page.mode.count())}
        self.assertEqual(texts["csp"], "Cyclic synchronous position (CSP)")
        self.assertEqual(texts["csv"], "Cyclic synchronous velocity (CSV) - own PDO mapping")
        self.assertIn("not reported by the drive", texts["pv"])
        model = page.mode.model()
        enabled = {page.mode.itemData(n): model.item(n).isEnabled() for n in range(page.mode.count())}
        self.assertEqual(enabled, {"csp": True, "csv": True, "pv": False, "pp": False})
        self.assertIn("0x6502", page.mode.itemData(page.mode.findData("pv"), gui.Qt.ToolTipRole))
        self.assertTrue(page.other_modes["csv"].isEnabled())
        self.assertEqual(page.other_modes["csv"].text(), "CSV (own PDO mapping)")
        self.assertFalse(page.other_modes["pp"].isEnabled())
        self.assertFalse(page.other_modes["hm"].isEnabled())
        self.assertIn("0x6502", page.other_modes["hm"].toolTip())
        self.assertFalse(page.other_modes["csp"].isEnabled())     # the mode itself
        page.other_modes["pp"].setChecked(True)                  # a file that has it anyway
        self.assertTrue(page.other_modes["pp"].isEnabled())      # can be unticked

    def test_start_over_beside_the_whole_slave_choices(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        self.assertEqual(page.again.text(), "Start over from the bus...")
        self.assertTrue(page.again.isEnabled())

    def test_ways_are_offered_and_used_only_when_chosen(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        self.assertFalse(page.pd_ways.isHidden())
        texts = [x.text() for x in page.pd_ways.findChildren(gui.QLabel)]
        self.assertTrue(any(t.startswith("Recommended: ") for t in texts), texts)
        self.assertEqual(len(s["process-data"]["outputs"][0]["entries"]), 1)     # nothing applied yet
        calls = []
        with mock.patch.object(w.session, "apply_pdos", lambda pos, way=None, pdo_set=None:
                               calls.append((pos, way, pdo_set)) or []), \
                mock.patch.object(w, "run", lambda what, fn, done: done(fn(lambda t: None))):
            buttons = [b for b in page.pd_ways.findChildren(gui.QPushButton) if b.text() == "Use this"]
            buttons[0].click()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 1)
        self.assertTrue(calls[0][1] or calls[0][2])

    def test_every_pdo_can_be_chosen(self):
        # the user's wish 2026-11-22: choose among the PDOs there are, the unsuitable marked,
        # taken at once
        w, page, s = self.page([(0x1701, 2, [(0x6040, 16), (0x607A, 32)])],
                               [(0x1A00, 3, [(0x6041, 16), (0x6064, 32)])])
        self.assertFalse(page.pa.isHidden())
        rx, tx = page.pa_boxes["rx"], page.pa_boxes["tx"]
        self.assertEqual([rx.itemData(i) for i in range(rx.count())], [0x1600, 0x1701, 0x1702])
        self.assertEqual(rx.currentData(), 0x1701)
        self.assertEqual(tx.currentData(), 0x1A00)
        n = rx.findData(0x1702)
        self.assertIn("lacks 0x607A", rx.itemText(n))                  # shown, not hidden
        self.assertEqual(rx.itemData(n, gui.Qt.ForegroundRole).color(), gui.AMBER)
        calls = []
        with mock.patch.object(w.session, "assign_pdos", lambda pos, idx: calls.append((pos, idx)) or []), \
                mock.patch.object(w, "run", lambda what, fn, done: done(fn(lambda t: None))):
            rx.setCurrentIndex(rx.findData(0x1701))
            rx.activated.emit(rx.currentIndex())                       # the same: nothing
            rx.setCurrentIndex(rx.findData(0x1600))
            rx.activated.emit(rx.currentIndex())
        self.assertEqual(calls, [(1, [0x1600, 0x1A00])])

    def test_the_pdos_are_judged_again_when_the_modes_change(self):
        w, page, s = self.page([(0x1701, 2, [(0x6040, 16), (0x607A, 32)])],
                               [(0x1A00, 3, [(0x6041, 16), (0x6064, 32)])])
        rx = page.pa_boxes["rx"]
        self.assertIn("has what CSP needs", rx.itemText(rx.findData(0x1701)))
        page.other_modes["hm"].setChecked(True)          # homing: 0x6060 and 0x6061 needed too
        self.assertIn("lacks 0x6060", rx.itemText(rx.findData(0x1701)))
        self.assertIn("0x6060", page.pd_errors.text())
        page.other_modes["hm"].setChecked(False)
        page.other_modes["csv"].setChecked(True)         # CSV: 0x60FF
        self.assertIn("0x60FF Target velocity", rx.itemText(rx.findData(0x1701)))
        self.assertIn("filled with what CSP, CSV needs", rx.itemText(rx.findData(0x1600)))
        page.other_modes["csv"].setChecked(False)
        page.mode.setCurrentIndex(page.mode.findData("csv"))  # the mode itself
        page.mode.activated.emit(page.mode.currentIndex())
        self.assertIn("lacks 0x60FF Target velocity", rx.itemText(rx.findData(0x1701)))
        self.assertIn("has what CSV needs", rx.itemText(rx.findData(0x1702)))

    def test_the_device_needs_button_follows_the_modes(self):
        w, page, s = self.page([(0x1701, 2, [(0x6040, 16), (0x607A, 32)])],
                               [(0x1A00, 3, [(0x6041, 16), (0x6064, 32)])])
        page.other_modes["csv"].setChecked(True)          # 0x6060 then set every cycle
        rows = [{"at": "PS", "object": "0x6060:00", "from": "profile"}]
        with mock.patch.object(w.session, "missing_init_cmds", side_effect=lambda pos: rows
                               if not (s.get("cia402") or {}).get("modes") else []):
            page.other_modes["csv"].setChecked(False)
            self.assertFalse(page.init_button.isHidden())
            page.other_modes["csv"].setChecked(True)
            self.assertTrue(page.init_button.isHidden())

    def test_the_slaves_own_mapping_is_offered(self):
        # way A: the slave's own mapping as the master read it: last in both boxes, red, not
        # advised, taken only after a yes
        from ecatsetup import tool as tool_mod
        own = tool_mod.parse_pdos("SM2: x\n  RxPDO 0x1600 \"\"\n    PDO entry 0x6040:00, 16 bit, \"\"\n"
                                  "    PDO entry 0x607a:00, 32 bit, \"\"\n"
                                  "SM3: x\n  TxPDO 0x1a00 \"\"\n    PDO entry 0x6041:00, 16 bit, \"\"\n"
                                  "    PDO entry 0x6064:00, 32 bit, \"\"\n")
        w, page, s = self.page([(0x1701, 2, [(0x6040, 16), (0x607A, 32)])],
                               [(0x1A00, 3, [(0x6041, 16), (0x6064, 32)])])
        with mock.patch.object(w.session, "own_mapping", return_value=own):
            page.show_assignment(s)
            rx, tx = page.pa_boxes["rx"], page.pa_boxes["tx"]
            last = rx.count() - 1
            self.assertEqual((rx.itemData(last), tx.itemData(tx.count() - 1)), ("own", "own"))
            self.assertIn("Not advised: the slave's own: 0x1600, 0x1A00; has what CSP needs", rx.itemText(last))
            self.assertEqual(rx.itemData(last, gui.Qt.ForegroundRole).color(), gui.GREY)   # unobtrusive
            self.assertIn("power goes off", rx.itemData(last, gui.Qt.ToolTipRole))
            self.assertEqual(rx.currentData(), 0x1701)            # the file's, as before
            calls = []
            with mock.patch.object(w.session, "use_own_pdos", lambda pos: calls.append(pos) or []), \
                    mock.patch.object(w, "run", lambda what, fn, done: done(fn(lambda t: None))):
                with mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.No):
                    rx.setCurrentIndex(last)
                    rx.activated.emit(last)
                self.assertEqual(calls, [])                       # No: nothing taken, the box back
                self.assertEqual(rx.currentData(), 0x1701)
                with mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Yes):
                    rx.setCurrentIndex(last)
                    rx.activated.emit(last)
            self.assertEqual(calls, [1])
            # configure-pdos false: the boxes show the slave's own, and a PDO can be chosen again
            s["configure-pdos"] = False
            s["process-data"]["inputs"][0]["pdo"] = "0x1A10"      # the slave's own, not in the ESI file
            page.show_assignment(s)
            self.assertFalse(page.pa.isHidden())
            self.assertEqual((rx.currentData(), tx.currentData()), ("own", "own"))
            picked = []
            with mock.patch.object(w.session, "assign_pdos", lambda pos, idx: picked.append(idx) or []), \
                    mock.patch.object(w, "run", lambda what, fn, done: done(fn(lambda t: None))):
                rx.setCurrentIndex(rx.findData(0x1702))
                rx.activated.emit(rx.currentIndex())
            self.assertEqual(picked, [[0x1702, 0x1A00]])           # the input's default with it

    def test_several_pdos_of_a_direction_are_kept_as_they_are(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)]), (0x1701, 2, [(0x607A, 32)])],
                               [(0x1A00, 3, [(0x6041, 16), (0x6064, 32)])])
        rx = page.pa_boxes["rx"]
        self.assertEqual(rx.itemText(0), "0x1600 + 0x1701 (as in the file)")
        self.assertIsNone(rx.currentData())

    def test_no_ways_when_the_mapping_is_enough(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16), (0x607A, 32)])],
                               [(0x1A00, 3, [(0x6041, 16), (0x6064, 32)])])
        self.assertTrue(page.pd_ways.isHidden())

    def test_profile_choice(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        items = [page.profile_box.itemData(i) for i in range(page.profile_box.count())]
        self.assertEqual(items, ["cia402", "generic"])
        self.assertIn("(recognised)", page.profile_box.itemText(0))
        calls = []
        with mock.patch.object(w.session, "change_profile", lambda pos, prof: calls.append((pos, prof)) or []), \
                mock.patch.object(w, "run", lambda what, fn, done: done(fn(lambda t: None))), \
                mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Yes):
            page.profile_box.setCurrentIndex(1)
            page.profile_chosen(1)
        self.assertEqual(calls, [(1, "generic")])

    def test_profile_round_trip_shows_and_hides_the_drive_parts(self):
        # generic and back, through the session (the slave read again for the profile)
        from test_pdoreq import proposal
        from ecatsetup import session as session_mod
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16), (0x607A, 32)])],
                               [(0x1A00, 3, [(0x6041, 16), (0x6064, 32)])])
        s["cia402"]["modes"] = ["hm"]
        info = w.session.bus[1].info

        def read_slave(pos, devices, mode, pdo_set=None, progress=None, extra_modes=(), way=None, profile=None):
            p = proposal(self.dev, mode, extra_modes, way=way, profile=profile)
            p.position = pos
            return info, None, p
        with mock.patch.object(session_mod, "read_slave", read_slave), \
                mock.patch.object(w, "run", lambda what, fn, done: done(fn(lambda t: None))), \
                mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Yes):
            self.assertFalse(page.cia402.isHidden())
            page.profile_box.setCurrentIndex(page.profile_box.findData("generic"))
            page.profile_chosen(0)
            w.select_slave(0)
            w.select_slave(1)
            self.assertEqual(s["profile"], "generic")
            self.assertNotIn("cia402", s)
            self.assertEqual(page.profile_box.currentData(), "generic")
            self.assertTrue(page.cia402.isHidden())           # mode, modes, PDOs per mode
            self.assertTrue(page.homing_card.isHidden())
            self.assertIn("recognised as CiA 402 drive", page.profile_hint.text())
            self.assertIn("Generic chosen", page.profile_hint.text())
            self.assertEqual([page.profile_box.itemData(i) for i in range(page.profile_box.count())],
                             ["cia402", "generic"])
            # and back
            page.profile_box.setCurrentIndex(page.profile_box.findData("cia402"))
            page.profile_chosen(0)
            w.select_slave(0)
            w.select_slave(1)
            self.assertEqual((s["profile"], s["cia402"]["mode"]), ("cia402", "csp"))
            self.assertFalse(page.cia402.isHidden())
            self.assertIn("CSP", page.mode.currentText())

    def test_profile_choice_for_a_modular_device(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        items = [page.profile_box.itemData(i) for i in range(page.profile_box.count())]
        self.assertEqual(items, ["mdp", "generic"])            # the EaserCAT 7000 says 5001
        self.assertIn("(recognised)", page.profile_box.itemText(0))
        self.assertTrue(page.cia402.isHidden())

    def test_profile_choice_cancelled(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        with mock.patch.object(w.session, "change_profile") as change, \
                mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Cancel):
            page.profile_box.setCurrentIndex(1)
            page.profile_chosen(1)
        change.assert_not_called()
        self.assertEqual(page.profile_box.currentData(), "cia402")

    def test_add_what_the_device_needs(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        # the fake bus has no InitCmds, but 0x6060 is not mapped: the mode at start is needed
        self.assertFalse(page.init_button.isHidden())
        self.assertIn("0x6060:00 = the mode", page.init_button.toolTip())
        missing = [{"at": "PS", "object": "0x8000:06", "type": "USINT", "value": 1},
                   {"at": "PS", "object": "0x1C32:02", "from": "cycle-time"}]
        with mock.patch.object(w.session, "missing_init_cmds", return_value=missing):
            page.show_settings(s)
            self.assertFalse(page.init_button.isHidden())
            self.assertIn("0x8000:06 = 1", page.init_button.toolTip())
            self.assertIn("0x1C32:02 = the cycle time", page.init_button.toolTip())
        with mock.patch.object(w.session, "add_device_needs", return_value=missing) as add:
            page.add_device_needs()
        add.assert_called_once_with(1)

    def test_the_init_cmds_card(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        # none in the file, but the mode at start is needed: shown, unticked, amber
        self.assertFalse(page.ic_card.isHidden())
        self.assertEqual(page.ic.rowCount(), 1)
        self.assertEqual(page.ic.item(0, 2).text(), "0x6060:00")
        self.assertEqual(page.ic.item(0, 0).checkState(), gui.Qt.Unchecked)
        self.assertIn("needed", page.ic.item(0, 5).text())
        page.ic.item(0, 0).setCheckState(gui.Qt.Checked)      # ticked: added to the file
        self.assertEqual(s["init-cmds"], [{"at": "PS", "object": "0x6060:00", "from": "profile"}])
        self.assertEqual(page.ic.item(0, 5).text(), "written at start")

    def test_a_command_not_needed_can_still_be_ticked(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16), (0x6060, 8)])], [(0x1A00, 3, [(0x6041, 16)])])
        row = {"at": "PS", "object": "0x6060:00", "from": "profile"}
        view = [{"row": row, "state": "not-needed", "file_index": None,
                 "text": "not needed: 0x6060 is in the PDOs: the profile sets the mode every cycle"},
                {"row": {"object": "0xF081:01"}, "state": "skipped", "file_index": None,
                 "text": "not written by ecat: a device object"}]
        with mock.patch.object(w.session, "init_cmd_view", return_value=view):
            page.show_init_cmds(s)
            self.assertTrue(page.ic.item(0, 0).flags() & gui.Qt.ItemIsUserCheckable)
            self.assertFalse(page.ic.item(1, 0).flags() & gui.Qt.ItemIsUserCheckable)
            page.ic.item(0, 0).setCheckState(gui.Qt.Checked)
        self.assertEqual(s["init-cmds"], [row])
        s["init-cmds"] = [{"at": "PS", "object": "0x6060:00", "from": "profile"},
                          {"at": "PS", "object": "0x8080:01", "complete": True, "data": "00 03"},
                          {"at": "PS", "object": "0x1C32:02", "from": "cycle-time"}]
        page.show_settings(s)
        self.assertFalse(page.ic_card.isHidden())
        self.assertEqual(page.ic.rowCount(), 3)
        self.assertEqual(page.ic.item(0, 3).text(), "the mode (CSP)")
        self.assertEqual(page.ic.item(1, 3).text(), "00 03 (complete access)")
        self.assertEqual(page.ic.item(2, 4).text(), "the cycle time")
        page.ic.item(1, 0).setCheckState(gui.Qt.Unchecked)    # turned off: kept, marked
        self.assertTrue(s["init-cmds"][1]["disabled"])
        page.ic.item(1, 0).setCheckState(gui.Qt.Checked)
        self.assertNotIn("disabled", s["init-cmds"][1])

    def test_the_mode_setting_follows_the_mode(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16), (0x607A, 32), (0x60FF, 32)])],
                               [(0x1A00, 3, [(0x6041, 16), (0x6064, 32), (0x606C, 32)])])
        s["settings"] = [{"object": "0x6060:00", "type": "SINT", "value": 8}]
        page.mode.setCurrentIndex(page.mode.findData("csv"))
        page.general_edited()
        self.assertEqual(s["settings"][0]["value"], 9)

    def test_editing_without_writing_the_pdos_is_refused(self):
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        s["configure-pdos"] = False
        with mock.patch.object(gui.QMessageBox, "warning") as warn:
            page.add_pd_missing()
        self.assertIn("not written to this slave", warn.call_args[0][2])

    def test_configure_pdos_only_in_the_pdo_boxes(self):
        # no box of its own: editing the slave's other fields keeps what the PDO
        # boxes chose (the user, 2026-11-24: one place for it)
        w, page, s = self.page([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        self.assertFalse(hasattr(page, "configure_pdos"))
        s["configure-pdos"] = False
        page.general_edited()
        self.assertIs(s["configure-pdos"], False)
        s.pop("configure-pdos")
        page.general_edited()
        self.assertNotIn("configure-pdos", s)


class TestCards(unittest.TestCase):
    """Where a proposal came from, and the synchronisation card."""

    @unittest.skipIf(gui is None, "PyQt5 is not installed")
    def test_source_rows(self):
        from types import SimpleNamespace
        from ecatsetup import propose
        self.assertIsNone(gui.source_rows(None, None)[0][0])
        p = propose.Proposal(position=0, vendor_id=1, product_code=2, revision=3, serial=0, name="x")
        p.pdo_source, p.profile = "SII", 0
        rows = gui.source_rows(p, SimpleNamespace(coe=None))
        self.assertEqual([r[0] for r in rows], [True, False, False, True, None])
        self.assertIn("No ESI file", rows[2][1])
        p.esi_path, p.esi_revision, p.esi_exact = "/x/dev.xml", 2, False
        rows = gui.source_rows(p, SimpleNamespace(coe=None))
        self.assertIn("dev.xml is for revision 0x00000002, the slave is 0x00000003", rows[2][1])
        p.esi_exact = True
        self.assertTrue(gui.source_rows(p, SimpleNamespace(coe=None))[2][0])

    @unittest.skipIf(gui is None, "PyQt5 is not installed")
    def test_plot(self):
        pins = {"pos-fb": "12.5", "pos-cmd": "13.0", "velocity-fb": "-4"}
        self.assertEqual(gui.plot_values("csp", pins.get, 10.0),
                         {"position": 2.5, "following error": 0.5, "velocity": -4.0})
        self.assertEqual(sorted(gui.plot_values("pp", pins.get, 10.0)), ["position", "velocity"])
        self.assertEqual(gui.plot_values("csp", {}.get, 0.0), {})
        app = QApplication.instance() or QApplication([])
        plot = gui.Plot()
        plot.reset("csp")
        for i in range(200):
            plot.add({"position": i / 10, "velocity": 1.0}, now=i * 0.2)
        self.assertIn(len(plot.samples), (50, 51))     # the last 10 s
        plot.resize(500, 200)
        self.assertFalse(plot.grab().isNull())          # paints
        del app

    def test_symptoms(self):
        from ecatsetup import symptoms
        self.assertTrue(all(len(x) == 3 and all(x) for x in symptoms.SYMPTOMS))
        self.assertIn("Zero Position command", symptoms.html())
        self.assertNotIn("zero-at-start", symptoms.html())
        flow = symptoms.flow_html()
        self.assertTrue(flow.index("Save a report") < flow.index("Watch LinuxCNC"))
        self.assertIn("Watch LinuxCNC shows", symptoms.html())

    @unittest.skipIf(gui is None, "PyQt5 is not installed")
    def test_sync_rows(self):
        pins = {"all-op": "TRUE", "dc-sync-failed": "FALSE", "dc-phased": "TRUE",
                "pll-err": "120", "shift-time": "500000", "turnaround-cycles": "2"}
        cfg = {"master": {"dc": {"servo-thread-follows-ref-clock": True,
                                 "sync0-shift-ns": "auto"}},
               "slaves": [{"name": "d", "dc": {"assign-activate": "0x0300"}}]}
        rows = gui.sync_rows(cfg, pins.get, [100000 + i * 1000 for i in range(10)], 1000000)
        self.assertEqual([r[0] for r in rows], [True] * 6, rows)       # + the room for two cycles
        self.assertIn("SYNC0 500 us after the start of the cycle, turn-around 2", rows[3][1])
        pins.update({"dc-sync-failed": "TRUE", "turnaround-cycles": "3", "dc-phased": "FALSE"})
        rows = gui.sync_rows(cfg, pins.get, [0, 0, 0, 0, 400000], 1000000)
        self.assertEqual([r[0] for r in rows], [True, False, False, False, False, True], rows)
        pins["turnaround-cycles"] = "0"
        self.assertIsNone(gui.sync_rows(cfg, pins.get, [], 1000000)[3][0])
        cfg["master"]["dc"] = {"servo-thread-follows-ref-clock": False, "sync0-shift-ns": 500000}
        rows = gui.sync_rows(cfg, pins.get, [], 1000000)
        self.assertTrue(any("slaves follow the servo thread" in r[1] for r in rows), rows)
        rows = gui.sync_rows({"slaves": [{"name": "io"}]}, pins.get, [], 1000000)
        self.assertEqual([r[0] for r in rows], [True, None])

    @unittest.skipIf(gui is None, "PyQt5 is not installed")
    def test_sync_rows_pll_and_room(self):
        pins = {"all-op": "TRUE", "dc-sync-failed": "FALSE", "dc-phased": "TRUE",
                "pll-err": "281", "shift-time": "80000", "turnaround-cycles": "2",
                "auto-shift-margin": "20833", "auto-shift-input-margin": "20833"}
        cfg = {"master": {"dc": {"servo-thread-follows-ref-clock": True, "sync0-shift-ns": "auto"}},
               "slaves": [{"name": "d", "dc": {"assign-activate": "0x0300"}}]}
        text = lambda rows, key: next(r for r in rows if key in r[1])
        # the pll-err as a spread when there are samples
        rows = gui.sync_rows(cfg, pins.get, [], 125000, pll=[-1900, 400, 2100, 0, 281])
        self.assertIn("pll-err 281 ns, -1900..2100 ns over the last 5 samples", text(rows, "pll-err")[1])
        rows = gui.sync_rows(cfg, pins.get, [], 125000, pll=[1, 2])
        self.assertEqual(text(rows, "pll-err")[1].split("(")[1], "pll-err 281 ns)")
        # the room for two cycles: 125 - 20.8 - 20.8 - 43 = 40 us; with the old 50 us margins -18 us
        offs = [8000, 20000, 51000, 30000, 12000]
        rows = gui.sync_rows(cfg, pins.get, offs, 125000)
        r = text(rows, "Room for two cycles")
        self.assertTrue(r[0])
        self.assertIn("+40 us (20.833 + 20.833 us of margin, the frames' send time spreads 43 us)", r[1])
        pins.update({"auto-shift-margin": "50000", "auto-shift-input-margin": "50000"})
        r = text(gui.sync_rows(cfg, pins.get, offs, 125000), "Room for two cycles")
        self.assertFalse(r[0])
        self.assertIn("Room for two cycles: -18 us (50 + 50 us of margin", r[1])
        self.assertIn("three-cycle position", r[1])
        # without the margin pins: the defaults for the period
        for k in ("auto-shift-margin", "auto-shift-input-margin"):
            pins.pop(k)
        r = text(gui.sync_rows(cfg, pins.get, offs, 125000), "Room for two cycles")
        self.assertIn("20.833 + 20.833 us", r[1])
        self.assertTrue(r[0])
        self.assertEqual(gui.room_for_two(125000, offs, 50000, 50000), -18000)
        self.assertEqual(gui.room_for_two(1000000, [50000, 80000], 50000, 50000), 870000)

    @unittest.skipIf(gui is None, "PyQt5 is not installed")
    def test_sync_rows_measured_and_irq(self):
        pins = {"all-op": "TRUE", "dc-sync-failed": "FALSE", "dc-phased": "TRUE",
                "pll-err": "0", "shift-time": "80000", "turnaround-cycles": "2"}
        ect = {"name": "x", "device": "ECT60", "identity": {"vendor": "0x00000A88", "product": "0x0A880002"},
               "dc": {"assign-activate": "0x0300"}}
        cfg = {"master": {"dc": {"servo-thread-follows-ref-clock": True, "sync0-shift-ns": "auto"}},
               "slaves": [ect, dict(ect, name="y")]}
        find = lambda rows, key: [r for r in rows if key in r[1]]
        for period, level in ((250000, True), (110000, None), (100000, False)):
            r = find(gui.sync_rows(cfg, pins.get, [], period), "measured on the bus")
            self.assertEqual(len(r), 1, period)                # one row for the two drives
            self.assertEqual(r[0][0], level, (period, r))
            self.assertIn("ECT60 (x, y)", r[0][1])
        self.assertIn("refused at 100 us (AL 0x0036)", r[0][1])
        # an extra row for the interrupts, as given
        rows = gui.sync_rows(cfg, pins.get, [], 125000, irq=(False, "NIC interrupts elsewhere"))
        self.assertIn((False, "NIC interrupts elsewhere"), rows)

    @unittest.skipIf(gui is None, "PyQt5 is not installed")
    def test_irq_row(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            def write(path, text):
                os.makedirs(os.path.dirname(os.path.join(d, path)), exist_ok=True)
                with open(os.path.join(d, path), "w") as f:
                    f.write(text)
            write("proc/interrupts", "           CPU0       CPU1\n"
                  "  1:        10          0  IO-APIC   1-edge      i8042\n"
                  "133:         1          0  PCI-MSIX-0000:02:00.0   0-edge      enp2s0\n"
                  "134:       100          0  PCI-MSIX-0000:02:00.0   1-edge      enp2s0-TxRx-0\n"
                  "135:       100          0  PCI-MSIX-0000:02:00.0   2-edge      enp2s0-TxRx-1\n"
                  "140:         5          0  PCI-MSI   other      enp2s0x-TxRx-0\n")
            for n in (133, 134, 135):
                write("proc/irq/%d/smp_affinity_list" % n, "0-2\n")
            write("sys/devices/system/cpu/isolated", "3\n")
            proc, sys = os.path.join(d, "proc"), os.path.join(d, "sys")
            ok, text = gui.irq_row("enp2s0", proc, sys)
            self.assertFalse(ok)
            self.assertIn("are on CPU 0-2, while CPU 3 is the isolated one", text)
            self.assertIn("irq-cpu: 3", text)
            for n in (133, 134, 135):
                write("proc/irq/%d/smp_affinity_list" % n, "3\n")
            ok, text = gui.irq_row("enp2s0", proc, sys)
            self.assertTrue(ok)
            self.assertIn("are on CPU 3, isolated for real time", text)
            write("proc/irq/135/smp_affinity_list", "2-3\n")        # one of them is not on the isolated CPU
            ok, text = gui.irq_row("enp2s0", proc, sys)
            self.assertFalse(ok)
            self.assertIn("are on CPU 2-3", text)
            write("proc/irq/135/smp_affinity_list", "3\n")
            write("sys/devices/system/cpu/isolated", "\n")
            ok, text = gui.irq_row("enp2s0", proc, sys)
            self.assertIsNone(ok)
            self.assertIn("no CPU is isolated", text)
            self.assertIsNone(gui.irq_row("enp9s0", proc, sys))               # not found
            self.assertIsNone(gui.irq_row(None, proc, sys))
            self.assertIsNone(gui.irq_row("enp2s0", os.path.join(d, "nowhere"), sys))
        self.assertEqual(gui._cpu_text({0, 1, 2, 5}), "0-2,5")
        self.assertEqual(gui._cpu_set("0-2,5"), {0, 1, 2, 5})
        self.assertEqual(gui._cpu_set(""), set())


class TestSteps(unittest.TestCase):
    def report(self, state, error=False, code=0, last=None):
        from ecatsetup import diagnose
        return diagnose.Report(0, "x", bool(error or last), state, error, code, last_code=last)

    @unittest.skipIf(gui is None, "PyQt5 is not installed")
    def test_steps(self):
        tags = lambda r: [gui.step_status(r, v)[0] for v, _, _ in gui.STEPS]
        self.assertEqual(tags(self.report(8)), ["✓ reached"] * 4)
        self.assertEqual(tags(self.report(2)), ["✓ reached", "✓ reached", "not reached",
                                                "not reached"])
        self.assertEqual(tags(self.report(2, last=0x1D)),
                         ["✓ reached", "✓ reached", "✕ refused", "not reached"])
        self.assertIn("0x001D", gui.step_status(self.report(2, last=0x1D), 4)[2])


if __name__ == "__main__":
    unittest.main()


class TestParameters(WindowBase):
    """The Parameters card: values for the slaves' parameters."""

    def encoder_window(self):
        from ecatsetup import integrate  # noqa: F401
        w = self.window()
        w.session.cfg["slaves"][0]["encoders"] = [{"name": "enc", "index": "0x6010"}]
        self.hal(w)
        w.refresh()
        return w

    def cell(self, page, pin, col=3):
        for r in range(page.params.rowCount()):
            if page.params.item(r, 0).text() == pin:
                return page.params.item(r, col)
        self.fail("%s is not in the table" % pin)

    def test_hidden_without_parameters_and_listed_with_them(self):
        w = self.window()
        self.assertTrue(self.hal(w).params_card.isHidden())
        w = self.encoder_window()
        page = self.hal(w)
        self.assertFalse(page.params_card.isHidden())
        slave = w.session.cfg["slaves"][0]["name"]
        pin = "%s.enc.pos-scale" % slave
        self.assertEqual(self.cell(page, pin, 2).text(), "1.0")
        self.assertEqual(self.cell(page, pin).text(), "")

    def test_a_value_is_stored_and_empty_removes_it(self):
        from ecatsetup import integrate
        w = self.encoder_window()
        page, cfg = w.hal_page, w.session.cfg
        pin = "%s.enc.pos-scale" % cfg["slaves"][0]["name"]
        self.cell(page, pin).setText("-4096")
        gui.QApplication.processEvents()
        gui.QApplication.processEvents()
        self.assertEqual(integrate.params(cfg), [{"pin": pin, "value": -4096.0}])
        self.assertEqual(self.cell(page, pin).text(), "-4096.0")
        self.cell(page, pin).setText("")
        gui.QApplication.processEvents()
        gui.QApplication.processEvents()
        self.assertEqual(integrate.params(cfg), [])

    def test_a_wrong_value_is_refused_and_said(self):
        from ecatsetup import integrate
        w = self.encoder_window()
        page, cfg = w.hal_page, w.session.cfg
        slave = cfg["slaves"][0]["name"]
        for pin, text in (("%s.enc.pos-scale" % slave, "abc"), ("%s.enc.pos-scale" % slave, "0"),
                          ("%s.enc.velocity-window-ms" % slave, "-5")):
            self.cell(page, pin).setText(text)
            gui.QApplication.processEvents()
            self.assertEqual(integrate.params(cfg), [], text)
            self.assertIn(pin, page.params_text.text())
            self.assertEqual(self.cell(page, pin).text(), "", text)


class TestSaveCapture(WindowBase):
    def test_writes_a_capture_of_the_fake_bus(self):
        from ecatsetup import capture
        w = self.window()
        path = os.path.join(self.dir.name, "c.tar.gz")
        real = tool.run

        def run(*args, binary=False):     # the fake bus, as the tool's calls
            a = [str(x) for x in args]
            if a[0] == "slaves" and "-v" not in a:
                return "0  0:0  PREOP  +  EaserCAT 7000\n1  0:1  PREOP  +  EaserCAT 7000\n"
            if a[0] == "sii_read":
                return self.tool.sii_data if binary else ""
            if a[0] == "sdos":
                return self.tool.sdo_text
            if a[0] == "slaves":
                return ("Vendor Id:       0xd0bed1d0\nProduct code:    0x00007000\n"
                        "Revision number: 0x00000002\nState: PREOP\n")
            if a[0] == "upload":
                return "0x0001 1\n"
            raise tool.ToolError("not on the fake bus")
        for p in self.tool.patches():   # the capture records tool.run itself
            p.stop()
        shown = []
        with mock.patch.object(tool, "run", run), \
                mock.patch.object(gui.QFileDialog, "getSaveFileName", return_value=(path, "")), \
                mock.patch.object(w, "run", lambda what, fn, done: done(fn(lambda t: None))), \
                mock.patch.object(capture.report, "build", return_value="# report\n"), \
                mock.patch.object(capture, "pcap", return_value=(None, "not here")), \
                mock.patch.object(gui.QMessageBox, "information",
                                  lambda *a, **k: shown.append(a[2])):
            w.trouble_page.capture.click()
        self.assertIs(tool.run, real)
        self.assertEqual([s["position"] for s in capture.Replay(path).manifest["slaves"]], [0, 1])
        self.assertIn("Wrote %s" % path, shown[0])
        self.assertIn("note: no frames: not here", shown[0])

    def test_cancelled(self):
        w = self.window()
        with mock.patch.object(gui.QFileDialog, "getSaveFileName", return_value=("", "")), \
                mock.patch.object(w, "run") as run:
            w.trouble_page.capture.click()
        run.assert_not_called()


class TestSignalNameInTheBox(WindowBase):
    """Typing a HAL signal's name in "Connected to" connects the pins on it."""

    def test_a_signal_name_stands_for_its_pins(self):
        from unittest import mock
        from ecatsetup import integrate
        sys.path.insert(0, os.path.dirname(__file__))
        import test_wiring as tw
        w = self.window()
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for name, text in (("machine.ini", tw.SPINDLE_INI), ("spindle_sim.hal", tw.SPINDLE_SIM)):
            with open(os.path.join(d.name, name), "w") as f:
                f.write(text)
        lib = tempfile.TemporaryDirectory()
        self.addCleanup(lib.cleanup)
        p = mock.patch.object(integrate, "HALLIB", lib.name)
        p.start()
        self.addCleanup(p.stop)
        s = w.session
        s.cfg = tw.encoder_cfg()
        page = self.hal(w)
        page.ini = lambda: os.path.join(d.name, "machine.ini")
        page.refresh()
        box = gui.QComboBox()
        box.setEditable(True)
        box.setEditText("spindle-revs")
        page.pin_edited("io.enc.pos", box)
        gui.QApplication.processEvents()
        self.assertEqual(integrate.nets(s.cfg),
                         [{"signal": "spindle-revs", "pins": ["io.enc.pos", "spindle.0.revs"]}])
        # the simulation's writer is told to give way
        self.assertIn("gives way", w.statusBar().currentMessage())
        # a signal the slave's pin cannot join: a second writer
        with open(os.path.join(d.name, "extra.hal"), "w") as f:
            f.write("loadrt scale names=real_rev\nnet real-revs real_rev.out => spindle.0.revs\n")
        with open(os.path.join(d.name, "machine.ini"), "w") as f:
            f.write(tw.SPINDLE_INI.replace("HALFILE = spindle_sim.hal",
                                           "HALFILE = spindle_sim.hal\nHALFILE = extra.hal"))
        s.cfg = tw.encoder_cfg()
        box.setEditText("real-revs")
        with mock.patch.object(gui.QMessageBox, "information") as info:
            page.pin_edited("io.enc.pos", box)
        self.assertTrue(info.called)
        self.assertEqual(integrate.nets(s.cfg), [])


    def test_a_pin_the_hal_files_connect_is_shown(self):
        # after From lcec the HAL files connect the encoder (Turner, 2026-11-14): the
        # column showed nothing, as if the pins were free
        from unittest import mock
        from ecatsetup import integrate
        sys.path.insert(0, os.path.dirname(__file__))
        import test_wiring as tw
        w = self.window()
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        hal = (tw.SPINDLE_SIM +
               "#ecatsetup: net spindle-revs lcec.0.3.enc-pos\n"
               "net spindle-revs io.enc.pos  #ecatsetup: added\n"
               "net spindle-revs => spindle.0.revs\n")
        for name, text in (("machine.ini", tw.SPINDLE_INI), ("spindle_sim.hal", hal)):
            with open(os.path.join(d.name, name), "w") as f:
                f.write(text)
        lib = tempfile.TemporaryDirectory()
        self.addCleanup(lib.cleanup)
        p = mock.patch.object(integrate, "HALLIB", lib.name)
        p.start()
        self.addCleanup(p.stop)
        w.session.cfg = tw.encoder_cfg()
        page = self.hal(w)
        page.ini = lambda: os.path.join(d.name, "machine.ini")
        page.refresh()
        row = next(r for r in range(page.pins.rowCount())
                   if page.pins.item(r, 2).text() == "io.enc.pos")
        self.assertIsNone(page.pins.cellWidget(row, 5))         # not a box to type in
        cell = page.pins.item(row, 5)
        self.assertIn("spindle-revs", cell.text())
        self.assertIn("spindle.0.revs", cell.text())
        self.assertNotIn("io.enc.pos", cell.text())
        self.assertFalse(cell.flags() & gui.Qt.ItemIsEditable)
        self.assertIn("spindle_sim.hal:", cell.toolTip())
        # a pin the HAL files do not connect keeps its box
        free = next(r for r in range(page.pins.rowCount())
                    if page.pins.item(r, 2).text() == "io.enc.velocity")
        self.assertIsNotNone(page.pins.cellWidget(free, 5))

    def test_a_pin_chosen_on_the_from_lcec_card_is_shown_before_apply(self):
        # the choice is in linuxcnc.old-pins; the HAL files still have the lcec pin
        from unittest import mock
        from ecatsetup import fromlcec, integrate
        sys.path.insert(0, os.path.dirname(__file__))
        import test_wiring as tw
        w = self.window()
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        hal = tw.SPINDLE_SIM + "net spindle-revs lcec.0.3.enc-pos => spindle.0.revs\n"
        for name, text in (("machine.ini", tw.SPINDLE_INI), ("spindle_sim.hal", hal)):
            with open(os.path.join(d.name, name), "w") as f:
                f.write(text)
        lib = tempfile.TemporaryDirectory()
        self.addCleanup(lib.cleanup)
        p = mock.patch.object(integrate, "HALLIB", lib.name)
        p.start()
        self.addCleanup(p.stop)
        w.session.cfg = tw.encoder_cfg()
        fromlcec.set_choice(w.session.cfg, "lcec.0.3.enc-pos", "io.enc.pos")
        page = self.hal(w)
        page.ini = lambda: os.path.join(d.name, "machine.ini")
        page.refresh()
        row = next(r for r in range(page.pins.rowCount())
                   if page.pins.item(r, 2).text() == "io.enc.pos")
        self.assertIsNone(page.pins.cellWidget(row, 5))
        cell = page.pins.item(row, 5)
        self.assertTrue(cell.text().startswith("spindle-revs: "))
        self.assertIn("spindle.0.revs", cell.text())
        self.assertNotIn("lcec", cell.text())
        self.assertIn("lcec.0.3.enc-pos", cell.toolTip())
        self.assertIn("Apply", cell.toolTip())


class TestStaleMaster(WindowBase):
    """A master with no slaves (or no link, or on another card) is offered a restart."""

    STALE = "Master0\n  Active: no\n  Slaves: 0\n  Ethernet devices:\n    Main: e8:ea:6a:09:88:2b (attached)\n      Link: DOWN\n"

    def test_offered_once_and_restarted_on_yes(self):
        from ecatsetup import masterstart
        w = self.window()
        ran = []
        with mock.patch.object(tool, "run", return_value=self.STALE), \
                mock.patch.object(masterstart, "linuxcnc_running", return_value=False), \
                mock.patch.object(gui.QMessageBox, "exec_", return_value=gui.QMessageBox.Yes) as ask, \
                mock.patch.object(w, "run", lambda what, fn, done: ran.append(what)):
            w.offer_master_restart()
            self.assertEqual(ran, ["Restarting the master..."])
            self.assertEqual(ask.call_count, 1)
            # asked once a session
            w.offer_master_restart()
            self.assertEqual(ask.call_count, 1)

    def test_a_good_master_or_none_asks_nothing(self):
        from ecatsetup import masterstart
        w = self.window()
        good = "Master0\n  Active: no\n  Slaves: 6\n  Ethernet devices:\n    Main: e8:ea:6a:09:88:2b\n      Link: UP\n"
        for out in (good, tool.ToolError("no master")):
            with mock.patch.object(tool, "run", side_effect=[out] if isinstance(out, Exception) else None,
                                   return_value=None if isinstance(out, Exception) else out), \
                    mock.patch.object(masterstart, "linuxcnc_running", return_value=False), \
                    mock.patch.object(gui.QMessageBox, "exec_") as ask:
                w.offer_master_restart()
                self.assertFalse(ask.called)


class TestNoStrayWindows(WindowBase):
    """A label with no parent that is shown becomes a small empty window of its own for as
    long as it takes to put it into a layout: it showed as a flash on the screen."""

    def test_a_pill_without_a_parent_is_not_shown(self):
        self.assertFalse(gui.pill("PREOP", "green").isVisible())
        self.assertFalse(gui.pill("", "green").isVisible())

    def test_no_window_but_the_main_one_is_shown(self):
        from PyQt5.QtCore import QEvent, QObject
        shown = []

        class Watch(QObject):
            def eventFilter(self, obj, event):
                if event.type() == QEvent.Show and hasattr(obj, "isWindow") and obj.isWindow():
                    shown.append("%s %r" % (type(obj).__name__, getattr(obj, "text", lambda: "")()))
                return False
        watch = Watch()
        self.app.installEventFilter(watch)
        self.addCleanup(lambda: self.app.removeEventFilter(watch))
        w = self.window()
        w.show()
        for page in (w.start_page, w.bus_page, w.trouble_page, w.test_page, w.live_page, w.hal_page):
            w.tabs.setCurrentWidget(page)
            self.app.processEvents()
        w.refresh()
        self.app.processEvents()
        self.assertEqual([x for x in shown if not x.startswith("MainWindow")], [])


class TestProcessDataColumns(WindowBase):
    """Scale, Offset, Bit pins and Invert are shown where an entry uses them, or when asked."""

    def test_unused_columns_are_hidden_until_asked_for(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        cols = (page.SCALE, page.OFFSET, page.SPLIT, page.INVERT)
        used = {c: any(page.pd.item(r, c).text() for r in range(page.pd.rowCount())) for c in cols}
        self.assertEqual({c: page.pd.isColumnHidden(c) for c in cols}, {c: not u for c, u in used.items()})
        self.assertTrue(any(page.pd.isColumnHidden(c) for c in cols), "the fake bus uses them all")
        page.pd_more.setChecked(True)
        self.assertFalse(any(page.pd.isColumnHidden(c) for c in cols))
        page.pd_more.setChecked(False)
        self.assertEqual({c: page.pd.isColumnHidden(c) for c in cols}, {c: not u for c, u in used.items()})

    def test_a_bit_field_shows_its_bit_pins(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        row = next(r for r in range(page.pd.rowCount())
                   if page.pd.item(r, page.PIN).flags() & gui.Qt.ItemIsEditable
                   and page.pd.item(r, page.PIN).text() and page._entry(r).get("bits", 1) > 1
                   and page.pd.cellWidget(r, page.HALTYPE) is not None)
        if not page.pd.isColumnHidden(page.SPLIT):
            page.pd_more.setChecked(False)
            page.pd.setColumnHidden(page.SPLIT, True)
        page.pd.cellWidget(row, page.HALTYPE).setCurrentText(page.BIT_FIELD)
        self.assertFalse(page.pd.isColumnHidden(page.SPLIT))


@unittest.skipIf(gui is None, "PyQt5 is not installed")
class TestTimingPage(WindowBase):
    """The real-time check's page: the steps, the findings, Try / Keep / Undo,
    against a fake tree, saved runs and a fake sudo (no halrun, no root)."""

    def setUp(self):
        super().setUp()         # the fake bus
        sys.path.insert(0, os.path.dirname(__file__))
        from test_rtcheck import fake_tree
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"XDG_STATE_HOME": os.path.join(self.tmp.name, "state")})
        p.start()
        self.addCleanup(p.stop)
        self.roots = fake_tree(os.path.join(self.tmp.name, "tree"))
        for target, value in (("ecatsetup.rtflow.can_sudo", False), ("ecatsetup.rtcheck.hal_running", False)):
            m = mock.patch(target, return_value=value)
            m.start()
            self.addCleanup(m.stop)

    def page(self, w):
        from ecatsetup import rtflow
        tp = w.timing_page
        tp.flow = rtflow.Flow()
        tp.flow.take_inventory(roots=self.roots)
        w.tabs.setCurrentWidget(tp)
        return tp

    def add_run(self, tp, late_us, name):
        from ecatsetup import rtcheck
        from test_rtrules import good_run
        r = good_run(tp.flow.inventory, late_us=late_us)
        path = os.path.join(self.tmp.name, name)
        r.save(path)
        tp.measured(rtcheck.Run.load_file(path))

    def test_steps_findings_and_a_change(self):
        from ecatsetup import rtflow
        w = self.window()
        tp = self.page(w)
        tp.refresh()
        self.assertIn("Baseline", tp.step_pill.text())
        self.assertTrue(tp.step_pill.isVisibleTo(tp))
        # every step shows, the ones done ticked, only the present one a pill
        self.assertEqual([lab.text() for lab in tp.step_labels],
                         ["\u2713 Inventory", "Step 2 of 6 \u00b7 Baseline", "3 Findings", "4 One change",
                          "5 Measure again", "6 Done"])
        self.assertEqual([lab.property("pill") for lab in tp.step_labels],
                         [None, "blue", None, None, None, None])
        self.assertFalse(tp.summary.isVisibleTo(tp))           # no run yet
        self.assertGreater(tp.inv_table.rowCount(), 10)
        self.assertTrue(tp.measure_button.isEnabled())
        self.assertFalse(tp.root.isEnabled())               # sudo would ask for a password
        self.add_run(tp, 600, "b1.json")
        self.add_run(tp, 610, "b2.json")
        self.assertIn("Findings", tp.step_pill.text())
        self.assertIs(tp.step_pill, tp.step_labels[2])
        self.assertEqual(tp.step_labels[1].text(), "\u2713 Baseline")
        # under Measure: the run in short, and the findings counted with the first named
        self.assertTrue(tp.summary.isVisibleTo(tp))
        found, _ = tp.flow.findings()
        summary = tp.summary_text.text()
        self.assertIn("servo-thread", summary)
        self.assertIn("<b>%d findings</b>" % len(found), summary)
        self.assertIn("Most to gain: %s" % found[0].title, summary)
        self.assertIn("Compared with the run before", summary)
        self.assertTrue(tp.goto_findings.isVisibleTo(tp))
        # the button takes the findings' top to the top of the page
        w.show_tab(tp)
        w.resize(1300, 700)
        w.show()
        self.addCleanup(w.hide)
        QApplication.processEvents()
        tp.goto_findings.click()
        bar = tp.scroller.verticalScrollBar()
        self.assertEqual(bar.value(), min(bar.maximum(), tp.find_card.y() - 12))
        self.assertGreater(bar.value(), 0)
        # the way is the baseline's from now on
        self.assertFalse(tp.load.isEnabled())
        self.assertIn("late at most", tp.result.toPlainText())
        titles = [v.f.title for v in tp.finding_views]
        self.assertTrue(any("Turbo" in t for t in titles))
        # Look mode: Try shown on run-time changes but not pressable; Improve (after a yes): pressable
        tries = [v.try_button for v in tp.finding_views if v.try_button.isVisibleTo(tp)]
        self.assertTrue(tries)
        self.assertFalse(any(b.isEnabled() for b in tries))
        self.assertIn("Tick Improve", tries[0].toolTip())
        # the computer is one line; the table on request
        self.assertFalse(tp.inv_table.isVisibleTo(tp))
        self.assertIn("Intel(R) N100", tp.inv_summary.text())
        tp.inv_toggle.click()
        self.assertTrue(tp.inv_table.isVisibleTo(tp))
        # the mode is two buttons
        with mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Yes):
            tp.improve_button.click()
        self.assertEqual(tp.mode.text(), "Improve")
        self.assertTrue(tp.improve_button.isChecked())
        self.assertFalse(tp.look_button.isChecked())
        self.assertIn("No restart", tp.mode_text.text())
        turbo = next(v for v in tp.finding_views if "Turbo" in v.f.title)
        self.assertTrue(turbo.try_button.isEnabled())
        calls = []
        asked = []
        # it asks first, with the command and that no restart is needed; No changes nothing
        with mock.patch.object(rtflow, "sudo", lambda cmd, runner=None: calls.append(cmd) or ""), \
                mock.patch.object(gui.QMessageBox, "question",
                                  side_effect=lambda *a: asked.append(a[2]) or gui.QMessageBox.No):
            turbo.try_button.click()
        self.assertEqual(calls, [])
        self.assertIn("No restart is needed", asked[0])
        self.assertIn("echo 1 > /sys/devices/system/cpu/intel_pstate/no_turbo", asked[0])
        with mock.patch.object(rtflow, "sudo", lambda cmd, runner=None: calls.append(cmd) or ""), \
                mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Yes):
            turbo.try_button.click()
        self.assertEqual(calls, ["echo 1 > /sys/devices/system/cpu/intel_pstate/no_turbo"])
        self.assertIn("Measure again", tp.step_pill.text())
        self.assertTrue(tp.undo_button.isVisibleTo(tp))
        self.assertFalse(tp.keep_button.isEnabled())         # not measured yet
        # the change stays where the findings are, with Undo and Keep beside it
        pv = tp.pending_view
        self.assertIs(tp.finding_views[0], pv)
        self.assertTrue(pv.undo_button.isEnabled())
        self.assertFalse(pv.keep_button.isEnabled())
        self.assertFalse(any(getattr(v, "f", None) and v.f.id == "turbo-on" for v in tp.finding_views))
        self.assertFalse(any(v.try_button.isEnabled() for v in tp.finding_views if hasattr(v, "try_button")))
        self.assertEqual(tp.change_table.rowCount(), 1)
        self.add_run(tp, 300, "a1.json")
        self.assertIn("better", tp.result.toPlainText())
        self.assertTrue(tp.keep_button.isEnabled())
        self.assertTrue(tp.pending_view.keep_button.isEnabled())
        tp.pending_view.keep_button.click()
        self.assertIsNone(tp.pending_view)
        self.assertEqual(tp.change_table.item(0, 1).text(), "kept")
        self.assertEqual(tp.change_table.item(0, 2).text(), "better")
        # undo it later from the table
        tp.change_table.selectRow(0)
        with mock.patch.object(rtflow, "sudo", lambda cmd, runner=None: calls.append(cmd) or ""):
            tp.undo_selected.click()
        self.assertEqual(calls[-1], "echo 0 > /sys/devices/system/cpu/intel_pstate/no_turbo")
        self.assertEqual(tp.change_table.item(0, 1).text(), "undone")

    def test_load_without_stress_ng(self):
        # zx45: "with load" ran at rest because stress-ng was missing; now the
        # page says so under Load and Measure does not start
        w = self.window()
        tp = self.page(w)
        tp.refresh()
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("ecatsetup.rtcheck.measure_threads") as run:
            tp.load.setCurrentIndex(tp.load.findData("stress"))
            self.assertTrue(tp.load_warn.isVisibleTo(tp))
            self.assertIn("sudo apt install stress-ng", tp.load_warn.text())
            tp.measure()
            self.assertIsNone(tp.worker)
            run.assert_not_called()
            tp.load.setCurrentIndex(tp.load.findData("rest"))
            self.assertFalse(tp.load_warn.isVisibleTo(tp))
        with mock.patch("shutil.which", return_value="/usr/bin/stress-ng"):
            tp.load.setCurrentIndex(tp.load.findData("stress"))
            self.assertFalse(tp.load_warn.isVisibleTo(tp))

    def test_password_asks_for_a_terminal(self):
        from ecatsetup import rtflow
        w = self.window()
        tp = self.page(w)
        self.add_run(tp, 600, "b1.json")
        self.add_run(tp, 610, "b2.json")
        with mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Yes):
            tp.improve.setChecked(True)
        turbo = next(v for v in tp.finding_views if "Turbo" in v.f.title)

        def needs_password(cmd, runner=None):
            raise rtflow.SudoError("sudo needs a password here", needs_password=True)
        shown = []

        def exec_(box):
            # the user ran the commands in a terminal and presses "I ran it"
            shown.append(box.informativeText())
            ran = next(b for b in box.buttons() if b.text() == "I ran it")
            box.clickedButton = lambda: ran
            return 0
        with mock.patch.object(rtflow, "sudo", needs_password), \
                mock.patch.object(gui.QMessageBox, "exec_", exec_), \
                mock.patch.object(gui.QMessageBox, "question", return_value=gui.QMessageBox.Yes):
            turbo.try_button.click()
        self.assertIn("sudo sh -c 'echo 1 > /sys/devices/system/cpu/intel_pstate/no_turbo'", shown[0])
        self.assertEqual(tp.change_table.item(0, 1).text(), "tried, by hand")

    def test_the_list_to_do_yourself(self):
        w = self.window()
        tp = self.page(w)
        self.add_run(tp, 600, "b1.json")
        self.add_run(tp, 610, "b2.json")
        shown = []
        with mock.patch.object(gui.TodoDialog, "exec_", lambda d: shown.append(d)):
            tp.todo_button.click()
        d = shown[0]
        text = d.text.toPlainText()
        self.assertIn("# Real-time settings to make yourself", text)
        found, _ = tp.flow.findings()
        self.assertTrue(any(f.title in text for f in found if f.kind != "measure"))
        path = os.path.join(self.tmp.name, "todo.md")
        with mock.patch.object(gui.QFileDialog, "getSaveFileName", return_value=(path, "")):
            d.save_button.click()
        with open(path) as f:
            self.assertEqual(f.read(), text + "\n")
        self.assertEqual(d.saved, path)

    def test_the_list_failing_does_not_end_the_window(self):
        w = self.window()
        tp = self.page(w)
        with mock.patch("ecatsetup.rttodo.text", side_effect=AttributeError("x")), \
                mock.patch.object(gui.QMessageBox, "warning") as warn, \
                mock.patch.object(gui.TodoDialog, "exec_") as shown:
            tp.todo_button.click()
        self.assertIn("could not be made: AttributeError: x", warn.call_args[0][2])
        self.assertFalse(shown.called)

    def test_report(self):
        w = self.window()
        tp = self.page(w)
        self.add_run(tp, 600, "b1.json")
        path = os.path.join(self.tmp.name, "r.md")
        with mock.patch.object(gui.QFileDialog, "getSaveFileName", return_value=(path, "")):
            tp.save_report.click()
        with open(path) as f:
            text = f.read()
        self.assertIn("# Real-time check report", text)
        self.assertIn(".json (baseline, reference)", text)


class TestHelp(WindowBase):
    """The Help tab: F1 and ? open the section for what one works with, Back returns."""

    def test_f1_and_back(self):
        w = self.window()
        self.assertEqual(w.tabs.bar.tabText(w.tabs.count() - 1), "Help")
        w.tabs.setCurrentWidget(w.bus_page)
        w.select(0)
        w.f1()
        self.assertIs(w.tabs.currentWidget(), w.help_page)
        h = w.help_page
        self.assertEqual(h.docs.sections[h.current].title, "Bringing up a new slave")   # a generic slave
        self.assertTrue(h.back_button.isVisibleTo(w))
        self.assertEqual(h.back_button.text(), "← Back to Bus")
        w.f1()                                          # F1 again: back where one was
        self.assertIs(w.tabs.currentWidget(), w.bus_page)
        self.assertFalse(h.back_button.isVisibleTo(w))

    def test_a_cards_question_mark(self):
        w = self.window()
        w.tabs.setCurrentWidget(w.hal_page)
        card = next(c for c in w.hal_page.findChildren(gui.Card)
                    if c.help_button is not None and c.title.text() == "JOINTS")
        card.help_button.click()
        h = w.help_page
        self.assertEqual(h.docs.sections[h.current].anchor, "joints")
        self.assertEqual(h.back_button.text(), "← Back to HAL & LinuxCNC")
        h.back_button.click()
        self.assertIs(w.tabs.currentWidget(), w.hal_page)

    def test_the_tab_keeps_its_place_and_suggests(self):
        w = self.window()
        h = w.help_page
        h.show_topic("Distributed clocks")
        w.tabs.setCurrentWidget(w.timing_page)
        w.tabs.setCurrentWidget(h)                      # by the tab: the last place, a suggestion only
        self.assertEqual(h.docs.sections[h.current].anchor, "distributed-clocks")
        self.assertFalse(h.back_button.isVisibleTo(w))
        self.assertIn("Timing", h.where.text())
        h.where.click()
        self.assertEqual(h.docs.sections[h.current].anchor, "the-computers-timing")

    def test_links_search_and_images(self):
        w = self.window()
        h = w.help_page
        w.tabs.setCurrentWidget(h)
        h.show_topic(("ecatsetup.md", "the-window"))
        h.link_clicked(QUrl("#distributed-clocks"))
        self.assertEqual(h.docs.sections[h.current].anchor, "distributed-clocks")
        h.link_clicked(QUrl("reference.md#dc"))
        self.assertEqual((h.docs.sections[h.current].file, h.docs.sections[h.current].anchor), ("reference.md", "dc"))
        h.go_back_in_help()
        self.assertEqual(h.docs.sections[h.current].anchor, "distributed-clocks")
        h.search.setText("turn-around SYNC0")
        self.assertGreater(h.tree.topLevelItemCount(), 1)
        first = h.tree.topLevelItem(0)
        h.tree_chosen(first)
        self.assertIn("turn-around", h.docs.text(h.current, whole=False).lower())
        h.search.clear()
        self.assertEqual(h.tree.topLevelItemCount(), len(h.docs.tops()))
        h.show_topic(("ecatsetup.md", "the-window"))       # has screenshots
        h.resize(700, 600)
        h.fit_images()
        doc = h.text.document()
        widths = []
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fmt = it.fragment().charFormat()
                if fmt.isImageFormat():
                    widths.append(fmt.toImageFormat().width())
                it += 1
            block = block.next()
        self.assertTrue(widths)
        self.assertTrue(all(0 < x <= h.text.viewport().width() for x in widths), widths)


@unittest.skipIf(gui is None, "PyQt5 is not installed")
class TestBringup(WindowBase):
    """The slave page's card Bringing it up (bringup.py)."""

    def setUp(self):
        super().setUp()
        from ecatsetup import quirks
        env = {"ECATSETUP_DEVICES": os.path.join(self.dir.name, "devices"),
               "ECATSETUP_MEASURED": os.path.join(self.dir.name, "measured")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(quirks.load, True)
        quirks.load(reload=True)

    def test_a_drive_has_the_card_and_what_the_workbench_saw_comes_in(self):
        from ecatsetup import bringup
        w = self.window()
        w.select_slave(1)
        page = w.slave_page
        self.assertTrue(page.bringup_card.isHidden())          # not a CiA 402 drive
        s = w.session.slave(0)
        s["profile"], s["cia402"] = "cia402", {"mode": "csp"}
        w.select_slave(0)
        self.assertFalse(page.bringup_card.isHidden())
        keys = [r.key for r in page.bringup_found]
        self.assertEqual(keys[-4:], ["op", "enable", "follows", "start"])
        mark, what, text, go, _keep = page.bringup_rows[keys.index("op")]
        self.assertEqual(mark.text(), "?")
        self.assertEqual(go.text(), "Workbench →")
        self.assertIn("shown only when tried", page.bringup_summary.text())
        # the workbench saw it in OP and enabled
        seen = w.bringup_seen.setdefault(0, bringup.Seen())
        seen.saw("run", True, 4)
        page.show_bringup()
        mark, what, text, go, _keep = page.bringup_rows[keys.index("op")]
        self.assertEqual(mark.text(), "✓")
        self.assertTrue(go.isHidden())
        # a row leads to its card
        n = keys.index("start")
        page.bringup_found[n].card = "operation"
        page.bringup_go(n)
        self.assertIs(w.tabs.currentWidget(), w.bus_page)

    def test_the_homing_goal_and_what_the_cards_tell(self):
        from ecatsetup import bringup
        w = self.window()
        s = w.session.slave(0)
        s["profile"], s["cia402"] = "cia402", {"mode": "csp", "modes": ["hm"]}
        s["homing"] = {"method": 21}
        w.select_slave(0)
        page = w.slave_page
        page.bringup_goal.setCurrentIndex(page.bringup_goal.findData(bringup.HOMING))
        page.show_bringup()
        keys = [r.key for r in page.bringup_found]
        self.assertEqual(keys[:3], ["hm", "methods", "method"])
        self.assertIn("switch-home", keys)
        # the Switches card's test result comes to the card
        card = w.test_page.switches
        card.slave = s
        card.test = {"kind": "home"}
        card.finish(("ok", "Set up right"))
        row = page.bringup_found[[r.key for r in page.bringup_found].index("switch-home")]
        self.assertEqual(row.state, bringup.OK)
        # and the Move card's homing
        from types import SimpleNamespace
        w.test_page.move.home_finished(SimpleNamespace(slave=s, name="x", axis=0), "homed", True)
        found = {r.key: r.state for r in page.bringup_found}
        self.assertEqual((found["homed"], found["after"]), (bringup.OK, bringup.OK))

    def test_the_workbench_tells_the_card(self):
        w = self.window()
        s = w.session.slave(0)
        s["profile"], s["cia402"] = "cia402", {"mode": "csp"}
        w.select_slave(0)
        tp = w.test_page
        values = {"slave-state-op": "TRUE", "cia-state": "4", "error-code": "0",
                  "drv-actual-position": "1234", "enable": "TRUE"}
        run = mock.Mock(log_path="/tmp/run-1", watching=False)
        tp.saw_for_bringup(run, 0, True, 4, values.get)
        seen = w.bringup_seen[0]
        self.assertIsNotNone(seen.op)
        self.assertIsNotNone(seen.enabled)
        self.assertEqual(seen.runs[0][:2], [1234, 1234])
        keys = [r.key for r in w.slave_page.bringup_found]
        self.assertEqual(w.slave_page.bringup_rows[keys.index("enable")][0].text(), "✓")
        # every drive, from all pins, not only the one shown
        name = s["name"]
        tp.saw_every_drive(mock.Mock(log_path="/tmp/run-2", watching=False),
                           {name + ".cia-state": "7", name + ".slave-state-op": "TRUE",
                            name + ".error-code": "0x8611", name + ".drv-actual-position": "99"})
        self.assertEqual(w.bringup_seen[0].fault, 0x8611)
        self.assertEqual(w.bringup_seen[0].runs[-1][:2], [99, 99])


class TestDeviceData(WindowBase):
    """Shared device data (prototype): remembering a fact where it is learnt,
    the Device data dialog, Share, Fetch, the facts one by one, Report."""

    def setUp(self):
        super().setUp()
        from ecatsetup import devicedataview, quirks
        self.dv, self.quirks = devicedataview, quirks
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"ECATSETUP_DEVICES": os.path.join(self.tmp.name, "devices"),
               "ECATSETUP_SHARED_CACHE": os.path.join(self.tmp.name, "cache"),
               "ECATSETUP_SETTINGS": os.path.join(self.tmp.name, "settings.yaml")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(quirks.load, True)
        quirks.load(reload=True)
        for target, kw in ((devicedataview.SourceDialog, {"exec_": mock.DEFAULT}),):
            m = mock.patch.multiple(target, **kw)
            mocks = m.start()
            mocks["exec_"].return_value = QDialog.Accepted
            self.addCleanup(m.stop)
        self.opened = []
        o = mock.patch.object(devicedataview, "open_url", self.opened.append)
        o.start()
        self.addCleanup(o.stop)

    def test_homing_methods_and_inputs_from_the_manual(self):
        from ecatsetup import devicedata, homing
        w = self.window()
        w.select_slave(0)
        ident = self.quirks.identity(w.session.slave(0))
        dlg = self.dv.MethodsDialog(w, None, {}, None)
        self.addCleanup(dlg.deleteLater)
        for n in (19, 20, 21, 22, 35, 36, 37):
            dlg.boxes[n].setChecked(True)
        dlg.more.setText("-1")
        dlg.texts[36].setText("torque homing towards CCW")
        dlg.ref.setText("manual 2.4.6")
        self.assertEqual(dlg.methods(), [-1, 19, 20, 21, 22, 35, 36, 37])
        path = self.dv.remember_methods(w, w, 0, None, None, ask=False, dialog=dlg)
        self.assertTrue(path.startswith(self.tmp.name))
        self.assertEqual(homing.supported(w.session.slave(0), read=lambda *a: None)[0],
                         [-1, 19, 20, 21, 22, 35, 36, 37])
        self.assertEqual(homing.method(36, w.session.slave(0)).text, "torque homing towards CCW")
        src = devicedata.sources(ident)["homing methods"]
        self.assertEqual((src["kind"], src["list"], src["relation"], src["ref"]),
                         ("manual", "closed", "completes", "manual 2.4.6"))
        # the inputs: a function object per input, its codes, the polarity bits
        dlg = self.dv.InputsDialog(w, ["IN1", "IN2"])
        self.addCleanup(dlg.deleteLater)
        self.assertEqual(list(dlg.rows), ["IN1", "IN2"])
        dlg.rows["IN1"][0].setText("0x2007:01")
        dlg.rows["IN1"][1].setText("0")
        with self.assertRaisesRegex(ValueError, "no function is needed"):
            dlg.inputs()
        for k, v in (("home", "3"), ("negative", "1"), ("positive", "2"), ("none", "0")):
            dlg.codes[k].setText(v)
        dlg.pobject.setText("0x2008:00")
        self.assertIsNotNone(self.dv.remember_inputs(w, w, 0, ask=False, dialog=dlg))
        got = self.quirks.of(ident)["homing"]["inputs"]
        self.assertEqual(got["function"]["objects"], {"IN1": "0x2007:01"})
        self.assertEqual(got["polarity"]["choices"], {"normally open": 1, "normally closed": 0})

    def test_the_bringup_card_keeps_what_it_saw(self):
        from ecatsetup import bringup
        w = self.window()
        s = w.session.slave(0)
        s["profile"], s["cia402"] = "cia402", {"mode": "csp"}
        w.select_slave(0)
        page = w.slave_page
        seen = w.bringup_seen.setdefault(0, bringup.Seen())
        seen.saw("r1", True, 7, error=0x8123, position=5000)
        seen.saw("r2", True, 7, error=0x8123, position=5001)
        page.show_bringup()
        keys = [r.key for r in page.bringup_found]
        n = keys.index("start")
        self.assertFalse(page.bringup_rows[n][4].isHidden())         # Remember...
        page.bringup_remember(n)
        ident = self.quirks.identity(s)
        self.assertIs(self.quirks.keeps_position(ident), True)
        self.assertIsNone(page.bringup_found[keys.index("start")].remember)   # known now
        # an error code: the meaning typed in the source dialog's text
        n = keys.index("enable")
        with mock.patch.object(self.dv.SourceDialog, "exec_",
                               lambda d: (d.note.setText("overcurrent"), QDialog.Accepted)[1]):
            page.bringup_remember(n)
        self.assertEqual(self.quirks.error_meaning(ident, 0x8123), "overcurrent")
        self.assertIn("overcurrent", page.bringup_found[n].text)

    def test_what_the_workbench_saw_is_offered_as_facts(self):
        from types import SimpleNamespace
        w = self.window()
        s = w.session.slave(0)
        s["profile"], s["cia402"] = "cia402", {"mode": "csp", "modes": ["hm"]}
        s["homing"] = {"method": 21}
        ident = self.quirks.identity(s)
        dt = SimpleNamespace(slave=s, axis=0, name="x")
        # a torque that never moved during a measurement
        card = w.test_page.response
        with mock.patch.object(card, "drive", lambda: dt):
            card.show_constant(SimpleNamespace(constant={0x6077: "it stayed at 0"}))
            self.assertFalse(card.ignore_button.isHidden())
            card.remember_constant()
        self.assertIn(0x6077, self.quirks.measure_ignore(ident))
        # a drive that did not follow after homing by 21
        with mock.patch.object(gui.QMessageBox, "question", lambda *a, **k: gui.QMessageBox.Yes):
            w.test_page.move.offer_enable_again(dt)
        self.assertEqual(self.quirks.of(ident)["homing"]["enable-again"], [21])
        from ecatsetup import devicedata
        self.assertEqual(devicedata.sources(ident)["homing enable-again"]["kind"], "observed")
        # known now: not asked again
        with mock.patch.object(gui.QMessageBox, "question", side_effect=AssertionError):
            self.assertIsNone(w.test_page.move.offer_enable_again(dt))

    def test_limits_and_tuning_objects_from_the_window(self):
        from types import SimpleNamespace
        from ecatsetup import tuning
        w = self.window()
        s = w.session.slave(0)
        ident = self.quirks.identity(s)
        dlg = self.dv.LimitsDialog(w)
        self.addCleanup(dlg.deleteLater)
        obj, typ, why = dlg.rows["following-error"]
        obj.setText("0x2022:00")
        dlg.rows["travel"][2].setText("ignored in CSP")
        self.assertIsNotNone(self.dv.remember_limits(w, w, 0, ask=False, dialog=dlg))
        lim = self.quirks.limits(ident)
        self.assertEqual(lim["following-error"], (0x2022, 0, "uint32"))
        self.assertEqual(lim["travel"], "ignored in CSP")
        # a tuning object found by Look for settings
        card = w.test_page.response
        c = tuning.Candidate(0x2009, 0, "Filter Time", "command-filter", "uint16", value=2000,
                             minimum=0, maximum=25600, factory=6400, unit="us")
        dt = SimpleNamespace(slave=s)
        with mock.patch.object(card, "drive", lambda: dt), mock.patch.object(card, "chosen", lambda: c), \
                mock.patch.object(self.dv.SourceDialog, "exec_",
                                  lambda d: (d.note.setText("sliding average"), QDialog.Accepted)[1]):
            card.remember_tuning()
        t = next(x for x in self.quirks.tuning(ident) if x["index"] == 0x2009)
        self.assertEqual((t["kind"], t["type"], t["max"], t["factory"], t["note"]),
                         ("command-filter", "UINT", 25600, 6400, "sliding average"))

    def test_the_source_asks_what_the_sort_of_fact_needs(self):
        # experience: where it worked; a list: closed or open; a property: neither
        w = self.window()
        d = self.dv.SourceDialog(w, "Setting 0x2009:00", "2000", None, path=("settings", "0x2009:00"))
        self.addCleanup(d.deleteLater)
        self.assertIsNotNone(d.context)
        self.assertIsNone(d.listing)
        d.context.setText("plasma cutter, CSP, 500 us")
        self.assertEqual(d.source()["context"], "plasma cutter, CSP, 500 us")
        d = self.dv.SourceDialog(w, "Homing methods", "[21]", None, path=("homing", "methods"))
        self.addCleanup(d.deleteLater)
        self.assertIsNone(d.context)
        self.assertEqual(d.source()["list"], "closed")
        d = self.dv.SourceDialog(w, "0x2010 is a command", None, None, note="", path=("commands", "0x2010"))
        self.addCleanup(d.deleteLater)
        self.assertTrue(d.context is None and d.listing is None)
        self.assertNotIn("context", d.source())

    def test_remember_share_fetch_choose_and_report(self):
        import json
        import tarfile
        import urllib.parse
        import yaml
        from ecatsetup import devicedata
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        ident = self.quirks.identity(w.session.slave(0))
        # a setting remembered for the device type, from the Settings card
        page.add_setting()
        row = page.st.rowCount() - 1
        page.st.item(row, 0).setText("0x2000:02")
        page.st.item(row, 3).setText("5")
        page.st.selectRow(row)
        path = page.remember_setting()
        self.assertTrue(path.startswith(self.tmp.name))
        self.assertEqual(self.quirks.settings(ident)[0]["value"], 5)
        # a command, from the SDO card
        w.test_page.remember_command((0x2001, 0))
        self.assertIn(0x2001, self.quirks.of(ident)["commands"])
        src = devicedata.sources(ident)
        self.assertEqual(src["settings 0x2000:02"]["kind"], "tried")
        # the device's page: the facts grouped, each with where it comes from and its card
        dlg = page.device_data()
        self.addCleanup(dlg.deleteLater)
        # a window of its own (not kept above the main window), one at a time
        self.assertIsNone(dlg.parent())
        self.assertIs(page.device_data(), dlg)
        html = dlg.page.toHtml()
        text = dlg.page.toPlainText()
        self.assertLess(text.index("Commands and settings"), text.index("Process data"))
        self.assertIn("Setting 0x2000:02", text)
        self.assertIn("value: 5", text)
        # the kind, the date and the slave's revision
        self.assertIn("tried on a machine, %s, 0x00000002" % devicedata.today(), text)
        self.assertIn("came with the program", text)                # the shipped pin names
        self.assertIn("card:settings", html)
        self.assertIn("forget:settings 0x2000:02", html)
        self.assertNotIn("forget:pins", html)                       # shipped: nothing to forget
        # a card link brings the card that owns the fact into view
        dlg.link(QUrl("card:sdo"))
        self.assertIs(w.tabs.currentWidget(), w.test_page)
        dlg.link(QUrl("card:settings"))
        self.assertIs(w.tabs.currentWidget(), w.bus_page)
        self.assertIs(w.current, 0)
        # a card hidden now: the one that leads to it, and the status says when it shows
        shown, why = self.dv.show_card(w, 0, "homing")
        self.assertIs(shown, w.slave_page.op_card)
        self.assertIn("tick Homing (hm)", why)
        shown, why = self.dv.show_card(w, 0, "switches")
        self.assertIs(shown, w.test_page.start_card)
        self.assertIn("Start the workbench", why)
        w.resize(1200, 700)
        w.show()
        QTest.qWait(50)
        w.bus_page.scroll.verticalScrollBar().setValue(0)
        shown, why = self.dv.show_card(w, 0, "settings")
        self.assertEqual((shown, why), (w.slave_page.settings_card, ""))
        QTest.qWait(120)
        # scrolled so that the card's top is in view
        area = w.bus_page.scroll
        top = shown.mapTo(area.widget(), shown.rect().topLeft()).y()
        bar = area.verticalScrollBar().value()
        self.assertGreater(bar, 0)
        self.assertTrue(bar <= top <= bar + area.viewport().height(), (bar, top))
        # Share opens the prefilled post, without asking in the test
        url = dlg.share(confirm=False)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(devicedata.from_post(q["body"][0]), devicedata.strip(
            devicedata.read_raw(devicedata.own_path(ident))))
        self.assertEqual(self.opened, [url])
        # someone shares another value; fetched, it is offered fact by fact
        theirs = devicedata.strip(devicedata.read_raw(devicedata.own_path(ident)))
        devicedata.put(theirs, ("settings", "0x2000:02"), {"type": "USINT", "value": 9})
        devicedata.put(theirs, ("commands", "0x2002"), "another command")
        folder = "devices/%08x-%08x" % ident
        tgz = os.path.join(self.tmp.name, "r.tar.gz")
        with tarfile.open(tgz, "w:gz") as tar:
            for name, text in (("index.json", json.dumps({"format": 1, "files": [
                    {"path": folder + "/5.yaml", "discussion": 5, "url": "https://example/5",
                     "author": "bob", "upvotes": 2, "reports": [{"result": "worked", "words": "fine"}]}]})),
                    (folder + "/5.yaml", yaml.safe_dump(theirs))):
                b = text.encode()
                info = tarfile.TarInfo("r-main/" + name)
                info.size = len(b)
                tar.addfile(info, __import__("io").BytesIO(b))
        with mock.patch.dict(os.environ, {"ECATSETUP_DEVICES_URL": "file://" + tgz}):
            dlg.fetch()
        self.assertEqual(dlg.file_box.count(), 1)
        self.assertEqual(dlg.file_box.itemText(0), "#5 · MetalMusings EaserCAT 7000 · 0x2001 is a command, not "
                         "a setting, 0x2002 is a command, not a setting and 1 more · bob · 2 up, 1 worked")
        self.assertIn("2 facts differ", dlg.file_info.text())
        # the whole shared file, grouped the same way, marked against what is in use
        dlg.whole_button.setChecked(True)
        text = dlg.page.toPlainText()
        self.assertIn("Shared file #5 by bob", text)
        # a setting is experience: another machine's value is context, not a conflict
        self.assertIn("in use: {type: USINT, value: 5", text)
        self.assertIn("From another machine: both can be right", text)
        self.assertIn("(experience from a machine)", text)
        self.assertNotIn("Pin for", text)                            # pins are never shared
        dlg.whole_button.setChecked(False)
        self.assertIn("1 of 2: 0x2002 is a command", dlg.step.text())
        self.assertIn("(a property of the device)", dlg.step.text())
        self.assertTrue(dlg.try_button.isHidden())
        dlg.use()
        self.assertEqual(self.quirks.of(ident)["commands"][0x2002], "another command")
        self.assertEqual(devicedata.sources(ident)["commands 0x2002"]["ref"], "https://example/5")
        self.assertIn("2 of 2: Setting 0x2000:02", dlg.step.text())
        self.assertIn("value: 5", dlg.step.text())               # in use
        self.assertIn("value: 9", dlg.step.text())               # shared
        self.assertFalse(dlg.try_button.isHidden())               # a setting can be tried
        dlg.skip_button.click()                                   # Skip leaves it as it was
        self.assertEqual(self.quirks.settings(ident)[0]["value"], 5)
        self.assertIn("last of them", dlg.step.text())
        # Try it: into the drive's RAM, its own value kept; Undo puts it back
        drive = {(0x2000, 2): 5, (0x6041, 0): 0}
        writes = []

        def download(pos, i, sub, v, t):
            writes.append((i, sub, v))
            drive[(i, sub)] = int(v)
        with mock.patch.object(self.dv.tool, "upload", lambda pos, i, sub, t: drive.get((i, sub))), \
                mock.patch.object(self.dv.tool, "download", download):
            dlg.go(1)
            dlg.try_button.click()
            self.assertEqual(drive[(0x2000, 2)], 9)
            self.assertTrue(dlg.undo_button.isEnabled())
            dlg.undo_button.click()
            self.assertEqual(drive[(0x2000, 2)], 5)
            self.assertFalse(dlg.undo_button.isEnabled())
            drive[(0x6041, 0)] = 0x27                       # operation enabled: refused
            with mock.patch.object(self.dv.QMessageBox, "warning") as warn:
                dlg.try_button.click()
            self.assertIn("enabled", warn.call_args[0][2])
            self.assertEqual(drive[(0x2000, 2)], 5)
        self.assertEqual(writes, [(0x2000, 2, "9"), (0x2000, 2, "5")])
        # Report: copied, the thread opened
        rep = mock.Mock()
        rep.exec_.return_value = QDialog.Accepted
        rep.text.return_value = devicedata.report(folder + "/5.yaml", "partly", dlg.used, words="x")
        text = dlg.report(rep)
        self.assertEqual(QApplication.clipboard().text(), text)
        self.assertEqual(self.opened[-1], "https://example/5")
        self.assertEqual(devicedata.read_report(text)["used"], ["commands 0x2002"])
        # Forget takes one of yours away again
        dlg.link(QUrl("forget:commands 0x2002"))
        self.assertNotIn(0x2002, self.quirks.of(ident)["commands"])
        self.assertNotIn("0x2002", dlg.page.toPlainText())

    def test_a_file_from_elsewhere_is_gone_through_like_a_shared_one(self):
        import yaml
        from ecatsetup import devicedata
        w = self.window()
        ident = self.quirks.identity(w.session.slave(0))
        dlg = self.dv.DeviceDataDialog(w, 0)
        self.addCleanup(dlg.deleteLater)
        raw = {"format": 1, "name": "x", "match": {"vendor": "0x%08X" % ident[0], "product": "0x%08X" % ident[1]},
               "commands": {"0x2003": "from the forum"}}
        path = os.path.join(self.tmp.name, "forum.yaml")
        with open(path, "w") as f:
            yaml.safe_dump(raw, f)
        e = dlg.open_file(path)
        self.assertEqual(e["problems"], [])
        self.assertIn("From forum.yaml", dlg.file_box.currentText())
        self.assertIn("1 of 1: 0x2003 is a command", dlg.step.text())
        self.assertTrue(dlg.report_button.isHidden())          # no thread to report in
        dlg.use()
        self.assertEqual(devicedata.sources(ident)["commands 0x2003"]["ref"], "forum.yaml")
        # pasted from a post, for another device: refused, nothing offered
        post = "Mine:\n```yaml\n%s```" % yaml.safe_dump(dict(raw, match={"vendor": "0x1", "product": "0x2"}))
        e = dlg.paste(post)
        self.assertIn("another device", e["problems"][0])
        self.assertIn("REFUSED", dlg.file_box.currentText())
        self.assertEqual(dlg.step.text(), "")
        with mock.patch.object(self.dv.QMessageBox, "warning") as warn:
            self.assertIsNone(dlg.paste("not a quirk file"))
        self.assertIn("Not read", warn.call_args[0][2])

    def test_share_with_nothing_of_your_own_says_how(self):
        w = self.window()
        dlg = self.dv.DeviceDataDialog(w, 0)
        self.addCleanup(dlg.deleteLater)
        self.assertTrue(dlg.share_button.isEnabled())
        with mock.patch.object(self.dv.QMessageBox, "information") as info:
            self.assertIsNone(dlg.share(confirm=False))
        self.assertIn("Remember for the device type", info.call_args[0][2])
        self.assertEqual(self.opened, [])

    def test_what_is_not_known_yet(self):
        from ecatsetup import devicedata, settings
        w = self.window()
        s = w.session.slave(0)
        s["profile"] = "cia402"
        dlg = self.dv.DeviceDataDialog(w, 0)
        self.addCleanup(dlg.deleteLater)
        self.assertIn("Which input is which is not known", dlg.page.toPlainText())
        self.assertIn("card:switches", dlg.page.toHtml())
        dlg.unknown_box.setChecked(False)
        self.assertNotIn("not known", dlg.page.toPlainText())
        self.assertIs(settings.load()[self.dv.UNKNOWNS_SETTING], False)
        ident = self.quirks.identity(s)
        devicedata.remember(ident, "x", ("homing", "input-bits", "IN1"), 16, {"kind": "tried"})
        dlg.unknown_box.setChecked(True)
        self.assertNotIn("Which input is which", dlg.page.toPlainText())
        self.assertIn("No commands known", dlg.page.toPlainText())

    def test_a_switch_test_offers_to_name_the_input(self):
        w = self.window()
        card = w.test_page.switches
        slave = {"name": "x", "position": 0, "profile": "cia402",
                 "identity": {"vendor": "0x00000099", "product": "0x00000001"},
                 "cia402": {"mode": "csp"},
                 "process-data": {"inputs": [{"pdo": "0x1A00", "entries": [
                     {"object": "0x60FD:00", "bits": 32, "type": "UDINT", "pin": "digital-inputs"}]}]}}
        card.show_drive(slave)
        card.start("home")
        card.step_test(0x0, 100.0)
        card.step_test(0x0, 101.1)
        card.step_test(0x100000, 101.2)
        card.step_test(0x100000, 101.6)
        self.assertFalse(card.name_button.isHidden())
        self.assertIn("bit 20", card.name_button.text())
        with mock.patch.object(w.session, "slave", lambda pos: slave):
            card.name_input("DI3")
        self.assertEqual(self.quirks.of((0x99, 1))["homing"]["input-bits"], {"DI3": 20})
        card.start("home")
        card.step_test(0x0, 200.0)
        card.step_test(0x0, 201.1)
        card.step_test(0x0, 216.2)
        self.assertTrue(card.name_button.isHidden())
