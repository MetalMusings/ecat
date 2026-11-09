"""EtherCAT Setup's window (PyQt5), laid out as the mockups in
docs/design/ethercat-setup.md: a header with the configuration and the
bus's state, and the pages Bus, Troubleshoot, Test and HAL & LinuxCNC.

    ecatsetup --gui [ethercat.yaml] [--ini machine.ini] [--esi DIR]

- Bus: the slaves, and per slave where its configuration comes from, its
  mode and sync, process data and pins, and settings; all editable,
  checked while editing, saved to ethercat.yaml.
- Troubleshoot: a slave's way from INIT to OP, what it says (AL status
  code, CiA 402 state), its cables, and its own messages (0x10F3).
- Test: the I/O test (testmode.py): ecat in halrun, pins live, outputs of
  I/O slaves set, SDOs read and written. Drives stay disabled.
- HAL & LinuxCNC: the pins and the lines for a HAL file; writing them
  into the configuration is step 5 of the plan.

The work is done by session.Session, diagnose and testmode; this file only
shows it. Reading the bus runs in a thread, so the window stays responsive.
"""

import copy
import os
import re
import sys

from PyQt5.QtCore import QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFontDatabase, QKeySequence
from PyQt5.QtWidgets import (
    QAbstractItemView, QAction, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QStackedWidget, QTabBar, QTableWidget, QTableWidgetItem,
    QToolButton, QVBoxLayout, QWidget)

from . import config, diagnose, drivetest, esi, propose, testmode, tool
from .session import Session, servo_period

TITLE = "EtherCAT Setup"
RED, AMBER, GREEN, GREY = QColor("#b42318"), QColor("#9a5b00"), QColor("#1a7f37"), QColor("#6b7280")
_SLAVE_REF = re.compile(r"^slave (\d+)\b")
MASTER_ITEM = -1

STYLE = """
QWidget { font-size: 13px; }
QMainWindow, QWidget#page { background: #eef1f4; }
QScrollArea { background: #eef1f4; border: none; }
QFrame#header { background: #161b22; }
QLabel#appTitle { font-size: 16px; font-weight: 600; color: #e6edf3; }
QLabel#appSub { color: #9aa4b2; }
QLabel#headerInfo { color: #c9d1d9; }
QLabel#logo { border: 2px solid #79a8ff; border-radius: 6px; font-weight: 700; padding: 0px 4px;
              color: #79a8ff; }
QFrame#nav { background: white; border-bottom: 1px solid #d8dee4; }
QFrame#nav QTabBar::tab { background: white; padding: 10px 16px; border: none;
              border-bottom: 2px solid transparent; color: #4b5563; }
QFrame#nav QTabBar::tab:selected { border-bottom: 2px solid #1f6feb; color: #111827; }
QFrame#card { background: white; border: 1px solid #dde2e8; border-radius: 10px; }
QFrame#card[tone="red"] { border: 1px solid #f1a7a0; background: #fff7f6; }
QFrame#card[tone="amber"] { border: 1px solid #f2c46d; background: #fff8e8; }
QFrame#card[tone="grey"] { background: #f6f7f9; }
QLabel#cardTitle { color: #4b5563; font-weight: 600; font-size: 11px; letter-spacing: 1px; }
QLabel#hint { color: #6b7280; }
QLabel#bigTitle { font-size: 19px; font-weight: 600; color: #111827; }
QLabel#fieldName { color: #6b7280; font-size: 11px; letter-spacing: 1px; }
QLabel#value { font-family: monospace; font-size: 18px; }
QLabel[pill="green"] { background: #dcf5e3; color: #1a7f37; }
QLabel[pill="blue"] { background: #dbe8ff; color: #1f4fbf; }
QLabel[pill="red"] { background: #fde2df; color: #b42318; }
QLabel[pill="amber"] { background: #fdecc8; color: #8a5300; }
QLabel[pill="grey"] { background: #eceff3; color: #4b5563; }
QLabel[pill="green"], QLabel[pill="blue"], QLabel[pill="red"], QLabel[pill="amber"],
QLabel[pill="grey"] { border-radius: 9px; padding: 2px 9px; font-weight: 600; font-size: 11px; }
QLabel[box] { border: 1px solid #d0d6de; border-radius: 6px; padding: 6px 10px; color: #374151;
              background: white; }
QLabel[box="on"] { border: 1px solid #1a7f37; background: #e6f6ea; color: #14532d; font-weight: 600; }
QLabel[box="fault"] { border: 1px solid #f1a7a0; color: #b42318; }
QLabel[box="fault-on"] { border: 1px solid #b42318; background: #fde2df; color: #b42318;
              font-weight: 600; }
QPushButton { background: white; border: 1px solid #cdd3db; border-radius: 6px; padding: 5px 12px; }
QPushButton:hover { background: #f3f5f8; }
QPushButton:disabled { color: #a0a7b2; }
QPushButton#primary { background: #1f6feb; color: white; border: 1px solid #1f6feb; font-weight: 600;
              padding: 7px 16px; }
QPushButton#primary:disabled { background: #8fb2ee; border-color: #8fb2ee; }
QPushButton#danger { background: #b42318; color: white; border: 2px solid white; font-weight: 700;
              padding: 6px 18px; }
QPushButton#big { padding: 14px; font-size: 15px; font-weight: 600; }
QPushButton#hold { padding: 16px; font-size: 15px; font-weight: 600; color: #14532d;
              background: #e6f6ea; border: 2px solid #1a7f37; border-radius: 8px; }
QPushButton#hold:pressed { background: #b7e4c3; }
QPushButton#hold:disabled { color: #9aa4b2; background: #f3f5f8; border-color: #d0d6de; }
QToolButton#menu { color: #e6edf3; background: #161b22; border: 1px solid #3a434e;
              border-radius: 6px; padding: 6px 12px; }
QToolButton#menu::menu-indicator { image: none; width: 0px; }
QListWidget#slaves { border: none; background: transparent; outline: none; }
QListWidget#slaves::item { border-radius: 8px; margin: 2px 0px; border: 1px solid transparent; }
QListWidget#slaves::item:selected { background: #e7efff; border: 1px solid #9dbcf5; }
QListWidget#messages { border: none; background: transparent; }
QTableWidget { border: none; gridline-color: #eceff3; background: white; }
QHeaderView::section { background: white; border: none; border-bottom: 1px solid #dde2e8;
              padding: 4px; color: #4b5563; font-weight: 600; }
QPlainTextEdit#code { background: #161b22; color: #e6edf3; border-radius: 8px; padding: 6px; }
"""


def mono():
    return QFontDatabase.systemFont(QFontDatabase.FixedFont)


def slave_of(message):
    """The slave position a message is about, or None."""
    m = _SLAVE_REF.match(message)
    return int(m.group(1)) if m else None


def parse_int(text):
    """A number as typed: decimal or 0x hex. Raises ValueError."""
    return int(text.strip(), 0)


def interfaces():
    try:
        return sorted(n for n in os.listdir("/sys/class/net") if n != "lo")
    except OSError:
        return []


def period_text(ns):
    if not ns:
        return "?"
    return "%g ms" % (ns / 1e6) if ns >= 100000 else "%d us" % (ns // 1000)


def state_kind(state):
    return {"OP": "green", "SAFEOP": "amber", "PREOP": "amber", "INIT": "red"}.get(
        (state or "").split("+")[0].strip(), "grey")


PROFILE_TEXT = {"cia402": "CiA 402 drive", "mdp": "Modular device", "generic": "Generic"}


# -- small building blocks

def restyle(w):
    w.style().unpolish(w)
    w.style().polish(w)


def pill(text="", kind="grey"):
    lab = QLabel(text)
    lab.setProperty("pill", kind)
    lab.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    lab.setVisible(bool(text))
    return lab


def set_pill(lab, text, kind):
    lab.setText(text)
    lab.setVisible(bool(text))
    if lab.property("pill") != kind:
        lab.setProperty("pill", kind)
        restyle(lab)


def label(text="", name=None, wrap=False):
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    return lab


class Card(QFrame):
    """A white card with an uppercase title and an optional hint."""

    def __init__(self, title=None, hint=None, tone=None):
        super().__init__()
        self.setObjectName("card")
        if tone:
            self.setProperty("tone", tone)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(16, 14, 16, 14)
        self.lay.setSpacing(8)
        self.head = QHBoxLayout()
        self.title = label(title.upper() if title else "", "cardTitle")
        self.head.addWidget(self.title)
        self.hint = label(hint or "", "hint", wrap=True)
        self.head.addWidget(self.hint, 1)
        if title or hint:
            self.lay.addLayout(self.head)

    def set_tone(self, tone):
        if (self.property("tone") or "") != (tone or ""):
            self.setProperty("tone", tone or "")
            restyle(self)

    def add(self, w, stretch=0):
        if isinstance(w, QWidget):
            self.lay.addWidget(w, stretch)
        else:
            self.lay.addLayout(w, stretch)
        return w


def fit_table(t, minimum=1):
    """Make a table as tall as its rows, so the page scrolls instead."""
    t.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    h = t.horizontalHeader().sizeHint().height() + 4
    for r in range(t.rowCount()):
        h += t.rowHeight(r)
    if t.rowCount() < minimum:
        h += 30 * (minimum - t.rowCount())
    h += t.horizontalScrollBar().sizeHint().height()
    t.setFixedHeight(h)


def table(columns, stretch=None):
    t = QTableWidget(0, len(columns))
    t.setHorizontalHeaderLabels(columns)
    t.verticalHeader().hide()
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    if stretch is not None:
        t.horizontalHeader().setSectionResizeMode(stretch, QHeaderView.Stretch)
    else:
        t.horizontalHeader().setStretchLastSection(True)
    t.setShowGrid(False)
    return t


def item(text, editable=False, tip=None, colour=None, font=None):
    it = QTableWidgetItem("" if text is None else str(text))
    if not editable:
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    if tip:
        it.setToolTip(tip)
    if colour is not None:
        it.setForeground(QBrush(colour))
    if font is not None:
        it.setFont(font)
    return it


def scroll(widget):
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    widget.setObjectName("page")
    sa.setWidget(widget)
    return sa


def page_layout(w):
    lay = QVBoxLayout(w)
    lay.setContentsMargins(24, 20, 24, 20)
    lay.setSpacing(16)
    return lay


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


# -- the header

class Header(QFrame):
    def __init__(self, win):
        super().__init__()
        self.setObjectName("header")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 10, 20, 10)
        lay.setSpacing(12)
        lay.addWidget(label("•-•", "logo"))
        lay.addWidget(label(TITLE, "appTitle"))
        lay.addWidget(label("for LinuxCNC", "appSub"))
        lay.addSpacing(16)
        self.info = label("", "headerInfo")
        self.info.setFont(mono())
        lay.addWidget(self.info)
        lay.addStretch(1)
        self.status = pill("", "grey")
        lay.addWidget(self.status)
        self.menu_button = QToolButton()
        self.menu_button.setObjectName("menu")
        self.menu_button.setText("File ▾")
        self.menu_button.setPopupMode(QToolButton.InstantPopup)
        lay.addWidget(self.menu_button)
        self.save = QPushButton("Save")
        self.save.setObjectName("primary")
        self.save.setToolTip("Write ethercat.yaml; the old file is kept as .bak")
        self.save.clicked.connect(win.save)
        lay.addWidget(self.save)
        self.stop = QPushButton("STOP")
        self.stop.setObjectName("danger")
        self.stop.setToolTip("Stop the test: outputs back to 0, halrun ends")
        self.stop.clicked.connect(win.stop_test)
        lay.addWidget(self.stop)


