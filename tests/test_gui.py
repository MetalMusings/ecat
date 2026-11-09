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

    def test_rename_slave(self):
        w = self.window()
        w.select_slave(1)
        w.slave_page.name.setText("thc")
        w.slave_page.general_edited()
        self.assertEqual(w.session.slave(1)["name"], "thc")
        self.assertIn("thc · Modular device", self.rows(w)[2][1])
        self.assertIs(w.bus_page.stack.currentWidget(), w.slave_page)
        self.assertTrue(any(r[2] == "thc.output4" for r in gui.config_pins(w.session.cfg)))

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
