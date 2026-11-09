"""EtherCAT Setup's window (PyQt5): read the bus, show the slaves, their
process data and pins, edit, check and save ethercat.yaml, and diagnose
the slaves. Nothing is written to the slaves and nothing moves.

    ecatsetup --gui [ethercat.yaml] [--ini machine.ini] [--esi DIR]

The work is done by session.Session and the modules the command line
uses; this file only shows it. Reading the bus runs in a thread, so the
window stays responsive.
"""

import copy
import os
import re
import sys

from PyQt5.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QFontDatabase, QKeySequence
from PyQt5.QtWidgets import (
    QAbstractItemView, QAction, QApplication, QCheckBox, QComboBox, QDockWidget, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget)

from . import config, diagnose, propose, testmode, tool
from .session import Session

TITLE = "EtherCAT Setup"
RED, AMBER, GREEN, GREY = QColor("#c62828"), QColor("#b26a00"), QColor("#2e7d32"), QColor("#777777")
_SLAVE_REF = re.compile(r"^slave (\d+)\b")
MASTER_ITEM = -1


def mono():
    return QFontDatabase.systemFont(QFontDatabase.FixedFont)


def slave_of(message):
    """The slave position a message is about, or None."""
    m = _SLAVE_REF.match(message)
    return int(m.group(1)) if m else None


def parse_int(text):
    """A number as typed: decimal or 0x hex. Raises ValueError."""
    return int(text.strip(), 0)


def diagnosis_findings(block):
    """The lines of one slave's diagnosis that say something is wrong."""
    out = []
    for line in block[1:]:
        t = line.strip()
        if t.startswith("->") and out:
            out[-1] += " " + t
        elif ("not OP" in t or ", error 0x" in t or t.startswith(("Error code", "Error register"))
              or (t.startswith("CiA 402:") and "Operation enabled" not in t)):
            out.append(t)
    return out


def interfaces():
    try:
        return sorted(n for n in os.listdir("/sys/class/net") if n != "lo")
    except OSError:
        return []


class Worker(QThread):
    """Runs fn(progress) in a thread."""
    progress = pyqtSignal(str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(self.progress.emit))
        except tool.ToolError as e:
            self.failed.emit("Cannot reach a master: %s\nStart one with: sudo ethercat-uspace start" % e)
        except testmode.TestError as e:
            self.failed.emit("Test mode: %s" % e)
        except Exception as e:  # shown to the user rather than lost in a thread
            self.failed.emit("%s: %s" % (type(e).__name__, e))