# -- the Bus page

class SlaveRow(QWidget):
    """One slave in the bus list: position, device, name and kind, state."""

    def __init__(self, pos_text, title, subtitle, state, kind, problem=None):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        pos = label(pos_text)
        pos.setFont(mono())
        pos.setFixedWidth(22)
        lay.addWidget(pos)
        text = QVBoxLayout()
        text.setSpacing(0)
        t = label(title, wrap=True)
        t.setStyleSheet("font-weight: 600; font-size: 14px;")
        text.addWidget(t)
        sub = label(subtitle, "hint")
        sub.setWordWrap(True)
        text.addWidget(sub)
        lay.addLayout(text, 1)
        col = QVBoxLayout()
        col.setSpacing(2)
        if state:
            col.addWidget(pill(state, kind), 0, Qt.AlignRight)
        if problem:
            col.addWidget(pill(problem[0], problem[1]), 0, Qt.AlignRight)
        lay.addLayout(col)


class BusList(Card):
    def __init__(self, win):
        super().__init__("Bus")
        self.win = win
        self.setFixedWidth(340)
        self.rescan = QPushButton("Rescan")
        self.rescan.setToolTip("Read the slaves on the bus: SII, CoE dictionary, ESI files. "
                               "Nothing is written to them.")
        self.rescan.clicked.connect(win.read_bus)
        self.head.addWidget(self.rescan)
        self.list = QListWidget()
        self.list.setObjectName("slaves")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.currentItemChanged.connect(self.changed)
        self.add(self.list, 1)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #e5e7eb;")
        self.add(line)
        self.summary = self.add(label("", wrap=True))
        self.messages = QListWidget()
        self.messages.setObjectName("messages")
        self.messages.setWordWrap(True)
        self.messages.setMaximumHeight(170)
        self.messages.itemClicked.connect(win.message_activated)
        self.messages.itemActivated.connect(win.message_activated)
        self.add(self.messages)
        buttons = QVBoxLayout()
        self.update_button = QPushButton("Update the file from the bus")
        self.update_button.setToolTip("Bring the file up to date with the bus: slaves keep their "
                                      "entries, new ones get proposals, missing ones are left out")
        self.update_button.clicked.connect(win.update_from_bus)
        buttons.addWidget(self.update_button)
        self.esi = QPushButton("+ Add ESI files...")
        self.esi.clicked.connect(win.add_esi)
        buttons.addWidget(self.esi)
        self.add(buttons)

    def changed(self, it, _prev=None):
        if it is not None:
            self.win.select(it.data(Qt.UserRole), source=self)

    def fill(self):
        win, s = self.win, self.win.session
        m = (s.cfg or {}).get("master") or {}
        self.title.setText(("BUS · %s" % m["interface"].upper()) if m.get("interface") else "BUS")
        self.list.blockSignals(True)
        self.list.clear()
        rows = []
        if s.cfg is not None or s.bus:
            n = len(win.messages_by_slave.get(None, []))
            rows.append((MASTER_ITEM, SlaveRow(
                "", "Master", " · ".join(x for x in (m.get("interface"), m.get("transport"),
                                                     "cycle " + period_text(s.period)) if x),
                "", "grey", ("%d error%s" % (n, "" if n == 1 else "s"), "red") if n else None)))
        file_slaves = {x.get("position"): x for x in (s.cfg or {}).get("slaves") or []}
        for pos in sorted(set(file_slaves) | set(s.bus)):
            f, b = file_slaves.get(pos), s.bus.get(pos)
            device = (f or {}).get("device") or (b.proposal.name if b else "")
            sub = " · ".join(x for x in ((f or {}).get("name"),
                                         PROFILE_TEXT.get((f or {}).get("profile"))) if x)
            state = b.info.get("state", "?") if b else ""
            rows.append((pos, SlaveRow(str(pos), device, sub or "not in the file", state,
                                       state_kind(state), win.file_problem(pos))))
        width = self.width() - 40      # the card's margins and the item's border
        for key, w in rows:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, key)
            w.setFixedWidth(width)
            w.adjustSize()
            it.setSizeHint(w.sizeHint())
            self.list.addItem(it)
            self.list.setItemWidget(it, w)
        self.list.blockSignals(False)
        self.select(win.current)
        self.summary.setText(win.bus_summary())
        self.update_button.setEnabled(bool(s.bus) and not win.busy)
        self.rescan.setEnabled(not win.busy)

    def select(self, key):
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == key:
                self.list.setCurrentRow(i)
                break
        self.list.blockSignals(False)


