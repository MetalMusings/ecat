"""EtherCAT Setup's window (PyQt5), laid out as the mockups in
docs/development.md: a header with the configuration and the
bus's state, and the pages Bus, Slave status, Test and HAL & LinuxCNC.

    ecatsetup --gui [ethercat.yaml] [--ini machine.ini] [--esi DIR]

- Bus: the slaves, and per slave where its configuration comes from, its
  mode and sync, process data and pins, and settings; all editable,
  checked while editing, saved to ethercat.yaml.
- Slave status: a slave's way from INIT to OP, what it says (AL status
  code, CiA 402 state), its cables, and its own messages (0x10F3).
- Test: the I/O test (testmode.py): ecat in halrun, pins live, outputs of
  I/O slaves set, SDOs read and written. Drives stay disabled.
- HAL & LinuxCNC: the pins and the lines for a HAL file; writing them
  into the configuration is step 5 of the plan.

The work is done by session.Session, diagnose and testmode; this file only
shows it. Reading the bus runs in a thread, so the window stays responsive.
"""

import copy
import html
import os
import re
import sys
import time

from PyQt5.QtCore import QSize, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFontDatabase, QKeySequence, QPainter, QPen
from PyQt5.QtWidgets import (
    QAbstractItemView, QAction, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea,
    QSizePolicy, QSpinBox, QStackedWidget, QStyledItemDelegate, QTabBar, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget, QCompleter)

from . import (config, diagnose, drivetest, esi, events, guidance, halcheck, halpins, homing, integrate, masterstart,
               netif, pindocs, propose, quirks, settings, symptoms, tasks, testmode, tool, update, wiring)
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
QLabel#axisBadge { background: #dbe8ff; color: #1f4fbf; font-weight: 700; font-size: 16px;
                   border-radius: 6px; }
QLabel#jointName { font-weight: 600; }
QLabel#problemText { color: #b42318; }
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
QPushButton#savedirty { background: #d29922; color: #1b1f24; border: 1px solid #d29922;
              font-weight: 700; padding: 7px 16px; }
QPushButton#savedirty:disabled { background: #6e5a2a; border-color: #6e5a2a; color: #c9d1d9; }
QPushButton#saveclean { background: transparent; color: #8b949e; border: 1px solid #30363d;
              padding: 7px 16px; }
QPushButton#danger { background: #b42318; color: white; border: 2px solid white; font-weight: 700;
              padding: 6px 18px; }
QPushButton#big { padding: 14px; font-size: 15px; font-weight: 600; }
QPushButton#home-ok { color: white; background: #1a7f37; border: 2px solid #14532d;
              border-radius: 6px; font-weight: 700; }
QPushButton#home-failed { color: white; background: #b42318; border: 2px solid #7a1710;
              border-radius: 6px; font-weight: 700; }
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


# size in bits of the CoE types, for showing a value in hex
TYPE_BITS = {"BOOL": 1, "BIT1": 1, "SINT": 8, "USINT": 8, "BYTE": 8, "INT": 16, "UINT": 16,
             "WORD": 16, "DINT": 32, "UDINT": 32, "DWORD": 32, "LINT": 64, "ULINT": 64,
             "LWORD": 64}


def show_value(v, ttype=None, hexa=False):
    """A setting's value as text: decimal, or (hexa) 0x hex for an integer, as wide as its
    type (a negative one as its two's complement). Anything else as it is."""
    if v is None:
        return ""
    if not hexa or isinstance(v, bool) or not isinstance(v, int):
        return v
    bits = TYPE_BITS.get(str(ttype or "").upper())
    if v < 0:
        if not bits:
            return v
        v += 1 << bits
    return "0x%0*X" % ((bits + 3) // 4 if bits and bits > 1 else 0, v)


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
    # an empty pill is hidden; one with text is left alone: setVisible(True) on a
    # label that has no parent yet shows it as a small window of its own, until it
    # is put into a layout
    if not text:
        lab.hide()
    return lab


def set_pill(lab, text, kind):
    lab.setText(text)
    lab.setVisible(bool(text))
    if lab.property("pill") != kind:
        lab.setProperty("pill", kind)
        restyle(lab)


class QuietCombo(QComboBox):
    """A combo box the mouse wheel does not change: scrolling a page over it must not
    pick a value (it once connected pins by itself); the wheel scrolls the page."""

    def __init__(self, *args):
        super().__init__(*args)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, ev):
        ev.ignore()


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


def fit_columns(t, stretch=None, wide=(), maxw=150, maxwide=280):
    """Size a table's columns to their text but no wider than maxw (maxwide for `wide`
    ones), still draggable, so that a row with long text does not push the right-hand
    columns out of reach. Long text is cut and shown whole in a tooltip or by editing."""
    h = t.horizontalHeader()
    for c in range(t.columnCount()):
        h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
    t.resizeColumnsToContents()
    for c in range(t.columnCount()):
        if c == stretch:
            h.setSectionResizeMode(c, QHeaderView.Stretch)
            continue
        w = min(t.columnWidth(c), maxwide if c in wide else maxw)
        h.setSectionResizeMode(c, QHeaderView.Interactive)
        t.setColumnWidth(c, w)
    t.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)


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
    def mark_dirty(self, dirty):
        """The Save button shows whether there is something to save."""
        self.save.setText("Save changes \u25cf" if dirty else "Saved")
        self.save.setToolTip("There are changes not written to the file yet: press to write "
                             "ethercat.yaml (the old file is kept as .bak)" if dirty else
                             "Everything is written to the file")
        name = "savedirty" if dirty else "saveclean"
        if self.save.objectName() != name:
            self.save.setObjectName(name)
            self.save.style().unpolish(self.save)
            self.save.style().polish(self.save)

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
        self.save.setObjectName("saveclean")
        self.save.setToolTip("Write ethercat.yaml; the old file is kept as .bak")
        self.save.clicked.connect(win.save)
        lay.addWidget(self.save)
        self.stop = QPushButton("STOP")
        self.stop.setObjectName("danger")
        self.stop.setToolTip("Stop the test: outputs back to 0, halrun ends. When watching "
                             "LinuxCNC: stop watching; LinuxCNC is not touched")
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
        self.start_master = QPushButton("Start the master...")
        self.start_master.setToolTip("Run sudo ethercat-uspace start, after you have seen "
                                     "the command and confirmed it")
        self.start_master.clicked.connect(lambda: win.offer_master_start(force=True))
        self.head.addWidget(self.start_master)
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
            # wrapped labels: the height depends on the width, which sizeHint() ignores
            for child in [w] + w.findChildren(QWidget):     # fonts come from the style sheet
                child.ensurePolished()
            w.layout().activate()
            height = w.layout().totalHeightForWidth(width)
            w.setFixedHeight(height)
            it.setSizeHint(QSize(width, height))
            self.list.addItem(it)
            self.list.setItemWidget(it, w)
        self.list.blockSignals(False)
        QTimer.singleShot(0, self.fit_rows)     # again when the rows are laid out and styled
        self.select(win.current)
        self.summary.setText(win.bus_summary())
        self.update_button.setEnabled(bool(s.bus) and not win.busy)
        self.rescan.setEnabled(not win.busy)
        self.start_master.setVisible(bool(s.bus_error) and not s.bus and win.test is None)
        self.start_master.setEnabled(not win.busy)

    def fit_rows(self):
        """Give each row the height its wrapped text needs at its real width."""
        for i in range(self.list.count()):
            it = self.list.item(i)
            w = self.list.itemWidget(it)
            if w is not None:
                h = w.layout().totalHeightForWidth(w.width())
                if h > 0 and h != it.sizeHint().height():
                    it.setSizeHint(QSize(w.width(), h))
                    w.setFixedHeight(h)

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
        self.interface = QuietCombo()
        self.interface.setEditable(True)
        self.interface.addItems(netif.interfaces())
        self.interface.editTextChanged.connect(self.edited)
        form.addRow("Network interface", self.interface)
        self.transport = QuietCombo()
        self.transport.addItems(sorted(config.TRANSPORTS))
        self.transport.currentTextChanged.connect(self.edited)
        form.addRow("Transport", self.transport)
        self.xdp_hint = label("", "hint", wrap=True)
        form.addRow("", self.xdp_hint)
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
        self.clock = QuietCombo()
        self.clock.addItem(self.CLOCK_SLAVES, False)
        self.clock.addItem(self.CLOCK_THREAD, True)
        self.clock.currentIndexChanged.connect(self.edited)
        dform.addRow("Who sets the time", self.clock)
        self.clock_hint = label("", "hint", wrap=True)
        dform.addRow("", self.clock_hint)
        self.ref_cycles_label = QLabel("Reference clock sync every (cycles)")
        dform.addRow(self.ref_cycles_label, self.ref_cycles)
        shift = QHBoxLayout()
        self.shift_auto = QCheckBox("auto")
        self.shift_auto.setToolTip("SYNC0 where the turn-around from output to status is the same "
                                   "in every cycle (two cycles)")
        tip = ("Time from the start of the servo period to SYNC0, for all DC slaves "
               "(lcec's globalShiftTime)")
        self.shift_auto.toggled.connect(self.edited)
        shift.addWidget(self.shift_auto)
        self.shift = QSpinBox()
        self.shift.setRange(-10000000, 10000000)
        self.shift.setSuffix(" ns")
        self.shift.valueChanged.connect(self.edited)
        shift.addWidget(self.shift, 1)
        shift_label = QLabel("Global shift time")
        shift_label.setToolTip(tip)
        self.shift.setToolTip(tip)
        dform.addRow(shift_label, shift)
        self.dc.add(dform)
        lay.addWidget(self.dc)
        lay.addStretch(1)

    CLOCK_SLAVES = "The slaves follow the servo thread (PC clock is the master)"
    CLOCK_THREAD = "The servo thread follows the slaves (reference clock is the master)"

    @property
    def follows(self):
        return bool(self.clock.currentData())

    def _show_clock(self):
        if self.follows:
            text = ("The servo period is adjusted a little every cycle to keep its phase to SYNC0 "
                    "(servo-thread-follows-ref-clock: true). Needed for the automatic SYNC0 shift.")
        else:
            text = ("The servo period stays as it is; the first DC slave's clock is steered to the "
                    "PC clock and the other slaves follow it (servo-thread-follows-ref-clock: false). "
                    "No automatic SYNC0 shift: set it by hand. Measured with ECT60 drives: the slaves' "
                    "clock drifted ~20 us/s against the thread, so SYNC0 wandered through the whole "
                    "period and the turn-around alternated between 2 and 3 cycles. Prefer the other "
                    "choice when drives count on 2.")
        self.clock_hint.setText(text)

    def _show_xdp(self):
        iface = self.interface.currentText().strip()
        if not iface:
            text = ""
        else:
            native = netif.xdp_native(iface) if iface in netif.interfaces() else None
            drv = netif.driver(iface) if native is not None else None
            name = "%s (%s)" % (iface, drv) if drv else iface
            text = ("%s can do xdp-native" % name if native else
                    "%s cannot do xdp-native: use raw or xdp-skb" % name if native is False else
                    "Not known whether %s can do xdp-native" % iface)
        self.xdp_hint.setText(text)
        self.xdp_hint.setVisible(bool(text))

    def _enable_dc(self):
        on = self.dc_on.isChecked()
        self.clock.setEnabled(on)
        self.ref_cycles.setEnabled(on and not self.follows)
        self.ref_cycles_label.setEnabled(on and not self.follows)
        self.shift_auto.setEnabled(on and self.follows)
        self.shift.setEnabled(on and not (self.follows and self.shift_auto.isChecked()))
        self._show_clock()

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
        self.clock.setCurrentIndex(1 if dc.get("servo-thread-follows-ref-clock") else 0)
        shift = dc.get("sync0-shift-ns")
        self.shift_auto.setChecked(shift == "auto")
        self.shift.setValue(shift if isinstance(shift, int) else 0)
        self._enable_dc()
        self._show_xdp()
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
            dc["servo-thread-follows-ref-clock"] = self.follows
            dc["sync0-shift-ns"] = ("auto" if self.shift_auto.isChecked() and self.follows
                                    else self.shift.value())
        else:
            m.pop("dc", None)
        self._enable_dc()
        self._show_xdp()
        self.win.changed()


def source_rows(p, b):
    """[(ok, text)] saying where a slave's proposal came from: ok True,
    False (something is missing or guessed) or None (nothing known)."""
    if not p:
        return [(None, "Not read from the bus: Rescan to see the slave's own data")]
    rows = [(True, "Slave's own EEPROM (SII): identity, PDOs, sync managers")]
    has_coe = b is not None and b.coe is not None and bool(b.coe)
    rows.append((has_coe, "CoE dictionary read from the slave: settings with their names and "
                 "types" if has_coe else "No CoE dictionary: names come from the SII"))
    if p.esi_path:
        if p.esi_exact:
            rows.append((True, "ESI file %s, for this revision" % os.path.basename(p.esi_path)))
        else:
            rows.append((False, "ESI file %s is for revision 0x%08X, the slave is 0x%08X: "
                         "check what it gave" % (os.path.basename(p.esi_path),
                                                 p.esi_revision or 0, p.revision or 0)))
    else:
        rows.append((False, "No ESI file: predefined PDO sets, DC modes, and the units and "
                     "value names of settings come from one (Choose ESI files...)"))
    rows.append((True, "Process data: %s" % (p.pdo_source or "-")))
    rows.append((bool(p.dc_source) or None, "DC: %s" % (p.dc_source or "not used")))
    if p.profile:
        rows.append((True, "Profile %s from %s" % (propose.PROFILE_NAMES.get(p.profile, p.profile),
                                                   p.profile_source or "?")))
    q = quirks.of((p.vendor_id, p.product_code))
    if q:
        rows.append((True, "Quirk file %s" % os.path.basename(q["path"])))
    return rows