class MasterPage(QWidget):
    """The master's settings: interface, transport, distributed clocks."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.loading = False
        lay = QVBoxLayout(self)
        title = QLabel("Master")
        title.setFont(QFont(title.font().family(), 14, QFont.Bold))
        lay.addWidget(title)

        form = QFormLayout()
        self.interface = QComboBox()
        self.interface.setEditable(True)
        self.interface.addItems(interfaces())
        self.interface.editTextChanged.connect(self.edited)
        form.addRow("Network interface", self.interface)
        self.transport = QComboBox()
        self.transport.addItems(sorted(config.TRANSPORTS))
        self.transport.currentTextChanged.connect(self.edited)
        form.addRow("Transport", self.transport)
        ini = QHBoxLayout()
        self.ini_label = QLabel()
        ini.addWidget(self.ini_label, 1)
        choose = QPushButton("INI file...")
        choose.clicked.connect(win.choose_ini)
        ini.addWidget(choose)
        form.addRow("Servo period", ini)
        lay.addLayout(form)

        self.dc = QGroupBox("Distributed clocks")
        self.dc.setCheckable(True)
        self.dc.toggled.connect(self.edited)
        dform = QFormLayout(self.dc)
        self.ref_cycles = QSpinBox()
        self.ref_cycles.setRange(0, 1000000)
        self.ref_cycles.valueChanged.connect(self.edited)
        dform.addRow("Reference clock sync every (cycles)", self.ref_cycles)
        self.follows = QCheckBox("The servo thread follows the reference clock")
        self.follows.toggled.connect(self.edited)
        dform.addRow(self.follows)
        shift = QHBoxLayout()
        self.shift_auto = QCheckBox("auto")
        self.shift_auto.setToolTip("SYNC0 where the turn-around from output to status is the same "
                                   "in every cycle (two cycles)")
        self.shift_auto.toggled.connect(self.edited)
        shift.addWidget(self.shift_auto)
        self.shift = QSpinBox()
        self.shift.setRange(-10000000, 10000000)
        self.shift.setSuffix(" ns")
        self.shift.valueChanged.connect(self.edited)
        shift.addWidget(self.shift, 1)
        dform.addRow("SYNC0 shift", shift)
        lay.addWidget(self.dc)
        lay.addStretch(1)

    def show_config(self):
        self.loading = True
        s = self.win.session
        m = (s.cfg or {}).get("master") or {}
        self.interface.setEditText(m.get("interface") or "")
        self.transport.setCurrentText(m.get("transport", "raw"))
        self.ini_label.setText(
            "%d ns, from %s" % (s.period, s.ini_path) if s.period else
            "unknown: choose the machine's INI file" if not s.ini_path else
            "not found in %s" % s.ini_path)
        dc = m.get("dc")
        self.dc.setChecked(dc is not None)
        dc = dc or {}
        self.ref_cycles.setValue(int(dc.get("ref-clock-sync-cycles") or 0))
        self.follows.setChecked(bool(dc.get("servo-thread-follows-ref-clock")))
        shift = dc.get("sync0-shift-ns")
        self.shift_auto.setChecked(shift == "auto")
        self.shift.setEnabled(shift != "auto")
        self.shift.setValue(shift if isinstance(shift, int) else 0)
        self.setEnabled(s.cfg is not None)
        self.loading = False

    def edited(self, *_):
        if self.loading or self.win.session.cfg is None:
            return
        m = self.win.session.cfg.setdefault("master", {})
        iface = self.interface.currentText().strip()
        if iface:
            m["interface"] = iface
        else:
            m.pop("interface", None)
        m["transport"] = self.transport.currentText()
        if self.dc.isChecked():
            dc = m.setdefault("dc", {})
            if self.ref_cycles.value():
                dc["ref-clock-sync-cycles"] = self.ref_cycles.value()
            else:
                dc.pop("ref-clock-sync-cycles", None)
            dc["servo-thread-follows-ref-clock"] = self.follows.isChecked()
            self.shift.setEnabled(not self.shift_auto.isChecked())
            dc["sync0-shift-ns"] = "auto" if self.shift_auto.isChecked() else self.shift.value()
        else:
            m.pop("dc", None)
        self.win.changed()


class SlavePage(QWidget):
    """One slave: general, process data, settings, diagnosis."""

    PD_COLUMNS = ["Dir", "PDO", "Object", "Name", "Bits", "Type", "Use", "Pin", "HAL type",
                  "Scale", "Offset", "Bit pins"]
    PIN, HALTYPE, SCALE, OFFSET, SPLIT = 7, 8, 9, 10, 11
    SET_COLUMNS = ["Object", "Name", "Type", "Value"]
    TEST_COLUMNS = ["Pin", "Type", "Direction", "Value", "Set"]
    TEST_TAB = 4

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.position = None
        self.loading = False
        lay = QVBoxLayout(self)
        self.title = QLabel()
        self.title.setFont(QFont(self.title.font().family(), 14, QFont.Bold))
        lay.addWidget(self.title)
        self.subtitle = QLabel()
        self.subtitle.setWordWrap(True)
        lay.addWidget(self.subtitle)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.tabs.addTab(self._general(), "General")
        self.tabs.addTab(self._process_data(), "Process data")
        self.tabs.addTab(self._settings(), "Settings")
        self.tabs.addTab(self._diagnosis(), "Diagnosis")
        self.tabs.addTab(self._test(), "Test")
        self.tabs.currentChanged.connect(lambda i: self.show_test() if i == self.TEST_TAB else None)

    # -- building

    def _general(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setToolTip("The start of the slave's HAL pin names: ecat.<name>.<pin>")
        self.name.textEdited.connect(self.general_edited)
        form.addRow("Name", self.name)
        self.identity = QLabel()
        self.identity.setFont(mono())
        self.identity.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Identity", self.identity)
        self.profile = QLabel()
        form.addRow("Profile", self.profile)
        self.esi = QLabel()
        self.esi.setWordWrap(True)
        form.addRow("ESI file", self.esi)
        lay.addLayout(form)

        self.cia402 = QGroupBox("CiA 402 drive")
        cform = QFormLayout(self.cia402)
        self.mode = QComboBox()
        for m, text in (("csp", "CSP: cyclic synchronous position (a joint)"),
                        ("csv", "CSV: cyclic synchronous velocity (a joint with a pid)"),
                        ("pv", "PV: profile velocity (a spindle or conveyor)"),
                        ("pp", "PP: profile position (positioning that is not a joint)")):
            self.mode.addItem(text, m)
        self.mode.activated.connect(self.general_edited)
        cform.addRow("Mode", self.mode)
        modes = QHBoxLayout()
        self.other_modes = {}
        for m in ("hm", "csp", "csv", "pv", "pp"):
            cb = QCheckBox(m.upper())
            cb.toggled.connect(self.general_edited)
            self.other_modes[m] = cb
            modes.addWidget(cb)
        modes.addStretch(1)
        cform.addRow("Other modes", modes)
        self.drive_modes = QLabel()
        cform.addRow("The drive supports", self.drive_modes)
        lay.addWidget(self.cia402)

        self.pdo_box = QGroupBox("Process data")
        pform = QFormLayout(self.pdo_box)
        self.configure_pdos = QCheckBox("Write the PDO mapping to the slave (configure-pdos)")
        self.configure_pdos.toggled.connect(self.general_edited)
        pform.addRow(self.configure_pdos)
        again = QHBoxLayout()
        self.pdo_set = QComboBox()
        self.pdo_set.setToolTip("The ESI file's predefined PDO assignments")
        again.addWidget(self.pdo_set, 1)
        self.again = QPushButton("Propose again from the bus")
        self.again.setToolTip("Read the slave and replace its process data, DC and settings with "
                              "a new proposal for the chosen mode and PDO assignment. The name is "
                              "kept.")
        self.again.clicked.connect(self.propose_again)
        again.addWidget(self.again)
        pform.addRow("PDO assignment", again)
        lay.addWidget(self.pdo_box)

        self.dc = QGroupBox("Distributed clocks")
        self.dc.setCheckable(True)
        self.dc.toggled.connect(self.general_edited)
        dform = QFormLayout(self.dc)
        self.assign = QLineEdit()
        self.assign.setToolTip("AssignActivate, as in the ESI file, such as 0x0300")
        self.assign.textEdited.connect(self.general_edited)
        dform.addRow("Assign activate", self.assign)
        self.sync0_cycles = QSpinBox()
        self.sync0_cycles.setRange(1, 1000)
        self.sync0_cycles.setSuffix(" x servo period")
        self.sync0_cycles.valueChanged.connect(self.general_edited)
        dform.addRow("SYNC0 cycle", self.sync0_cycles)
        self.sync0_shift = QSpinBox()
        self.sync0_shift.setRange(-10000000, 10000000)
        self.sync0_shift.setSuffix(" ns")
        self.sync0_shift.valueChanged.connect(self.general_edited)
        dform.addRow("SYNC0 shift", self.sync0_shift)
        lay.addWidget(self.dc)

        self.notes = QLabel()
        self.notes.setWordWrap(True)
        self.notes.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.notes)
        lay.addStretch(1)
        return w

    def _process_data(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        hint = QLabel("Pins are ecat.<name>.<pin>. Entries the profile handles have no pins of "
                      "their own. Clear a pin to map the entry without a pin.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.pd = QTableWidget(0, len(self.PD_COLUMNS))
        self.pd.setHorizontalHeaderLabels(self.PD_COLUMNS)
        self.pd.verticalHeader().hide()
        self.pd.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.pd.horizontalHeader().setSectionResizeMode(self.PIN, QHeaderView.Stretch)
        self.pd.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pd.itemChanged.connect(self.pd_edited)
        lay.addWidget(self.pd)
        return w

    def _settings(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        hint = QLabel("SDOs written to the slave at every start, in PREOP. Type an object such as "
                      "0x2000:01; its name and type come from the slave's dictionary when the bus "
                      "has been read.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.st = QTableWidget(0, len(self.SET_COLUMNS))
        self.st.setHorizontalHeaderLabels(self.SET_COLUMNS)
        self.st.verticalHeader().hide()
        self.st.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.st.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.st.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.st.itemChanged.connect(self.st_edited)
        lay.addWidget(self.st)
        buttons = QHBoxLayout()
        add = QPushButton("Add setting")
        add.clicked.connect(self.add_setting)
        remove = QPushButton("Remove setting")
        remove.clicked.connect(self.remove_setting)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        return w

    def _diagnosis(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.diag_state = QLabel()
        top.addWidget(self.diag_state, 1)
        run = QPushButton("Diagnose this slave")
        run.clicked.connect(lambda: self.win.diagnose([self.position]))
        top.addWidget(run)
        lay.addLayout(top)
        self.diag = QPlainTextEdit()
        self.diag.setReadOnly(True)
        self.diag.setFont(mono())
        self.diag.setPlaceholderText("Not diagnosed yet. The diagnosis only reads: the slave's "
                                     "state, error counters per port (watched for 2 s), its "
                                     "diagnosis history and, for a drive, the CiA 402 state.")
        lay.addWidget(self.diag)
        return w

    def _test(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.test_state = QLabel()
        self.test_state.setWordWrap(True)
        lay.addWidget(self.test_state)
        self.tp = QTableWidget(0, len(self.TEST_COLUMNS))
        self.tp.setHorizontalHeaderLabels(self.TEST_COLUMNS)
        self.tp.verticalHeader().hide()
        self.tp.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tp.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tp.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self.tp, 1)
        self.tp_rows = {}

        box = QGroupBox("SDO (CoE object)")
        form = QHBoxLayout(box)
        self.sdo_obj = QLineEdit()
        self.sdo_obj.setPlaceholderText("0x2000:01")
        self.sdo_obj.setMaximumWidth(110)
        self.sdo_obj.editingFinished.connect(self.sdo_object_typed)
        form.addWidget(self.sdo_obj)
        self.sdo_type = QComboBox()
        self.sdo_type.addItem("type from the slave", None)
        for t in sorted(testmode.TOOL_TYPES.values()) + ["string"]:
            self.sdo_type.addItem(t, t)
        form.addWidget(self.sdo_type)
        read = QPushButton("Read")
        read.clicked.connect(self.sdo_read)
        form.addWidget(read)
        self.sdo_value = QLineEdit()
        self.sdo_value.setPlaceholderText("value")
        form.addWidget(self.sdo_value, 1)
        write = QPushButton("Write...")
        write.clicked.connect(self.sdo_write)
        form.addWidget(write)
        self.sdo_name = QLabel()
        lay.addWidget(box)
        lay.addWidget(self.sdo_name)
        return w

    # -- showing

    def show_slave(self, position):
        self.position = position
        self.loading = True
        try:
            self._show()
        finally:
            self.loading = False

    def _show(self):
        win, pos = self.win, self.position
        s = win.session.slave(pos)
        b = win.session.bus.get(pos)
        p = b.proposal if b else None
        self.title.setText("Slave %d · %s" % (pos, (s or {}).get("device") or (p.name if p else "")))
        where = []
        if b:
            where.append("on the bus, %s" % b.info.get("state", "?"))
        else:
            where.append("not read from the bus")
        where.append("in the file" if s else "not in the file: add it with Update from bus")
        self.subtitle.setText("; ".join(where))
        for i in (0, 1, 2, self.TEST_TAB):
            self.tabs.setTabEnabled(i, s is not None)
        if s is None:
            self.tabs.setCurrentIndex(3)
            self.name.setText("")
            self.show_diagnosis()
            return

        self.name.setText(s.get("name", ""))
        ident = s.get("identity") or {}
        text = "vendor %s  product %s  revision %s" % (ident.get("vendor"), ident.get("product"),
                                                       ident.get("revision", "-"))
        if b:
            i = b.info
            bus_ident = "vendor 0x%08X  product 0x%08X  revision 0x%08X" % (
                i["vendor_id"] or 0, i["product_code"] or 0, i["revision"] or 0)
            if bus_ident != text:
                text += "\non the bus: " + bus_ident
        self.identity.setText(text)
        self.profile.setText({"cia402": "CiA 402 drive", "mdp": "Modular device (ETG.5001)",
                              "generic": "none: configured from its process data"}.get(
                                  s.get("profile"), str(s.get("profile"))))
        self.esi.setText(p.esi_path or "no matching file" if p else "-")

        is402 = s.get("profile") == "cia402"
        self.cia402.setVisible(is402)
        if is402:
            c = s.get("cia402") or {}
            i = self.mode.findData(c.get("mode"))
            self.mode.setCurrentIndex(max(i, 0))
            for m, cb in self.other_modes.items():
                cb.setChecked(m in (c.get("modes") or []))
                cb.setEnabled(m != c.get("mode"))
            self.drive_modes.setText(" ".join(p.drive_modes) if p and p.drive_modes
                                     else "unknown (0x6502 not read)")

        self.configure_pdos.setChecked(bool(s.get("configure-pdos", True)))
        self.pdo_set.clear()
        self.pdo_set.addItem("The ESI file's default, or the slave's own" if p and p.pdo_sets
                             else "The slave's own mapping, or the ESI file's", None)
        for name in (p.pdo_sets if p else []):
            self.pdo_set.addItem(name, name)
        self.again.setEnabled(b is not None and not win.busy)

        dc = s.get("dc")
        self.dc.setChecked(dc is not None)
        dc = dc or {}
        self.assign.setText(str(dc.get("assign-activate", "")))
        self.sync0_cycles.setValue(int(dc.get("sync0-cycles") or 1))
        self.sync0_shift.setValue(int(dc.get("sync0-shift-ns") or 0))
        notes = list(p.notes) if p else []
        self.notes.setText("\n".join("Note: %s" % n for n in notes))

        self.show_process_data(s)
        self.show_settings(s)
        self.show_diagnosis()
        self.show_test()

    def _item(self, text, editable=False, tip=None):
        it = QTableWidgetItem("" if text is None else str(text))
        if not editable:
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            if text in ("profile", "gap"):
                it.setForeground(QBrush(GREY))
        if tip:
            it.setToolTip(tip)
        return it

    def show_process_data(self, s):
        self.pd.setRowCount(0)
        self.pd_rows = []
        pd = s.get("process-data") or {}
        for direction in ("outputs", "inputs"):
            for pi, pdo in enumerate(pd.get(direction) or []):
                for ei, e in enumerate(pdo.get("entries") or []):
                    self._pd_row(direction, pdo, pi, e, ei)
        self.pd.resizeColumnsToContents()
        self.pd.horizontalHeader().setSectionResizeMode(self.PIN, QHeaderView.Stretch)

    def _pd_row(self, direction, pdo, pi, e, ei):
        r = self.pd.rowCount()
        self.pd.insertRow(r)
        self.pd_rows.append((direction, pi, ei))
        try:
            index, sub = config.parse_obj(e.get("object"))
            name = self.win.session.object_name(self.position, index, sub)
        except ValueError:
            name = ""
        use = e.get("use") or ("split" if "split" in e else "pin" if "pin" in e else "-")
        fixed = use in ("profile", "gap")
        cells = ["out" if direction == "outputs" else "in", pdo.get("pdo"), e.get("object"), name,
                 e.get("bits"), e.get("type"), use]
        for c, v in enumerate(cells):
            self.pd.setItem(r, c, self._item(v))
        self.pd.setItem(r, self.PIN, self._item(e.get("pin", ""), editable=not fixed and "split" not in e))
        self.pd.setItem(r, self.SCALE, self._item(e.get("scale", ""), editable=not fixed))
        self.pd.setItem(r, self.OFFSET, self._item(e.get("offset", ""), editable=not fixed))
        self.pd.setItem(r, self.SPLIT, self._item(", ".join(p or "" for p in e.get("split", [])),
                                                  editable=not fixed and "pin" not in e,
                                                  tip="One pin per bit, from bit 0, comma separated; "
                                                      "leave a bit empty to give it no pin"))
        if fixed:
            self.pd.setItem(r, self.HALTYPE, self._item(""))
            return
        box = QComboBox()
        box.addItems([""] + sorted(config.HAL_TYPES))
        box.setCurrentText(e.get("hal-type", ""))
        box.setEnabled("pin" in e)
        box.currentTextChanged.connect(lambda t, row=r: self.hal_type_edited(row, t))
        self.pd.setCellWidget(r, self.HALTYPE, box)

    def _entry(self, row):
        direction, pi, ei = self.pd_rows[row]
        s = self.win.session.slave(self.position)
        return s["process-data"][direction][pi]["entries"][ei]

    def show_settings(self, s):
        self.loading = True
        self.st.setRowCount(0)
        for st in s.get("settings") or []:
            r = self.st.rowCount()
            self.st.insertRow(r)
            value = st.get("value", "")
            if "value" not in st and "data" in st:
                value = "data %s" % st["data"]
            for c, v in enumerate((st.get("object"), st.get("name", ""), st.get("type", ""), value)):
                self.st.setItem(r, c, self._item(v, editable=True))
        self.st.resizeColumnsToContents()
        self.loading = False

    def _prefix(self):
        s = self.win.session.slave(self.position)
        return "%s." % s["name"] if s and s.get("name") else None

    def show_test(self):
        """Build the pin table of the running test for this slave."""
        self.tp.setRowCount(0)
        self.tp_rows = {}
        run = self.win.test
        prefix = self._prefix()
        if run is None or not run.running:
            self.test_state.setText(
                "The I/O test is off. Start it with Test > Start test: the ecat component runs "
                "this configuration in halrun, so inputs show here live and outputs can be set. "
                "Drives stay disabled. SDOs can be read and written whenever a master runs.")
            return
        if prefix is None:
            return
        try:
            pins = run.pins(prefix)
        except testmode.TestError as e:
            self.test_state.setText(str(e))
            return
        drive = prefix.startswith(run.read_only)
        self.test_state.setText(
            "Test running. " + ("This is a CiA 402 drive: its pins are shown, not set; drives are "
                                "tested with motion in the CiA 402 test mode." if drive else
                                "Pins set here go back to 0 when the test stops. Mind what is "
                                "wired to the outputs."))
        for name in sorted(pins):
            pin = pins[name]
            r = self.tp.rowCount()
            self.tp.insertRow(r)
            self.tp_rows[name] = r
            # HAL's direction is the component's: its IN pins are the slave's outputs
            direction = {"IN": "to the slave", "OUT": "from the slave"}.get(pin.dir, pin.dir)
            for c, v in enumerate((name[len(prefix):], pin.type, direction, pin.value)):
                self.tp.setItem(r, c, self._item(v))
            if run.writable(pin):
                self.tp.setCellWidget(r, 4, self._setter(pin))

    def _setter(self, pin):
        if pin.type == "bit":
            w = QCheckBox()
            w.setChecked(pin.value == "TRUE")
            w.toggled.connect(lambda on, p=pin: self.win.set_pin(p, on))
            return w
        w = QLineEdit(pin.value)
        w.setToolTip("Type a value and press Enter")
        w.returnPressed.connect(lambda p=pin, e=w: self.win.set_pin(p, e.text()))
        return w

    def update_test(self):
        """Refresh the values of the pin table (timer)."""
        run, prefix = self.win.test, self._prefix()
        if run is None or prefix is None:
            return
        try:
            pins = run.pins(prefix)
        except testmode.TestError:
            return
        if set(pins) != set(self.tp_rows):
            self.show_test()
            return
        for name, r in self.tp_rows.items():
            it = self.tp.item(r, 3)
            if it.text() != pins[name].value:
                it.setText(pins[name].value)

    def _sdo_target(self):
        try:
            index, sub = config.parse_obj(self.sdo_obj.text().strip())
        except ValueError:
            self.win.status("Type an object as 0xIIII:SS, such as 0x2000:01")
            return None
        return index, sub

    def sdo_object_typed(self):
        t = self._sdo_target()
        if t:
            b = self.win.session.bus.get(self.position)
            entry = b.coe.entries.get(t) if b and b.coe else None
            self.sdo_name.setText("%s (%s, %s)" % (
                self.win.session.object_name(self.position, *t), entry.data_type, entry.access)
                if entry else "Not in the dictionary read from the slave: choose a type")

    def sdo_read(self):
        t = self._sdo_target()
        if not t:
            return
        try:
            text = tool.upload_text(self.position, t[0], t[1], self.sdo_type.currentData())
        except tool.ToolError as e:
            self.win.status(str(e).splitlines()[-1])
            return
        if self.sdo_type.currentData() != "string" and len(text.split()) == 2:
            text = text.split()[-1]     # "0x0010 16": hex and decimal
        self.sdo_value.setText(text)
        self.win.status("0x%04X:%02X = %s" % (t[0], t[1], text))

    def sdo_write(self):
        t = self._sdo_target()
        value = self.sdo_value.text().strip()
        s = self.win.session.slave(self.position) or {}
        if not t or not value:
            return
        why = testmode.sdo_write_refused(s, *t)
        if why:
            QMessageBox.warning(self, TITLE, "Not written: %s" % why)
            return
        if QMessageBox.question(
                self, TITLE, "Write %s to 0x%04X:%02X of slave %d?\n\nThe slave keeps it until "
                "it restarts (or for good, if it stores it). To have it written at every start, "
                "add it under Settings." % (value, t[0], t[1], self.position),
                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        try:
            tool.download(self.position, t[0], t[1], value, self.sdo_type.currentData())
        except tool.ToolError as e:
            QMessageBox.warning(self, TITLE, "The slave refused:\n%s" % e)
            return
        self.win.status("Wrote 0x%04X:%02X = %s to slave %d" % (t[0], t[1], value, self.position))

    def show_diagnosis(self):
        b = self.win.session.bus.get(self.position)
        self.diag_state.setText("State when last read: %s" % b.info.get("state") if b else
                                "Not read from the bus")
        self.diag.setPlainText(self.win.diagnoses.get(self.position, ""))

    # -- editing

    def general_edited(self, *_):
        if self.loading:
            return
        s = self.win.session.slave(self.position)
        if s is None:
            return
        s["name"] = self.name.text().strip()
        if s.get("profile") == "cia402":
            c = s.setdefault("cia402", {})
            c["mode"] = self.mode.currentData()
            others = [m for m, cb in self.other_modes.items() if cb.isChecked() and m != c["mode"]]
            for m, cb in self.other_modes.items():
                cb.setEnabled(m != c["mode"])
            if others:
                c["modes"] = others
            else:
                c.pop("modes", None)
        s["configure-pdos"] = self.configure_pdos.isChecked()
        if self.dc.isChecked():
            dc = s.setdefault("dc", {})
            dc["assign-activate"] = self.assign.text().strip()
            dc["sync0-cycles"] = self.sync0_cycles.value()
            dc["sync0-shift-ns"] = self.sync0_shift.value()
        else:
            s.pop("dc", None)
        self.win.changed()

    def pd_edited(self, item):
        if self.loading:
            return
        row, col = item.row(), item.column()
        e = self._entry(row)
        text = item.text().strip()
        if col == self.PIN:
            if text:
                e["pin"] = text
                e.setdefault("hal-type", propose.HAL_TYPES.get(e.get("type"), "s32"))
            else:
                e.pop("pin", None)
                e.pop("hal-type", None)
        elif col in (self.SCALE, self.OFFSET):
            key = "scale" if col == self.SCALE else "offset"
            if not text:
                e.pop(key, None)
            else:
                try:
                    e[key] = float(text)
                except ValueError:
                    self.win.status("%s must be a number" % key)
                    return
        elif col == self.SPLIT:
            pins = [p.strip() or None for p in text.split(",")] if text else []
            while pins and pins[-1] is None:
                pins.pop()
            if pins:
                e["split"] = pins
            else:
                e.pop("split", None)
        else:
            return
        self._refresh_row(row, e)
        self.win.changed()

    def _refresh_row(self, row, e):
        self.loading = True
        use = e.get("use") or ("split" if "split" in e else "pin" if "pin" in e else "-")
        self.pd.item(row, 6).setText(use)
        pin_item = self.pd.item(row, self.PIN)
        split_item = self.pd.item(row, self.SPLIT)
        for it, on in ((pin_item, "split" not in e), (split_item, "pin" not in e)):
            it.setFlags(it.flags() | Qt.ItemIsEditable if on else it.flags() & ~Qt.ItemIsEditable)
        box = self.pd.cellWidget(row, self.HALTYPE)
        if box:
            box.blockSignals(True)
            box.setCurrentText(e.get("hal-type", ""))
            box.setEnabled("pin" in e)
            box.blockSignals(False)
        self.loading = False

    def hal_type_edited(self, row, text):
        if self.loading:
            return
        e = self._entry(row)
        if "pin" not in e:
            return
        e["hal-type"] = text
        self.win.changed()

    def st_edited(self, item):
        if self.loading:
            return
        s = self.win.session.slave(self.position)
        st = s["settings"][item.row()]
        text = item.text().strip()
        col = item.column()
        if col == 0:
            st["object"] = text
            try:
                index, sub = config.parse_obj(text)
            except ValueError:
                self.win.changed()
                return
            b = self.win.session.bus.get(self.position)
            entry = b.coe.entries.get((index, sub)) if b and b.coe else None
            if entry:
                st["name"] = self.win.session.object_name(self.position, index, sub)
                st["type"] = propose.COE_TYPES.get(entry.data_type, st.get("type", ""))
                self.show_settings(s)
        elif col == 1:
            if text:
                st["name"] = text
            else:
                st.pop("name", None)
        elif col == 2:
            st["type"] = text.upper()
        elif col == 3:
            if text.startswith("data "):
                st.pop("value", None)
                st["data"] = text[5:].strip()
            else:
                st.pop("data", None)
                try:
                    st["value"] = float(text) if st.get("type") in ("REAL", "LREAL") else parse_int(text)
                except ValueError:
                    st["value"] = text
        self.win.changed()

    def add_setting(self):
        s = self.win.session.slave(self.position)
        if s is None:
            return
        s.setdefault("settings", []).append({"object": "0x0000:00", "type": "UINT", "value": 0})
        self.show_settings(s)
        self.st.setCurrentCell(self.st.rowCount() - 1, 0)
        self.st.editItem(self.st.item(self.st.rowCount() - 1, 0))
        self.win.changed()

    def remove_setting(self):
        s = self.win.session.slave(self.position)
        rows = sorted({i.row() for i in self.st.selectedIndexes()}, reverse=True)
        if s is None or not rows:
            return
        for r in rows:
            del s["settings"][r]
        self.show_settings(s)
        self.win.changed()

    def propose_again(self):
        self.win.propose_again(self.position, self.mode.currentData()
                               if self.cia402.isVisible() else None, self.pdo_set.currentData())


class MainWindow(QMainWindow):
    def __init__(self, session):
        super().__init__()
        self.session = session
        self.diagnoses = {}     # position -> diagnosis text
        self.worker = None
        self.test = None        # testmode.TestRun while the I/O test runs
        self.test_timer = QTimer(self)
        self.test_timer.setInterval(200)
        self.test_timer.timeout.connect(self.poll_test)
        self.messages_by_slave = {}
        self.resize(1300, 850)

        self._actions()
        split = QSplitter()
        self.slaves = QTreeWidget()
        self.slaves.setHeaderLabels(["#", "Name", "Bus", "File", "Device"])
        self.slaves.setRootIsDecorated(False)
        self.slaves.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.slaves.currentItemChanged.connect(self.selected)
        split.addWidget(self.slaves)
        self.pages = QStackedWidget()
        self.empty = QLabel("Read the bus or open a configuration file.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.master_page = MasterPage(self)
        self.slave_page = SlavePage(self)
        for w in (self.empty, self.master_page, self.slave_page):
            self.pages.addWidget(w)
        split.addWidget(self.pages)
        split.setStretchFactor(1, 1)
        split.setSizes([480, 820])
        self.setCentralWidget(split)

        bottom = QTabWidget()
        self.messages = QListWidget()
        self.messages.itemActivated.connect(self.message_activated)
        self.messages.itemClicked.connect(self.message_activated)
        bottom.addTab(self.messages, "Messages")
        self.yaml = QPlainTextEdit()
        self.yaml.setReadOnly(True)
        self.yaml.setFont(mono())
        bottom.addTab(self.yaml, "File contents")
        dock = QDockWidget("Messages and file", self)
        dock.setObjectName("bottom")
        dock.setWidget(bottom)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.resizeDocks([dock], [170], Qt.Vertical)

        self.bus_label = QLabel()
        self.statusBar().addPermanentWidget(self.bus_label)
        self.refresh()

    def _actions(self):
        tb = self.addToolBar("Main")
        tb.setObjectName("main")
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        menu = self.menuBar().addMenu("&File")
        busm = self.menuBar().addMenu("&Bus")

        def act(text, slot, where, key=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if key:
                a.setShortcut(key)
            if tip:
                a.setToolTip(tip)
                a.setStatusTip(tip)
            for w in where:
                w.addAction(a)
            return a

        self.a_open = act("Open...", self.open_file, (menu, tb), QKeySequence.Open,
                          "Open an ethercat.yaml")
        self.a_import = act("Import lcec XML...", self.import_xml, (menu,), None,
                            "Convert an lcec ethercat-conf.xml, with what was read from the bus")
        self.a_save = act("Save", self.save, (menu, tb), QKeySequence.Save,
                          "Write ethercat.yaml; the old file is kept as .bak")
        self.a_save_as = act("Save as...", self.save_as, (menu,), QKeySequence.SaveAs)
        menu.addSeparator()
        act("Quit", self.close, (menu,), QKeySequence.Quit)
        tb.addSeparator()
        self.a_read = act("Read bus", self.read_bus, (busm, tb), QKeySequence.Refresh,
                          "Read the slaves on the bus: SII, CoE dictionary, ESI files. "
                          "Nothing is written to them.")
        self.a_update = act("Update from bus", self.update_from_bus, (busm, tb), None,
                            "Bring the file up to date with the bus: slaves keep their entries, "
                            "new ones get proposals, missing ones are left out")
        self.a_check = act("Check", self.check, (busm, tb), None,
                           "Check the file, and the bus against it")
        self.a_diag = act("Diagnose", lambda: self.diagnose(None), (busm, tb), None,
                          "State, cable errors, the slaves' own messages and CiA 402 state")
        testm = self.menuBar().addMenu("&Test")
        tb.addSeparator()
        self.a_test = act("Start test", self.start_test, (testm, tb), None,
                          "Run this configuration in halrun: read inputs, set outputs. "
                          "Drives stay disabled.")
        self.a_test_stop = act("Stop test", self.stop_test, (testm, tb), None,
                               "Put set outputs back to 0 and stop halrun")

    # -- state shown

    @property
    def busy(self):
        return self.worker is not None

    def status(self, text):
        self.statusBar().showMessage(text, 8000)

    def changed(self):
        """The configuration was edited: show the file, check it, mark the list."""
        self.check(quiet=True)
        self.refresh(keep_page=True)

    def refresh(self, keep_page=False):
        s = self.session
        name = os.path.basename(s.path) if s.path else ("new configuration" if s.cfg else "")
        self.setWindowTitle("%s%s - %s" % (name, " *" if s.dirty else "", TITLE) if name else TITLE)
        self.yaml.setPlainText(s.dumps())
        if s.bus_error:
            self.bus_label.setText("No master running: start one with sudo ethercat-uspace start")
        elif s.bus:
            self.bus_label.setText("Bus: %d slaves read" % len(s.bus))
        else:
            self.bus_label.setText("Bus not read")
        has_cfg = s.cfg is not None
        self.a_save.setEnabled(has_cfg and not self.busy)
        self.a_save_as.setEnabled(has_cfg and not self.busy)
        self.a_check.setEnabled(has_cfg and not self.busy)
        self.a_update.setEnabled(bool(s.bus) and not self.busy)
        for a in (self.a_read, self.a_diag, self.a_import, self.a_open):
            a.setEnabled(not self.busy)
        testing = self.test is not None
        self.a_test.setEnabled(has_cfg and not self.busy and not testing)
        self.a_test_stop.setEnabled(testing and not self.busy)
        if testing:
            self.bus_label.setText("Test running")
        self.fill_list(keep_page)

    def fill_list(self, keep_page):
        s = self.session
        current = self.slaves.currentItem()
        selected = current.data(0, Qt.UserRole) if current else None
        self.slaves.blockSignals(True)
        self.slaves.clear()
        if s.cfg is not None or s.bus:
            m = (s.cfg or {}).get("master") or {}
            it = QTreeWidgetItem(["", "Master", "", self._file_text(None)[0],
                                  " ".join(x for x in (m.get("interface"), m.get("transport")) if x)])
            it.setData(0, Qt.UserRole, MASTER_ITEM)
            self._colour(it, 3, self._file_text(None)[1])
            self.slaves.addTopLevelItem(it)
        file_slaves = {x.get("position"): x for x in (s.cfg or {}).get("slaves") or []}
        for pos in sorted(set(file_slaves) | set(s.bus)):
            f, b = file_slaves.get(pos), s.bus.get(pos)
            title = (f or {}).get("name") or ""
            device = (f or {}).get("device") or (b.proposal.name if b else "")
            state = b.info.get("state", "?") if b else ""
            file_text, file_colour = self._file_text(pos)
            it = QTreeWidgetItem([str(pos), title, state, file_text, device])
            it.setData(0, Qt.UserRole, pos)
            it.setToolTip(4, device)
            self._colour(it, 2, GREEN if state == "OP" else AMBER if state else GREY)
            self._colour(it, 3, file_colour)
            msgs = self.messages_by_slave.get(pos)
            if msgs:
                it.setToolTip(3, "\n".join(msgs))
            self.slaves.addTopLevelItem(it)
        self.slaves.blockSignals(False)
        for i in range(self.slaves.topLevelItemCount()):
            it = self.slaves.topLevelItem(i)
            if it.data(0, Qt.UserRole) == selected:
                self.slaves.blockSignals(keep_page)
                self.slaves.setCurrentItem(it)
                self.slaves.blockSignals(False)
                break
        else:
            if self.slaves.topLevelItemCount():
                self.slaves.setCurrentItem(self.slaves.topLevelItem(0))
            else:
                self.pages.setCurrentWidget(self.empty)

    def _file_text(self, pos):
        s = self.session
        if s.cfg is None:
            return "", GREY
        if pos is None:
            n = len(self.messages_by_slave.get(None, []))
            return ("%d error%s" % (n, "" if n == 1 else "s"), RED) if n else ("ok", GREEN)
        if s.slave(pos) is None:
            return "not in file", AMBER
        if s.bus and pos not in s.bus:
            return "not on bus", RED
        n = len(self.messages_by_slave.get(pos, []))
        if n:
            return "%d problem%s" % (n, "" if n == 1 else "s"), RED
        return "ok", GREEN

    @staticmethod
    def _colour(item, col, colour):
        item.setForeground(col, QBrush(colour))

    def selected(self, item, _previous=None):
        if item is None:
            self.pages.setCurrentWidget(self.empty)
            return
        pos = item.data(0, Qt.UserRole)
        if pos == MASTER_ITEM:
            self.master_page.show_config()
            self.pages.setCurrentWidget(self.master_page)
        else:
            self.slave_page.show_slave(pos)
            self.pages.setCurrentWidget(self.slave_page)

    def select_slave(self, pos):
        for i in range(self.slaves.topLevelItemCount()):
            it = self.slaves.topLevelItem(i)
            if it.data(0, Qt.UserRole) == pos:
                self.slaves.setCurrentItem(it)
                return

    def show_page_again(self):
        it = self.slaves.currentItem()
        if it:
            self.selected(it)

    def set_messages(self, groups):
        """groups: [(colour, prefix, [message])]."""
        self.messages.clear()
        for colour, prefix, msgs in groups:
            for m in msgs:
                it = QListWidgetItem("%s%s" % (prefix, m))
                it.setForeground(QBrush(colour))
                it.setData(Qt.UserRole, slave_of(m))
                self.messages.addItem(it)

    def message_activated(self, item):
        pos = item.data(Qt.UserRole)
        if pos is not None:
            self.select_slave(pos)

    # -- work in a thread

    def run(self, what, fn, done):
        if self.busy:
            return
        self.worker = Worker(fn, self)
        self.worker.progress.connect(self.status)
        self.worker.done.connect(lambda result: self._finished(done, result))
        self.worker.failed.connect(self._failed)
        self.status(what)
        QApplication.setOverrideCursor(Qt.BusyCursor)
        self.refresh(keep_page=True)
        self.worker.start()

    def _finish_worker(self):
        self.worker.wait()
        self.worker = None
        QApplication.restoreOverrideCursor()

    def _finished(self, done, result):
        self._finish_worker()
        done(result)
        self.refresh()
        self.show_page_again()

    def _failed(self, text):
        self._finish_worker()
        if text.startswith("Cannot reach a master"):
            self.session.bus_error = text
        self.set_messages([(RED, "", text.splitlines())])
        self.status(text.splitlines()[0])
        self.refresh()
        self.show_page_again()

    # -- actions

    def read_bus(self):
        def done(bus):
            notes = []
            if self.session.cfg is None:
                self.session.new()
                notes.append("A new configuration proposed from the bus; save it to keep it")
            self.status("Read %d slaves" % len(bus))
            self.check(extra=notes)
        self.run("Reading the bus...", lambda progress: self.session.read_bus(progress), done)

    def update_from_bus(self):
        before = copy.deepcopy(self.session.cfg)
        notes = self.session.update_from_bus()
        self.status("Updated from the bus" if self.session.cfg != before else
                    "The file matches the bus")
        self.check(extra=notes or ["Update: the file matches the bus"])
        self.refresh()
        self.show_page_again()

    def propose_again(self, pos, mode, pdo_set):
        if self.session.slave(pos) is not None and QMessageBox.question(
                self, TITLE, "Replace slave %d's process data, pins, DC and settings with a new "
                "proposal from the bus? Its name is kept; other changes made to it are lost "
                "(until you save, the file is not changed)." % pos,
                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self.run("Reading slave %d..." % pos,
                 lambda progress: self.session.propose_again(pos, mode, pdo_set),
                 lambda notes: self.check(extra=["slave %d: proposed again: %s" % (pos, n)
                                                 for n in notes] or
                                          ["slave %d: proposed again" % pos]))

    def check(self, quiet=False, extra=()):
        errors, problems, notes = self.session.check()
        by_slave = {}
        for m in errors + problems:
            by_slave.setdefault(slave_of(m), []).append(m)
        self.messages_by_slave = by_slave
        groups = [(RED, "Error: ", errors), (RED, "Bus: ", problems), (GREY, "Bus: ", notes),
                  (GREY, "", list(extra))]
        if not quiet:
            if self.session.cfg is not None and not errors and not problems:
                groups.append((GREEN, "", ["No errors in the file" +
                                           (", and the bus matches it" if self.session.bus else
                                            "; the bus was not read")]))
            self.status("%d errors" % len(errors + problems) if errors or problems else "Checked")
        self.set_messages(groups)
        if not quiet:
            self.refresh(keep_page=True)

    def diagnose(self, positions):
        def work(progress):
            pos_list = positions or sorted(self.session.bus) or tool.slave_positions()
            lines = []
            progress("Diagnosing %d slave%s (watching the error counters for 2 s)..."
                     % (len(pos_list), "" if len(pos_list) == 1 else "s"))
            found = diagnose.diagnose(pos_list, 2.0, out=lines.append)
            states = {pos: tool.slave_info(pos) for pos in pos_list}
            return found, lines, states

        def done(result):
            found, lines, states = result
            current, blocks = None, {}
            for line in lines:
                m = re.match(r"^Slave (\d+):", line)
                if m:
                    current = int(m.group(1))
                if current is not None:
                    blocks.setdefault(current, []).append(line)
            for pos, block in blocks.items():
                self.diagnoses[pos] = "\n".join(block).rstrip()
            for pos, info in states.items():
                if pos in self.session.bus:
                    self.session.bus[pos].info.update(state=info["state"],
                                                      al_status_code=info.get("al_status_code"))
            summary = "Diagnosis: %s" % ("no problems found" if not found else
                                         "%d problem%s found" % (found, "" if found == 1 else "s"))
            findings = ["slave %d: %s" % (pos, f) for pos, block in sorted(blocks.items())
                        for f in diagnosis_findings(block)]
            extra = []
            if all(i.get("state") == "PREOP" for i in states.values()) and states:
                extra.append("All slaves are in PREOP: normal while only a free-running master "
                             "runs (ethercat-uspace start); LinuxCNC takes them to OP")
            self.set_messages([(RED if found else GREEN, "", [summary]), (RED, "", findings),
                               (GREY, "", extra)])
            self.status(summary)
            if positions and len(positions) == 1:
                self.select_slave(positions[0])
                self.slave_page.tabs.setCurrentIndex(3)
        self.run("Diagnosing...", work, done)

    # -- the I/O test

    def start_test(self):
        errors = config.check(self.session.cfg)
        if errors:
            self.check()
            QMessageBox.warning(self, TITLE, "The configuration has errors; fix them first.")
            return
        if QMessageBox.question(
                self, TITLE, "Start the I/O test?\n\nThe ecat component runs this configuration "
                "(as edited, saved or not) in halrun with a servo thread of %d ns, and takes the "
                "slaves to OP. LinuxCNC must not run.\n\nOutputs of I/O slaves can then be set "
                "from each slave's Test tab: make sure nothing wired to them can do harm. "
                "CiA 402 drives stay disabled." % (self.session.period or 1000000),
                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        try:
            run = testmode.TestRun(copy.deepcopy(self.session.cfg), self.session.period)
        except testmode.TestError as e:
            QMessageBox.warning(self, TITLE, str(e))
            return

        def started(_):
            self.test = run
            self.test_timer.start()
            self.set_messages([(GREEN, "", ["Test running; log: %s" % run.log_path])])
        self.run("Starting the test...", lambda progress: run.start(progress), started)

    def stop_test(self):
        if self.test is None:
            return
        self.test_timer.stop()
        run = self.test

        def stopped(_):
            self.test = None
            self.set_messages([(GREY, "", [
                "Test stopped. The free-running master does not come back by itself: to read "
                "the bus again, sudo ethercat-uspace start"])])
        self.run("Stopping the test...", lambda progress: run.stop(), stopped)

    def set_pin(self, pin, value):
        if self.test is None:
            return
        try:
            self.test.set_pin(pin, value)
        except (testmode.TestError, ValueError) as e:
            self.status(str(e))

    def poll_test(self):
        run = self.test
        if run is None or self.busy:
            return
        if not run.running:
            self.test_timer.stop()
            tail = run.log_tail()
            run.stop()
            self.test = None
            self.set_messages([(RED, "", ["The test stopped by itself (halrun ended):"] +
                                tail.splitlines())])
            self.refresh(keep_page=True)
            return
        try:
            m = run.pins("ecat.0.")
        except testmode.TestError:
            return
        get = lambda n: m["ecat.0." + n].value if "ecat.0." + n in m else "?"
        responding = get("slaves-responding")
        try:
            responding = int(responding, 0)
        except ValueError:
            pass
        self.bus_label.setText("Test running: %s, %s slaves responding, link %s" % (
            "all OP" if get("all-op") == "TRUE" else "NOT all OP", responding,
            "up" if get("link-up") == "TRUE" else "DOWN"))
        if self.pages.currentWidget() is self.slave_page and \
                self.slave_page.tabs.currentIndex() == SlavePage.TEST_TAB:
            self.slave_page.update_test()

    def ask_save(self):
        """True if it is fine to drop the configuration being edited."""
        if not self.session.dirty:
            return True
        r = QMessageBox.question(self, TITLE, "The configuration has changes that are not saved. "
                                 "Save them?", QMessageBox.Save | QMessageBox.Discard |
                                 QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

    def start_dir(self):
        for p in (self.session.path, self.session.ini_path):
            if p:
                return os.path.dirname(os.path.abspath(p))
        return os.path.expanduser("~/linuxcnc/configs")

    def open_file(self):
        if not self.ask_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open an ecat configuration", self.start_dir(),
                                              "ecat configuration (*.yaml *.yml);;All files (*)")
        if path:
            self.load(path)

    def load(self, path):
        try:
            self.session.load(path)
        except Exception as e:
            QMessageBox.warning(self, TITLE, "Cannot read %s:\n%s" % (path, e))
            return False
        self.check()
        self.refresh()
        self.show_page_again()
        return True

    def import_xml(self):
        if not self.ask_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import an lcec XML file", self.start_dir(),
                                              "lcec configuration (*.xml);;All files (*)")
        if not path:
            return
        try:
            notes = self.session.import_xml(path)
        except Exception as e:
            QMessageBox.warning(self, TITLE, "Cannot import %s:\n%s" % (path, e))
            return
        if not self.session.bus:
            notes.insert(0, "the bus was not read: imported from the XML file alone")
        self.check(extra=["Import: %s" % n for n in notes])
        self.refresh()
        self.show_page_again()

    def save(self):
        if not self.session.path:
            return self.save_as()
        return self._save(self.session.path)

    def save_as(self):
        start = self.session.path or os.path.join(self.start_dir(), "ethercat.yaml")
        path, _ = QFileDialog.getSaveFileName(self, "Save the ecat configuration", start,
                                              "ecat configuration (*.yaml *.yml)")
        return self._save(path) if path else False

    def _save(self, path):
        existed = os.path.exists(path)
        try:
            self.session.save(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, TITLE, str(e))
            self.check()
            return False
        self.status("Wrote %s%s" % (path, " (the old file is %s.bak)" % path if existed else ""))
        self.refresh(keep_page=True)
        return True

    def choose_ini(self):
        path, _ = QFileDialog.getOpenFileName(self, "The machine's INI file", self.start_dir(),
                                              "LinuxCNC INI (*.ini);;All files (*)")
        if path:
            from .session import servo_period
            self.session.ini_path, self.session.period = path, servo_period(path)
            self.master_page.show_config()

    def closeEvent(self, event):
        if self.busy or not self.ask_save():
            event.ignore()
            return
        if self.test is not None:
            self.test_timer.stop()
            self.test.stop()
            self.test = None
        event.accept()


def main(path=None, esi_paths=(), ini_path=None, mode="csp", read_bus=True):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(TITLE)
    win = MainWindow(Session(esi_paths, ini_path, mode))
    if path:
        if os.path.exists(path):
            win.load(path)
        else:
            win.session.path = path
    win.show()
    if read_bus:
        win.read_bus()
    return app.exec_()
