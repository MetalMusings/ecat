"""Tests of the session behind EtherCAT Setup's window, and of the window
itself (offscreen, skipped without PyQt5), against a fake bus.

Run with: python3 -m unittest discover -s tests
"""

import copy
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# quirk files: only those that come with the program
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
# state (test lock, drive limits): never the user's own
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = \
        __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
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

    def rows(self, w):
        """(key, texts) of the bus list's entries."""
        lst = w.bus_page.list.list
        out = []
        for i in range(lst.count()):
            it = lst.item(i)
            texts = [lab.text() for lab in lst.itemWidget(it).findChildren(gui.QLabel)]
            out.append((it.data(gui.Qt.UserRole), texts))
        return out

    def test_start_page(self):
        from ecatsetup import tasks
        w = self.window()
        pages = {"bus": w.bus_page, "status": w.trouble_page, "test": w.test_page,
                 "hal": w.hal_page}
        known = set(pages) | {"scan", "open", "update", "esi", "watch"}
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
        self.assertIn("io-1", w.hal_page.yaml.toPlainText())
        self.assertIn("Bus read · 2 slaves", w.header.status.text())

    def test_edit_a_pin_and_see_an_error(self):
        w = self.window()
        w.select_slave(0)
        page = w.slave_page
        row = next(r for r in range(page.pd.rowCount())
                   if page.pd.item(r, page.PIN).flags() & gui.Qt.ItemIsEditable
                   and page.pd.item(r, page.PIN).text())
        page.pd.item(row, page.PIN).setText("bad pin")
        self.assertEqual(page._entry(row)["pin"], "bad pin")
        self.assertIn("bad pin", w.hal_page.yaml.toPlainText())
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
        self.assertEqual(page.pd.item(row, 5).text(), "split")
        page.pd.cellWidget(row, page.HALTYPE).setCurrentText("u32")
        self.assertEqual(e["pin"], base)
        self.assertEqual(e["hal-type"], "u32")
        self.assertNotIn("split", e)

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
        # a single bit pin: yes, and only with the type bit
        row = next(r for r in range(page.pd.rowCount()) if "pin" in page._entry(r)
                   and not page._entry(r).get("use"))
        e = page._entry(row)
        page.pd.cellWidget(row, page.HALTYPE).setCurrentText("u32")
        page.pd.item(row, page.INVERT).setText("yes")
        self.assertNotIn("invert", e)
        page.pd.cellWidget(row, page.HALTYPE).setCurrentText("bit")
        page.pd.item(row, page.INVERT).setText("yes")
        self.assertIs(e["invert"], True)
        page.pd.cellWidget(row, page.HALTYPE).setCurrentText("u32")
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
        self.assertIn("zero-at-start: true", w.hal_page.yaml.toPlainText())

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
        self.assertEqual([r[0] for r in rows], [True] * 5, rows)
        self.assertIn("SYNC0 500 us after the start of the cycle, turn-around 2", rows[3][1])
        pins.update({"dc-sync-failed": "TRUE", "turnaround-cycles": "3", "dc-phased": "FALSE"})
        rows = gui.sync_rows(cfg, pins.get, [0, 0, 0, 0, 400000], 1000000)
        self.assertEqual([r[0] for r in rows], [True, False, False, False, False], rows)
        pins["turnaround-cycles"] = "0"
        self.assertIsNone(gui.sync_rows(cfg, pins.get, [], 1000000)[3][0])
        rows = gui.sync_rows({"slaves": [{"name": "io"}]}, pins.get, [], 1000000)
        self.assertEqual([r[0] for r in rows], [True, None])


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