class SettingPicker(QDialog):
    """Choose settings from a slave's object dictionary: the objects that can
    be written in PREOP, filterable, with what is known about them."""

    MANUFACTURER = range(0x2000, 0x6000)
    COLUMNS = ["Object", "Name", "Type", "Bits", "Access", "Range / default"]
    # objects that are process data mapping and assignment, not settings
    NOT_SETTINGS = (range(0x1600, 0x1800), range(0x1A00, 0x1C80))

    def __init__(self, parent, slave, dictionary, have=()):
        super().__init__(parent)
        self.setWindowTitle("Add settings - %s" % TITLE)
        self.resize(820, 560)
        self.slave, self.dictionary, self.have = slave, dictionary, set(have)
        lay = QVBoxLayout(self)
        lay.addWidget(label("Objects of the slave's dictionary (%s). Bold: manufacturer-specific "
                            "(0x2000-0x5FFF). Commands, which do something when written, cannot "
                            "be settings." % dictionary.source, "hint", wrap=True))
        top = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by object or name")
        self.filter.textChanged.connect(self.refill)
        self.only_writable = QCheckBox("Writable in PREOP only")
        self.only_writable.setChecked(True)
        self.only_writable.toggled.connect(self.refill)
        top.addWidget(self.filter, 1)
        top.addWidget(self.only_writable)
        lay.addLayout(top)
        self.table = table(self.COLUMNS, 1)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self.show_detail)
        self.table.itemDoubleClicked.connect(lambda *_: self.accept())
        lay.addWidget(self.table, 1)
        self.detail = label("", "hint", wrap=True)
        lay.addWidget(self.detail)
        self.read_now = QCheckBox("Take each value from the slave as it is now")
        self.read_now.setChecked(True)
        self.read_now.setToolTip("Otherwise the ESI file's default, or 0")
        lay.addWidget(self.read_now)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("Add")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.rows = []
        self.refill()

    def candidates(self):
        """The entries that may be offered, with why one cannot be chosen."""
        out = []
        text = self.filter.text().strip().lower()
        for (index, sub), e in sorted(self.dictionary.entries.items()):
            if any(index in r for r in self.NOT_SETTINGS) or e.name.startswith(("SubIndex", "Max SubIndex")):
                continue
            if (index, sub) in self.have:
                continue
            if self.only_writable.isChecked() and not e.writable_preop:
                continue
            name = self.dictionary.name(index, sub)
            if text and text not in ("0x%04x:%02x %s" % (index, sub, name)).lower():
                continue
            out.append((e, name, guidance.command_reason(self.slave, index, e)))
        return out

    def refill(self, *_):
        t = self.table
        t.setRowCount(0)
        self.rows = []
        for e, name, cmd in self.candidates():
            r = t.rowCount()
            t.insertRow(r)
            self.rows.append(e)
            grey = GREY if cmd else None
            tip = cmd[1] if cmd else None
            cells = ["0x%04X:%02X" % (e.index, e.subindex), name, e.data_type, e.bitlen, e.access,
                     guidance.describe_range(e)]
            for c, v in enumerate(cells):
                it = item(v, colour=grey, tip=tip,
                          font=mono() if c == 0 else None)
                if c == 0 and e.index in self.MANUFACTURER:
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                if cmd and cmd[0] == "error":
                    it.setFlags(it.flags() & ~(Qt.ItemIsSelectable | Qt.ItemIsEnabled))
                t.setItem(r, c, it)
        t.resizeColumnsToContents()
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.show_detail()

    def show_detail(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if not rows:
            self.detail.setText("%d objects. Select the ones to add; double-click adds one." %
                                len(self.rows))
            return
        e = self.rows[rows[0]]
        cmd = guidance.command_reason(self.slave, e.index, e)
        self.detail.setText("%s%s" % (guidance.describe(e), "\n" + cmd[1] if cmd else ""))

    def chosen(self):
        """The selected entries, commands left out."""
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.rows[r] for r in rows]

class ObjectDelegate(QStyledItemDelegate):
    """Settings' Object cell: completes from the dictionary."""

    def __init__(self, parent, entries):
        super().__init__(parent)
        self.entries = entries      # a function giving ["0x8010:01  Maximal current", ...]

    def createEditor(self, parent, option, index):
        e = QLineEdit(parent)
        c = QCompleter(self.entries(), e)
        c.setCaseSensitivity(Qt.CaseInsensitive)
        c.setFilterMode(Qt.MatchContains)
        e.setCompleter(c)
        return e


