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

from ecatsetup import config, guidance, session, tool  # noqa: E402

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
                for name in ("slave_positions", "slave_info", "sii", "sdos", "upload", "master_mac")] + [
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
    from PyQt5.QtCore import QEvent
    from PyQt5.QtWidgets import QApplication
    from ecatsetup import gui
except ImportError:  # pragma: no cover
    gui = None


@unittest.skipIf(gui is None, "PyQt5 is not installed")
class WindowBase(BusTest):
    """A window against the fake bus; the tests are in the classes that use it."""

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
                 "hal": w.hal_page, "timing": w.timing_page}
        known = set(pages) | {"scan", "open", "update", "esi", "watch", "ini"}
        self.assertEqual({t for _, _, steps in tasks.TASKS for _, t, _ in steps} - known, set())
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
            w._failed("Test mode: The kernel EtherCAT master is loaded.\nStop it first")
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
        self.assertFalse(page.home_method.model().item(i).isEnabled())   # the ECT60 has no 1
        page.home_method.setCurrentIndex(page.home_method.findData(17))
        page.homing_edited()
        self.assertEqual(s["homing"]["method"], 17)
        self.assertIn("0x6098:00", [x["object"] for x in s["settings"]])
        page.home_fields["fast"].setValue(4000)
        self.assertEqual(s["homing"]["fast"], 4000)
        page.home_method.setCurrentIndex(page.home_method.findData(36))
        page.homing_edited()
        self.assertIn("0x2030:01", [x["object"] for x in s["settings"]])
        page.home_method.setCurrentIndex(0)
        page.homing_edited()
        self.assertNotIn("homing", s)
        self.assertNotIn("0x6098:00", [x["object"] for x in s.get("settings") or []])

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

    def test_commands_are_not_offered_as_settings(self):
        w = self.window()
        w.select_slave(0)
        s = w.session.slave(0)
        with mock.patch.object(guidance, "command_reason",
                               lambda slave, index, entry=None: ("error", "a command")
                               if index == 0x2000 else None):
            dlg = gui.SettingPicker(w.slave_page, s, w.session.dictionary(0), set())
            row = [dlg.table.item(r, 0).text() for r in range(dlg.table.rowCount())].index("0x2000:02")
            self.assertFalse(dlg.table.item(row, 0).flags() & gui.Qt.ItemIsSelectable)
            n = len(s.get("settings") or [])
            w.slave_page.add_entries(s, [w.session.dictionary(0).entries[(0x2000, 2)]])
            self.assertEqual(len(s.get("settings") or []), n)       # refused

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
        # the advice follows the isolated CPUs
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "devices", "system", "cpu"))
            self.assertIn("No CPU is isolated", m.cpu_text(d))
            with open(os.path.join(d, "devices", "system", "cpu", "isolated"), "w") as f:
                f.write("3\n")
            self.assertIn("CPU 3 isolated", m.cpu_text(d))

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
        tp = w.test_page
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
        tp = w.test_page
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
                 "free": [], "rtapi": False, "linuxcnc": [], "kernel_master": True}
        with mock.patch.object(events, "PATH", path), \
                mock.patch.object(report, "master_facts", lambda: facts), \
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

    def test_test_page_without_a_test(self):
        w = self.window()
        w.select_slave(0)
        w.tabs.setCurrentWidget(w.test_page)
        self.assertFalse(w.test_page.start_card.isHidden())
        self.assertTrue(w.test_page.io.isHidden())
        self.assertTrue(w.header.stop.isHidden())


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
        self.assertIn("zero-at-start", symptoms.html())
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
        for page in (w.start_page, w.bus_page, w.trouble_page, w.test_page, w.hal_page):
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