class MasterView(QWidget):
    """The master's settings: interface, transport, distributed clocks."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        lay.addWidget(label("Master", "bigTitle"))
        card = Card("Master", "The network card and how frames are sent.")
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
        self.ini_label = label(wrap=True)
        ini.addWidget(self.ini_label, 1)
        choose = QPushButton("INI file...")
        choose.clicked.connect(win.choose_ini)
        ini.addWidget(choose)
        form.addRow("Servo period", ini)
        card.add(form)
        lay.addWidget(card)

        self.dc = Card("Distributed clocks", "Keep the slaves' clocks in step with the servo thread.")
        self.dc_on = QCheckBox("Use distributed clocks")
        self.dc_on.toggled.connect(self.edited)
        self.dc.add(self.dc_on)
        dform = QFormLayout()
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
        self.dc.add(dform)
        lay.addWidget(self.dc)
        lay.addStretch(1)

    def _enable_dc(self):
        on = self.dc_on.isChecked()
        for w in (self.ref_cycles, self.follows, self.shift_auto):
            w.setEnabled(on)
        self.shift.setEnabled(on and not self.shift_auto.isChecked())

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
        self.dc_on.setChecked(dc is not None)
        dc = dc or {}
        self.ref_cycles.setValue(int(dc.get("ref-clock-sync-cycles") or 0))
        self.follows.setChecked(bool(dc.get("servo-thread-follows-ref-clock")))
        shift = dc.get("sync0-shift-ns")
        self.shift_auto.setChecked(shift == "auto")
        self.shift.setValue(shift if isinstance(shift, int) else 0)
        self._enable_dc()
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
        if self.dc_on.isChecked():
            dc = m.setdefault("dc", {})
            if self.ref_cycles.value():
                dc["ref-clock-sync-cycles"] = self.ref_cycles.value()
            else:
                dc.pop("ref-clock-sync-cycles", None)
            dc["servo-thread-follows-ref-clock"] = self.follows.isChecked()
            dc["sync0-shift-ns"] = "auto" if self.shift_auto.isChecked() else self.shift.value()
        else:
            m.pop("dc", None)
        self._enable_dc()
        self.win.changed()


class SlaveView(QWidget):
    """One slave on the Bus page."""

    PD_COLUMNS = ["Dir", "PDO", "Object", "Name", "Type", "Use", "HAL pin", "HAL type",
                  "Scale", "Offset", "Bit pins"]
    PIN, HALTYPE, SCALE, OFFSET, SPLIT = 6, 7, 8, 9, 10
    SET_COLUMNS = ["Object", "Name", "Type", "Value"]

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.position = None
        self.loading = False
        self.pd_rows = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        # the slave
        head = Card()
        row = QHBoxLayout()
        self.title = label("", "bigTitle")
        row.addWidget(self.title)
        self.state = pill()
        row.addWidget(self.state)
        self.profile = pill("", "blue")
        row.addWidget(self.profile)
        self.where = label("", "hint")
        row.addWidget(self.where)
        row.addStretch(1)
        trouble = QPushButton("Troubleshoot")
        trouble.clicked.connect(lambda: win.show_tab(win.trouble_page))
        row.addWidget(trouble)
        test = QPushButton("Test this slave")
        test.clicked.connect(lambda: win.show_tab(win.test_page))
        row.addWidget(test)
        head.add(row)
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        self.fields = {}
        for c, name in enumerate(("Vendor", "Product", "Revision", "Clock")):
            grid.addWidget(label(name.upper(), "fieldName"), 0, c)
            v = label()
            v.setFont(mono())
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(v, 1, c)
            self.fields[name] = v
        grid.addWidget(label("NAME (PIN PREFIX)", "fieldName"), 0, 4)
        self.name = QLineEdit()
        self.name.setToolTip("The start of the slave's HAL pin names: <name>.<pin>")
        self.name.textEdited.connect(self.general_edited)
        grid.addWidget(self.name, 1, 4)
        grid.setColumnStretch(4, 1)
        head.add(grid)
        self.bus_ident = head.add(label("", "hint", wrap=True))
        lay.addWidget(head)

        self.problems = Card("Problems", tone="red")
        self.problem_text = self.problems.add(label(wrap=True))
        lay.addWidget(self.problems)

        # where the configuration comes from, and operation and sync
        two = QHBoxLayout()
        two.setSpacing(16)
        self.source = Card("Where the configuration comes from")
        self.source_rows = []
        for _ in range(3):
            r = QHBoxLayout()
            mark = pill()
            mark.setFixedWidth(26)
            mark.setAlignment(Qt.AlignCenter)
            text = label(wrap=True)
            r.addWidget(mark)
            r.addWidget(text, 1)
            self.source.add(r)
            self.source_rows.append((mark, text))
        esi_row = QHBoxLayout()
        self.esi_button = QPushButton("Choose ESI files...")
        self.esi_button.clicked.connect(win.add_esi)
        esi_row.addWidget(self.esi_button)
        esi_row.addStretch(1)
        self.source.add(esi_row)
        self.source.lay.addStretch(1)
        two.addWidget(self.source, 1)

        op = Card("Operation and sync")
        self.cia402 = QWidget()
        cform = QFormLayout(self.cia402)
        cform.setContentsMargins(0, 0, 0, 0)
        self.mode = QComboBox()
        for m, text in (("csp", "Cyclic synchronous position (CSP)"),
                        ("csv", "Cyclic synchronous velocity (CSV)"),
                        ("pv", "Profile velocity (PV), a spindle or conveyor"),
                        ("pp", "Profile position (PP), positioning that is not a joint")):
            self.mode.addItem(text, m)
        self.mode.activated.connect(self.general_edited)
        cform.addRow("Mode of operation", self.mode)
        modes = QHBoxLayout()
        self.other_modes = {}
        for m in ("hm", "csp", "csv", "pv", "pp"):
            cb = QCheckBox(m.upper())
            cb.toggled.connect(self.general_edited)
            self.other_modes[m] = cb
            modes.addWidget(cb)
        modes.addStretch(1)
        cform.addRow("Other modes", modes)
        self.supported = QHBoxLayout()
        self.supported.addStretch(1)
        cform.addRow("Supported (0x6502)", self.supported)
        cform.setRowWrapPolicy(QFormLayout.WrapLongRows)
        op.add(self.cia402)
        again = QHBoxLayout()
        self.pdo_set = QComboBox()
        self.pdo_set.setToolTip("The ESI file's predefined PDO assignments")
        again.addWidget(self.pdo_set, 1)
        self.again = QPushButton("Propose again")
        self.again.setToolTip("Read the slave and replace its process data, DC and settings with "
                              "a new proposal for the chosen mode and PDO assignment. The name is "
                              "kept.")
        self.again.clicked.connect(self.propose_again)
        again.addWidget(self.again)
        op.add(again)
        self.configure_pdos = QCheckBox("Write the PDO mapping to the slave (configure-pdos)")
        self.configure_pdos.toggled.connect(self.general_edited)
        op.add(self.configure_pdos)
        sync = QHBoxLayout()
        sync.addWidget(label("Sync:"))
        self.dc = QCheckBox("DC SYNC0")
        self.dc.toggled.connect(self.general_edited)
        sync.addWidget(self.dc)
        self.assign = QLineEdit()
        self.assign.setMaximumWidth(90)
        self.assign.setMinimumWidth(70)
        self.assign.setToolTip("AssignActivate, as in the ESI file, such as 0x0300")
        self.assign.textEdited.connect(self.general_edited)
        sync.addWidget(self.assign)
        self.sync0_cycles = QSpinBox()
        self.sync0_cycles.setRange(1, 1000)
        self.sync0_cycles.setPrefix("cycle ")
        self.sync0_cycles.setSuffix(" x")
        self.sync0_cycles.valueChanged.connect(self.general_edited)
        sync.addWidget(self.sync0_cycles)
        self.sync0_shift = QSpinBox()
        self.sync0_shift.setRange(-10000000, 10000000)
        self.sync0_shift.setPrefix("shift ")
        self.sync0_shift.setSuffix(" ns")
        self.sync0_shift.valueChanged.connect(self.general_edited)
        sync.addWidget(self.sync0_shift)
        sync.addStretch(1)
        op.add(sync)
        op.lay.addStretch(1)
        two.addWidget(op, 1)
        lay.addLayout(two)

        # process data
        pd = Card("Process data", "Pins are named <name>.<pin>. Clear a pin to map the entry "
                                  "without one; entries the profile handles have no pins.")
        self.pd = table(self.PD_COLUMNS, self.PIN)
        self.pd.itemChanged.connect(self.pd_edited)
        pd.add(self.pd)
        lay.addWidget(pd)

        # settings
        st = Card("Settings", "SDOs written at every start, in PREOP. Type an object such as "
                              "0x2000:01; name and type come from the slave's dictionary.")
        self.st = table(self.SET_COLUMNS, 1)
        self.st.itemChanged.connect(self.st_edited)
        st.add(self.st)
        buttons = QHBoxLayout()
        add = QPushButton("Add setting")
        add.clicked.connect(self.add_setting)
        remove = QPushButton("Remove setting")
        remove.clicked.connect(self.remove_setting)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        st.add(buttons)
        lay.addWidget(st)

        self.notes = Card("Notes from the proposal")
        self.notes_text = self.notes.add(label(wrap=True))
        lay.addWidget(self.notes)
        lay.addStretch(1)

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
        state = b.info.get("state", "?") if b else ""
        set_pill(self.state, state, state_kind(state))
        set_pill(self.profile, ("Profile: %s" % PROFILE_TEXT.get(s.get("profile"), s.get("profile")))
                 if s else "", "blue")
        self.where.setText("" if s and b else "not read from the bus" if s else
                           "not in the file: add it with Update from bus")
        self.name.setEnabled(s is not None)
        problems = win.messages_by_slave.get(pos, [])
        self.problems.setVisible(bool(problems))
        self.problem_text.setText("\n".join(problems))

        ident = (s or {}).get("identity") or {}
        info = b.info if b else {}

        def hx(v):
            return "0x%08X" % v if isinstance(v, int) else (v or "-")
        self.fields["Vendor"].setText(hx(ident.get("vendor") or info.get("vendor_id")))
        self.fields["Product"].setText(hx(ident.get("product") or info.get("product_code")))
        self.fields["Revision"].setText(hx(ident.get("revision") or info.get("revision")))
        self.fields["Clock"].setText("DC" if info.get("dc") else "no DC" if b else "-")
        bus_ident = ""
        try:
            want = (int(str(ident.get("vendor")), 16), int(str(ident.get("product")), 16),
                    int(str(ident.get("revision", "0")), 16))
        except ValueError:
            want = None
        if s and b and want != (info["vendor_id"], info["product_code"], info["revision"]):
            bus_ident = "On the bus: vendor 0x%08X, product 0x%08X, revision 0x%08X" % (
                info["vendor_id"] or 0, info["product_code"] or 0, info["revision"] or 0)
        self.bus_ident.setText(bus_ident)
        self.bus_ident.setVisible(bool(bus_ident))
        self.name.setText((s or {}).get("name", ""))

        # sources
        rows = []
        if p:
            rows.append((True, "Slave's own EEPROM (SII): PDOs, sync managers"))
            has_coe = b.coe is not None and bool(b.coe)
            rows.append((has_coe, "CoE dictionary read from the slave" if has_coe else
                         "No CoE dictionary: names come from the SII"))
            rows.append((bool(p.esi_path), "ESI file: %s" % os.path.basename(p.esi_path)
                         if p.esi_path else "No ESI file for this product"))
        else:
            rows.append((None, "Not read from the bus: Rescan to see the slave's own data"))
        for (mark, text), row in zip(self.source_rows, rows + [None] * 3):
            if row is None:
                mark.hide()
                text.hide()
                continue
            ok, t = row
            set_pill(mark, "✓" if ok else "!" if ok is False else "–",
                     "green" if ok else "amber" if ok is False else "grey")
            text.show()
            text.setText(t)
        self.esi_button.setVisible(bool(p) and not p.esi_path)

        editable = (self.cia402, self.pdo_set, self.configure_pdos, self.dc, self.pd, self.st)
        for w in editable:
            w.setEnabled(s is not None)
        if s is None:
            self.again.setEnabled(False)
            for w in (self.assign, self.sync0_cycles, self.sync0_shift):
                w.setEnabled(False)
            self.pd.setRowCount(0)
            self.st.setRowCount(0)
            self.notes.hide()
            return

        is402 = s.get("profile") == "cia402"
        self.cia402.setVisible(is402)
        if is402:
            c = s.get("cia402") or {}
            i = self.mode.findData(c.get("mode"))
            self.mode.setCurrentIndex(max(i, 0))
            for m, cb in self.other_modes.items():
                cb.setChecked(m in (c.get("modes") or []))
                cb.setEnabled(m != c.get("mode"))
            while self.supported.count() > 1:
                w = self.supported.takeAt(0).widget()
                if w:
                    w.deleteLater()
            chips = p.drive_modes if p and p.drive_modes else ["not read"]
            for m in chips:
                self.supported.insertWidget(self.supported.count() - 1, pill(m.upper(), "grey"))

        self.configure_pdos.setChecked(bool(s.get("configure-pdos", True)))
        self.pdo_set.clear()
        self.pdo_set.addItem("PDO assignment: the ESI file's default" if p and p.pdo_sets
                             else "PDO assignment: the slave's own", None)
        for name in (p.pdo_sets if p else []):
            self.pdo_set.addItem(name, name)
        self.again.setEnabled(b is not None and not win.busy)

        dc = s.get("dc")
        self.dc.setChecked(dc is not None)
        dc = dc or {}
        self.assign.setText(str(dc.get("assign-activate", "")))
        self.sync0_cycles.setValue(int(dc.get("sync0-cycles") or 1))
        self.sync0_shift.setValue(int(dc.get("sync0-shift-ns") or 0))
        for w in (self.assign, self.sync0_cycles, self.sync0_shift):
            w.setEnabled(self.dc.isChecked())
        notes = list(p.notes) if p else []
        self.notes.setVisible(bool(notes))
        self.notes_text.setText("\n".join(notes))

        self.show_process_data(s)
        self.show_settings(s)

    def show_process_data(self, s):
        self.loading = True
        self.pd.setRowCount(0)
        self.pd_rows = []
        b = self.win.session.bus.get(self.position)
        self.pd_names = {(e.index, e.subindex): e.name for c in (b.proposal.pdos if b else [])
                         for e in c.entries}
        pd = s.get("process-data") or {}
        for direction in ("outputs", "inputs"):
            for pi, pdo in enumerate(pd.get(direction) or []):
                for ei, e in enumerate(pdo.get("entries") or []):
                    self._pd_row(s, direction, pdo, pi, e, ei)
        self.pd.resizeColumnsToContents()
        self.pd.horizontalHeader().setSectionResizeMode(self.PIN, QHeaderView.Stretch)
        fit_table(self.pd)
        self.loading = False

    @staticmethod
    def _use(e):
        return e.get("use") or ("split" if "split" in e else "pin" if "pin" in e else "-")

    def _pd_row(self, s, direction, pdo, pi, e, ei):
        r = self.pd.rowCount()
        self.pd.insertRow(r)
        self.pd_rows.append((direction, pi, ei))
        try:
            index, sub = config.parse_obj(e.get("object"))
            name = self.win.session.object_name(self.position, index, sub) or \
                self.pd_names.get((index, sub), "")
        except ValueError:
            name = ""
        use = self._use(e)
        fixed = use in ("profile", "gap")
        cells = ["out" if direction == "outputs" else "in", pdo.get("pdo"), e.get("object"), name,
                 "%s %s" % (e.get("type", ""), e.get("bits")), use]
        for c, v in enumerate(cells):
            self.pd.setItem(r, c, item(v, colour=GREY if v in ("profile", "gap") else None,
                                       font=mono() if c in (1, 2) else None))
        pin_text = e.get("pin", "")
        if use == "profile":
            pin_text = "handled by the %s" % ("CiA 402 profile" if s.get("profile") == "cia402"
                                              else "encoder function")
        self.pd.setItem(r, self.PIN, item(pin_text, editable=not fixed and "split" not in e,
                                          colour=GREY if fixed else None,
                                          font=None if fixed else mono()))
        self.pd.setItem(r, self.SCALE, item(e.get("scale", ""), editable=not fixed))
        self.pd.setItem(r, self.OFFSET, item(e.get("offset", ""), editable=not fixed))
        self.pd.setItem(r, self.SPLIT, item(", ".join(p or "" for p in e.get("split", [])),
                                            editable=not fixed and "pin" not in e,
                                            tip="One pin per bit, from bit 0, comma separated; "
                                                "leave a bit empty to give it no pin"))
        if fixed:
            self.pd.setItem(r, self.HALTYPE, item(""))
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
                self.st.setItem(r, c, item(v, editable=True, font=mono() if c == 0 else None))
        self.st.resizeColumnsToContents()
        self.st.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        fit_table(self.st, minimum=1)
        self.loading = False

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
        for w in (self.assign, self.sync0_cycles, self.sync0_shift):
            w.setEnabled(self.dc.isChecked())
        self.win.changed()

    def pd_edited(self, it):
        if self.loading:
            return
        row, col = it.row(), it.column()
        e = self._entry(row)
        text = it.text().strip()
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
        self.pd.item(row, 5).setText(self._use(e))
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

    def st_edited(self, it):
        if self.loading:
            return
        s = self.win.session.slave(self.position)
        st = s["settings"][it.row()]
        text = it.text().strip()
        col = it.column()
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


class BusPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.setObjectName("page")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(20)
        self.list = BusList(win)
        lay.addWidget(self.list)
        self.stack = QStackedWidget()
        self.empty = label("Read the bus (Rescan) or open a configuration file (File > Open).")
        self.empty.setAlignment(Qt.AlignCenter)
        self.master = MasterView(win)
        self.slave = SlaveView(win)
        for w in (self.empty, self.master, self.slave):
            self.stack.addWidget(w)
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 8, 0)
        cl.addWidget(self.stack)
        self.scroll = scroll(content)
        lay.addWidget(self.scroll, 1)


# -- the Troubleshoot page

STEPS = [(1, "INIT", "Slave answers; identity read."),
         (2, "PREOP", "Mailbox works; SDOs can be read and written."),
         (4, "SAFEOP", "Inputs run; outputs are held safe."),
         (8, "OP", "Cyclic data in both directions.")]


def step_status(report, value):
    """(tag, pill kind, text or None) of one state, for a diagnose.Report."""
    order = [v for v, _, _ in STEPS]
    if report.state in order and value <= report.state:
        return "✓ reached", "green", None
    nxt = next((v for v in order if v > report.state), None)
    if value == nxt and (report.error or report.last_code):
        c = report.code if report.error else report.last_code
        return "✕ refused", "red", "0x%04X %s" % (c, diagnose.AL_CODES.get(c, ("unknown code",))[0])
    return "not reached", "grey", None


class StepCard(Card):
    def __init__(self):
        super().__init__()
        self.tag = pill()
        self.lay.addWidget(self.tag)
        self.name = label()
        self.name.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.lay.addWidget(self.name)
        self.text = label("", "hint", wrap=True)
        self.lay.addWidget(self.text)
        self.lay.addStretch(1)

    def show_step(self, name, desc, status):
        tag, kind, extra = status
        set_pill(self.tag, tag, kind)
        self.name.setText(name)
        self.text.setText(extra or desc)
        self.set_tone({"red": "red", "grey": "grey"}.get(kind))


class TroubleshootPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        lay = page_layout(content)
        outer.addWidget(scroll(content))

        row = QHBoxLayout()
        self.title = label("", "bigTitle")
        row.addWidget(self.title)
        self.state = pill()
        row.addWidget(self.state)
        row.addStretch(1)
        self.slave = QComboBox()
        self.slave.activated.connect(lambda i: win.select(self.slave.itemData(i), source=self))
        row.addWidget(self.slave)
        self.again = QPushButton("Examine again")
        self.again.setToolTip("Read the slave's state, its error counters (watched for 2 s), its "
                              "messages and, for a drive, the CiA 402 state. Only reads.")
        self.again.clicked.connect(lambda: win.examine([win.current]))
        row.addWidget(self.again)
        self.all = QPushButton("Examine all")
        self.all.clicked.connect(lambda: win.examine(None))
        row.addWidget(self.all)
        lay.addLayout(row)

        self.waiting = Card("Not examined yet")
        self.waiting.add(label("Examining only reads: the slave's state (INIT, PREOP, SAFEOP, OP) "
                               "and AL status code, the error counters per port while they are "
                               "watched for 2 s, the slave's own diagnosis history, and for a "
                               "CiA 402 drive its state and error code. Press Examine again or "
                               "Examine all.", "hint", wrap=True))
        lay.addWidget(self.waiting)

        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(16)
        steps = QHBoxLayout()
        steps.setSpacing(12)
        self.steps = [StepCard() for _ in STEPS]
        for c in self.steps:
            steps.addWidget(c, 1)
        body.addLayout(steps)
        self.step_note = label("", "hint", wrap=True)
        body.addWidget(self.step_note)

        two = QHBoxLayout()
        two.setSpacing(16)
        self.says = Card("What the slave says")
        self.headline = self.says.add(label(wrap=True))
        self.headline.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.explain = self.says.add(label(wrap=True))
        self.drive_title = self.says.add(label("CIA 402 DRIVE", "cardTitle"))
        self.drive = self.says.add(label(wrap=True))
        self.says.lay.addStretch(1)
        two.addWidget(self.says, 3)
        self.cable = Card("Cable and link")
        self.ports = table(["Port", "Link", "RX errors", "PHY errors", "Lost link"])
        self.cable.add(self.ports)
        self.cable_verdict = self.cable.add(label("", "hint", wrap=True))
        self.cable.lay.addStretch(1)
        two.addWidget(self.cable, 2)
        body.addLayout(two)

        self.history = Card("Messages from the slave (0x10F3)", "Newest first, from the slave's "
                                                                "diagnosis history.")
        self.history_table = table(["Message"], 0)
        self.history.add(self.history_table)
        self.history_none = self.history.add(label("", "hint", wrap=True))
        body.addWidget(self.history)
        lay.addWidget(self.body)
        lay.addStretch(1)

    def fill_slaves(self):
        win = self.win
        self.slave.blockSignals(True)
        self.slave.clear()
        for pos in win.positions():
            self.slave.addItem("Slave %d · %s" % (pos, win.device(pos)), pos)
        self.slave.setCurrentIndex(self.slave.findData(win.current))
        self.slave.blockSignals(False)
        self.again.setEnabled(not win.busy and isinstance(win.current, int) and win.current >= 0)
        self.all.setEnabled(not win.busy)

    def show_slave(self):
        win = self.win
        self.fill_slaves()
        pos = win.current
        r = win.reports.get(pos) if isinstance(pos, int) and pos >= 0 else None
        if r is None:
            self.title.setText("Slave %d · %s" % (pos, win.device(pos))
                               if isinstance(pos, int) and pos >= 0 else "Choose a slave")
            set_pill(self.state, "", "grey")
            self.waiting.show()
            self.body.hide()
            return
        self.waiting.hide()
        self.body.show()
        if r.state == 8 and not r.error:
            self.title.setText("Slave %d · %s is in OP" % (pos, win.device(pos)))
            set_pill(self.state, "OP", "green")
        else:
            self.title.setText("Slave %d · %s does not reach OP" % (pos, win.device(pos)))
            set_pill(self.state, "Stopped in %s%s" % (r.state_name, " + ERROR" if r.error else ""),
                     "red" if r.error or r.last_code else "amber")
        for card, (value, step, desc) in zip(self.steps, STEPS):
            card.show_step(step, desc, step_status(r, value))
        self.step_note.setText(
            "The slave waits in PREOP: normal while only the free-running master runs "
            "(ethercat-uspace start). LinuxCNC, or the test, takes it to OP."
            if r.state == 2 and not r.error and not r.last_code else "")
        self.step_note.setVisible(bool(self.step_note.text()))

        # what it says
        code = r.code if r.error else r.last_code
        if code:
            text, hint = diagnose.AL_CODES.get(code, ("unknown code", ""))
            self.headline.setText("AL status 0x%04X  %s" % (code, text))
            self.explain.setText((hint[:1].upper() + hint[1:] + "." if hint else "") + (
                "" if r.error else "\nKept by the master: the slave's own code was cleared when "
                "the master acknowledged the error."))
            self.says.set_tone("red")
        elif r.flagged and r.state != 8:
            self.headline.setText("The slave refused a state change")
            self.explain.setText("The master acknowledged the error, which cleared the slave's "
                                 "code; it is only in the master's log (\"AL status message\").")
            self.says.set_tone("red")
        else:
            self.headline.setText("No error reported (AL status 0x%04X)" % r.code)
            self.explain.setText("")
            self.says.set_tone(None)
        self.explain.setVisible(bool(self.explain.text()))
        lines = []
        if r.cia402 is not None:
            _, lines = diagnose.cia402_findings(r.cia402)
        if r.error_register:
            lines.append("Error register 0x1001: 0x%02X" % r.error_register)
        self.drive_title.setVisible(r.cia402 is not None)
        self.drive.setText("\n".join(line.strip() for line in lines))
        self.drive.setVisible(bool(lines))

        # cables
        self.ports.setRowCount(0)
        grew = []
        for p in r.ports:
            if not p.shown and p.number > 1:
                continue
            row = self.ports.rowCount()
            self.ports.insertRow(row)
            role = {0: " (in)", 1: " (out)"}.get(p.number, "")
            self.ports.setItem(row, 0, item("%d%s" % (p.number, role), font=mono()))
            self.ports.setItem(row, 1, item("up" if p.link else "open",
                                            colour=GREEN if p.link else GREY))
            for c, k in ((2, "rx"), (3, "phy"), (4, "lost")):
                v = p.now[k]
                text = ("255+" if v == 255 else str(v)) if p.link or v else "–"
                bad = k in p.grew
                if bad:
                    text += "  (+%d)" % p.delta[k]
                self.ports.setItem(row, c, item(text, colour=RED if bad else None, font=mono()))
            if p.grew:
                grew.append(p.number)
        fit_table(self.ports)
        last = not any(p.link for p in r.ports[1:])
        if grew:
            self.cable_verdict.setText(
                "Errors grew on port %s while watched for %g s: the cable or connector at that "
                "port, or the device on its other end." % (", ".join(map(str, grew)), r.seconds))
            self.cable.set_tone("red")
        else:
            self.cable_verdict.setText(
                "No errors grew in %g s; counts since power-on can be old." % r.seconds +
                (" Port 1 is open: this is the last slave." if last else ""))
            self.cable.set_tone(None)

        # history
        self.history_table.setRowCount(0)
        for line in reversed(r.history):
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            self.history_table.setItem(row, 0, item(line, font=mono()))
        fit_table(self.history_table)
        self.history_table.setVisible(bool(r.history))
        self.history_none.setText("" if r.history else
                                  "The slave keeps no diagnosis history (0x10F3).")
        self.history_none.setVisible(not r.history)


# -- the Test page

DRIVE_STATES = ["Not ready", "Switch on disabled", "Ready to switch on", "Switched on",
                "Operation enabled"]
STATUS_BITS = [(0, "Ready to switch on"), (1, "Switched on"), (2, "Operation enabled"),
               (3, "Fault"), (4, "Voltage enabled"), (5, "Quick stop not active"),
               (6, "Switch on disabled"), (7, "Warning"), (9, "Remote"), (10, "Target reached")]
LIVE_VALUES = [("pos-fb", "Position feedback"), ("drv-actual-position", "Position actual (0x6064)"),
               ("drv-actual-velocity", "Velocity actual (0x606C)"),
               ("actual-torque", "Torque actual (0x6077)"), ("error-code", "Error code (0x603F)"),
               ("fault-reason", "Fault reason (ecat)")]


class PinTable(QTableWidget):
    """Pins of a running test, with a setter for those that may be set."""

    COLUMNS = ["Pin", "Type", "Value", "Set"]

    def __init__(self, win):
        super().__init__(0, len(self.COLUMNS))
        self.win = win
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().hide()
        self.setShowGrid(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.rows = {}

    def fill(self, pins, prefix, run):
        self.setRowCount(0)
        self.rows = {}
        for name in sorted(pins):
            pin = pins[name]
            r = self.rowCount()
            self.insertRow(r)
            self.rows[name] = r
            for c, v in enumerate((name[len(prefix):], pin.type, pin.value)):
                self.setItem(r, c, item(v, font=mono() if c == 2 else None))
            if run.writable(pin):
                self.setCellWidget(r, 3, self._setter(pin))
        fit_table(self)

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

    def update_values(self, pins):
        for name, r in self.rows.items():
            if name in pins:
                it = self.item(r, 2)
                if it.text() != pins[name].value:
                    it.setText(pins[name].value)


class MoveCard(Card):
    """Moving one CSP drive: limits written to it, hold-to-enable, jog."""

    SETTINGS = [("scale", "Scale (counts per mm)", 0.000001, 1e9, 6),
                ("max-velocity", "Max velocity (mm/s)", 0.01, 100, 2),
                ("max-acceleration", "Max acceleration (mm/s²)", 0.1, 5000, 1),
                ("travel", "Travel from start (± mm)", 0.1, 500, 1),
                ("following-error", "Following error (mm)", 0.01, 100, 2),
                ("max-torque", "Max torque (%)", 1, 300, 0)]

    def __init__(self, win):
        super().__init__("Move")
        self.win = win
        self.slave = None
        # before: the limits
        self.setup = QWidget()
        form = QFormLayout(self.setup)
        form.setContentsMargins(0, 0, 0, 0)
        self.fields = {}
        for key, text, lo, hi, dec in self.SETTINGS:
            box = QDoubleSpinBox()
            box.setRange(lo, hi)
            box.setDecimals(dec)
            box.setValue(drivetest.DEFAULTS[key])
            form.addRow(text, box)
            self.fields[key] = box
        self.prepare = QPushButton("Write limits to the drive")
        self.prepare.setObjectName("primary")
        self.prepare.setToolTip("Reads the drive's own values, writes the test's limits to it, and "
                                "sets velocity, acceleration and travel in HAL. The drive stays "
                                "disabled; its values are written back when you are done.")
        self.prepare.clicked.connect(self.do_prepare)
        form.addRow(self.prepare)
        self.add(self.setup)
        self.unsupported = self.add(label("", "hint", wrap=True))

        # then: moving
        self.moving = QWidget()
        ml = QVBoxLayout(self.moving)
        ml.setContentsMargins(0, 0, 0, 0)
        self.hold = QPushButton("Hold to enable")
        self.hold.setObjectName("hold")
        self.hold.pressed.connect(lambda: self.enable(True))
        self.hold.released.connect(lambda: self.enable(False))
        ml.addWidget(self.hold)
        ml.addWidget(label("The drive is enabled only while a button is held. Release to stop.",
                           "hint", wrap=True))
        jog = QHBoxLayout()
        self.jog_minus = QPushButton("– Jog")
        self.jog_plus = QPushButton("Jog +")
        for b, sign in ((self.jog_minus, -1), (self.jog_plus, 1)):
            b.setToolTip("Hold: enables the drive and moves one step; release to stop")
            b.pressed.connect(lambda s=sign: self.jog(s))
            b.released.connect(lambda: self.enable(False))
            jog.addWidget(b)
        jog.addWidget(label("Step", "hint"))
        self.step = QComboBox()
        for st in ("0.1", "1", "5"):
            self.step.addItem("%s mm" % st, float(st))
        self.step.setCurrentIndex(1)
        jog.addWidget(self.step)
        ml.addLayout(jog)
        row = QHBoxLayout()
        self.reset = QPushButton("Reset fault")
        self.reset.pressed.connect(lambda: self.fault_reset(True))
        self.reset.released.connect(lambda: self.fault_reset(False))
        row.addWidget(self.reset)
        self.done = QPushButton("Done: put the drive's limits back")
        self.done.clicked.connect(self.do_restore)
        row.addWidget(self.done)
        ml.addLayout(row)
        self.where = label("", "mono")
        self.where.setFont(mono())
        ml.addWidget(self.where)
        self.add(self.moving)
        self.lay.addStretch(1)

    def test(self):
        return self.win.drive_tests.get(self.slave["name"]) if self.slave else None

    def show_drive(self, slave):
        self.slave = slave
        run = self.win.test
        csp = run is not None and slave["name"] in run.drives
        dt = self.test()
        self.setup.setVisible(csp and (dt is None or not dt.prepared))
        self.moving.setVisible(dt is not None and dt.prepared)
        if not csp:
            self.unsupported.setText("Only drives in CSP can be moved in the test for now.")
        else:
            self.unsupported.setText("")
            if dt is None:
                scale = drivetest.hal_scale(os.path.dirname(os.path.abspath(
                    self.win.session.ini_path)) if self.win.session.ini_path else "",
                    slave["name"])
                if scale:
                    self.fields["scale"].setValue(scale)
                    self.fields["scale"].setToolTip("From the configuration's HAL files")
        self.unsupported.setVisible(bool(self.unsupported.text()))
        self.win.test_page.show_limits(dt)

    def settings(self):
        return {k: box.value() for k, box in self.fields.items()}

    def do_prepare(self):
        run, s = self.win.test, self.slave
        if run is None or s is None:
            return
        b = self.win.session.bus.get(s["position"])
        dt = drivetest.DriveTest(run, s, b.coe if b else None, self.settings())
        try:
            dt.prepare()
        except (testmode.TestError, tool.ToolError) as e:
            QMessageBox.warning(self, TITLE, "The drive was not prepared:\n%s" % e)
            return
        self.win.drive_tests[s["name"]] = dt
        self.show_drive(s)

    def do_restore(self):
        dt = self.test()
        if dt is None:
            return
        failed = dt.restore()
        del self.win.drive_tests[self.slave["name"]]
        if failed:
            QMessageBox.warning(self, TITLE, "Not all of the drive's values could be written back; "
                                "they are kept in %s:\n%s" % (drivetest.RESTORE_FILE,
                                                             "\n".join(failed)))
        else:
            self.win.status("The drive's own limits are back")
        self.show_drive(self.slave)

    def enable(self, on):
        dt = self.test()
        if dt is None:
            return
        try:
            dt.enable(on)
        except testmode.TestError as e:
            self.win.status(str(e))

    def jog(self, sign):
        dt = self.test()
        if dt is None:
            return
        try:
            dt.enable(True)
            dt.jog(sign * self.step.currentData())
        except testmode.TestError as e:
            self.win.status(str(e))

    def fault_reset(self, on):
        dt = self.test()
        if dt is not None:
            try:
                dt.fault_reset(on)
            except testmode.TestError as e:
                self.win.status(str(e))

    def update_values(self, v):
        dt = self.test()
        if dt is None or not dt.prepared:
            return
        try:
            fb = float(v("pos-fb"))
        except (TypeError, ValueError):
            return
        self.where.setText("position %+.3f mm from start, target %+.3f mm"
                           % (fb - dt.start, dt.target - dt.start))


class TestPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.shown_for = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        lay = page_layout(content)
        outer.addWidget(scroll(content))

        self.banner = Card(tone="amber")
        self.banner_text = self.banner.add(label(wrap=True))
        lay.addWidget(self.banner)

        self.start_card = Card("I/O test")
        self.start_card.add(label(
            "The ecat component runs this configuration (as edited, saved or not) in halrun with a "
            "servo thread, without LinuxCNC, and takes the slaves to OP. Inputs show here live, "
            "and outputs of I/O slaves can be set. Pins set during the test go back to 0 when it "
            "stops. A CSP drive can be moved in small steps, after limits are written to it, "
            "only while a button is held.\n\n"
            "Before starting: stop LinuxCNC and the kernel master (sudo systemctl stop ethercat), "
            "and make sure nothing wired to the outputs can do harm.", wrap=True))
        self.start = QPushButton("Start the I/O test")
        self.start.setObjectName("primary")
        self.start.clicked.connect(win.start_test)
        row = QHBoxLayout()
        row.addWidget(self.start)
        row.addStretch(1)
        self.start_card.add(row)
        lay.addWidget(self.start_card)

        row = QHBoxLayout()
        self.title = label("", "bigTitle")
        row.addWidget(self.title)
        self.mode_pill = pill("", "blue")
        row.addWidget(self.mode_pill)
        self.state = pill()
        row.addWidget(self.state)
        row.addStretch(1)
        self.slave = QComboBox()
        self.slave.activated.connect(lambda i: win.select(self.slave.itemData(i), source=self))
        row.addWidget(self.slave)
        lay.addLayout(row)

        # a drive
        self.drive = QWidget()
        dl = QVBoxLayout(self.drive)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(16)
        chain = Card("Drive state (CiA 402)")
        crow = QHBoxLayout()
        self.chain = []
        for i, name in enumerate(DRIVE_STATES):
            if i:
                crow.addWidget(label("→", "hint"))
            box = QLabel(name)
            box.setProperty("box", "off")
            crow.addWidget(box)
            self.chain.append(box)
        crow.addStretch(1)
        self.fault_box = QLabel("Fault")
        self.fault_box.setProperty("box", "fault")
        crow.addWidget(self.fault_box)
        chain.add(crow)
        self.limits_banner = Card(tone="amber")
        self.limits_text = self.limits_banner.add(label(wrap=True))
        dl.addWidget(self.limits_banner)
        dl.addWidget(chain)
        three = QHBoxLayout()
        three.setSpacing(16)
        self.move = MoveCard(win)
        three.addWidget(self.move, 2)
        live = Card("Live values")
        grid = QGridLayout()
        grid.setSpacing(10)
        self.live = {}
        for i, (pin, text) in enumerate(LIVE_VALUES):
            tile = Card()
            tile.lay.setContentsMargins(12, 10, 12, 10)
            tile.add(label(text, "hint"))
            v = tile.add(label("–", "value"))
            grid.addWidget(tile, i // 2, i % 2)
            self.live[pin] = (tile, v)
        live.add(grid)
        live.lay.addStretch(1)
        three.addWidget(live, 3)
        sw = Card("Status word")
        self.sw_value = label()
        self.sw_value.setFont(mono())
        sw.head.addWidget(self.sw_value)
        self.bits = {}
        for bit, text in STATUS_BITS:
            r = QHBoxLayout()
            chip = pill("0", "grey")
            chip.setFixedWidth(26)
            chip.setAlignment(Qt.AlignCenter)
            r.addWidget(chip)
            r.addWidget(label(text), 1)
            sw.add(r)
            self.bits[bit] = chip
        sw.lay.addStretch(1)
        three.addWidget(sw, 2)
        dl.addLayout(three)
        lay.addWidget(self.drive)

        # an I/O slave
        self.io = QWidget()
        il = QHBoxLayout(self.io)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(16)
        self.outputs_card = Card("Outputs (to the slave)", "Set here; back to 0 when the test "
                                                           "stops.")
        self.outputs = PinTable(win)
        self.outputs_card.add(self.outputs)
        self.outputs_card.lay.addStretch(1)
        il.addWidget(self.outputs_card, 1)
        self.inputs_card = Card("Inputs (from the slave)", "Live.")
        self.inputs = PinTable(win)
        self.inputs_card.add(self.inputs)
        self.inputs_card.lay.addStretch(1)
        il.addWidget(self.inputs_card, 1)
        lay.addWidget(self.io)

        self.all_card = Card("All pins", "As the component shows them; a drive's pins are not "
                                         "set in the I/O test.")
        self.all_pins = PinTable(win)
        self.all_card.add(self.all_pins)
        lay.addWidget(self.all_card)

        # SDOs
        sdo = Card("SDO (CoE object)", "Read and write whenever a master runs: the test, or the "
                                       "free-running master.")
        form = QHBoxLayout()
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
        sdo.add(form)
        self.sdo_name = sdo.add(label("", "hint"))
        lay.addWidget(sdo)
        lay.addStretch(1)

    @property
    def position(self):
        return self.win.current

    def show_limits(self, dt):
        if dt is None or not dt.prepared:
            self.limits_banner.hide()
            return
        self.limits_banner.show()
        self.limits_text.setText(
            "<b>Safety limits written to the drive</b> &nbsp; " +
            " &nbsp; ".join(lim.text for lim in dt.limits) +
            "<br>heartbeat %d ms: the drive is disabled if this program stops answering. %s"
            % (testmode.WATCHDOG_MS, " ".join(n[:1].upper() + n[1:] + "." for n in dt.notes)))

    def _prefix(self):
        pos = self.position
        s = self.win.session.slave(pos) if isinstance(pos, int) and pos >= 0 else None
        return "%s." % s["name"] if s and s.get("name") else None

    def fill_slaves(self):
        win = self.win
        self.slave.blockSignals(True)
        self.slave.clear()
        for pos in win.positions():
            self.slave.addItem("Slave %d · %s" % (pos, win.device(pos)), pos)
        self.slave.setCurrentIndex(self.slave.findData(win.current))
        self.slave.blockSignals(False)
        self.start.setEnabled(win.session.cfg is not None and not win.busy)

    def show_slave(self):
        """Lay the page out for the chosen slave and the test's state."""
        win, run = self.win, self.win.test
        self.fill_slaves()
        running = run is not None and run.running
        self.banner.setVisible(running)
        self.start_card.setVisible(not running)
        if running:
            self.banner_text.setText(
                "<b>Test running</b> &nbsp; ecat in halrun, servo %s. Outputs set here go back "
                "to 0 when the test stops (STOP, top right). A drive moves only while a button is "
                "held, and stops if this program stops answering.<br>Log: %s"
                % (period_text(run.period), run.log_path))
        pos = self.position
        s = win.session.slave(pos) if isinstance(pos, int) and pos >= 0 else None
        if s is None:
            self.title.setText("Slave %d is not in the file" % pos
                               if isinstance(pos, int) and pos >= 0 else "Choose a slave")
            for w in (self.drive, self.io, self.all_card):
                w.hide()
            set_pill(self.mode_pill, "", "blue")
            set_pill(self.state, "", "grey")
            self.shown_for = None
            return
        self.title.setText("Slave %d · %s · %s" % (pos, win.device(pos), s.get("name")))
        is402 = s.get("profile") == "cia402"
        set_pill(self.mode_pill, ((s.get("cia402") or {}).get("mode") or "").upper()
                 if is402 else "", "blue")
        self.drive.setVisible(running and is402)
        self.io.setVisible(running and not is402)
        self.all_card.setVisible(running and is402)
        if not running:
            set_pill(self.state, "", "grey")
            self.shown_for = None
            return
        prefix = self._prefix()
        try:
            pins = run.pins(prefix)
        except testmode.TestError as e:
            win.status(str(e))
            return
        if is402:
            self.all_pins.fill(pins, prefix, run)
            self.move.show_drive(s)
        else:
            self.outputs.fill({n: p for n, p in pins.items() if p.dir == "IN"}, prefix, run)
            self.inputs.fill({n: p for n, p in pins.items() if p.dir != "IN"}, prefix, run)
        self.shown_for = (pos, prefix, frozenset(pins))
        self.update_values(pins)

    def update_values(self, pins=None):
        """Refresh live values (timer)."""
        run, prefix = self.win.test, self._prefix()
        if run is None or prefix is None or self.shown_for is None:
            return
        if pins is None:
            try:
                pins = run.pins(prefix)
            except testmode.TestError:
                return
            if (self.position, prefix, frozenset(pins)) != self.shown_for:
                self.show_slave()
                return

        def v(name):
            p = pins.get(prefix + name)
            return p.value if p else None
        op = v("slave-state-op") == "TRUE"
        set_pill(self.state, "OP" if op else "not OP" if v("slave-online") == "TRUE" else
                 "offline", "green" if op else "red")
        if not self.drive.isHidden():
            try:
                st = int(v("cia-state") or "-1")
            except ValueError:
                st = -1
            for i, box in enumerate(self.chain):
                want = "on" if i == st else "off"
                if box.property("box") != want:
                    box.setProperty("box", want)
                    restyle(box)
            want = "fault-on" if st in (6, 7) else "fault"
            if self.fault_box.property("box") != want:
                self.fault_box.setProperty("box", want)
                restyle(self.fault_box)
            for pin, (tile, lab) in self.live.items():
                val = v(pin)
                tile.setVisible(val is not None)
                if val is not None:
                    if pin == "error-code":
                        try:
                            val = diagnose.error_text(int(val, 0))
                        except ValueError:
                            pass
                    lab.setText(val)
            try:
                sw = int(v("statusword") or "0", 0)
            except ValueError:
                sw = 0
            self.sw_value.setText("0x%04X" % sw)
            for bit, chip in self.bits.items():
                on = bool(sw >> bit & 1)
                set_pill(chip, "1" if on else "0",
                         ("red" if bit in (3, 7) else "green") if on else "grey")
            self.all_pins.update_values(pins)
            self.move.update_values(v)
        else:
            self.outputs.update_values(pins)
            self.inputs.update_values(pins)

    # SDOs

    def _sdo_target(self):
        try:
            index, sub = config.parse_obj(self.sdo_obj.text().strip())
        except ValueError:
            self.win.status("Type an object as 0xIIII:SS, such as 0x2000:01")
            return None
        return index, sub

    def sdo_object_typed(self):
        t = self._sdo_target()
        pos = self.position
        if t and isinstance(pos, int) and pos >= 0:
            b = self.win.session.bus.get(pos)
            entry = b.coe.entries.get(t) if b and b.coe else None
            self.sdo_name.setText("%s (%s, %s)" % (
                self.win.session.object_name(pos, *t), entry.data_type, entry.access)
                if entry else "Not in the dictionary read from the slave: choose a type")

    def sdo_read(self):
        t = self._sdo_target()
        pos = self.position
        if not t or not isinstance(pos, int) or pos < 0:
            return
        try:
            text = tool.upload_text(pos, t[0], t[1], self.sdo_type.currentData())
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
        pos = self.position
        s = self.win.session.slave(pos) if isinstance(pos, int) and pos >= 0 else None
        if not t or not value or s is None:
            return
        why = testmode.sdo_write_refused(s, *t)
        if why:
            QMessageBox.warning(self, TITLE, "Not written: %s" % why)
            return
        if QMessageBox.question(
                self, TITLE, "Write %s to 0x%04X:%02X of slave %d?\n\nThe slave keeps it until "
                "it restarts (or for good, if it stores it). To have it written at every start, "
                "add it under Settings on the Bus page." % (value, t[0], t[1], pos),
                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        try:
            tool.download(pos, t[0], t[1], value, self.sdo_type.currentData())
        except tool.ToolError as e:
            QMessageBox.warning(self, TITLE, "The slave refused:\n%s" % e)
            return
        self.win.status("Wrote 0x%04X:%02X = %s to slave %d" % (t[0], t[1], value, pos))


# -- the HAL & LinuxCNC page

def config_pins(cfg):
    """(slave position, signal, pin, HAL type, direction) of the pins a
    configuration gives."""
    rows = []
    for s in (cfg or {}).get("slaves") or []:
        name = s.get("name")
        for direction in ("outputs", "inputs"):
            way = "to the slave" if direction == "outputs" else "from the slave"
            for pdo in (s.get("process-data") or {}).get(direction) or []:
                for e in pdo.get("entries") or []:
                    if "pin" in e:
                        rows.append((s["position"], e.get("object"), "%s.%s" % (name, e["pin"]),
                                     e.get("hal-type"), way))
                    for i, p in enumerate(e.get("split") or []):
                        if p:
                            rows.append((s["position"], "%s bit %d" % (e.get("object"), i),
                                         "%s.%s" % (name, p), "bit", way))
        if s.get("profile") == "cia402":
            rows.append((s["position"], "CiA 402 profile", "%s.*" % name, "",
                         "see docs/cia402.md"))
        for ch in s.get("encoders") or []:
            rows.append((s["position"], "encoder %s" % ch.get("index"),
                         "%s.%s.*" % (name, ch.get("name")), "", "see docs/encoder.md"))
    return rows


class HalPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        lay = QHBoxLayout(content)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(20)
        outer.addWidget(scroll(content))

        left = QVBoxLayout()
        left.setSpacing(16)
        pins = Card("Inputs and outputs", "The pins this configuration gives, to connect in HAL.")
        self.pins = table(["Slave", "Signal", "HAL pin", "Type", "Direction"], 2)
        pins.add(self.pins)
        left.addWidget(pins)
        yaml_card = Card("File contents", "ethercat.yaml as it will be written.")
        self.yaml = QPlainTextEdit()
        self.yaml.setReadOnly(True)
        self.yaml.setFont(mono())
        self.yaml.setMinimumHeight(360)
        yaml_card.add(self.yaml)
        left.addWidget(yaml_card)
        left.addStretch(1)
        lay.addLayout(left, 3)

        right = QVBoxLayout()
        apply = Card("Apply to LinuxCNC")
        apply.add(label("Writing ethercat.hal and the INI changes into the configuration comes "
                        "in step 5 of the plan. Until then, put these lines in the machine's HAL "
                        "file, in place of lcec's:", wrap=True))
        self.code = QPlainTextEdit()
        self.code.setObjectName("code")
        self.code.setReadOnly(True)
        self.code.setFont(mono())
        self.code.setFixedHeight(110)
        apply.add(self.code)
        apply.add(label("ecat.read-all goes first in the servo thread, ecat.write-all last.",
                        "hint", wrap=True))
        right.addWidget(apply)
        right.addStretch(1)
        lay.addLayout(right, 2)

    def refresh(self):
        s = self.win.session
        self.yaml.setPlainText(s.dumps())
        name = os.path.basename(s.path) if s.path else "ethercat.yaml"
        self.code.setPlainText("loadrt ecat config=%s\naddf ecat.read-all  servo-thread\n"
                               "# ... the other functions ...\naddf ecat.write-all servo-thread"
                               % name)
        self.pins.setRowCount(0)
        for row in config_pins(s.cfg):
            r = self.pins.rowCount()
            self.pins.insertRow(r)
            for c, v in enumerate(row):
                self.pins.setItem(r, c, item(v, font=mono() if c in (1, 2) else None))
        fit_table(self.pins, minimum=1)


# -- the window

class NavTabs(QStackedWidget):
    """Pages under a white strip of tabs, as in the mockups."""

    def __init__(self):
        super().__init__()
        self.strip = QFrame()
        self.strip.setObjectName("nav")
        lay = QHBoxLayout(self.strip)
        lay.setContentsMargins(12, 0, 12, 0)
        self.bar = QTabBar()
        self.bar.setDrawBase(False)
        self.bar.setExpanding(False)
        lay.addWidget(self.bar)
        lay.addStretch(1)
        self.bar.currentChanged.connect(self.setCurrentIndex)

    def addTab(self, page, text):
        self.addWidget(page)
        self.bar.addTab(text)

    def setCurrentWidget(self, page):
        self.bar.setCurrentIndex(self.indexOf(page))

class MainWindow(QMainWindow):
    def __init__(self, session):
        super().__init__()
        self.session = session
        self.reports = {}       # position -> diagnose.Report
        self.worker = None
        self.test = None        # testmode.TestRun while the I/O test runs
        self.current = None     # the chosen slave's position, MASTER_ITEM, or None
        self.messages_by_slave = {}
        self.test_timer = QTimer(self)
        self.test_timer.setInterval(200)
        self.test_timer.timeout.connect(self.poll_test)
        self.drive_tests = {}   # slave name -> drivetest.DriveTest
        # the heartbeat: a drive is shut down in the servo thread if it stops
        self.heartbeat = QTimer(self)
        self.heartbeat.setInterval(100)
        self.heartbeat.timeout.connect(self.tick)
        QApplication.instance().applicationStateChanged.connect(self.app_state)
        self.resize(1440, 940)
        self.setStyleSheet(STYLE)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = Header(self)
        lay.addWidget(self.header)
        self.tabs = NavTabs()
        self.bus_page = BusPage(self)
        self.trouble_page = TroubleshootPage(self)
        self.test_page = TestPage(self)
        self.hal_page = HalPage(self)
        self.tabs.addTab(self.bus_page, "Bus")
        self.tabs.addTab(self.trouble_page, "Troubleshoot")
        self.tabs.addTab(self.test_page, "Test")
        self.tabs.addTab(self.hal_page, "HAL && LinuxCNC")
        self.tabs.currentChanged.connect(lambda _: self.show_current())
        lay.addWidget(self.tabs.strip)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self._actions()
        self.refresh()

    @property
    def slave_page(self):
        return self.bus_page.slave

    @property
    def master_page(self):
        return self.bus_page.master

    @property
    def messages(self):
        return self.bus_page.list.messages

    def _actions(self):
        menu = QMenu(self)

        def act(text, slot, key=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if key:
                a.setShortcut(key)
                self.addAction(a)
            menu.addAction(a)
            return a

        self.a_open = act("Open...", self.open_file, QKeySequence.Open)
        self.a_import = act("Import lcec XML...", self.import_xml)
        menu.addSeparator()
        self.a_save = act("Save", self.save, QKeySequence.Save)
        self.a_save_as = act("Save as...", self.save_as, QKeySequence.SaveAs)
        menu.addSeparator()
        self.a_read = act("Rescan the bus", self.read_bus, QKeySequence.Refresh)
        self.a_update = act("Update from bus", self.update_from_bus)
        self.a_check = act("Check", self.check)
        self.a_esi = act("Add ESI files...", self.add_esi)
        menu.addSeparator()
        self.a_test = act("Start the I/O test", self.start_test)
        self.a_test_stop = act("Stop the test", self.stop_test)
        menu.addSeparator()
        act("Quit", self.close, QKeySequence.Quit)
        self.header.menu_button.setMenu(menu)

    # -- state shown

    @property
    def busy(self):
        return self.worker is not None

    def status(self, text):
        self.statusBar().showMessage(text, 8000)

    def positions(self):
        s = self.session
        return sorted({x.get("position") for x in (s.cfg or {}).get("slaves") or []} | set(s.bus))

    def device(self, pos):
        f, b = self.session.slave(pos), self.session.bus.get(pos)
        return (f or {}).get("device") or (b.proposal.name if b else "?")

    def file_problem(self, pos):
        """(text, pill kind) for a slave's entry in the list, or None."""
        s = self.session
        if s.cfg is None:
            return None
        if s.slave(pos) is None:
            return "not in file", "amber"
        if s.bus and pos not in s.bus:
            return "not on bus", "red"
        n = len(self.messages_by_slave.get(pos, []))
        if n:
            return "%d problem%s" % (n, "" if n == 1 else "s"), "red"
        return None

    def bus_summary(self):
        s = self.session
        if s.bus_error and not s.bus:
            return "No master running: start one with sudo ethercat-uspace start, then Rescan."
        if not s.bus:
            return "The bus has not been read."
        if s.cfg is None:
            return "%d slaves on the bus." % len(s.bus)
        odd = [pos for pos in self.positions() if self.file_problem(pos)]
        where = "the saved configuration" if not s.dirty else "the configuration"
        if not odd:
            return "The bus matches %s." % where
        return "The bus matches %s, except slave%s %s." % (
            where, "" if len(odd) == 1 else "s", ", ".join(map(str, odd)))

    def changed(self):
        """The configuration was edited: check it, show it, mark the list."""
        self.check(quiet=True)
        self.refresh(keep_page=True)

    def refresh(self, keep_page=False):
        s = self.session
        name = os.path.basename(s.path) if s.path else ("new configuration" if s.cfg else "")
        self.setWindowTitle("%s%s - %s" % (name, " *" if s.dirty else "", TITLE) if name else TITLE)
        m = (s.cfg or {}).get("master") or {}
        cfg_name = os.path.basename(os.path.dirname(os.path.abspath(s.path))) if s.path else (
            "new" if s.cfg else "none")
        self.header.info.setText("   ".join(x for x in (
            "config: %s%s" % (cfg_name, " *" if s.dirty else ""), m.get("interface"),
            m.get("transport"), "cycle %s%s" % (period_text(s.period),
                                                " (from INI)" if s.period else "")) if x))
        testing = self.test is not None
        if testing:
            if not self.header.status.text().startswith("● TEST"):
                set_pill(self.header.status, "● TEST MODE · I/O", "amber")
        elif s.bus_error and not s.bus:
            set_pill(self.header.status, "● No master", "grey")
        elif s.bus:
            bad = {p for p in self.positions() if self.file_problem(p)}
            bad |= {p for p, b in s.bus.items() if b.info.get("state") not in ("OP", "PREOP")}
            n = len(bad)
            set_pill(self.header.status, "● %d slave%s need%s attention" % (
                n, "" if n == 1 else "s", "s" if n == 1 else "") if n else
                "● Bus read · %d slaves" % len(s.bus), "amber" if n else "green")
        else:
            set_pill(self.header.status, "● Bus not read", "grey")
        has_cfg = s.cfg is not None
        self.header.save.setEnabled(has_cfg and not self.busy)
        self.header.save.setVisible(not testing)
        self.header.stop.setVisible(testing)
        self.header.stop.setEnabled(not self.busy)
        for a in (self.a_save, self.a_save_as, self.a_check):
            a.setEnabled(has_cfg and not self.busy)
        self.a_update.setEnabled(bool(s.bus) and not self.busy)
        for a in (self.a_read, self.a_import, self.a_open, self.a_esi):
            a.setEnabled(not self.busy)
        self.a_test.setEnabled(has_cfg and not self.busy and not testing)
        self.a_test_stop.setEnabled(testing and not self.busy)
        if self.current is None or (self.current != MASTER_ITEM and
                                    self.current not in self.positions()):
            self.current = MASTER_ITEM if (s.cfg is not None or s.bus) else None
        self.bus_page.list.fill()
        self.hal_page.refresh()
        if not keep_page:
            self.show_current()
        else:
            self.trouble_page.fill_slaves()
            self.test_page.fill_slaves()

    def select(self, key, source=None):
        self.current = key
        if source is not self.bus_page.list:
            self.bus_page.list.select(key)
        self.show_current()

    def select_slave(self, pos):
        self.select(pos)

    def show_tab(self, page):
        if self.tabs.currentWidget() is page:
            self.show_current()
        else:
            self.tabs.setCurrentWidget(page)

    def show_current(self):
        page = self.tabs.currentWidget()
        key = self.current
        if page is self.bus_page:
            stack = self.bus_page.stack
            if key is None:
                stack.setCurrentWidget(self.bus_page.empty)
            elif key == MASTER_ITEM:
                self.bus_page.master.show_config()
                stack.setCurrentWidget(self.bus_page.master)
            else:
                self.bus_page.slave.show_slave(key)
                stack.setCurrentWidget(self.bus_page.slave)
        elif page is self.trouble_page:
            self.trouble_page.show_slave()
        elif page is self.test_page:
            self.test_page.show_slave()
        elif page is self.hal_page:
            self.hal_page.refresh()

    def set_messages(self, groups):
        """groups: [(colour, prefix, [message])]."""
        lst = self.messages
        lst.clear()
        for colour, prefix, msgs in groups:
            for m in msgs:
                it = QListWidgetItem("%s%s" % (prefix, m))
                it.setForeground(QBrush(colour))
                it.setData(Qt.UserRole, slave_of(m))
                lst.addItem(it)
        lst.setVisible(lst.count() > 0)

    def message_activated(self, it):
        pos = it.data(Qt.UserRole)
        if pos is not None:
            self.show_tab(self.bus_page)
            self.select(pos)

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

    def _failed(self, text):
        self._finish_worker()
        if text.startswith("Cannot reach a master"):
            self.session.bus_error = text
        self.set_messages([(RED, "", text.splitlines())])
        self.status(text.splitlines()[0])
        self.refresh()

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

    def add_esi(self):
        path = QFileDialog.getExistingDirectory(self, "A folder of ESI files", self.start_dir())
        if not path:
            return
        self.session.esi_paths.append(path)
        self.session.esi_devices = esi.load_dirs(self.session.esi_paths)
        self.status("%d device descriptions read" % len(self.session.esi_devices))
        if self.session.bus:
            self.read_bus()

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

    def examine(self, positions):
        """Diagnose slaves (only reads) and show them on the Troubleshoot page."""
        def work(progress):
            on_bus = set(tool.slave_positions())
            pos_list = [p for p in (positions or self.positions() or sorted(on_bus))
                        if p in on_bus]
            progress("Examining %d slave%s (watching the error counters for 2 s)..."
                     % (len(pos_list), "" if len(pos_list) == 1 else "s"))
            reports = diagnose.examine(pos_list, 2.0)
            states = {pos: tool.slave_info(pos) for pos in pos_list}
            return reports, states

        def done(result):
            reports, states = result
            for r in reports:
                self.reports[r.position] = r
            for pos, info in states.items():
                if pos in self.session.bus:
                    self.session.bus[pos].info.update(state=info["state"],
                                                      al_status_code=info.get("al_status_code"))
            bad = [r for r in reports if r.error or r.state != 8 or any(p.grew for p in r.ports)
                   or (r.cia402 and diagnose.cia402_findings(r.cia402)[0])]
            self.status("Examined %d slave%s; %d need attention" % (
                len(reports), "" if len(reports) == 1 else "s", len(bad)))
            if positions and len(positions) == 1:
                self.current = positions[0]
                self.bus_page.list.select(self.current)
            if self.tabs.currentWidget() is not self.trouble_page:
                self.tabs.setCurrentWidget(self.trouble_page)
        self.run("Examining...", work, done)

    def diagnose(self, positions):
        self.examine(positions)

    # -- the I/O test

    def start_test(self):
        if self.session.cfg is None or self.test is not None:
            return
        errors = config.check(self.session.cfg)
        if errors:
            self.check()
            QMessageBox.warning(self, TITLE, "The configuration has errors; fix them first.")
            return
        if QMessageBox.question(
                self, TITLE, "Start the I/O test?\n\nThe ecat component runs this configuration "
                "(as edited, saved or not) in halrun with a servo thread of %s, and takes the "
                "slaves to OP. LinuxCNC must not run.\n\nOutputs of I/O slaves can then be set "
                "on the Test page: make sure nothing wired to them can do harm. "
                "CiA 402 drives stay disabled until you write limits to one and hold a button."
                % period_text(self.session.period or 1000000),
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
            self.heartbeat.start()
            self.set_messages([(GREEN, "", ["Test running; log: %s" % run.log_path])])
            if self.tabs.currentWidget() is not self.test_page:
                self.tabs.setCurrentWidget(self.test_page)
        self.run("Starting the test...", lambda progress: run.start(progress), started)

    def stop_test(self):
        if self.test is None:
            return
        self.test_timer.stop()
        run = self.test
        tests = list(self.drive_tests.values())

        def work(progress):
            failed = []
            run.disable_drives()
            for dt in tests:
                progress("Writing the drive's own limits back to %s" % dt.name)
                failed += ["%s %s" % (dt.name, f) for f in dt.restore()]
            run.stop()
            return failed

        def stopped(failed):
            self.heartbeat.stop()
            self.drive_tests = {}
            if failed:
                QMessageBox.warning(self, TITLE, "Not all of the drives' values could be written "
                                    "back; they are kept in %s:\n%s"
                                    % (drivetest.RESTORE_FILE, "\n".join(failed)))
            self.test = None
            self.set_messages([(GREY, "", [
                "Test stopped. The free-running master does not come back by itself: to read "
                "the bus again, sudo ethercat-uspace start"])])
        self.run("Stopping the test...", work, stopped)

    def set_pin(self, pin, value):
        if self.test is None:
            return
        try:
            self.test.set_pin(pin, value)
        except (testmode.TestError, ValueError) as e:
            self.status(str(e))

    def tick(self):
        """The drives' heartbeat, every 100 ms while the test runs."""
        if self.test is not None:
            try:
                self.test.tick_watchdogs()
            except (testmode.TestError, OSError):
                pass

    def app_state(self, state):
        """A held button can lose its release when the window loses focus:
        then no drive stays enabled."""
        if state != Qt.ApplicationActive and self.test is not None:
            self.test.disable_drives()

    def offer_restore(self):
        """Limits a crashed drive test left in drives: offer to put them back."""
        pending = drivetest.pending_restores()
        if not pending:
            return
        text = "\n".join("slave %s (%s): 0x%04X:%02X back to %s" % (
            e.get("position"), e.get("slave"), e["index"], e["subindex"], e["original"])
            for e in pending)
        if QMessageBox.question(
                self, TITLE, "A drive test ended without writing these drive values back:\n\n%s"
                "\n\nWrite them back now? (A master must run.)" % text,
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        done, failed = drivetest.restore_pending()
        if failed:
            QMessageBox.warning(self, TITLE, "Not written back (kept in %s):\n%s"
                                % (drivetest.RESTORE_FILE, "\n".join(failed)))
        else:
            self.status("Written back: %s" % "; ".join(done))

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
            self.refresh()
            return
        try:
            m = run.pins("ecat.0.")
        except testmode.TestError:
            return

        def get(n):
            return m["ecat.0." + n].value if "ecat.0." + n in m else "?"
        try:
            responding = int(get("slaves-responding"), 0)
        except ValueError:
            responding = "?"
        all_op = get("all-op") == "TRUE"
        set_pill(self.header.status, "● TEST MODE · %s · %s, %s slaves%s" % (
            "drive can move" if any(dt.prepared for dt in self.drive_tests.values()) else "I/O",
            "all OP" if all_op else "NOT all OP", responding,
            "" if get("link-up") == "TRUE" else ", link DOWN"), "amber" if all_op else "red")
        if self.tabs.currentWidget() is self.test_page:
            self.test_page.update_values()

    # -- files

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
            self.session.ini_path, self.session.period = path, servo_period(path)
            self.refresh(keep_page=True)
            self.bus_page.master.show_config()

    def closeEvent(self, event):
        if self.busy or not self.ask_save():
            event.ignore()
            return
        if self.test is not None:
            self.test_timer.stop()
            self.test.disable_drives()
            for dt in self.drive_tests.values():
                dt.restore()
            self.drive_tests = {}
            self.test.stop()
            self.heartbeat.stop()
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
    QTimer.singleShot(500, win.offer_restore)
    return app.exec_()