class SlaveView(QWidget):
    """One slave on the Bus page."""

    PD_COLUMNS = ["Dir", "PDO", "Object", "Name", "Type", "Use", "HAL pin", "HAL type",
                  "Scale", "Offset", "Bit pins", "Invert"]
    PIN, HALTYPE, SCALE, OFFSET, SPLIT, INVERT = 6, 7, 8, 9, 10, 11
    SET_COLUMNS = ["Object", "Name", "Type", "Value", "Now"]

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.position = None
        self.loading = False
        self.pd_rows = []
        self.present = {}       # (position, object) -> the slave's present value, as text
        self.home_support = (None, None)
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
        trouble = QPushButton("Status")
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
        for _ in range(7):
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
        self.quirk_button = QPushButton("Save as quirk file...")
        self.quirk_button.setToolTip(
            "Save this slave's pins and settings as your own quirk file for the device "
            "(vendor and product), so the next proposal for one starts from them")
        self.quirk_button.clicked.connect(self.save_quirk)
        esi_row.addWidget(self.quirk_button)
        esi_row.addStretch(1)
        self.source.add(esi_row)
        self.source.lay.addStretch(1)
        two.addWidget(self.source, 1)

        op = Card("Operation and sync")
        self.cia402 = QWidget()
        cform = QFormLayout(self.cia402)
        cform.setContentsMargins(0, 0, 0, 0)
        self.mode = QuietCombo()
        for m, text in (("csp", "Cyclic synchronous position (CSP)"),
                        ("csv", "Cyclic synchronous velocity (CSV)"),
                        ("pv", "Profile velocity (PV), a spindle or conveyor"),
                        ("pp", "Profile position (PP), positioning that is not a joint")):
            self.mode.addItem(text, m)
        self.mode.activated.connect(self.general_edited)
        cform.addRow("Mode of operation", self.mode)
        # modes the drive may switch to while it runs (opmode-cmd), and homing by the
        # drive's own method; both are cia402.modes in the file
        modes = QHBoxLayout()
        self.other_modes = {}
        for m in ("csp", "csv", "pv", "pp"):
            cb = QCheckBox(m.upper())
            cb.setToolTip("Lets the drive switch between the mode above and %s while it runs, "
                          "with the pin opmode-cmd. The mode above is the one it starts in."
                          % m.upper())
            cb.toggled.connect(self.general_edited)
            self.other_modes[m] = cb
            modes.addWidget(cb)
        modes.addStretch(1)
        cform.addRow("Can also switch to", modes)
        self.opmode_hint = label("", "hint", wrap=True)
        cform.addRow("", self.opmode_hint)
        hm = QCheckBox("Homing by the drive's own method")
        hm.setToolTip("Adds the homing card below. Homing is not an opmode-cmd value: it has its "
                      "own pins.")
        hm.toggled.connect(self.general_edited)
        self.other_modes["hm"] = hm
        cform.addRow("Homing", hm)
        self.zero_at_start = QCheckBox("Position counts from 0 at start")
        self.zero_at_start.setToolTip(
            "For a drive that keeps its position from an earlier run. The position the drive "
            "reports when the bus reaches OP is taken as 0 (pin pos-zero); nothing is written to "
            "the drive. Homing by the drive clears it. Instead of an SDO such as the ECT60's "
            "0x2010 (Zero Position).")
        self.zero_at_start.toggled.connect(self.general_edited)
        cform.addRow("Start position", self.zero_at_start)
        self.supported = QHBoxLayout()
        self.supported.addStretch(1)
        cform.addRow("Supported (0x6502)", self.supported)
        cform.setRowWrapPolicy(QFormLayout.WrapLongRows)
        op.add(self.cia402)
        again = QHBoxLayout()
        self.pdo_set = QuietCombo()
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
        self.assign.setPlaceholderText("0x0300")
        self.assign.setToolTip(
            "AssignActivate: which sync signals the slave uses, in hex. 0x0300 (SYNC0) is common for "
            "DC sync; 0x0000 means free run, the same as leaving DC SYNC0 off. The value is "
            "device-specific: take it from the ESI file's OpMode (AssignActivate) or the "
            "slave's manual.")
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

        # homing (CiA 402 hm)
        self.homing_card = Card("Homing by the drive",
                                "The drive's own homing (0x6098), used by the test's Home button "
                                "and by LinuxCNC when the joint homes through the drive. The "
                                "choice is written to Settings.")
        hform = QFormLayout()
        hform.setContentsMargins(0, 0, 0, 0)
        self.home_axis = QuietCombo()
        self.home_axis.activated.connect(lambda *_: self.show_homing(
            self.win.session.slave(self.position)))
        hform.addRow("Axis", self.home_axis)
        self.home_method = QuietCombo()
        self.home_method.activated.connect(self.homing_edited)
        hform.addRow("Method", self.home_method)
        self.home_text = label("", "hint", wrap=True)
        self.home_fields = {}
        for key, _i, _s, _t, text in homing.PARAMS:
            box = QSpinBox()
            box.setRange(-2000000000, 2000000000)
            box.setToolTip(text)
            box.valueChanged.connect(self.homing_edited)
            self.home_fields[key] = box
            hform.addRow(text[0].upper() + text[1:], box)
        self.home_hform = hform
        self.homing_card.add(self.home_text)
        self.homing_card.add(hform)
        self.home_ini_title = label("In LinuxCNC's INI file:", "hint")
        self.homing_card.add(self.home_ini_title)
        self.home_ini = QPlainTextEdit()
        self.home_ini.setReadOnly(True)
        self.home_ini.setFont(mono())
        self.home_ini.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.homing_card.add(self.home_ini)
        self.home_copy = QPushButton("Copy the INI lines")
        self.home_copy.clicked.connect(lambda: QApplication.clipboard().setText(
            self.home_ini.toPlainText()))
        self.homing_card.add(self.home_copy)
        lay.addWidget(self.homing_card)

        # process data
        pd = Card("Process data", "Pins are named <name>.<pin>. Clear a pin to map the entry "
                                  "without one; entries the profile handles have no pins.")
        self.pd = table(self.PD_COLUMNS, self.PIN)
        self.pd.itemChanged.connect(self.pd_edited)
        self.pd.cellDoubleClicked.connect(self.pd_double_clicked)
        pd.add(self.pd)
        lay.addWidget(pd)

        # settings
        st = Card("Settings", "SDOs written at every start, in PREOP. Add them from the slave's "
                              "dictionary, or type an object such as 0x2000:01 (it completes); "
                              "name and type come from the dictionary.")
        self.st = table(self.SET_COLUMNS, 1)
        self.st.itemChanged.connect(self.st_edited)
        self.st.setItemDelegateForColumn(0, ObjectDelegate(self.st, self.dictionary_lines))
        st.add(self.st)
        self.st_problems = st.add(label(wrap=True))
        buttons = QHBoxLayout()
        add = QPushButton("Add setting...")
        add.setToolTip("Choose from the objects the slave's dictionary (or its ESI file) lists")
        add.clicked.connect(self.choose_settings)
        remove = QPushButton("Remove setting")
        remove.clicked.connect(self.remove_setting)
        self.read_now_button = QPushButton("Read present values")
        self.read_now_button.setToolTip("Read each setting's object from the slave, to compare")
        self.read_now_button.clicked.connect(self.read_present_values)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addWidget(self.read_now_button)
        self.hex_box = QCheckBox("Values in hex")
        self.hex_box.setToolTip("Show the values (and what the slave has now) as 0x hex. You can "
                                "type a value in either form: 255 or 0xFF")
        self.hex_box.setChecked(self.win.hex_values)
        self.hex_box.toggled.connect(self.win.set_hex_values)
        buttons.addWidget(self.hex_box)
        buttons.addStretch(1)
        st.add(buttons)
        lay.addWidget(st)

        self.notes = Card("Notes from the proposal")
        self.notes_text = self.notes.add(label(wrap=True))
        lay.addWidget(self.notes)
        lay.addStretch(1)

    def save_quirk(self):
        s = self.win.session.slave(self.position)
        if not s or not quirks.identity(s):
            return
        start = os.path.join(quirks.user_dir(), quirks.file_name(s))
        q = quirks.of(s)
        if q and not q["path"].startswith(quirks.SHIPPED):
            start = q["path"]
        path, _ = QFileDialog.getSaveFileName(self, "Save as quirk file - %s" % TITLE, start,
                                              "Quirk files (*.yaml)")
        if not path:
            return
        try:
            quirks.save(s, path)
        except (OSError, quirks.QuirkError) as e:
            QMessageBox.warning(self, TITLE, "Could not save the quirk file: %s" % e)
            return
        where = "" if os.path.dirname(os.path.abspath(path)) == os.path.abspath(
            quirks.user_dir()) else (" It is read only from %s; move it there to use it."
                                     % quirks.user_dir())
        self.win.status("Quirk file saved: %s.%s" % (path, where))

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
        rows = source_rows(p, b)
        for (mark, text), row in zip(self.source_rows, rows + [None] * len(self.source_rows)):
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
            self.homing_card.hide()
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
            self.show_opmode_hint(c)
            self.zero_at_start.setChecked(bool(c.get("zero-at-start")))
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
        self.show_homing(s)

    OPMODE_NUMBER = {"pp": 1, "pv": 3, "csp": 8, "csv": 9}

    def show_opmode_hint(self, c):
        """What opmode-cmd takes, for the modes chosen (the same numbers as the profile)."""
        others = [m for m in (c.get("modes") or []) if m in self.OPMODE_NUMBER]
        if not others:
            self.opmode_hint.setText("")
            return
        nums = ["0 = %s (the mode above)" % (c.get("mode") or "").upper()]
        nums += ["%d = %s" % (self.OPMODE_NUMBER[m], m.upper()) for m in others]
        self.opmode_hint.setText("The drive gets the pin opmode-cmd: %s. Try it at the machine "
                                 "first: it is not yet tried on a drive." % ", ".join(nums))

    HOME_DEFAULTS = {"fast": 3000, "slow": 1000, "accel": 5000, "offset": 0}

    def home_axis_number(self):
        return max(self.home_axis.currentIndex(), 0)

    def show_homing(self, s):
        c = (s or {}).get("cia402") or {}
        shown = bool(s) and s.get("profile") == "cia402" and ("hm" in (c.get("modes") or [])
                                                           or bool(s.get("homing")))
        self.homing_card.setVisible(shown)
        if not shown:
            return
        names = config.axes(s)
        if self.home_axis.count() != len(names):
            self.home_axis.clear()
            self.home_axis.addItems(names)
        self.home_axis.setVisible(len(names) > 1)
        self.home_hform.labelForField(self.home_axis).setVisible(len(names) > 1)
        axis = self.home_axis_number()
        b = self.win.session.bus.get(self.position)
        coe = b.coe.entries if b and b.coe else {}
        base = 0x60E3 + axis * config.AXIS_OFFSET
        read = (lambda i, sub, t: tool.upload(self.position, i, sub, t)) if (base, 0) in coe \
            else (lambda i, sub, t: None)
        try:
            nums, where = homing.supported(s, axis, read)
        except tool.ToolError:
            nums, where = None, None
        chosen = homing.choice(s, axis)
        self.loading = True
        self.home_method.clear()
        self.home_method.addItem("Not set here (the drive's own 0x6098, or a setting of yours)", None)
        for m, ok in homing.choices(s, axis, read):
            text = "%d  %s" % (m.number, m.text)
            if ok is False:
                text += "  (the drive does not support it)"
            self.home_method.addItem(text, m.number)
            if ok is False:
                self.home_method.model().item(self.home_method.count() - 1).setEnabled(False)
        i = self.home_method.findData(chosen["method"]) if chosen else 0
        self.home_method.setCurrentIndex(max(i, 0))
        for key, box in self.home_fields.items():
            box.setValue(int((chosen or {}).get(key, self.HOME_DEFAULTS[key]) or 0))
        self.loading = False
        self.home_support = (nums, where)
        self.show_homing_text(s, chosen, nums, where)

    def show_homing_text(self, s, chosen, nums, where):
        parts = []
        if chosen:
            m = homing.method(chosen["method"], s)
            parts.append(m.text + ".")
            parts.append(("The axis does not move." if not m.moves else
                          "The axis MOVES. " + ("Needs: %s." % ", ".join(m.needs) if m.needs else "")))
            used = homing.parameters(chosen["method"], s)
        else:
            used = []
        parts.append("Supported methods: %s." % (
            "%s (from the %s)" % (", ".join(map(str, nums)), where) if nums
            else "not known for this drive, every method of the standard is offered"))
        self.home_text.setText(" ".join(parts))
        lines = homing.ini_help(self.win.session.cfg, s, self.home_axis_number())
        self.home_ini.setPlainText("\n".join(t for t, _ in lines) if lines else "")
        self.home_ini.setFixedHeight(self.home_ini.fontMetrics().lineSpacing()
                                     * (len(lines or ()) + 2) + 16)
        for w in (self.home_ini_title, self.home_ini, self.home_copy):
            w.setVisible(bool(lines))
        for key, box in self.home_fields.items():
            self.home_hform.labelForField(box).setVisible(key in used)
            box.setVisible(key in used)

    def homing_edited(self, *_):
        if self.loading:
            return
        s = self.win.session.slave(self.position)
        if s is None:
            return
        axis = self.home_axis_number()
        number = self.home_method.currentData()
        chosen = None
        if number is not None:
            chosen = {"method": number}
            for key in homing.parameters(number, s):
                chosen[key] = self.home_fields[key].value()
        homing.apply(s, axis, chosen)
        self.show_settings(s)
        self.show_homing_text(s, chosen, *self.home_support)
        self.win.changed()

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
        fit_columns(self.pd, stretch=self.PIN, wide=(self.SPLIT,))
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
                                       font=mono() if c in (1, 2) else None,
                                       tip=str(v) if v and c == 3 else None))
        pin_text = e.get("pin", "")
        if use == "profile":
            pin_text = "handled by the %s" % ("CiA 402 profile" if s.get("profile") == "cia402"
                                              else "encoder function")
        self.pd.setItem(r, self.PIN, item(pin_text, editable=not fixed and "split" not in e,
                                          colour=GREY if fixed else None,
                                          font=None if fixed else mono(),
                                          tip="A bit field has no pin name: its bit pins are named "
                                              "in the Bit pins column (double-click here to go "
                                              "there)" if "split" in e else None))
        self.pd.setItem(r, self.SCALE, item(e.get("scale", ""), editable=not fixed))
        self.pd.setItem(r, self.OFFSET, item(e.get("offset", ""), editable=not fixed))
        self.pd.setItem(r, self.SPLIT, item(", ".join(p or "" for p in e.get("split", [])),
                                            editable=not fixed and "pin" not in e,
                                            tip="One pin per bit, from bit 0, comma separated; "
                                                "leave a bit empty to give it no pin"
                                                + ("\n\n" + ", ".join(p or "-" for p in
                                                                       e.get("split", []))
                                                   if "split" in e else "")))
        self.pd.setItem(r, self.INVERT, item(self._invert_text(e), editable=not fixed,
                                             tip="A normally closed switch reads 1 when it is "
                                                 "open: invert the pin so that it reads 0 then. "
                                                 "A bit pin: yes. A bit field: the names of "
                                                 "the bit pins to invert, comma separated"))
        if fixed:
            self.pd.setItem(r, self.HALTYPE, item(""))
            return
        box = QuietCombo()
        box.addItems([""] + sorted(config.HAL_TYPES) + [self.BIT_FIELD])
        box.setCurrentText(self._type_text(e))
        box.setEnabled(self._type_enabled(e))
        box.currentTextChanged.connect(lambda t, row=r: self.hal_type_edited(row, t))
        self.pd.setCellWidget(r, self.HALTYPE, box)

    BIT_FIELD = "bit field"      # in the type list: one bit pin per bit (`split`)

    @staticmethod
    def _type_text(e):
        return SlaveView.BIT_FIELD if "split" in e else e.get("hal-type", "")

    @staticmethod
    def _type_enabled(e):
        # a bit field needs more than one bit
        return "pin" in e or "split" in e

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
            value = show_value(st.get("value", ""), st.get("type"), self.win.hex_values)
            if "value" not in st and "data" in st:
                value = "data %s" % st["data"]
            for c, v in enumerate((st.get("object"), st.get("name", ""), st.get("type", ""), value)):
                self.st.setItem(r, c, item(v, editable=True, font=mono() if c == 0 else None))
            now = self.present.get((self.position, st.get("object")), "")
            try:
                now = str(show_value(int(now, 0), st.get("type"), self.win.hex_values))
            except ValueError:
                pass
            self.st.setItem(r, 4, item(now, font=mono(), colour=GREY))
        fit_columns(self.st, stretch=1)
        fit_table(self.st, minimum=1)
        self.show_setting_problems(s)
        self.loading = False

    def dictionary_lines(self):
        """"0x8010:01  Maximal current" for each writable object of the dictionary."""
        d = self.win.session.dictionary(self.position)
        return ["0x%04X:%02X  %s" % (i, sub, d.name(i, sub)) for (i, sub), e in sorted(d.entries.items())
                if e.writable_preop and not e.name.startswith(("SubIndex", "Max SubIndex"))]

    def show_setting_problems(self, s):
        """Colour the rows that have a problem and list the problems under the table."""
        found = guidance.check_slave(s, self.win.session.dictionary(self.position))
        lines = []
        for row, probs in found.items():
            level = "error" if any(lv == "error" for lv, _ in probs) else "warning"
            tip = "\n".join(t for _, t in probs)
            for c in range(self.st.columnCount()):
                it = self.st.item(row, c)
                if it is not None:
                    it.setForeground(QBrush(RED if level == "error" else AMBER))
                    it.setToolTip(tip)
            lines.extend("%s: %s" % (s["settings"][row].get("object"), t) for _, t in probs)
        self.st_problems.setText("\n".join(lines))
        self.st_problems.setVisible(bool(lines))

    # -- editing

    def general_edited(self, *_):
        if self.loading:
            return
        s = self.win.session.slave(self.position)
        if s is None:
            return
        old = s.get("name")
        s["name"] = self.name.text().strip()
        if old != s["name"]:
            renamed = integrate.rename_slave(self.win.session.cfg, old, s["name"])
            self.offer_rename_in_hal(renamed)
        if s.get("profile") == "cia402":
            c = s.setdefault("cia402", {})
            c["mode"] = self.mode.currentData()
            others = [m for m, cb in self.other_modes.items() if cb.isChecked() and m != c["mode"]]
            for m, cb in self.other_modes.items():
                cb.setEnabled(m != c["mode"])
            self.show_opmode_hint({"mode": c["mode"], "modes": others})
            if others:
                c["modes"] = others
            else:
                c.pop("modes", None)
            if self.zero_at_start.isChecked():
                c["zero-at-start"] = True
            else:
                c.pop("zero-at-start", None)
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
        self.show_homing(s)
        self.win.changed()

    @staticmethod
    def _invert_text(e):
        inv = e.get("invert")
        if isinstance(inv, list):
            return ", ".join(inv)
        return "yes" if inv else ""

    def pd_edited(self, it):
        if self.loading:
            return
        row, col = it.row(), it.column()
        e = self._entry(row)
        text = it.text().strip()
        if col == self.PIN:
            if text:
                e["pin"] = text
                e.setdefault("hal-type", propose.HAL_TYPES.get(e.get("type"), "sint"))
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
            self._keep_inverted_names(e)
        elif col == self.INVERT:
            if "pin" in e:
                if text.lower() in ("yes", "y", "true", "1") and e.get("hal-type") == "bool":
                    e["invert"] = True
                else:
                    if text and e.get("hal-type") != "bool":
                        self.win.status("Only bit pins can be inverted")
                    e.pop("invert", None)
            elif "split" in e:
                names = [n.strip() for n in text.split(",") if n.strip()]
                bad = [n for n in names if n not in e["split"]]
                if bad:
                    self.win.status("Not a bit pin of this entry: %s" % ", ".join(bad))
                names = [n for n in names if n in e["split"]]
                if names:
                    e["invert"] = names
                else:
                    e.pop("invert", None)
            else:
                e.pop("invert", None)
            self.loading = True
            it.setText(self._invert_text(e))
            self.loading = False
        else:
            return
        self._refresh_row(row, e)
        self.win.changed()

    @staticmethod
    def _keep_inverted_names(e):
        """After the bit pins changed: invert only names that are still there."""
        if isinstance(e.get("invert"), list):
            keep = [n for n in e["invert"] if n in (e.get("split") or [])]
            if keep:
                e["invert"] = keep
            else:
                e.pop("invert", None)

    def pd_double_clicked(self, row, col):
        """A bit field has no pin name of its own: go to its bit pins instead."""
        if col != self.PIN or row >= len(self.pd_rows):
            return
        if "split" in self._entry(row):
            self.pd.setCurrentCell(row, self.SPLIT)
            self.pd.editItem(self.pd.item(row, self.SPLIT))

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
            box.setCurrentText(self._type_text(e))
            box.setEnabled(self._type_enabled(e))
            box.blockSignals(False)
        self.loading = False

    def offer_rename_in_hal(self, renamed):
        """After a rename: offer to rename the pins in the user's HAL files too."""
        ini = self.win.session.ini_path
        if not ini or not os.path.exists(ini):
            return
        try:
            p = integrate.rename_in_hal(ini, renamed)
        except OSError:
            return
        if not p.changes:
            return
        files = "\n".join("%s: %s" % (c.name, c.what) for c in p.changes)
        if QMessageBox.question(
                self, TITLE, "Your HAL files use the old name:\n\n%s\n\nRename the pins there "
                "too? Each file is kept as .bak first; comments are left as they are."
                % files, QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            self.win.status("Your HAL files still use the old name: rename the pins there by "
                            "hand, or the HAL check (HAL & LinuxCNC page) lists them")
            return
        try:
            integrate.apply(p)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, TITLE, "Not renamed:\n%s" % e)
            return
        self.win.status("Renamed in %s" % ", ".join(c.name for c in p.changes))

    def hal_type_edited(self, row, text):
        if self.loading:
            return
        e = self._entry(row)
        if "pin" not in e and "split" not in e:
            return
        if text == self.BIT_FIELD:
            if "split" in e:
                return
            # one bit pin per bit, named <pin>-0, <pin>-1 ... (rename them in the Bit pins column)
            n = min(int(e.get("bits") or 0), 32)
            if n < 2:
                self.win.status("A bit field needs an entry of more than one bit")
                self._refresh_row(row, e)
                return
            base = e.pop("pin")
            for k in ("hal-type", "scale", "offset", "invert"):
                e.pop(k, None)
            e["split"] = ["%s-%d" % (base, i) for i in range(n)]
            self.loading = True
            self.pd.item(row, self.SPLIT).setText(", ".join(e["split"]))
            self.pd.item(row, self.PIN).setText("")
            self.pd.item(row, self.SCALE).setText("")
            self.pd.item(row, self.OFFSET).setText("")
            self.loading = False
        elif "split" in e:
            # back to one pin for the whole entry
            first = (next((p for p in e["split"] if p), None) or "pin-0")
            base = first[:-2] if first.endswith("-0") else first
            e.pop("split")
            e.pop("invert", None)
            e["pin"] = base
            e["hal-type"] = text or propose.HAL_TYPES.get(e.get("type"), "sint")
            self.loading = True
            self.pd.item(row, self.SPLIT).setText("")
            self.pd.item(row, self.PIN).setText(base)
            self.loading = False
        else:
            e["hal-type"] = text
            if text != "bool":
                e.pop("invert", None)
        self.loading = True
        self.pd.item(row, self.INVERT).setText(self._invert_text(e))
        self.loading = False
        self._refresh_row(row, e)
        self.win.changed()

    def st_edited(self, it):
        if self.loading:
            return
        s = self.win.session.slave(self.position)
        st = s["settings"][it.row()]
        text = it.text().strip()
        col = it.column()
        if col == 0:
            text = text.split()[0] if text else text     # a completion: "0x8010:01  Name"
            st["object"] = text
            try:
                index, sub = config.parse_obj(text)
            except ValueError:
                self.win.changed()
                return
            d = self.win.session.dictionary(self.position)
            entry = d.entries.get((index, sub))
            if entry:
                st["name"] = d.name(index, sub)
                st["type"] = propose.COE_TYPES.get(entry.data_type, st.get("type", ""))
                if "value" in st and st["value"] in (0, "") and entry.default is not None:
                    st["value"] = entry.default
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
        self.loading = True
        self.show_setting_problems(s)
        self.loading = False
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

    def choose_settings(self):
        """Add settings chosen from the slave's dictionary."""
        s = self.win.session.slave(self.position)
        if s is None:
            return
        d = self.win.session.dictionary(self.position)
        if not d.entries:
            self.win.status("No object dictionary (the slave was not read and no ESI file matches): "
                            "type the object")
            self.add_setting()
            return
        have = set()
        for st in s.get("settings") or []:
            try:
                have.add(config.parse_obj(st.get("object")))
            except ValueError:
                pass
        dlg = SettingPicker(self, s, d, have)
        if dlg.exec_() != QDialog.Accepted:
            return
        self.add_entries(s, dlg.chosen(), dlg.read_now.isChecked())

    def add_entries(self, s, entries, from_slave=True):
        """Add dictionary entries as settings, valued as the slave has them now,
        else the ESI default, else 0."""
        d = self.win.session.dictionary(self.position)
        present = self.win.session.bus.get(self.position) is not None
        for e in entries:
            if guidance.command_reason(s, e.index, e) and guidance.command_reason(s, e.index, e)[0] == "error":
                continue
            ctype = guidance.config_type(e) or "UDINT"
            value = None
            if from_slave and present and e.data_type != "string":
                try:
                    value = tool.upload(self.position, e.index, e.subindex, e.data_type)
                except (tool.ToolError, OSError):
                    value = None
            if value is None:
                value = e.default if e.default is not None else 0
            st = {"object": config.obj(e.index, e.subindex), "name": d.name(e.index, e.subindex),
                  "type": ctype, "value": float(value) if ctype in ("REAL", "LREAL") else value}
            s.setdefault("settings", []).append(st)
        self.show_settings(s)
        self.win.changed()

    def read_present_values(self):
        """Fill the Now column from the slave."""
        s = self.win.session.slave(self.position)
        if s is None:
            return
        for st in s.get("settings") or []:
            try:
                index, sub = config.parse_obj(st.get("object"))
            except ValueError:
                continue
            ttype = guidance.tool_type(st.get("type"))
            text = ""
            if ttype:
                try:
                    v = tool.upload(self.position, index, sub, ttype)
                except (tool.ToolError, OSError):
                    v = None
                text = "" if v is None else str(v)
            self.present[(self.position, st.get("object"))] = text or "?"
        self.show_settings(s)
        self.win.status("Read the settings' present values from slave %s" % self.position)

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


# -- the Slave status page

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
        self.slave = QuietCombo()
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
        self.report = QPushButton("Save report...")
        self.report.setToolTip("Write one file to attach when asking for help: versions, the "
                               "configuration, the INI lines, a diagnosis of the bus and the "
                               "event logs. Only reads; nothing is sent anywhere.")
        self.report.clicked.connect(win.save_report)
        row.addWidget(self.report)
        lay.addLayout(row)

        flow = Card("Something seems wrong in a running machine",
                    "What to do, in this order. None of it changes anything on the machine.")
        flow_text = flow.add(label(wrap=True))
        flow_text.setTextFormat(Qt.RichText)
        flow_text.setText(symptoms.flow_html())
        flow_row = QHBoxLayout()
        report2 = QPushButton("Save report...")
        report2.clicked.connect(win.save_report)
        flow_row.addWidget(report2)
        self.watch_button = QPushButton("Watch LinuxCNC (read only)")
        self.watch_button.clicked.connect(lambda: win.start_watch())
        flow_row.addWidget(self.watch_button)
        flow_row.addStretch(1)
        flow.lay.addLayout(flow_row)
        lay.addWidget(flow)

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

        # the flight recorder: events of the bus while LinuxCNC ran
        self.events = Card("Event log", "What the ecat component recorded: link, states, "
                                        "working counter, drive faults, the stop guard. It is "
                                        "kept after LinuxCNC stops; the run before is kept too.")
        er = QHBoxLayout()
        self.events_run = QuietCombo()
        self.events_run.addItem("This run (or the last)", events.PATH)
        self.events_run.addItem("The run before", events.PATH + ".prev")
        self.events_run.currentIndexChanged.connect(lambda _i: self.show_events())
        er.addWidget(self.events_run)
        self.events_slave = QCheckBox("Only this slave and the master")
        self.events_slave.toggled.connect(lambda _on: self.show_events())
        er.addWidget(self.events_slave)
        er.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.show_events)
        er.addWidget(refresh)
        self.events.lay.addLayout(er)
        self.events_info = self.events.add(label("", "hint", wrap=True))
        self.events_table = table(["Time", "", "Event"], 2)
        self.events.add(self.events_table)
        lay.addWidget(self.events)
        guide = Card("Symptoms", "What is seen in LinuxCNC, what it usually comes from, and "
                                 "where to look here.")
        self.guide_text = guide.add(label(wrap=True))
        self.guide_text.setTextFormat(Qt.RichText)
        self.guide_text.setText(symptoms.html())
        lay.addWidget(guide)
        lay.addStretch(1)
        self.events_timer = QTimer(self)
        self.events_timer.setInterval(2000)
        self.events_timer.timeout.connect(self.show_events)

    def showEvent(self, ev):
        super().showEvent(ev)
        self.show_events()
        self.events_timer.start()

    def hideEvent(self, ev):
        self.events_timer.stop()
        super().hideEvent(ev)

    def show_events(self):
        """Fill the event log card, newest first."""
        path = self.events_run.currentData()
        try:
            log = events.read(path)
        except events.EventsError as e:
            self.events_info.setText(str(e))
            self.events_table.setRowCount(0)
            fit_table(self.events_table, minimum=1)
            return
        names = events.names_of(self.win.session.cfg)
        shown = log.events
        pos = self.win.current if isinstance(self.win.current, int) else None
        if self.events_slave.isChecked() and pos is not None:
            shown = [e for e in shown if e.slave in (-1, pos)]
        self.events_info.setText("ecat %s since %s, %s%s" % (
            "runs" if log.running else "ran", events.stamp(log.start), log.config or "?",
            "; %d older events were overwritten" % log.lost if log.lost > 0 else ""))
        rows = list(reversed(shown[-200:]))
        t = self.events_table
        t.setRowCount(len(rows))
        colours = {"info": None, "warn": QColor("#b26a00"), "error": QColor("#c62828")}
        for r, e in enumerate(rows):
            sev, msg = events.text(e, names)
            t.setItem(r, 0, item(events.stamp(e.time), font=mono()))
            t.setItem(r, 1, item({"info": "", "warn": "WARN", "error": "ERROR"}[sev],
                                 colour=colours[sev]))
            t.setItem(r, 2, item(msg, colour=colours[sev]))
        fit_table(t, minimum=1)

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
            short = name[len(prefix):] if name.startswith(prefix) else name   # an axis's pin
            for c, v in enumerate((short, pin.type, pin.value)):
                self.setItem(r, c, item(v, font=mono() if c == 2 else None))
            if run.writable(pin):
                self.setCellWidget(r, 3, self._setter(pin))
        fit_table(self)

    def _setter(self, pin):
        if pin.type == "bool":
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


LANES = {"csp": [("position", "mm"), ("following error", "mm"), ("velocity", "mm/s")],
         "pp": [("position", "mm"), ("velocity", "mm/s")],
         "pv": [("position", "mm"), ("velocity", "mm/s")]}


def plot_values(mode, v, start):
    """{lane: value} of a drive's pins (v(name) -> text or None) for the
    plot: the position from where the test started, the following error
    (CSP: the command less the feedback) and the velocity."""
    try:
        fb = float(v("pos-fb"))
    except (TypeError, ValueError):
        return {}
    out = {"position": fb - start}
    if mode == "csp":
        try:
            out["following error"] = float(v("pos-cmd")) - fb
        except (TypeError, ValueError):
            pass
    try:
        out["velocity"] = float(v("velocity-fb"))
    except (TypeError, ValueError):
        pass
    return out


class Plot(QWidget):
    """The last seconds of a drive's position, following error and velocity,
    one strip each with its own scale."""

    SECONDS = 10.0
    COLOURS = {"position": "#2563eb", "following error": "#b42318", "velocity": "#1a7f37"}

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(170)
        self.lanes = []
        self.samples = []      # (time, {lane: value})

    def reset(self, mode):
        self.lanes = LANES.get(mode, LANES["csp"])
        self.samples = []
        self.update()

    def add(self, values, now=None):
        now = time.monotonic() if now is None else now
        if values:
            self.samples = [x for x in self.samples if now - x[0] <= self.SECONDS] + [(now, values)]
            self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        n = max(len(self.lanes), 1)
        h, w = self.height() / n, self.width()
        p.setFont(mono())
        now = self.samples[-1][0] if self.samples else 0.0
        for i, (lane, unit) in enumerate(self.lanes):
            top = i * h
            pts = [(t, v[lane]) for t, v in self.samples if lane in v]
            p.setPen(QPen(QColor("#9ca3af"), 1))
            p.drawLine(0, int(top + h - 1), w, int(top + h - 1))
            if not pts:
                continue
            lo, hi = min(x[1] for x in pts), max(x[1] for x in pts)
            if hi - lo < 1e-9:
                lo, hi = lo - 0.5, hi + 0.5
            pad = (hi - lo) * 0.1
            lo, hi = lo - pad, hi + pad
            line = QPen(QColor(self.COLOURS.get(lane, "#000000")), 2)
            p.setPen(line)
            prev = None
            for t, val in pts:
                x = w - (now - t) / self.SECONDS * (w - 150) - 0
                y = top + 4 + (hi - val) / (hi - lo) * (h - 10)
                if prev:
                    p.drawLine(int(prev[0]), int(prev[1]), int(x), int(y))
                prev = (x, y)
            p.setPen(QPen(QColor(self.COLOURS.get(lane, "#000000"))))
            p.drawText(6, int(top + 14), "%s %+.3f %s" % (lane, pts[-1][1], unit))
            p.drawText(6, int(top + h - 5), "%+.3f..%+.3f" % (lo + pad, hi - pad))
        p.end()


class MoveCard(Card):
    """Moving one drive: limits written to it, hold-to-enable, and by its
    mode: jog (CSP), steps (PP) or run (PV); home if it has hm."""

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
        self.plot_for = None
        # before: the limits
        self.setup = QWidget()
        form = QFormLayout(self.setup)
        form.setContentsMargins(0, 0, 0, 0)
        self.fields = {}
        self.axis = QuietCombo()
        self.axis.setToolTip("The drive has several axes: the one to move")
        self.axis.currentIndexChanged.connect(lambda _i: self.slave and self.show_drive(self.slave))
        form.addRow("Axis", self.axis)
        self.axis_row = form
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
        self.hold = QPushButton("Hold to enable (no motion)")
        self.hold.setObjectName("hold")
        self.hold.setToolTip("Enables the drive while held, without moving it: to see that it "
                             "reaches \"Operation enabled\". Not needed for jogging.")
        self.hold.pressed.connect(lambda: self.enable(True))
        self.hold.released.connect(lambda: self.enable(False))
        ml.addWidget(self.hold)
        ml.addWidget(label("Every button here enables the drive only while it is held; release "
                           "to stop. Use one at a time: the jog buttons enable the drive by "
                           "themselves.", "hint", wrap=True))
        self.jog_row = QWidget()
        jog = QHBoxLayout(self.jog_row)
        jog.setContentsMargins(0, 0, 0, 0)
        self.jog_minus = QPushButton("– Jog")
        self.jog_plus = QPushButton("Jog +")
        for b, sign in ((self.jog_minus, -1), (self.jog_plus, 1)):
            b.pressed.connect(lambda s=sign: self.jog(s))
            b.released.connect(lambda: self.enable(False))
            jog.addWidget(b)
        jog.addWidget(label("Step", "hint"))
        self.step = QuietCombo()
        for st in ("0.1", "1", "5"):
            self.step.addItem("%s mm" % st, float(st))
        self.step.setCurrentIndex(1)
        jog.addWidget(self.step)
        ml.addWidget(self.jog_row)
        self.run_row = QWidget()
        rr = QHBoxLayout(self.run_row)
        rr.setContentsMargins(0, 0, 0, 0)
        self.run_minus = QPushButton("– Run")
        self.run_plus = QPushButton("Run +")
        for b, sign in ((self.run_minus, -1), (self.run_plus, 1)):
            b.setToolTip("Hold alone: enables the drive and runs it at the max velocity; release "
                         "to stop (it ramps down with its own deceleration)")
            b.pressed.connect(lambda s=sign: self.run_velocity(s))
            b.released.connect(lambda: self.enable(False))
            rr.addWidget(b)
        rr.addStretch(1)
        ml.addWidget(self.run_row)
        self.home_button = QPushButton("Home (hold)")
        self.home_button.setToolTip("Hold alone: enables the drive and homes it by its own "
                                    "method (0x6098); releasing stops the homing")
        self.home_button.pressed.connect(self.home)
        self.home_button.released.connect(lambda: self.enable(False))
        ml.addWidget(self.home_button)
        self.home_info = label("", "hint", wrap=True)
        ml.addWidget(self.home_info)
        self.home_ok = QCheckBox("The homing may move the axis")
        self.home_ok.setToolTip("The drive's homing method drives the axis to a switch or a stop. "
                                "Tick it only when the axis is free to move that way and the "
                                "emergency stop is within reach.")
        self.home_ok.toggled.connect(self.home_allowed)
        ml.addWidget(self.home_ok)
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
        self.plot = Plot()
        ml.addWidget(self.plot)
        self.add(self.moving)
        self.lay.addStretch(1)

    def axis_name(self):
        """The pin prefix of the axis chosen (the slave's name for one axis)."""
        names = config.axes(self.slave)
        i = self.axis.currentIndex() if self.axis.count() == len(names) else 0
        return names[max(i, 0)] if names else None

    def test(self):
        return self.win.drive_tests.get(self.axis_name()) if self.slave else None

    def show_drive(self, slave):
        names = config.axes(slave)
        if slave is not self.slave or self.axis.count() != len(names):
            self.axis.blockSignals(True)
            self.axis.clear()
            self.axis.addItems(names)
            self.axis.blockSignals(False)
        self.axis.setVisible(len(names) > 1)
        self.axis_row.labelForField(self.axis).setVisible(len(names) > 1)
        self.slave = slave
        run = self.win.test
        ok = run is not None and not run.watching and drivetest.testable(slave)
        dt = self.test()
        mode = drivetest.mode_of(slave)
        self.setup.setVisible(ok and (dt is None or not dt.prepared))
        self.moving.setVisible(dt is not None and dt.prepared)
        homes = dt is not None and dt.prepared and dt.homes
        self.home_info.setVisible(bool(homes))
        stays = homes and dt.homing_method in drivetest.HOMING_STAYS
        self.home_ok.setVisible(bool(homes) and not stays)
        if homes:
            self.home_info.setText("Homing: " + drivetest.homing_text(dt.homing_method))
            self.home_ok.blockSignals(True)
            self.home_ok.setChecked(dt.home_moves_ok)
            self.home_ok.blockSignals(False)
        if dt is not None and dt.prepared and self.plot_for != (dt.name, dt.mode):
            self.plot_for = (dt.name, dt.mode)
            self.plot.reset(dt.mode)
        self.jog_row.setVisible(mode in ("csp", "pp"))
        self.run_row.setVisible(mode == "pv")
        self.home_button.setVisible(drivetest.can_home(slave))
        tip = ("Hold alone (not with Hold to enable): enables the drive and moves one step; "
               "hold until the step is done, release to stop")
        if mode == "pp":
            tip += ". In PP the drive plans the step itself, from where it stands"
        for b in (self.jog_minus, self.jog_plus):
            b.setToolTip(tip)
        if run is not None and run.watching:
            self.unsupported.setText("LinuxCNC is only watched: nothing here moves the drive.")
        elif not ok:
            self.unsupported.setText("Only cia402 drives in CSP, PP or PV, or with hm in their "
                                     "modes, can be moved in the test.")
        else:
            self.unsupported.setText("")
            if dt is None:
                scale = drivetest.hal_scale(os.path.dirname(os.path.abspath(
                    self.win.session.ini_path)) if self.win.session.ini_path else "",
                    self.axis_name())
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
        if self.fields["scale"].value() == 1.0 and QMessageBox.question(
                self, TITLE, "The scale is 1 count per mm, so a 1 mm step is 1 count and nothing "
                "seems to move (the velocity is in counts too). Enter the drive's counts per "
                "mm in the Scale field first.\n\nContinue with a scale of 1?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        b = self.win.session.bus.get(s["position"])
        dt = drivetest.DriveTest(run, s, b.coe if b else None, self.settings(),
                                 axis=max(self.axis.currentIndex(), 0))
        try:
            dt.prepare()
        except (testmode.TestError, tool.ToolError) as e:
            QMessageBox.warning(self, TITLE, "The drive was not prepared:\n%s" % e)
            return
        self.win.drive_tests[dt.name] = dt
        self.show_drive(s)

    def do_restore(self):
        dt = self.test()
        if dt is None:
            return
        failed = dt.restore()
        del self.win.drive_tests[dt.name]
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
            self.win.tested_drives.add(dt.name)
        except testmode.TestError as e:
            self.win.status(str(e))

    def run_velocity(self, sign):
        dt = self.test()
        if dt is None:
            return
        try:
            dt.enable(True)
            dt.run_velocity(sign)
        except testmode.TestError as e:
            self.win.status(str(e))

    def home_allowed(self, on):
        dt = self.test()
        if dt is not None:
            dt.home_moves_ok = on

    def home_finished(self, dt, result):
        """A homing ended: the Home button says how it went, until the next one."""
        ok = result == "homed"
        if self.test() is dt:
            self.home_state("home-ok" if ok else "home-failed",
                            "✓ Homed: position is 0" if ok else "✕ Homing failed")
        self.win.status("%s: homing %s%s" % (dt.name, "done" if ok else "failed",
                        "; the position is the homed one and the drive is disabled" if ok else
                        " (the drive reported an error)"))

    def home_state(self, name, text):
        self.home_button.setObjectName(name)
        self.home_button.setText(text)
        self.home_button.style().unpolish(self.home_button)
        self.home_button.style().polish(self.home_button)

    def home(self):
        dt = self.test()
        if dt is None:
            return
        self.home_state("", "Home (hold)")
        try:
            dt.enable(True)
            dt.home()
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
        self.plot.add(plot_values(dt.mode, v, dt.start))
        try:
            fb = float(v("pos-fb"))
        except (TypeError, ValueError):
            return
        if dt.mode == "pv":
            text = "position %+.3f mm from start, velocity %s mm/s" % (
                fb - dt.start, v("velocity-fb") or "?")
        else:
            text = "position %+.3f mm from start, target %+.3f mm" % (
                fb - dt.start, dt.target - dt.start)
            if dt.mode == "pp":
                text += ", done" if v("pp-done") == "TRUE" else ""
        if dt.homes:
            text += "\nhoming: %s" % ("in progress" if v("stat-homing") == "TRUE" else
                                       "error" if v("home-error") == "TRUE" else
                                       "homed" if v("stat-homed") == "TRUE" else "not homed")
        self.where.setText(text)


def sync_rows(cfg, get, offsets, period):
    """[(ok, text)] about the bus's synchronisation while the test runs.
    get(name): an ecat.0 pin's value as text, or None; offsets: send-offset
    samples (ns); period: the servo period (ns)."""
    rows = []
    all_op = get("all-op") == "TRUE"
    rows.append((all_op, "All slaves in OP" if all_op else "Not all slaves in OP"))
    m = (cfg or {}).get("master") or {}
    dc_slaves = [s for s in (cfg or {}).get("slaves") or [] if s.get("dc")]
    if not dc_slaves:
        rows.append((None, "No slave uses distributed clocks: no SYNC0 to place"))
        return rows
    if get("dc-sync-failed") == "TRUE":
        rows.append((False, "The master's DC sync check timed out for a slave: its clock was "
                     "not within 10 us when SYNC0 started (Troubleshoot, Event log)"))
    else:
        rows.append((True, "DC: %d slave%s, clocks in step" % (
            len(dc_slaves), "" if len(dc_slaves) == 1 else "s")))
    follows = ((m.get("dc") or {}).get("servo-thread-follows-ref-clock") is True)
    if follows:
        phased = get("dc-phased") == "TRUE"
        rows.append((phased, "The servo thread follows the reference clock%s (pll-err %s ns)"
                     % ("" if phased else ": not locked yet", get("pll-err") or "?")))
    else:
        rows.append((True, "The slaves follow the servo thread (the servo period is not adjusted; "
                     "pll-err %s ns is only measured)" % (get("pll-err") or "?")))
    try:
        shift = int(get("shift-time") or 0)
    except ValueError:
        shift = 0
    try:
        turn = int(get("turnaround-cycles") or 0)
    except ValueError:
        turn = 0
    auto = (m.get("dc") or {}).get("sync0-shift-ns") == "auto"
    if auto and turn == 0:
        rows.append((None, "SYNC0 shift: measuring the frame's timing (2 s after all OP)"))
    elif turn == 2 or not auto:
        rows.append((True if turn in (0, 2) else False,
                     "SYNC0 %d us after the start of the cycle%s" % (
                         shift // 1000, ", turn-around 2 cycles" if turn == 2 else "")))
    else:
        rows.append((False, "Turn-around %d cycles, SYNC0 at %d us: the frame's timing varies "
                     "too much for 2 (servo thread latency); drive profiles count on 2"
                     % (turn, shift // 1000)))
    if len(offsets) >= 5 and period:
        lo, hi = min(offsets), max(offsets)
        ok = hi - lo < period // 4
        rows.append((ok, "Frame sent %d..%d us into the cycle (%d samples)%s" % (
            lo // 1000, hi // 1000, len(offsets),
            "" if ok else ": it varies by more than a quarter cycle, check the latency")))
    return rows


class TestPage(QWidget):
    def show_sync(self, get, period):
        """Fill the synchronisation card from the ecat.0 pins (get)."""
        try:
            self.offsets = (self.offsets + [int(get("send-offset"))])[-50:]
        except (TypeError, ValueError):
            pass
        rows = sync_rows(self.win.session.cfg, get, self.offsets, period)
        for (mark, text), row in zip(self.sync_rows, rows + [None] * len(self.sync_rows)):
            mark.setVisible(row is not None)
            text.setVisible(row is not None)
            if row is None:
                continue
            ok, t = row
            set_pill(mark, "✓" if ok else "!" if ok is False else "–",
                     "green" if ok else "red" if ok is False else "grey")
            text.setText(t)
        self.sync_card.show()

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
        self.watch = QPushButton("Watch LinuxCNC that runs (read only)")
        self.watch.setToolTip("Show the pins of the ecat component in a running LinuxCNC, live. "
                              "Nothing is set; the machine is not touched.")
        self.watch.clicked.connect(win.start_watch)
        row.addWidget(self.watch)
        self.start_card.add(row)
        self.starting = QWidget()
        sl = QVBoxLayout(self.starting)
        sl.setContentsMargins(0, 0, 0, 0)
        self.starting_text = label("", "mono", wrap=True)
        sl.addWidget(self.starting_text)
        bar = QProgressBar()
        bar.setRange(0, 0)          # busy: the number of steps is known, not how long each takes
        bar.setTextVisible(False)
        bar.setMaximumHeight(8)
        sl.addWidget(bar)
        self.starting.hide()
        self.start_card.add(self.starting)
        lay.addWidget(self.start_card)

        # synchronisation, while the test runs
        self.sync_card = Card("Synchronisation", "Distributed clocks, where SYNC0 falls in the "
                                                 "cycle and when the frame is sent; from the "
                                                 "component's ecat.0 pins.")
        self.sync_rows = []
        for _ in range(7):
            r = QHBoxLayout()
            mark = pill()
            mark.setFixedWidth(26)
            mark.setAlignment(Qt.AlignCenter)
            text = label(wrap=True)
            r.addWidget(mark)
            r.addWidget(text, 1)
            self.sync_card.add(r)
            self.sync_rows.append((mark, text))
        self.offsets = []
        lay.addWidget(self.sync_card)
        self.sync_card.hide()

        row = QHBoxLayout()
        self.title = label("", "bigTitle")
        row.addWidget(self.title)
        self.mode_pill = pill("", "blue")
        row.addWidget(self.mode_pill)
        self.state = pill()
        row.addWidget(self.state)
        row.addStretch(1)
        self.slave = QuietCombo()
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
        sdo = self.sdo_card = Card("SDO (CoE object)", "Read and write whenever a master runs: "
                                   "the test, or the free-running master.")
        form = QHBoxLayout()
        self.sdo_obj = QLineEdit()
        self.sdo_obj.setPlaceholderText("0x2000:01")
        self.sdo_obj.setMaximumWidth(110)
        self.sdo_obj.editingFinished.connect(self.sdo_object_typed)
        form.addWidget(self.sdo_obj)
        self.sdo_type = QuietCombo()
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
        run_cmd = QPushButton("Run command...")
        run_cmd.setToolTip("For an object that does something when written (zero position, "
                           "store parameters): once, on request, never at start")
        run_cmd.clicked.connect(self.sdo_run_command)
        form.addWidget(run_cmd)
        self.hex_box = QCheckBox("Hex")
        self.hex_box.setToolTip("Show a value read as 0x hex instead of decimal. You can write "
                                "a value in either form: 255 or 0xFF")
        self.hex_box.setChecked(self.win.hex_values)
        self.hex_box.toggled.connect(self.win.set_hex_values)
        form.addWidget(self.hex_box)
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

    def _axis_prefix(self):
        """The drive axis's pin prefix, when it is not the slave's name (a
        drive with several axes); else None."""
        s = self.win.session.slave(self.position) if isinstance(self.position, int) else None
        if not s or len(config.axes(s)) < 2 or self.move.slave is not s:
            return None
        return self.move.axis_name() + "."

    def _pins(self, run, prefix):
        pins = run.pins(prefix)
        axis = self._axis_prefix()
        if axis:
            pins.update(run.pins(axis))
        return pins

    def fill_slaves(self):
        win = self.win
        self.slave.blockSignals(True)
        self.slave.clear()
        for pos in win.positions():
            self.slave.addItem("Slave %d · %s" % (pos, win.device(pos)), pos)
        self.slave.setCurrentIndex(self.slave.findData(win.current))
        self.slave.blockSignals(False)
        self.start.setEnabled(win.session.cfg is not None and not win.busy)
        self.watch.setEnabled(win.session.cfg is not None and not win.busy)

    def show_slave(self):
        """Lay the page out for the chosen slave and the test's state."""
        win, run = self.win, self.win.test
        self.fill_slaves()
        running = run is not None and run.running
        self.banner.setVisible(running)
        self.start_card.setVisible(not running)
        # while watching, LinuxCNC's master owns the bus: nothing is read or written from here
        watching = running and run.watching
        self.sdo_card.setEnabled(not watching)
        self.sdo_card.hint.setText("Not while watching: LinuxCNC's master owns the bus."
                                   if watching else "Read and write whenever a master runs: "
                                   "the test, or the free-running master.")
        if running and run.watching:
            self.banner_text.setText(
                "<b>Watching LinuxCNC</b> &nbsp; The pins are read live from its HAL, every "
                "200 ms. Nothing is set, started or stopped from here; STOP (top right) ends "
                "the watching only.")
        elif running:
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
        if is402:
            self.move.show_drive(s)     # first: it knows the axis
        try:
            pins = self._pins(run, prefix)
        except testmode.TestError as e:
            win.status(str(e))
            return
        if is402:
            self.all_pins.fill(pins, prefix, run)
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
                pins = self._pins(run, prefix)
            except testmode.TestError:
                return
            if (self.position, prefix, frozenset(pins)) != self.shown_for:
                self.show_slave()
                return

        axis = self._axis_prefix()

        def v(name):
            p = (pins.get(axis + name) if axis else None) or pins.get(prefix + name)
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
            # "0x0010 16": hex and decimal
            text = text.split()[0 if self.win.hex_values else -1]
        self.sdo_value.setText(text)
        self.win.status("0x%04X:%02X = %s" % (t[0], t[1], text))

    def sdo_run_command(self):
        """Run a command object once: confirmed, and not while a drive is enabled."""
        t = self._sdo_target()
        pos = self.position
        s = self.win.session.slave(pos) if isinstance(pos, int) and pos >= 0 else None
        if not t or s is None:
            return
        entry = self.win.session.dictionary(pos).entries.get(t)
        known = quirks.command_run(s, *t) or {}
        reason = guidance.command_reason(s, t[0], entry)
        if reason is None:
            QMessageBox.information(
                self, TITLE, "0x%04X:%02X is not known as a command. Use Write for an ordinary "
                "object; to make this one a command, list it under commands: in a quirk file "
                "(docs/ecatsetup.md)." % t)
            return
        value = str(known["value"]) if known.get("value") is not None else \
            self.sdo_value.text().strip()
        if not value:
            self.win.status("Type the value the command takes")
            return
        dtype = known.get("type") or self.sdo_type.currentData()
        try:
            enabled = testmode.drives_enabled(self.win.session.cfg, tool.upload)
        except (tool.ToolError, OSError) as e:
            QMessageBox.warning(self, TITLE, "Could not tell whether a drive is enabled (%s): "
                                "not run." % e)
            return
        if enabled:
            QMessageBox.warning(self, TITLE, "Not run: %s %s enabled. Disable the drive first."
                                % (", ".join(enabled), "is" if len(enabled) == 1 else "are"))
            return
        if QMessageBox.question(
                self, TITLE, "Run the command 0x%04X:%02X on slave %d, writing %s?\n\n%s\n%s\n\n"
                "It runs once, now; it is not written at start." % (
                    t[0], t[1], pos, value, reason[1], known.get("confirm") or ""),
                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        try:
            tool.download(pos, t[0], t[1], value, dtype)
        except tool.ToolError as e:
            QMessageBox.warning(self, TITLE, "The slave refused:\n%s" % e)
            return
        self.win.status("Ran command 0x%04X:%02X = %s on slave %d" % (t[0], t[1], value, pos))

    def sdo_write(self):
        t = self._sdo_target()
        value = self.sdo_value.text().strip()
        pos = self.position
        s = self.win.session.slave(pos) if isinstance(pos, int) and pos >= 0 else None
        if not t or not value or s is None:
            return
        why = testmode.sdo_write_refused(s, *t)
        if why:
            QMessageBox.warning(self, TITLE, "Not written: %s%s" % (
                why, "\n\nUse Run command for a command." if quirks.command(s, t[0]) else ""))
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
                                         "%s.%s" % (name, p), "bool", way))
        names = config.axes(s)
        for n, a in enumerate(names):
            rows.append((s["position"], "CiA 402 axis %d" % n if len(names) > 1 else "CiA 402 profile",
                         "%s.*" % a, "", "see docs/component.md"))
        for ch in s.get("encoders") or []:
            prefix = "%s.%s." % (name, ch.get("name"))
            pins = sorted((p, t, d) for p, (t, d) in halpins.pins(cfg).items()
                          if p.startswith(prefix) and d in ("in", "out", "io"))
            for p, t, d in pins:
                rows.append((s["position"], "encoder %s" % ch.get("index"), p, t,
                             {"in": "to the slave", "out": "from the slave",
                              "io": "both ways"}[d]))
            if not pins:
                rows.append((s["position"], "encoder %s" % ch.get("index"), prefix + "*", "",
                             "see docs/component.md"))
    return rows


class JointRow(QWidget):
    """One joint of the INI file and the drive it is connected to."""

    def __init__(self, page, joint, letter, jtype):
        super().__init__()
        self.page, self.joint = page, joint
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 4)
        badge = label(letter or "–", "axisBadge")
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedSize(34, 34)
        lay.addWidget(badge)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(label("joint.%d" % joint, "jointName"))
        text.addWidget(label(jtype.lower(), "hint"))
        lay.addLayout(text)
        lay.addSpacing(12)
        lay.addWidget(label("→", "hint"))
        self.drive = QuietCombo()
        self.drive.setMinimumWidth(220)
        self.drive.currentIndexChanged.connect(self.edited)
        lay.addWidget(self.drive)
        lay.addWidget(label("counts per unit", "hint"))
        self.scale = QDoubleSpinBox()
        self.scale.setRange(-1e9, 1e9)
        self.scale.setDecimals(6)
        self.scale.setMinimumWidth(130)
        self.scale.setToolTip("The drive's pos-scale: counts per machine unit (mm or degree)")
        self.scale.valueChanged.connect(self.edited)
        lay.addWidget(self.scale)
        self.calc = QPushButton("Calculate...")
        self.calc.setToolTip("The scale from the drive's counts per motor revolution and the "
                             "screw's or pulley's lead")
        self.calc.clicked.connect(self.calculate)
        lay.addWidget(self.calc)
        self.tested = pill("", "green")
        lay.addWidget(self.tested)
        lay.addStretch(1)

    def fill(self, cfg, tested):
        """Fill from the configuration without editing it."""
        self.drive.blockSignals(True)
        self.scale.blockSignals(True)
        self.drive.clear()
        self.drive.addItem("not connected here", None)
        for name in integrate.drives(cfg):
            self.drive.addItem(name, name)
        j = next((j for j in integrate.joints(cfg) if j.get("joint") == self.joint), None)
        i = self.drive.findData(j["slave"]) if j else 0
        self.drive.setCurrentIndex(max(i, 0))
        self.scale.setValue(float(j.get("scale", 1.0)) if j else 1.0)
        self.scale.setEnabled(j is not None)
        self.calc.setEnabled(j is not None)
        name = j["slave"] if j else None
        set_pill(self.tested, "moved in the test" if name in tested else "", "green")
        self.drive.blockSignals(False)
        self.scale.blockSignals(False)

    def calculate(self):
        """Counts per unit from counts per revolution, lead and gear."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Scale of joint.%d - %s" % (self.joint, TITLE))
        form = QFormLayout(dlg)
        boxes = {}
        for key, text, value, dec, tip in (
                ("counts", "Counts per motor revolution", 10000, 0,
                 "What the drive counts per revolution in its position units: for a stepper "
                 "drive its pulses per revolution setting, for a servo its encoder resolution "
                 "(0x608F) through its gear (0x6091)"),
                ("lead", "Units per screw revolution", 5, 4,
                 "The lead of the screw, or the travel per pulley revolution, in machine units "
                 "(mm or inch; degrees for a rotary axis)"),
                ("gear", "Motor revolutions per screw revolution", 1, 4,
                 "The belt or gear reduction; 1 for a motor on the screw")):
            b = QDoubleSpinBox()
            b.setRange(0, 1e9)
            b.setDecimals(dec)
            b.setValue(value)
            b.setToolTip(tip)
            form.addRow(text, b)
            boxes[key] = b
        result = label("", "mono")
        form.addRow("Counts per unit", result)

        def show():
            try:
                result.setText("%.6g" % integrate.scale_from(
                    boxes["counts"].value(), boxes["lead"].value(), boxes["gear"].value()))
            except ValueError as e:
                result.setText(str(e))
        for b in boxes.values():
            b.valueChanged.connect(show)
        show()
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        if dlg.exec_() != QDialog.Accepted:
            return
        try:
            value = integrate.scale_from(boxes["counts"].value(), boxes["lead"].value(),
                                         boxes["gear"].value())
        except ValueError:
            return
        self.scale.setValue(value)

    def edited(self, *_):
        cfg = self.page.win.session.cfg
        if cfg is None:
            return
        name = self.drive.currentData()
        scale = self.scale.value()
        if name and scale == 0:
            scale = 1.0
        if name and not any(j.get("joint") == self.joint for j in integrate.joints(cfg)):
            # a drive newly chosen: its scale as the test or the HAL files had it
            known = self.page.known_scales.get(name)
            if known:
                scale = known
        integrate.set_joint(cfg, self.joint, name, scale)
        self.page.win.changed()


class HalPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.rows = []
        self.rows_for = None
        self.known_scales = {}
        self.proposal = {}
        self.plan = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        # one column, in the order of the work: joints, pins, the checks
        # (HAL check and the file side by side), and writing last, full width
        # for the pins table and the changes
        lay = QVBoxLayout(content)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(16)
        outer.addWidget(scroll(content))

        self.axes = Card("Joints", "Choose the drive for each joint.")
        self.no_ini = QWidget()
        nl = QHBoxLayout(self.no_ini)
        nl.setContentsMargins(0, 0, 0, 0)
        nl.addWidget(label("Choose the machine's INI file to connect its joints to drives.",
                           "hint"))
        choose = QPushButton("INI file...")
        choose.clicked.connect(win.choose_ini)
        nl.addWidget(choose)
        nl.addStretch(1)
        self.axes.add(self.no_ini)
        # LinuxCNC writes the .expanded file again at every start
        self.expanded_warning = self.axes.add(label(wrap=True))
        self.expanded_warning.setStyleSheet("color: #b26a00;")
        self.expanded_warning.hide()
        self.rows_box = QVBoxLayout()
        self.axes.lay.addLayout(self.rows_box)
        row = QHBoxLayout()
        self.take_over = QPushButton("Take over from the HAL files")
        self.take_over.setToolTip("Connect the joints to the drives as your HAL files connect "
                                  "them now, with their pos-scale")
        self.take_over.clicked.connect(self.do_take_over)
        row.addWidget(self.take_over)
        self.take_over_text = label("", "hint", wrap=True)
        row.addWidget(self.take_over_text, 1)
        self.axes.lay.addLayout(row)
        lay.addWidget(self.axes)

        # what the machine still gets from the simulation, or from nothing
        self.wiring_card = Card("Still to connect",
                                "LinuxCNC's inputs and outputs to the machine that the "
                                "simulation still drives or reads, or that nothing does. "
                                "Choose the slave's pin for each.")
        self.wiring_text = self.wiring_card.add(label("", "hint", wrap=True))
        self.wiring = table(["LinuxCNC pin", "Type", "Now", "Status", "Slave pin", "Invert"], 2)
        self.wiring.setWordWrap(True)
        self.wiring.setTextElideMode(Qt.ElideNone)
        self.wiring_card.add(self.wiring)
        self.wiring_info = label("", "hint", wrap=True)
        self.wiring_info.setTextFormat(Qt.RichText)
        self.wiring_info.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.wiring_info.setOpenExternalLinks(True)
        self.wiring_card.add(self.wiring_info)
        self.wiring.itemSelectionChanged.connect(self.show_pin_info)
        self.wiring_rows = []
        lay.addWidget(self.wiring_card)

        pins = Card("Inputs and outputs",
                    "The pins this configuration gives. Connect a slave's pin to LinuxCNC pins "
                    "here (several separated by spaces), or in your own HAL files.")
        self.pins = table(["Slave", "Signal", "HAL pin", "Type", "Direction", "Connected to"], 5)
        pins.add(self.pins)
        row = QHBoxLayout()
        self.take_over_io = QPushButton("Take over from the HAL files")
        self.take_over_io.setToolTip("Connect the slaves' pins here as your HAL files (not the "
                                     "POSTGUI ones) connect them now; those lines are then "
                                     "commented out there")
        self.take_over_io.clicked.connect(self.do_take_over_io)
        row.addWidget(self.take_over_io)
        self.take_over_io_text = label("", "hint", wrap=True)
        row.addWidget(self.take_over_io_text, 1)
        pins.lay.addLayout(row)
        self.other_nets = pins.add(label("", "hint", wrap=True))
        self.io_proposal = {}
        self.hal_signals = {}
        self.hal_uses = {}
        lay.addWidget(pins)

        # values for the parameters of the slaves (setp lines in ethercat.hal)
        self.params_card = Card("Parameters",
                                "Values for the slaves' parameters, written as setp lines in "
                                "ethercat.hal. Leave the value empty to keep the default.")
        self.params = table(["Parameter", "Type", "Default", "Value"])
        self.params.itemChanged.connect(self.param_edited)
        self.params_card.add(self.params)
        self.params_text = self.params_card.add(label("", "hint", wrap=True))
        lay.addWidget(self.params_card)

        # the HAL files against the configuration
        self.check_card = Card("HAL check", "Your HAL files, ethercat-load.hal and ethercat.hal "
                                            "against this configuration, as LinuxCNC would read "
                                            "them: pins that do not exist (a slave renamed, an "
                                            "entry removed), pins on two signals, two writers.")
        self.check_text = self.check_card.add(label("", wrap=True))
        self.check_table = table(["", "Where", "What"], 2)
        self.check_table.setWordWrap(True)      # a long message is read whole, not cut off
        self.check_table.setTextElideMode(Qt.ElideNone)
        self.check_table.horizontalHeader().sectionResized.connect(
            lambda *_: QTimer.singleShot(0, self.fit_check_table))
        self.check_card.add(self.check_table)
        yaml_card = Card("File contents", "ethercat.yaml as it will be written.")
        self.yaml = QPlainTextEdit()
        self.yaml.setReadOnly(True)
        self.yaml.setFont(mono())
        self.yaml.setMinimumHeight(360)
        yaml_card.add(self.yaml)
        # beside the taller file view the check card keeps its content at the top
        self.check_card.lay.addStretch(1)
        checks = QHBoxLayout()
        checks.setSpacing(16)
        checks.addWidget(self.check_card, 1)
        checks.addWidget(yaml_card, 1)
        lay.addLayout(checks)

        # writing: what and why on the left, the changes beside them
        apply = Card("Apply to LinuxCNC")
        cols = QHBoxLayout()
        cols.setSpacing(16)
        side = QVBoxLayout()
        side.setSpacing(8)
        self.apply_text = label("", wrap=True)
        side.addWidget(self.apply_text)
        self.files = QVBoxLayout()
        self.files.setSpacing(4)
        side.addLayout(self.files)
        self.code = QPlainTextEdit()
        self.code.setObjectName("code")
        self.code.setReadOnly(True)
        self.code.setFont(mono())
        self.code.setMinimumHeight(260)
        self.code.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.problems = label("", "problemText", wrap=True)
        side.addWidget(self.problems)
        self.notes = label("", "hint", wrap=True)
        side.addWidget(self.notes)
        self.check_at_start = QCheckBox("Check the HAL files when LinuxCNC starts")
        self.check_at_start.setToolTip("ethercat-load.hal runs ecatsetup --check-hal first: "
                                       "LinuxCNC stops with a clear message when a HAL file uses "
                                       "a pin the configuration does not have. Takes a second.")
        self.check_at_start.toggled.connect(self.check_at_start_edited)
        side.addWidget(self.check_at_start)
        buttons = QHBoxLayout()
        self.write_button = QPushButton("Write")
        self.write_button.setObjectName("primary")
        self.write_button.clicked.connect(self.write)
        buttons.addWidget(self.write_button)
        self.show_button = QPushButton("Show all changes...")
        self.show_button.clicked.connect(self.show_changes)
        buttons.addWidget(self.show_button)
        buttons.addStretch(1)
        side.addLayout(buttons)
        side.addStretch(1)
        cols.addLayout(side, 2)
        cols.addWidget(self.code, 3)
        apply.lay.addLayout(cols)
        lay.addWidget(apply)
        lay.addStretch(1)

    # what is shown

    def refresh(self):
        s = self.win.session
        self.yaml.setPlainText(s.dumps())
        self.show_pins()
        self.show_params()
        if self.win.tabs.currentWidget() is self:
            self.show_joints()
            self.show_wiring()
            self.show_plan()
            self.show_check()
        self.check_at_start.blockSignals(True)
        self.check_at_start.setChecked(integrate.check_at_start(s.cfg))
        self.check_at_start.setEnabled(s.cfg is not None)
        self.check_at_start.blockSignals(False)

    @staticmethod
    def param_shown(value, htype):
        if value is None:
            return ""
        if htype == "bool":
            return "true" if value else "false"
        return str(value)

    def show_params(self):
        cfg = self.win.session.cfg
        known = halpins.parameters(cfg) if cfg is not None else {}
        values = {e.get("pin"): e.get("value") for e in integrate.params(cfg)} \
            if cfg is not None else {}
        self.params.blockSignals(True)
        self.params.setRowCount(0)
        for pin in sorted(known):
            htype, default = known[pin]
            r = self.params.rowCount()
            self.params.insertRow(r)
            self.params.setItem(r, 0, item(pin, font=mono()))
            self.params.setItem(r, 1, item(htype))
            self.params.setItem(r, 2, item(self.param_shown(default, htype)))
            self.params.setItem(r, 3, item(self.param_shown(values.get(pin), htype),
                                           editable=True, font=mono()))
        self.params.blockSignals(False)
        fit_table(self.params, minimum=1)
        self.params_card.setVisible(bool(known))

    def param_edited(self, it):
        cfg = self.win.session.cfg
        if cfg is None or it.column() != 3:
            return
        pin = self.params.item(it.row(), 0).text()
        htype = halpins.parameters(cfg).get(pin, (None,))[0]
        text = it.text().strip()
        try:
            value = self.parse_param(text, htype)
            if value is not None and pin in halpins.encoder_scales(cfg) and value == 0:
                raise ValueError("the scale must not be 0")
        except ValueError as e:
            self.params_text.setText("%s: %s" % (pin, e))
            QTimer.singleShot(0, self.show_params)
            return
        self.params_text.setText("")
        if value == next((e.get("value") for e in integrate.params(cfg)
                          if e.get("pin") == pin), None):
            return
        integrate.set_param(cfg, pin, value)
        QTimer.singleShot(0, self.win.changed)

    @staticmethod
    def parse_param(text, htype):
        """The value a parameter's cell says (None: empty, the default); ValueError
        with the reason when it does not fit the type."""
        if text == "":
            return None
        if htype == "bool":
            t = text.lower()
            if t in ("true", "1", "yes", "on"):
                return True
            if t in ("false", "0", "no", "off"):
                return False
            raise ValueError("true or false")
        try:
            if htype == "real":
                v = float(text)
                if v != v or abs(v) == float("inf"):
                    raise ValueError
                return v
            v = int(text, 0)
        except ValueError:
            raise ValueError("a number" if htype == "real" else "a whole number") from None
        if htype == "uint" and v < 0:
            raise ValueError("a whole number, not negative")
        return v

    def check_at_start_edited(self, on):
        cfg = self.win.session.cfg
        if cfg is None or on == integrate.check_at_start(cfg):
            return
        lc = cfg.setdefault("linuxcnc", {})
        if on:
            lc["check-hal-at-start"] = True
        else:
            lc.pop("check-hal-at-start", None)
            if not lc:
                cfg.pop("linuxcnc", None)
        self.win.changed()

    def show_check(self):
        s, ini = self.win.session, self.ini()
        t = self.check_table
        t.setRowCount(0)
        t.setVisible(False)
        if ini is None or s.cfg is None:
            self.check_text.setText("Choose the machine's INI file to check its HAL files.")
            fit_table(t, minimum=1)
            return
        try:
            found = halcheck.check(ini, s.cfg)
        except OSError as e:
            self.check_text.setText(str(e))
            return
        errors = sum(1 for f in found if f.severity == "error")
        self.check_text.setText(
            "The HAL files match the configuration." if not found else
            "%d error%s, %d warning%s. Errors are what HAL would refuse at start, or what cannot "
            "work." % (errors, "" if errors == 1 else "s", len(found) - errors,
                       "" if len(found) - errors == 1 else "s"))
        t.setRowCount(len(found))
        t.setVisible(bool(found))
        for r, f in enumerate(sorted(found, key=lambda f: (f.severity != "error", f.file, f.line))):
            colour = QColor("#c62828") if f.severity == "error" else QColor("#b26a00")
            t.setItem(r, 0, item("ERROR" if f.severity == "error" else "WARN", colour=colour))
            t.setItem(r, 1, item("%s:%d" % (f.file, f.line) if f.line else f.file, font=mono()))
            t.setItem(r, 2, item(f.text, colour=colour, tip=f.text))
        self.fit_check_table()

    def fit_check_table(self):
        """Rows as tall as their wrapped text, at the column's present width."""
        t = self.check_table
        t.resizeRowsToContents()
        fit_table(t, minimum=1)

    def show_pins(self):
        s, ini = self.win.session, self.ini()
        self.hal_signals, self.io_proposal, self.hal_uses = {}, {}, {}
        if ini and s.cfg is not None:
            try:
                self.hal_signals = integrate.signals(integrate.user_hal_files(ini), ini)
                self.io_proposal = integrate.propose_nets(ini, s.cfg)
                self.hal_uses = halcheck.uses(ini)[0]
            except OSError:
                pass
        own = integrate.slave_pins(s.cfg)
        offered = integrate.suggestions(ini)
        shown = set()
        self.pins.setRowCount(0)
        for row in config_pins(s.cfg):
            r = self.pins.rowCount()
            self.pins.insertRow(r)
            for c, v in enumerate(row):
                self.pins.setItem(r, c, item(v, font=mono() if c in (1, 2) else None))
            pin = row[2]
            if pin not in own or s.cfg is None:
                self.pins.setItem(r, 5, item(""))
                continue
            shown.add(pin)
            htype, way = own[pin]
            net = integrate.net_of(s.cfg, pin)
            others = [x for x in (net or {}).get("pins", []) if x != pin]
            box = QuietCombo()
            box.setEditable(True)
            box.setInsertPolicy(QComboBox.NoInsert)
            box.addItem("")
            for x in offered:
                k = integrate.known_pin(x)
                if k and k[0] == htype and (k[1] == "io" if way == "io" else k[1] not in (way, "io")):
                    box.addItem(x)
            box.setEditText(" ".join(others))
            box.lineEdit().setCursorPosition(0)
            box.setFont(mono())
            box.setMinimumWidth(260)
            tip = ["Signal %s" % net["signal"]] if net else []
            theirs = [(sig, sorted(p for p in pins if p != pin))
                      for sig, pins in self.hal_signals.items() if pin in pins]
            for sig, pins in theirs:
                tip.append("In your HAL files: %s %s" % (sig, " ".join(pins)))
            for u in self.hal_uses.get(pin, []):
                tip.append("  %s:%d %s" % (u.file, u.line, "net %s" % u.signal if u.signal
                                           else "setp"))
            if not tip:
                tip.append("Not connected; type LinuxCNC pins, or choose one")
            box.setToolTip("\n".join(tip))
            box.lineEdit().editingFinished.connect(lambda p=pin, b=box: self.pin_edited(p, b))
            box.activated.connect(lambda _i, p=pin, b=box: self.pin_edited(p, b))
            self.pins.setCellWidget(r, 5, box)
        fit_table(self.pins, minimum=1)
        rest = [n for n in integrate.nets(s.cfg)
                if not any(x in shown for x in n.get("pins") or [])]
        self.other_nets.setText("Also connected: " + "; ".join(
            "%s (%s)" % (n.get("signal"), " ".join(n.get("pins") or [])) for n in rest))
        self.other_nets.setVisible(bool(rest))
        self.take_over_io.setVisible(bool(self.io_proposal))
        self.take_over_io_text.setVisible(bool(self.io_proposal))
        self.take_over_io_text.setText("Your HAL files connect " + ", ".join(
            "%s (%s)" % (sig, " ".join(p for p in pins if integrate.is_own(s.cfg, p)))
            for sig, pins in sorted(self.io_proposal.items())) + ".")

    STATUS_TEXT = {"simulated": "simulated", "open": "not connected", "connected": "connected"}

    def show_wiring(self):
        s, ini = self.win.session, self.ini()
        t = self.wiring
        t.setRowCount(0)
        self.wiring_rows = []
        if ini is None or s.cfg is None:
            self.wiring_card.setVisible(False)
            return
        try:
            self.wiring_rows = wiring.rows(ini, s.cfg)
        except OSError as e:
            self.wiring_text.setText("The HAL files could not be read: %s" % e)
            self.wiring_rows = []
        self.wiring_card.setVisible(bool(self.wiring_rows))
        own = integrate.slave_pins(s.cfg)
        todo = [r for r in self.wiring_rows if r.todo]
        has_bits = any(ht == "bool" for ht, _ in own.values())
        text = ("%d still simulated or not connected." % len(todo) if todo else
                "Everything here is connected to the machine.")
        if todo and not has_bits:
            text += (" The slaves give no bit pins yet: on the Bus page, choose \"bit field\" "
                     "as the HAL type of a digital input or output entry to get one pin per bit.")
        self.wiring_text.setText(text)
        t.setRowCount(len(self.wiring_rows))
        for i, r in enumerate(self.wiring_rows):
            colour = (QColor("#2e7d32") if r.status == "connected" else
                      QColor("#b26a00") if r.status == "simulated" else QColor("#c62828"))
            arrow = {"in": "\u2192 LinuxCNC", "out": "LinuxCNC \u2192", "io": "\u2194"}[r.way]
            doc = pindocs.info(r.pin)
            t.setItem(i, 0, item(r.pin, font=mono(), tip="%s (%s)%s" % (
                r.pin, arrow, "\n" + doc.text if doc else "")))
            t.setItem(i, 1, item(r.htype))
            now = r.now if r.status != "connected" else ("replaces " + r.now if r.now else "")
            where = r.where + (": a .tcl file, change it by hand" if r.tcl and r.status != "open"
                               else "")
            t.setItem(i, 2, item(now, tip="\n".join(x for x in (now, where) if x)))
            st = self.STATUS_TEXT.get(r.status, r.status)
            if r.target != r.pin:
                st += " (at %s)" % r.target
            t.setItem(i, 3, item(st, colour=colour, tip=where or None))
            box = QuietCombo()
            box.addItem("")
            want = {"in": "out", "out": "in", "io": "io"}[r.way]
            for pin, (ht, w) in sorted(own.items()):
                if w == want and ht == r.htype:
                    box.addItem(pin)
            if r.slave and box.findText(r.slave) < 0:
                box.addItem(r.slave)
            box.setCurrentText(r.slave)
            box.setFont(mono())
            box.setMinimumWidth(220)
            box.setEnabled(box.count() > 1)
            box.setToolTip("The slave's %s pin to connect to %s" % (
                {"in": "input", "out": "output", "io": "handshake pin (both ways)"}[r.way],
                r.target) if box.count() > 1 else
                "No slave pin of type %s %s: make one on the Bus page" % (
                    r.htype, {"in": "from the machine", "out": "to the machine",
                              "io": "that both sides drive (such as an encoder channel's "
                                    "index-enable)"}[r.way]))
            box.currentTextChanged.connect(lambda text, row=r: self.wiring_edited(row, text))
            t.setCellWidget(i, 4, box)
            inv = QCheckBox()
            state = integrate.pin_inverted(s.cfg, r.slave) if r.slave else None
            inv.setEnabled(state is not None)
            inv.setChecked(bool(state))
            inv.setToolTip(
                "Make the slave's pin the opposite of its bit (for a normally closed switch such "
                "as an E-stop). It is a property of the pin: every net it is in gets it, and "
                "the Process data page shows it in the Invert column" if state is not None else
                "Only a bit pin (a bool pin or a bit of a bit field) can be inverted: connect one "
                "first" if not r.slave else "Only a bit pin can be inverted")
            inv.toggled.connect(lambda on, pin=r.slave: self.wiring_inverted(pin, on))
            t.setCellWidget(i, 5, inv)
        t.resizeRowsToContents()
        fit_table(t, minimum=1)
        self.show_pin_info()

    def show_pin_info(self):
        """What the selected LinuxCNC pin does, with links to LinuxCNC's documentation
        (the text is in pindocs.py; nothing is fetched)."""
        rows = self.wiring.selectionModel().selectedRows() if self.wiring_rows else []
        if not rows or rows[0].row() >= len(self.wiring_rows):
            self.wiring_info.setText("Select a pin to see what it does.")
            return
        pin = self.wiring_rows[rows[0].row()].pin
        doc = pindocs.info(pin)
        if doc is None:
            self.wiring_info.setText("<b>%s</b>: no documentation known (a pin of your own "
                                     "component, or one LinuxCNC does not document)."
                                     % html.escape(pin))
            return
        self.wiring_info.setText(
            '<b>%s</b>: %s<br><a href="%s">Documentation of the pin</a> &nbsp;|&nbsp; '
            '<a href="%s">The whole %s page</a>' % (
                html.escape(pin), html.escape(doc.text), doc.pin_url, doc.page_url,
                html.escape(doc.title)))

    def wiring_edited(self, row, slave_pin):
        cfg = self.win.session.cfg
        if cfg is None or slave_pin == row.slave:
            return
        wiring.connect(cfg, row, slave_pin)
        QTimer.singleShot(0, self.win.changed)

    def wiring_inverted(self, pin, on):
        cfg = self.win.session.cfg
        if cfg is None or not pin or integrate.pin_inverted(cfg, pin) == on:
            return
        if integrate.set_pin_inverted(cfg, pin, on):
            QTimer.singleShot(0, self.win.changed)

    def pin_edited(self, pin, box):
        cfg = self.win.session.cfg
        if cfg is None:
            return
        others = box.currentText().replace(",", " ").split()
        net = integrate.net_of(cfg, pin)
        if others == [x for x in (net or {}).get("pins", []) if x != pin]:
            return
        integrate.set_net(cfg, pin, others)
        # the box sends this from its own signal: rebuild the table after it
        QTimer.singleShot(0, self.win.changed)

    def do_take_over_io(self):
        cfg = self.win.session.cfg
        for sig, pins in sorted(self.io_proposal.items()):
            own = [x for x in pins if integrate.is_own(cfg, x)]
            integrate.set_net(cfg, own[0], [x for x in pins if x != own[0]], sig)
        self.win.changed()

    def ini(self):
        p = self.win.session.ini_path
        return p if p and os.path.exists(p) else None

    def show_joints(self):
        s, ini = self.win.session, self.ini()
        self.no_ini.setVisible(ini is None)
        expanded = bool(ini and ini.endswith(integrate.EXPANDED))
        self.expanded_warning.setVisible(expanded)
        if expanded:
            src = integrate.expanded_source(ini)
            name = os.path.basename(src) if src else "the INI file"
            self.expanded_warning.setText(
                "%s is the file LinuxCNC writes from %s at every start. It may be out of date "
                "(joints that were removed since), and nothing can be written into it: choose "
                "%s with INI file..." % (os.path.basename(ini), name, name))
        joints = []
        if ini:
            try:
                joints = integrate.ini_joints(ini)
            except OSError:
                joints = []
        key = (ini, tuple(joints))
        if key != self.rows_for:
            for r in self.rows:
                r.setParent(None)
            self.rows = [JointRow(self, j, letter, jtype) for j, letter, jtype in joints]
            for r in self.rows:
                self.rows_box.addWidget(r)
            self.rows_for = key
            self.proposal = {}
            self.known_scales = {}
            if ini and s.cfg is not None:
                try:
                    self.proposal = integrate.propose_joints(ini, s.cfg)
                    files = integrate.user_hal_files(ini)
                    setp = integrate.setp_values(files, ini)
                    e = integrate.read_ini(ini)
                    for name in integrate.drives(s.cfg):
                        v = integrate.resolve(setp.get("%s.pos-scale" % name), e)
                        try:
                            if v is not None:
                                self.known_scales[name] = float(v)
                        except ValueError:
                            pass
                except OSError:
                    pass
        self.axes.hint.setText("Joints from %s. Choose the drive for each; ethercat.hal "
                               "connects them." % os.path.basename(ini) if ini else "")
        tested = set(self.win.tested_drives)
        for r in self.rows:
            r.fill(s.cfg, tested)
            r.setEnabled(s.cfg is not None)
        pending = {j: v for j, v in self.proposal.items()
                   if j not in {x.get("joint") for x in integrate.joints(s.cfg)}}
        self.take_over.setVisible(bool(pending))
        self.take_over_text.setVisible(bool(pending))
        self.take_over_text.setText("Your HAL files connect " + ", ".join(
            "joint.%d to %s" % (j, name) for j, (name, _) in sorted(pending.items())) + ".")

    def do_take_over(self):
        cfg = self.win.session.cfg
        for j, (name, scale) in sorted(self.proposal.items()):
            if j not in {x.get("joint") for x in integrate.joints(cfg)}:
                integrate.set_joint(cfg, j, name, scale)
        self.win.changed()

    def yaml_name(self):
        return os.path.basename(self.win.session.path) if self.win.session.path \
            else "ethercat.yaml"

    def show_plan(self):
        s, ini = self.win.session, self.ini()
        while self.files.count():
            w = self.files.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
        self.plan = None
        if ini is None or s.cfg is None:
            self.apply_text.setText("Choose the machine's INI file (Bus page, Master, or the "
                                    "Joints card): the files are written in its folder.")
            self.code.setPlainText("")
            self.problems.setText("")
            self.notes.setText("")
            self.write_button.setEnabled(False)
            self.show_button.setEnabled(False)
            self.write_button.setText("Write")
            return
        d = os.path.dirname(os.path.abspath(ini))
        try:
            self.plan = p = integrate.plan(ini, s.cfg, self.yaml_name(), s.drive_max_counts())
        except OSError as e:
            self.problems.setText(str(e))
            self.write_button.setEnabled(False)
            return
        self.apply_text.setText(
            "Written in the configuration folder %s. Your own HAL files are changed only where "
            "they would clash with ethercat.hal: those lines are commented out. Every changed "
            "file is kept as .bak." % d)
        dest = os.path.join(d, self.yaml_name())
        rows = [("change" if os.path.exists(dest) else "new", self.yaml_name(), "the bus")]
        rows += [(c.kind, c.name, c.what) for c in p.changes]
        for kind, name, what in rows:
            r = QWidget()
            rl = QHBoxLayout(r)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.addWidget(pill(kind, "blue" if kind == "new" else "amber"))
            n = label(name)
            n.setFont(mono())
            rl.addWidget(n)
            rl.addWidget(label(what, "hint"))
            rl.addStretch(1)
            self.files.addWidget(r)
        main = next((c for c in p.changes if c.name == integrate.MAIN_FILE), None)
        if main is None:
            path = os.path.join(d, integrate.MAIN_FILE)
            text = open(path, errors="replace").read() if os.path.exists(path) else ""
        else:
            text = main.new
        self.code.setPlainText(text)
        errors = list(p.errors) + config.check(s.cfg)
        self.problems.setText("\n".join("✗ " + e for e in errors))
        self.problems.setVisible(bool(errors))
        self.notes.setText("\n".join(["\u26a0 " + w for w in p.warnings] +
                                    ["· " + n for n in p.notes]))
        self.write_button.setText("Write to %s" % os.path.basename(d))
        self.write_button.setEnabled(not errors and not self.win.busy and self.win.test is None)
        self.show_button.setEnabled(True)

    def changes_text(self):
        out = []
        integrate.describe(self.plan, out.append)
        return "\n".join(out)

    def show_changes(self):
        if self.plan is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Changes - %s" % TITLE)
        dlg.resize(900, 650)
        dl = QVBoxLayout(dlg)
        text = QPlainTextEdit(self.changes_text())
        text.setReadOnly(True)
        text.setFont(mono())
        dl.addWidget(text)
        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.rejected.connect(dlg.reject)
        dl.addWidget(box)
        dlg.exec_()

    def write(self):
        s, ini = self.win.session, self.ini()
        if self.plan is None or ini is None:
            return
        if self.win.test is not None:
            QMessageBox.warning(self, TITLE, "Stop the test first.")
            return
        d = os.path.dirname(os.path.abspath(ini))
        dest = os.path.join(d, self.yaml_name())
        names = [self.yaml_name()] + [c.name for c in self.plan.changes]
        if QMessageBox.question(
                self, TITLE, "Write these files in %s?\n\n%s\n\nEvery existing file is kept as "
                ".bak. LinuxCNC must not run with this configuration now." % (d, "\n".join(names)),
                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        if self.plan.warnings and QMessageBox.warning(
                self, TITLE, "%s\n\nWrite here anyway?" % "\n\n".join(self.plan.warnings),
                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        if self.plan.edited and QMessageBox.warning(
                self, TITLE, "%s was changed by hand since EtherCAT Setup wrote it. Writing "
                "replaces the change (Show all changes shows it as removed); move it into a "
                "HAL file of your own first if it is needed.\n\nWrite anyway?"
                % ", ".join(self.plan.edited),
                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        try:
            s.save(dest)
            written = integrate.apply(self.plan, overwrite_edited=True)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, TITLE, "Not all was written:\n%s" % e)
            self.win.refresh(keep_page=True)
            return
        self.win.status("Wrote %s" % ", ".join(os.path.basename(x) for x in [dest] + written))
        self.rows_for = None
        self.win.refresh(keep_page=True)


# -- the window

class StartPage(QWidget):
    """Where to begin: the tasks, each with its steps and where to do them."""

    def __init__(self, win):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        lay = page_layout(content)
        outer.addWidget(scroll(content))
        lay.addWidget(label("What do you want to do?", "bigTitle"))
        self.state = label("", "hint", wrap=True)
        lay.addWidget(self.state)
        self.buttons = []
        for title, when, steps in tasks.TASKS:
            card = Card(title, when)
            for n, (text, target, button) in enumerate(steps, 1):
                row = QHBoxLayout()
                num = pill(str(n), "blue")
                num.setFixedWidth(26)
                num.setAlignment(Qt.AlignCenter)
                row.addWidget(num)
                row.addWidget(label(text, wrap=True), 1)
                b = QPushButton(button)
                b.clicked.connect(lambda _c=False, t=target: win.go(t))
                row.addWidget(b)
                self.buttons.append((target, b))
                card.add(row)
            lay.addWidget(card)
        lay.addStretch(1)

    def show_state(self, win):
        s = win.session
        parts = ["configuration: %s" % ("open" if s.cfg is not None else "none"),
                 "bus: %s" % ("%d slaves read" % len(s.bus) if s.bus else "not read"),
                 "INI file: %s" % (os.path.basename(s.ini_path) if s.ini_path else "none")]
        self.state.setText("   ·   ".join(parts))


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
        self.master_offer_off = False       # "Don't ask again this session"
        self.master_just_started = False    # a failed read after a start is not offered again
        self.test_timer = QTimer(self)
        self.test_timer.setInterval(200)
        self.test_timer.timeout.connect(self.poll_test)
        self.pending_timer = QTimer(self)
        self.pending_timer.setInterval(25)
        self.pending_timer.timeout.connect(self.pending_tick)
        self.drive_tests = {}   # slave name -> drivetest.DriveTest
        self.tested_drives = set()  # drives moved in the test since the program started
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
        self.hex_values = False     # SDO values shown in hex (Settings and the SDO card)
        self.header = Header(self)
        lay.addWidget(self.header)
        self.tabs = NavTabs()
        self.start_page = StartPage(self)
        self.bus_page = BusPage(self)
        self.trouble_page = TroubleshootPage(self)
        self.test_page = TestPage(self)
        self.hal_page = HalPage(self)
        self.tabs.addTab(self.start_page, "Start")
        self.tabs.addTab(self.bus_page, "Bus")
        self.tabs.addTab(self.trouble_page, "Slave status")
        self.tabs.addTab(self.test_page, "Test")
        self.tabs.addTab(self.hal_page, "HAL && LinuxCNC")
        self.tabs.currentChanged.connect(lambda _: self.show_current())
        lay.addWidget(self.tabs.strip)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self._actions()
        # the start page when there is nothing yet, else where the work is
        if session.cfg is not None or session.bus:
            self.tabs.setCurrentWidget(self.bus_page)
        self.refresh()

    def go(self, target):
        """A start page button: a page or an action."""
        pages = {"bus": self.bus_page, "status": self.trouble_page, "test": self.test_page,
                 "hal": self.hal_page}
        actions = {"scan": self.read_bus, "open": self.open_file, "update": self.update_from_bus,
                   "esi": self.add_esi, "watch": self.start_watch}
        if target in pages:
            self.show_tab(pages[target])
        elif target in actions and not self.busy:
            actions[target]()

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
        self.a_watch = act("Watch LinuxCNC that runs (read only)", self.start_watch)
        self.a_test_stop = act("Stop the test or the watching", self.stop_test)
        menu.addSeparator()
        act("Quit", self.close, QKeySequence.Quit)
        self.header.menu_button.setMenu(menu)

    # -- state shown

    @property
    def busy(self):
        return self.worker is not None

    def status(self, text):
        self.statusBar().showMessage(text, 8000)
        page = getattr(self, "test_page", None)
        if page is not None and not page.starting.isHidden():
            page.starting_text.setText(text)

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
            return "No master running: Start the master (sudo ethercat-uspace start), then Rescan."
        if not s.bus:
            return "The bus has not been read."
        if s.cfg is None:
            return "%d slaves on the bus." % len(s.bus)
        odd = [pos for pos in self.positions() if self.file_problem(pos)]
        where = "the saved configuration" if not s.dirty else "the configuration"
        try:
            ch = s.bus_changes()
        except Exception:      # the summary must not fail on an odd file
            ch = None
        if ch is not None and (ch.moved or ch.new or ch.gone):
            return ("The bus differs from %s: %s. Update from bus keeps the names and pins of "
                    "slaves that moved." % (where, ch.summary()))
        if not odd:
            return "The bus matches %s." % where
        return "The bus matches %s, except slave%s %s." % (
            where, "" if len(odd) == 1 else "s", ", ".join(map(str, odd)))

    def set_hex_values(self, on):
        """Show SDO values in hex (or decimal) in the Settings table and the SDO card."""
        if on == self.hex_values:
            return
        self.hex_values = on
        for box in (self.slave_page.hex_box, self.test_page.hex_box):
            box.blockSignals(True)
            box.setChecked(on)
            box.blockSignals(False)
        s = self.session.slave(self.slave_page.position)
        if s is not None:
            self.slave_page.show_settings(s)

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
            if not self.header.status.text().startswith(("● TEST", "● WATCH")):
                set_pill(self.header.status, "● WATCHING LinuxCNC" if self.test.watching
                         else "● TEST MODE · I/O", "blue" if self.test.watching else "amber")
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
        self.header.mark_dirty(has_cfg and s.dirty)
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
        self.a_watch.setEnabled(has_cfg and not self.busy and not testing)
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
        elif page is self.start_page:
            self.start_page.show_state(self)

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
        self.test_page.starting.hide()
        if text.startswith("Cannot reach a master"):
            self.session.bus_error = text
        self.set_messages([(RED, "", text.splitlines())])
        self.status(text.splitlines()[0])
        self.refresh()
        if text.startswith("Cannot reach a master") and not self.master_just_started:
            QTimer.singleShot(0, self.offer_master_start)
        self.master_just_started = False
        if text.startswith("Test mode: "):
            # the messages are not on the Test page: without this the button seems dead
            QMessageBox.warning(self, TITLE, "The test did not start.\n\n" + text[len("Test mode: "):])

    # -- starting the master

    def offer_master_start(self, force=False):
        """No master answers: show the sudo command and start the master if
        the user confirms. Never while LinuxCNC runs or a test is on, and
        not again for the session once the box was ticked (the button
        still works)."""
        if self.busy or self.test is not None or masterstart.linuxcnc_running():
            return
        if self.master_offer_off and not force:
            return
        p = masterstart.plan(self.session.cfg)
        box = QMessageBox(self)
        box.setWindowTitle(TITLE)
        box.setIcon(QMessageBox.Question)
        box.setText("No master is running. Start one with this command?")
        box.setInformativeText("\n".join(p.notes + ([""] if p.notes else []) + [
            masterstart.text(p), "",
            "The window itself does not run as root; sudo must work without a password "
            "here, or you start the master in a terminal."]))
        again = QCheckBox("Don't ask again this session")
        box.setCheckBox(again)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        yes = box.exec_() == QMessageBox.Yes
        if again.isChecked():
            self.master_offer_off = True
        if not yes:
            return

        def work(progress):
            progress("Starting the master...")
            return masterstart.run(p, noninteractive=True)

        def done(result):
            ok, message = result
            if not ok:
                self.set_messages([(RED, "", message.splitlines())])
                self.status(message.splitlines()[0])
                return
            self.master_just_started = True
            self.read_bus()

        self.run("Starting the master...", work, done)

    # -- actions

    def read_bus(self):
        def done(bus):
            notes = []
            if self.session.cfg is None:
                notes += self.session.new()
                notes.append("A new configuration proposed from the bus; save it to keep it")
            self.master_just_started = False
            self.status("Read %d slaves" % len(bus))
            self.check(extra=notes)
        self.run("Reading the bus...", lambda progress: self.session.read_bus(progress), done)

    def update_from_bus(self):
        s = self.session
        ch = s.bus_changes()
        if ch is not None and ch.any:
            lines = []
            for name, dev, a, b in ch.moved:
                lines.append("moved: %s (%s), position %d -> %d; keeps its name and pins"
                             % (name, dev or "?", a, b))
            for pos, dev in ch.new:
                lines.append("new: position %d, %s; proposed from its own data" % (pos, dev or "?"))
            for name, dev, pos in ch.gone:
                lines.append("missing: %s (%s) at position %d; left out" % (name, dev or "?", pos))
            for name, pos in ch.modules:
                lines.append("modules: %s at %d has other modules; propose it again afterwards"
                             % (name, pos))
            for name, pos, a, b in ch.revision:
                lines.append("revision: %s at %d, %s -> %s; check its process data"
                             % (name, pos, a, b))
            then = update.consequences(s.cfg, ch, s.ini_path if s.ini_path and
                                       os.path.exists(s.ini_path) else None)
            text = "The bus differs from the configuration:\n\n" + "\n".join(lines)
            if then:
                text += "\n\nWhat depends on the missing slaves:\n" + "\n".join(then[:12])
                if len(then) > 12:
                    text += "\n... and %d more (the HAL check lists them)" % (len(then) - 12)
            text += "\n\nUpdate the configuration? (The file is written only when you save.)"
            if QMessageBox.question(self, TITLE, text,
                                    QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
                return
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
        try:
            settings.add_esi_folder(os.path.abspath(path))   # remembered for the next run
        except OSError:
            pass
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
        """Diagnose slaves (only reads) and show them on the Slave status page."""
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

    def save_report(self):
        """Write a report file for asking for help (report.py)."""
        from . import report
        s = self.session
        base = os.path.dirname(os.path.abspath(s.ini_path or s.path or os.path.expanduser("~/x")))
        path, _ = QFileDialog.getSaveFileName(self, "Save report - %s" % TITLE,
                                              os.path.join(base, report.default_name()),
                                              "Report (*.md);;All files (*)")
        if not path:
            return
        cfg_path = s.path if s.path and os.path.exists(s.path) else None

        def work(progress):
            progress("Writing the report (diagnosing the bus if a master runs)...")
            text = report.build(cfg_path, s.ini_path if s.ini_path and os.path.exists(s.ini_path)
                                else None)
            with open(path, "w") as f:
                f.write(text)
            return path

        def done(p):
            self.status("Wrote %s; look it through before posting it" % p)
        self.run("Writing the report...", work, done)

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
            self.test_page.starting.hide()
            self.test = run
            self.test_timer.start()
            self.heartbeat.start()
            self.pending_timer.start()
            self.set_messages([(GREEN, "", ["Test running; log: %s" % run.log_path])])
            if self.tabs.currentWidget() is not self.test_page:
                self.tabs.setCurrentWidget(self.test_page)
        self.test_page.starting_text.setText("Starting the test...")
        self.test_page.starting.show()
        self.run("Starting the test...", lambda progress: run.start(progress), started)

    def start_watch(self):
        if self.session.cfg is None or self.test is not None:
            return
        run = testmode.WatchRun(copy.deepcopy(self.session.cfg), self.session.period)

        def started(_):
            self.test = run
            self.test_timer.start()
            self.set_messages([(GREEN, "", ["Watching LinuxCNC: read only."])])
            if not isinstance(self.current, int) or self.current < 0:
                # watching is often started from the Start page: show a slave, not an empty page
                first = self.positions()[:1]
                if first:
                    self.current = first[0]
            if self.tabs.currentWidget() is not self.test_page:
                self.tabs.setCurrentWidget(self.test_page)
            self.refresh()
        self.run("Looking for LinuxCNC...", lambda progress: run.start(progress), started)

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
            self.pending_timer.stop()
            self.drive_tests = {}
            if failed:
                QMessageBox.warning(self, TITLE, "Not all of the drives' values could be written "
                                    "back; they are kept in %s:\n%s"
                                    % (drivetest.RESTORE_FILE, "\n".join(failed)))
            watched = run.watching
            self.test = None
            self.test_page.sync_card.hide()
            self.test_page.offsets = []
            if watched:
                self.set_messages([(GREY, "", ["Stopped watching; LinuxCNC was not touched."])])
                self.refresh()
                return
            self.set_messages([(GREY, "", [
                "Test stopped. The free-running master does not come back by itself: to read "
                "the bus again, start it (Start the master... on the Bus card)"])])
            try:
                tool.master_mac()
            except tool.ToolError:
                QTimer.singleShot(0, self.offer_master_start)
        self.run("Stopping the test...", work, stopped)

    def set_pin(self, pin, value):
        if self.test is None:
            return
        try:
            self.test.set_pin(pin, value)
        except (testmode.TestError, ValueError) as e:
            self.status(str(e))

    def pending_tick(self):
        if self.test is not None and not self.test.watching and not self.busy and \
                any(dt.pending is not None or dt.homing for dt in self.drive_tests.values()):
            self.service_pending()

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
        if not pending or testmode.test_holder():
            # while another window runs a test, the values are its own
            return
        text = "\n".join("slave %s (%s): 0x%04X:%02X back to %s" % (
            e.get("position"), e.get("slave"), e["index"], e["subindex"], e["original"])
            for e in pending)
        if QMessageBox.question(
                self, TITLE, "A drive test ended without writing these drive values back:\n\n%s"
                "\n\nWrite them back now? (A master must run.)" % text,
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            done, failed = drivetest.restore_pending()
        except testmode.TestError as e:
            QMessageBox.warning(self, TITLE, str(e))
            return
        if failed:
            QMessageBox.warning(self, TITLE, "Not written back (kept in %s):\n%s"
                                % (drivetest.RESTORE_FILE, "\n".join(failed)))
        else:
            self.status("Written back: %s" % "; ".join(done))

    def service_pending(self):
        """A PP move, PV run or homing asked for while the drive was
        enabling: given as soon as the drive is enabled (every 25 ms). A
        homing that ended: the drive is disabled (the axis does not go on
        pressing against its stop) and the Move card says how it went."""
        for dt in list(self.drive_tests.values()):
            if dt.homing:
                try:
                    result = dt.home_result()
                    if result:
                        dt.enable(False)
                        self.test_page.move.home_finished(dt, result)
                except testmode.TestError as e:
                    self.status(str(e))
            if dt.pending is not None:
                try:
                    on = dt.pin("stat-op-enabled") == "TRUE"
                    if dt.tick(on):
                        self.tested_drives.add(dt.name)
                except testmode.TestError as e:
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
            self.test_page.sync_card.hide()
            self.test_page.offsets = []
            if run.watching:
                self.set_messages([(AMBER, "", ["LinuxCNC has stopped: the watching ended."])])
            else:
                self.set_messages([(RED, "", ["The test stopped by itself (halrun ended):"] +
                                    tail.splitlines())])
            self.refresh()
            return
        try:
            m = run.pins("ecat.0.")
        except testmode.TestError:
            return
        self.service_pending()

        def get(n):
            return m["ecat.0." + n].value if "ecat.0." + n in m else "?"
        try:
            responding = int(get("slaves-responding"), 0)
        except ValueError:
            responding = "?"
        all_op = get("all-op") == "TRUE"
        set_pill(self.header.status, "● %s · %s · %s, %s slaves%s" % (
            "WATCHING" if run.watching else "TEST MODE",
            "read only" if run.watching else
            "drive can move" if any(dt.prepared for dt in self.drive_tests.values()) else "I/O",
            "all OP" if all_op else "NOT all OP", responding,
            "" if get("link-up") == "TRUE" else ", link DOWN"),
            ("blue" if all_op else "red") if run.watching else "amber" if all_op else "red")
        if self.tabs.currentWidget() is self.test_page:
            self.test_page.show_sync(lambda n: m["ecat.0." + n].value if "ecat.0." + n in m
                                     else None, run.period)
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
        folder = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(folder):
            if QMessageBox.question(
                    self, TITLE, "The folder does not exist:\n%s\n\nCreate it and save %s there?"
                    % (folder, os.path.basename(path)),
                    QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
                return False
            try:
                os.makedirs(folder)
            except OSError as e:
                QMessageBox.warning(self, TITLE, "Cannot create %s:\n%s" % (folder, e))
                return False
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
            self.set_ini(path)

    def set_ini(self, path):
        """Use an INI file; for the .expanded file LinuxCNC writes, offer the
        INI file it is made from."""
        src = integrate.expanded_source(path)
        if src and QMessageBox.question(
                self, TITLE, "%s is the file LinuxCNC writes from %s at every start, with its "
                "#INCLUDEs in place. It is not up to date until the next start, and what is "
                "written into it is lost then.\n\nUse %s instead?" % (
                    os.path.basename(path), os.path.basename(src),
                    os.path.basename(src))) == QMessageBox.Yes:
            path = src
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
            self.pending_timer.stop()
            self.test = None
            self.test_page.sync_card.hide()
            self.test_page.offsets = []
        event.accept()


def main(path=None, esi_paths=(), ini_path=None, mode="csp", read_bus=True):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(TITLE)
    # the ESI folders given before, then those on the command line
    esi_paths = list(dict.fromkeys(settings.esi_folders() + list(esi_paths)))
    win = MainWindow(Session(esi_paths, ini_path, mode))
    if path:
        if os.path.exists(path):
            win.load(path)
        else:
            win.session.path = path
    win.show()
    if ini_path and integrate.expanded_source(ini_path):
        QTimer.singleShot(300, lambda: win.set_ini(ini_path))
    if read_bus:
        win.read_bus()
    QTimer.singleShot(500, win.offer_restore)
    from . import quirks
    quirks.load()
    if quirks.problems:
        QTimer.singleShot(700, lambda: QMessageBox.warning(
            win, TITLE, "Quirk files that could not be read (they are not used):\n\n"
            + "\n".join(quirks.problems)))
    return app.exec_()
