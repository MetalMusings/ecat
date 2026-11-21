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

from PyQt5.QtCore import QLocale, QPointF, QSize, QThread, QTimer, Qt, pyqtSignal
from PyQt5.QtGui import (QBrush, QColor, QFont, QFontDatabase, QKeySequence, QPainter, QPalette, QPen,
                         QPolygonF)
from PyQt5.QtWidgets import (
    QAbstractItemView, QAction, QActionGroup, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QInputDialog,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea,
    QProxyStyle, QSizePolicy, QSpinBox, QStackedWidget, QStyle, QStyledItemDelegate, QTabBar, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget, QCompleter)

from . import (config, dcinfo, diagnose, drivetest, esi, events, explain, fromlcec, guidance, halcheck, halpins, pdoedit, pdoreq,
               homing,
               integrate, lcnc_env, masterstart, measured,
               netif, pindocs, propose, quirks, response, settings, symptoms, tasks, testmode, tool, tuning,
               update, wiring)
from .session import Session, servo_period

TITLE = "EtherCAT Setup"
RED, AMBER, GREEN, GREY = QColor("#b42318"), QColor("#9a5b00"), QColor("#1a7f37"), QColor("#6b7280")
# the Sync row's choices besides the slave's DC modes
DC_FREE, DC_OWN = "free", "own"
CLOCK_FIELD = {"64": "DC, 64 bit", "32": "DC, 32 bit", "delay": "delay measurement only (no SYNC0)",
               "none": "no DC"}
_SLAVE_REF = re.compile(r"^slave (\d+)\b")
MASTER_ITEM = -1

# The window's colours by role; STYLE_TEMPLATE names them. The header bar is
# dark in both modes. RED, AMBER, GREEN and GREY (table text, messages) follow
# the mode too: apply_theme() changes those QColor objects in place.
THEMES = {
    "light": dict(
        bg="#eef1f4", surface="white", line="#d8dee4", border="#dde2e8", rule="#e5e7eb",
        text1="#111827", text2="#4b5563", text3="#6b7280", boxtext="#374151",
        boxborder="#d0d6de", btnborder="#cdd3db", hover="#f3f5f8", disabled="#a0a7b2",
        accent="#1f6feb", accentdis="#8fb2ee", selbg="#e7efff", selborder="#9dbcf5",
        head="#161b22", headtext="#e6edf3", headsub="#9aa4b2", headinfo="#c9d1d9", logo="#79a8ff",
        redtext="#b42318", redbg="#fde2df", redborder="#f1a7a0", redcard="#fff7f6",
        ambertext="#8a5300", amberbg="#fdecc8", amberborder="#f2c46d", ambercard="#fff8e8",
        greentext="#1a7f37", greenbg="#dcf5e3", onbg="#e6f6ea", ontext="#14532d",
        holdpress="#b7e4c3", bluebg="#dbe8ff", bluetext="#1f4fbf", greybg="#eceff3",
        greycard="#f6f7f9",
        red="#b42318", amber="#9a5b00", green="#1a7f37", grey="#6b7280"),
    "dark": dict(
        bg="#0d1117", surface="#161b22", line="#30363d", border="#30363d", rule="#30363d",
        text1="#e6edf3", text2="#9da7b3", text3="#8b949e", boxtext="#c9d1d9",
        boxborder="#3d444d", btnborder="#3d444d", hover="#21262d", disabled="#6e7681",
        accent="#1f6feb", accentdis="#1f3a64", selbg="#1c2d4a", selborder="#388bfd",
        head="#010409", headtext="#e6edf3", headsub="#9aa4b2", headinfo="#c9d1d9", logo="#79a8ff",
        redtext="#ff7b72", redbg="#3d1a1a", redborder="#8e2f2a", redcard="#25171a",
        ambertext="#e3b341", amberbg="#3a2a0c", amberborder="#9e6a03", ambercard="#251f12",
        greentext="#3fb950", greenbg="#12301c", onbg="#12301c", ontext="#7ee2a8",
        holdpress="#1b4d2c", bluebg="#1c2d4a", bluetext="#79a8ff", greybg="#21262d",
        greycard="#1b2028",
        red="#ff7b72", amber="#e3b341", green="#3fb950", grey="#8b949e"),
}
THEME_NAMES = {"light": "Light", "dark": "Dark"}

STYLE_TEMPLATE = """
QWidget {{ font-size: 13px; }}
QMainWindow, QWidget#page {{ background: {bg}; }}
QScrollArea {{ background: {bg}; border: none; }}
QFrame#header {{ background: {head}; }}
QLabel#appTitle {{ font-size: 16px; font-weight: 600; color: {headtext}; }}
QLabel#appSub {{ color: {headsub}; }}
QLabel#headerInfo {{ color: {headinfo}; }}
QLabel#logo {{ border: 2px solid {logo}; border-radius: 6px; font-weight: 700; padding: 0px 4px;
              color: {logo}; }}
QFrame#nav {{ background: {surface}; border-bottom: 1px solid {line}; }}
QFrame#nav QTabBar::tab {{ background: {surface}; padding: 10px 16px; border: none;
              border-bottom: 2px solid transparent; color: {text2}; }}
QFrame#nav QTabBar::tab:selected {{ border-bottom: 2px solid {accent}; color: {text1}; }}
QFrame#card {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; }}
QFrame#summaryBox {{ background: {greycard}; border: 1px solid {border}; border-radius: 8px; }}
QFrame#card[tone="red"] {{ border: 1px solid {redborder}; background: {redcard}; }}
QFrame#card[tone="amber"] {{ border: 1px solid {amberborder}; background: {ambercard}; }}
QFrame#card[tone="grey"] {{ background: {greycard}; }}
QLabel#cardTitle {{ color: {text2}; font-weight: 600; font-size: 11px; letter-spacing: 1px; }}
QLabel#hint {{ color: {text3}; }}
QLabel#axisBadge {{ background: {bluebg}; color: {bluetext}; font-weight: 700; font-size: 16px;
                   border-radius: 6px; }}
QLabel#jointName {{ font-weight: 600; }}
QLabel#problemText {{ color: {redtext}; }}
QLabel#bigTitle {{ font-size: 19px; font-weight: 600; color: {text1}; }}
QLabel#fieldName {{ color: {text3}; font-size: 11px; letter-spacing: 1px; }}
QLabel#value {{ font-family: monospace; font-size: 18px; }}
QLabel[pill="green"] {{ background: {greenbg}; color: {greentext}; }}
QLabel[pill="blue"] {{ background: {bluebg}; color: {bluetext}; }}
QLabel[pill="red"] {{ background: {redbg}; color: {redtext}; }}
QLabel[pill="amber"] {{ background: {amberbg}; color: {ambertext}; }}
QLabel[pill="grey"] {{ background: {greybg}; color: {text2}; }}
QLabel[pill="green"], QLabel[pill="blue"], QLabel[pill="red"], QLabel[pill="amber"],
QLabel[pill="grey"] {{ border-radius: 9px; padding: 2px 9px; font-weight: 600; font-size: 11px; }}
QLabel[box] {{ border: 1px solid {boxborder}; border-radius: 6px; padding: 6px 10px; color: {boxtext};
              background: {surface}; }}
QLabel[box="on"] {{ border: 1px solid {greentext}; background: {onbg}; color: {ontext}; font-weight: 600; }}
QLabel[box="fault"] {{ border: 1px solid {redborder}; color: {redtext}; }}
QLabel[box="fault-on"] {{ border: 1px solid {redtext}; background: {redbg}; color: {redtext};
              font-weight: 600; }}
QPushButton {{ background: {surface}; border: 1px solid {btnborder}; border-radius: 6px; padding: 5px 12px; }}
QPushButton:hover {{ background: {hover}; }}
QPushButton:disabled {{ color: {disabled}; }}
QPushButton#primary {{ background: {accent}; color: white; border: 1px solid {accent}; font-weight: 600;
              padding: 7px 16px; }}
QPushButton#primary:disabled {{ background: {accentdis}; border-color: {accentdis}; }}
QPushButton#savedirty {{ background: #d29922; color: #1b1f24; border: 1px solid #d29922;
              font-weight: 700; padding: 7px 16px; }}
QPushButton#savedirty:disabled {{ background: #6e5a2a; border-color: #6e5a2a; color: #c9d1d9; }}
QPushButton#saveclean {{ background: transparent; color: #8b949e; border: 1px solid #30363d;
              padding: 7px 16px; }}
QPushButton#danger {{ background: #b42318; color: white; border: 2px solid white; font-weight: 700;
              padding: 6px 18px; }}
QPushButton#big {{ padding: 14px; font-size: 15px; font-weight: 600; }}
QPushButton#home-ok {{ color: white; background: #1a7f37; border: 2px solid #14532d;
              border-radius: 6px; font-weight: 700; }}
QPushButton#home-failed {{ color: white; background: #b42318; border: 2px solid #7a1710;
              border-radius: 6px; font-weight: 700; }}
QPushButton#hold {{ padding: 16px; font-size: 15px; font-weight: 600; color: {ontext};
              background: {onbg}; border: 2px solid {greentext}; border-radius: 8px; }}
QPushButton#hold:pressed {{ background: {holdpress}; }}
QPushButton#hold:disabled {{ color: {disabled}; background: {hover}; border-color: {boxborder}; }}
QToolButton#menu {{ color: #e6edf3; background: #161b22; border: 1px solid #3a434e;
              border-radius: 6px; padding: 6px 12px; }}
QToolButton#menu::menu-indicator {{ image: none; width: 0px; }}
QListWidget#slaves {{ border: none; background: transparent; outline: none; }}
QListWidget#slaves::item {{ border-radius: 8px; margin: 2px 0px; border: 1px solid transparent; }}
QListWidget#slaves::item:selected {{ background: {selbg}; border: 1px solid {selborder}; }}
QListWidget#messages {{ border: none; background: transparent; }}
QTableWidget {{ border: none; gridline-color: {greybg}; background: {surface}; }}
QHeaderView::section {{ background: {surface}; border: none; border-bottom: 1px solid {border};
              padding: 4px; color: {text2}; font-weight: 600; }}
QPlainTextEdit#code {{ background: {head}; color: {headtext}; border-radius: 8px; padding: 6px; }}
QFrame#rule {{ color: {rule}; }}
QLabel#warnText {{ color: {ambertext}; }}
"""


def style(theme="light"):
    return STYLE_TEMPLATE.format(**THEMES[theme])


STYLE = style()
_default_style = None   # (style name, palette) of the application before any mode
_current = {"theme": "light"}


def theme_setting():
    """The mode saved in settings.yaml (`theme:`), light if none."""
    t = settings.load().get("theme")
    return t if t in THEMES else "light"


def apply_theme(theme):
    """The application's palette and the shared text colours for a mode. The
    window's style sheet is set by MainWindow.set_theme."""
    global _default_style
    from PyQt5.QtWidgets import QStyleFactory  # noqa: F401 (only here)
    app = QApplication.instance()
    c = THEMES[theme]
    _current["theme"] = theme
    for colour, key in ((RED, "red"), (AMBER, "amber"), (GREEN, "green"), (GREY, "grey")):
        colour.setNamedColor(c[key])
    if _default_style is None:
        _default_style = (app.style().objectName(), QPalette(app.palette()))
    want = _default_style[0] if theme == "light" else "fusion"
    proxied = isinstance(app.style(), IndicatorStyle)
    if app.style().objectName().lower() != want.lower() or proxied != (theme == "dark"):
        base = QStyleFactory.create(want) or QStyleFactory.create(app.style().objectName())
        app.setStyle(IndicatorStyle(base) if theme == "dark" else base)
    if theme == "light":
        app.setPalette(_default_style[1])     # as the window was before any mode
        return
    # a dark palette for what the style sheet does not cover (fields, combos,
    # menus, dialogs); Fusion honours a palette fully
    pal = QPalette()
    roles = {QPalette.Window: c["bg"], QPalette.WindowText: c["text1"],
             QPalette.Base: "#0d1117", QPalette.AlternateBase: c["surface"],
             QPalette.Text: c["text1"], QPalette.Button: c["surface"],
             QPalette.ButtonText: c["text1"], QPalette.BrightText: "#ffffff",
             QPalette.ToolTipBase: c["surface"], QPalette.ToolTipText: c["text1"],
             QPalette.Highlight: c["accent"], QPalette.HighlightedText: "#ffffff",
             QPalette.Link: c["bluetext"], QPalette.LinkVisited: c["bluetext"],
             QPalette.PlaceholderText: c["text3"], QPalette.Light: c["boxborder"],
             QPalette.Midlight: c["border"], QPalette.Mid: c["border"],
             QPalette.Dark: "#010409", QPalette.Shadow: "#000000"}
    for role, value in roles.items():
        pal.setColor(role, QColor(value))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(c["disabled"]))
    app.setPalette(pal)


class IndicatorStyle(QProxyStyle):
    """Fusion with check boxes and radio buttons that show on a dark
    surface: Fusion fills them with the base colour (near black) and draws
    their edge darker than the window, so on the dark cards they were black
    on dark grey. Light mode keeps the platform's own."""

    def __init__(self, base):
        super().__init__(base)
        self.setObjectName(base.objectName())

    def drawPrimitive(self, element, option, painter, widget=None):
        if element not in (QStyle.PE_IndicatorCheckBox, QStyle.PE_IndicatorRadioButton,
                           QStyle.PE_IndicatorItemViewItemCheck):
            return super().drawPrimitive(element, option, painter, widget)
        c = THEMES["dark"]
        on = bool(option.state & QStyle.State_On)
        partly = bool(option.state & QStyle.State_NoChange)
        enabled = bool(option.state & QStyle.State_Enabled)
        r = option.rect.adjusted(1, 1, -1, -1)
        side = min(r.width(), r.height())
        r.setSize(QSize(side, side))
        r.moveCenter(option.rect.center())
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        edge = QColor(c["text3"] if enabled else c["boxborder"])
        fill = QColor(c["accent"] if (on or partly) and enabled else
                      c["accentdis"] if (on or partly) else c["hover"])
        if on or partly:
            edge = fill
        elif option.state & QStyle.State_MouseOver and enabled:
            edge = QColor(c["text2"])
        painter.setPen(QPen(edge, 1.5))
        painter.setBrush(fill)
        if element == QStyle.PE_IndicatorRadioButton:
            painter.drawEllipse(r)
            if on:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#ffffff"))
                d = side * 0.36
                painter.drawEllipse(r.center(), d / 2 + 0.5, d / 2 + 0.5)
        else:
            painter.drawRoundedRect(r, 3, 3)
            painter.setPen(QPen(QColor("#ffffff"), max(1.6, side / 8.0), Qt.SolidLine, Qt.RoundCap,
                                Qt.RoundJoin))
            x, y, w = r.x(), r.y(), float(side)
            if partly:
                painter.drawLine(QPointF(x + w * 0.25, y + w * 0.5), QPointF(x + w * 0.75, y + w * 0.5))
            elif on:
                painter.drawPolyline(QPolygonF([QPointF(x + w * 0.24, y + w * 0.52),
                                                QPointF(x + w * 0.43, y + w * 0.70),
                                                QPointF(x + w * 0.77, y + w * 0.31)]))
        painter.restore()


_MONO = []


def mono():
    """The fixed-width font (looked up once: it is asked for in every table cell)."""
    if not _MONO:
        _MONO.append(QFontDatabase.systemFont(QFontDatabase.FixedFont))
    return QFont(_MONO[0])


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


def use_point():
    """Numbers with . as the decimal point and no thousands separator in every
    widget, whatever the system's language: the values go into HAL and YAML files,
    which take only that."""
    c = QLocale.c()
    c.setNumberOptions(QLocale.OmitGroupSeparator | QLocale.RejectGroupSeparator)
    QLocale.setDefault(c)


def hal_connection_item(pin, uses, sigs):
    """The Connected to cell of a slave pin that the user's HAL files connect: the
    signal and its other pins, grey and read-only, the lines in the tooltip."""
    parts = []
    for sig in dict.fromkeys(u.signal for u in uses):
        others = sorted(dict.fromkeys(p for p in sigs.get(sig, []) if p != pin))
        parts.append("%s: %s" % (sig, " ".join(others)) if others else sig)
    it = item("  ".join(parts), font=mono(), colour=GREY)
    it.setToolTip("Connected in your HAL files:\n" + "\n".join(
        "  %s:%d net %s" % (u.file, u.line, u.signal) for u in uses) +
        "\nChange it there (or on the From lcec card); a connection here would put the pin "
        "on a second signal.")
    return it


def pdo_set_in_use(slave, proposal):
    """The name of the ESI's predefined PDO assignment the slave's process data is, or
    None (the default, the slave's own or an edited one)."""
    if not slave or not proposal or not proposal.pdo_set_pdos:
        return None
    pd = slave.get("process-data") or {}
    have = []
    for pdo in (pd.get("outputs") or []) + (pd.get("inputs") or []):
        try:
            have.append((int(pdo.get("sm")), int(str(pdo.get("pdo")), 16)))
        except (TypeError, ValueError):
            return None
    have.sort()
    return next((n for n, pdos in proposal.pdo_set_pdos.items() if pdos == have), None)


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
        line.setObjectName("rule")
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
                                                     "cycle " + period_text(s.period or 1000000)) if x),
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
        # typed in without an INI file (trying slaves without a machine
        # configuration); the INI file's SERVO_PERIOD when there is one
        self.period = QSpinBox()
        self.period.setRange(50, 100000)
        self.period.setSingleStep(250)
        self.period.setSuffix(" us")
        self.period.setValue(1000)
        self.period.setToolTip("The servo thread's period for the I/O test and the checks, "
                               "without an INI file. With one, its SERVO_PERIOD is used.")
        self.period.valueChanged.connect(self.period_edited)
        ini.addWidget(self.period)
        self.ini_label = label(wrap=True)
        ini.addWidget(self.ini_label, 1)
        choose = QPushButton("INI file...")
        choose.clicked.connect(win.choose_ini)
        ini.addWidget(choose)
        form.addRow("Servo period", ini)
        self.irq_cpu = QSpinBox()
        self.irq_cpu.setRange(-1, 1023)
        self.irq_cpu.setSpecialValueText("not pinned")
        self.irq_cpu.setToolTip("Pin the network card's interrupts to this CPU when the master "
                                "starts: the servo thread's (isolcpus). Needs the privilege "
                                "rtapi_app has. master: irq-cpu")
        self.irq_cpu.valueChanged.connect(self.edited)
        form.addRow("Interrupts on CPU", self.irq_cpu)
        self.cpu = QSpinBox()
        self.cpu.setRange(-1, 1023)
        self.cpu.setSpecialValueText("not bound")
        self.cpu.setToolTip("The CPU for the master's own threads. master: cpu")
        self.cpu.valueChanged.connect(self.edited)
        form.addRow("Master threads on CPU", self.cpu)
        self.cpu_hint = label("", "hint", wrap=True)
        form.addRow("", self.cpu_hint)
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
        self.period.setValue((s.period or 1000000) // 1000)
        self.period.setEnabled(not s.period_from_ini)
        self.ini_label.setText(
            "from %s" % s.ini_path if s.period_from_ini else
            "typed in: no INI file chosen" if not s.ini_path else
            "typed in: no SERVO_PERIOD in %s" % s.ini_path)
        self.irq_cpu.setValue(int(m.get("irq-cpu", -1)))
        self.cpu.setValue(int(m.get("cpu", -1)))
        self.cpu_hint.setText(self.cpu_text())
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

    @staticmethod
    def cpu_text(sys="/sys"):
        """The isolated CPUs, as advice for the two CPU fields."""
        try:
            with open(os.path.join(sys, "devices", "system", "cpu", "isolated")) as f:
                isolated = _cpu_set(f.read())
        except (OSError, ValueError):
            isolated = set()
        if not isolated:
            return ("No CPU is isolated for real time on this computer (isolcpus): pinning "
                    "the interrupts helps most on the servo thread's own CPU.")
        return ("CPU %s isolated for real time: give the interrupts the servo thread's CPU "
                "(a lost cycle at 250 us and shorter was seen with them elsewhere)."
                % _cpu_text(isolated))

    def period_edited(self, value):
        s = self.win.session
        if self.loading or s.period_from_ini:
            return
        s.period = value * 1000
        self.win.refresh(keep_page=True)

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
        for key, box in (("irq-cpu", self.irq_cpu), ("cpu", self.cpu)):
            if box.value() >= 0:
                m[key] = box.value()
            else:
                m.pop(key, None)
        if self.dc_on.isChecked():
            dc = m.setdefault("dc", {})
            if dc.get("servo-thread-follows-ref-clock", True) and not self.follows and \
                    not 1 <= self.ref_cycles.value() <= config.REF_SYNC_MAX:
                # the slaves follow the servo thread from now: they need the
                # reference clock synchronised often (config.dc_warnings)
                self.loading = True
                self.ref_cycles.setValue(config.REF_SYNC_MAX)
                self.loading = False
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


def write_summary(yaml_name, plan):
    """What the question before writing lists: each file with what is done
    to it, then the plan's notes (a .tcl file turned into .hal, ...)."""
    lines = ["%s: the bus" % yaml_name]
    lines += ["%s: %s" % (c.name, c.what) if c.what else c.name for c in plan.changes]
    if plan.notes:
        lines += [""] + ["- " + n for n in plan.notes]
    return "\n".join(lines)


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

    PD_COLUMNS = ["PDO", "Object", "Name", "Type", "Use", "HAL pin", "HAL type",
                  "Scale", "Offset", "Bit pins", "Invert"]
    USE, PIN, HALTYPE, SCALE, OFFSET, SPLIT, INVERT = 4, 5, 6, 7, 8, 9, 10
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
        self.mode_marks = label("", "hint", wrap=True)
        self.mode_marks.setToolTip("Whether the device's PDOs can give what each mode needs, from its "
                                   "ESI file: ready (a PDO assignment has it), build (an own mapping "
                                   "in its open PDOs), impossible, unknown")
        cform.addRow("PDOs per mode", self.mode_marks)
        cform.setRowWrapPolicy(QFormLayout.WrapLongRows)
        prof = QHBoxLayout()
        prof.addWidget(label("Profile:"))
        self.profile_box = QuietCombo()
        self.profile_box.setToolTip(self.PROFILE_TIP)
        self.profile_box.activated.connect(self.profile_chosen)
        prof.addWidget(self.profile_box, 1)
        op.add(prof)
        self.profile_hint = op.add(label("", "hint", wrap=True))
        op.add(self.cia402)
        again = QHBoxLayout()
        self.pdo_set = QuietCombo()
        self.pdo_set.setToolTip("The ESI file's predefined PDO assignments. Choosing one "
                                "proposes the slave again with it (asks first).")
        # long ESI names must not push the button off a small screen
        self.pdo_set.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.pdo_set.setMinimumContentsLength(16)
        self.pdo_set.activated.connect(self.pdo_set_chosen)
        again.addWidget(self.pdo_set, 1)
        self.again = QPushButton("Propose again")
        self.again.setToolTip("Read the slave and replace its process data, DC and settings with "
                              "a new proposal for the chosen mode and PDO assignment. The name is "
                              "kept.")
        self.again.clicked.connect(self.propose_again)
        again.addWidget(self.again)
        op.add(again)
        self.used = QCheckBox("Used by LinuxCNC")
        self.used.setToolTip("Untick for a slave that is only on the bus: ecat checks that it is the slave "
                             "the file says (at start), and then leaves it alone (no configuration, no pins, "
                             "it stays in PREOP). What the entry holds is kept for when you tick this again. "
                             "A slave with a network address (EoE) is used.")
        self.used.toggled.connect(self.general_edited)
        op.add(self.used)
        self.configure_pdos = QCheckBox("Write the PDO mapping to the slave (configure-pdos)")
        self.configure_pdos.toggled.connect(self.general_edited)
        op.add(self.configure_pdos)
        sync = QHBoxLayout()
        sync.addWidget(label("Sync:"))
        self.dc_mode = QuietCombo()
        self.dc_mode.setToolTip(
            "Free run, one of the slave's DC modes (from its ESI file or SII, with the "
            "AssignActivate it needs), or DC mode with user settings: any AssignActivate, typed "
            "in the field. A choice the slave does not describe gets a warning, nothing is "
            "forbidden.")
        self.dc_mode.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.dc_mode.setMinimumContentsLength(16)       # long ESI names: see pdo_set
        self.dc_mode.activated.connect(self.dc_mode_chosen)
        sync.addWidget(self.dc_mode, 1)
        self.assign = QLineEdit()
        self.assign.setMaximumWidth(90)
        self.assign.setMinimumWidth(70)
        self.assign.setPlaceholderText("0x0300")
        self.assign.setToolTip(
            "AssignActivate: which sync signals the slave uses, in hex, such as 0x0300 (SYNC0). "
            "Typed here only with \"DC mode with user settings\"; a mode sets it.")
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
        op.add(sync)
        # SYNC1, for DC modes that use it (AssignActivate bit 0x0400)
        sync1 = QHBoxLayout()
        self.sync1_label = label("SYNC1:")
        sync1.addWidget(self.sync1_label)
        self.sync1_cycles = QSpinBox()
        self.sync1_cycles.setRange(0, 65535)
        self.sync1_cycles.setPrefix("every ")
        self.sync1_cycles.setSuffix(" SYNC0")
        self.sync1_cycles.setToolTip("SYNC1 every n SYNC0 cycles (0: none); from the DC mode in the ESI file")
        self.sync1_cycles.valueChanged.connect(self.general_edited)
        sync1.addWidget(self.sync1_cycles)
        self.sync1_shift = QSpinBox()
        self.sync1_shift.setRange(-10000000, 10000000)
        self.sync1_shift.setPrefix("shift ")
        self.sync1_shift.setSuffix(" ns")
        self.sync1_shift.setToolTip("SYNC1 after SYNC0, in ns (less than the SYNC0 cycle)")
        self.sync1_shift.valueChanged.connect(self.general_edited)
        sync1.addWidget(self.sync1_shift)
        sync1.addStretch(1)
        self.sync1_row = QWidget()
        self.sync1_row.setLayout(sync1)
        sync1.setContentsMargins(0, 0, 0, 0)
        op.add(self.sync1_row)
        self.dc_hint = op.add(label("", "hint", wrap=True))
        self.dc_cycle = op.add(label("", "hint", wrap=True))
        self.dc_warn = op.add(label("", wrap=True))
        self.dc_warn.setObjectName("warnText")
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

        # serial channels (EL6001, EL6021, ...)
        self.serial_card = Card("Serial ports",
                                "Each channel of the terminal is a serial port on the computer "
                                "(a program such as mb2hal opens it). The program's baud rate is "
                                "used; parity and data bits are the frame's here.")
        self.serial_grid = QGridLayout()
        self.serial_grid.setContentsMargins(0, 0, 0, 0)
        self.serial_card.add(self.serial_grid)
        self.serial_hint = self.serial_card.add(label("", "hint", wrap=True))
        self.serial_lines_title = label("For mb2hal's INI file (a transaction on this port):", "hint")
        self.serial_card.add(self.serial_lines_title)
        self.serial_lines = QPlainTextEdit()
        self.serial_lines.setReadOnly(True)
        self.serial_lines.setFont(mono())
        self.serial_lines.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.serial_card.add(self.serial_lines)
        self.serial_copy = QPushButton("Copy the mb2hal lines")
        self.serial_copy.clicked.connect(lambda: QApplication.clipboard().setText(
            self.serial_lines.toPlainText()))
        self.serial_card.add(self.serial_copy)
        self.serial_widgets = []
        lay.addWidget(self.serial_card)

        # Ethernet over EtherCAT
        self.eoe_card = Card("Network (EoE)",
                             "The slave speaks Ethernet over EtherCAT. With an address here the "
                             "computer reaches it (ping, its web page) while LinuxCNC or a test "
                             "runs.")
        self.eoe_on = QCheckBox("Give the slave an IP address")
        self.eoe_on.toggled.connect(self.eoe_toggled)
        self.eoe_card.add(self.eoe_on)
        self.eoe_form = QFormLayout()
        self.eoe_form.setContentsMargins(0, 0, 0, 0)
        self.eoe_fields = {}
        for key, text, example, tip in self.EOE_FIELDS:
            box = QLineEdit()
            box.setFont(mono())
            box.setPlaceholderText(example)
            box.setToolTip(tip)
            box.setMaximumWidth(280)
            box.editingFinished.connect(lambda key=key: self.eoe_edited(key))
            self.eoe_fields[key] = box
            if key == "mask":       # a row only where the file has a mask (show_eoe)
                self.eoe_mask_label = label(text)
                self.eoe_mask_label.hide()
                box.hide()
            else:
                self.eoe_form.addRow(text, box)
        self.eoe_card.add(self.eoe_form)
        self.eoe_hint = self.eoe_card.add(label("", "hint", wrap=True))
        self.eoe_hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.eoe_errors = self.eoe_card.add(label("", "problemText", wrap=True))
        self.eoe_warn = self.eoe_card.add(label("", "warnText", wrap=True))
        self.eoe_kept = {}      # position -> the block as it was when switched off
        lay.addWidget(self.eoe_card)

        # process data
        pd = Card("Process data", "Pins are named <name>.<pin>. Clear a pin to map the entry "
                                  "without one; entries the profile handles have no pins.")
        self.pd = table(self.PD_COLUMNS, self.PIN)
        self.pd_more = QCheckBox("All columns")
        self.pd_more.setToolTip("Scale, Offset, Bit pins and Invert are shown when an entry uses "
                                "them; tick this to see them all, to set one")
        self.pd_more.toggled.connect(self.pd_more_toggled)
        pd.head.addWidget(self.pd_more)
        self.pd.itemChanged.connect(self.pd_edited)
        self.pd.cellDoubleClicked.connect(self.pd_double_clicked)
        pd.add(self.pd)
        self.pd_errors = pd.add(label("", "problemText", wrap=True))
        self.pd_warn = pd.add(label("", "warnText", wrap=True))
        self.pd_notes = pd.add(label("", "hint", wrap=True))
        self.pd_ways = QWidget()
        self.pd_ways_lay = QVBoxLayout(self.pd_ways)
        self.pd_ways_lay.setContentsMargins(0, 0, 0, 0)
        pd.add(self.pd_ways)
        self.pd_ways.hide()
        pd_buttons = QHBoxLayout()
        self.pd_add_object = QPushButton("Add object...")
        self.pd_add_object.setToolTip("Map one more object in an open PDO (one the ESI file lets be mapped)")
        self.pd_add_object.clicked.connect(self.add_pd_object)
        self.pd_remove = QPushButton("Remove entry")
        self.pd_remove.setToolTip("Take the selected entry out of its PDO (not from a fixed PDO)")
        self.pd_remove.clicked.connect(self.remove_pd_entry)
        self.pd_add_pdo = QPushButton("Add PDO...")
        self.pd_add_pdo.setToolTip("Assign another of the device's PDOs, with its entries")
        self.pd_add_pdo.clicked.connect(self.add_pd_pdo)
        self.pd_add_missing = QPushButton("Add what is missing")
        self.pd_add_missing.setToolTip("Map the objects the mode set needs and the file lacks")
        self.pd_add_missing.clicked.connect(self.add_pd_missing)
        for b in (self.pd_add_object, self.pd_remove, self.pd_add_pdo, self.pd_add_missing):
            pd_buttons.addWidget(b)
        pd_buttons.addStretch(1)
        pd.add(pd_buttons)
        self.pd_adds = []       # what the status line says is missing: [(index, "rx"/"tx")]
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
        self.init_button = QPushButton("Add what the device needs")
        self.init_button.setToolTip("Add the settings the device's ESI file writes at start (its InitCmds) "
                                    "and, for a drive whose 0x6060 is not in the process data, its mode")
        self.init_button.clicked.connect(self.add_init_settings)
        buttons.addWidget(self.init_button)
        self.ferror_button = QPushButton("Suggest following-error window")
        self.ferror_button.setToolTip(
            "For a CiA 402 drive: add the drive's following-error window as a setting, "
            "twice the joint's FERROR in the joint's counts, so that LinuxCNC's own "
            "following error trips first and the drive is the second line of defence")
        self.ferror_button.clicked.connect(self.suggest_following_error)
        buttons.addWidget(self.ferror_button)
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
        self.show_profile(s, p, b)
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
        self.fields["Clock"].setText(CLOCK_FIELD.get(info.get("dc_clock"), "DC" if info.get("dc") else
                                                     "no DC") if b else "-")
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

        editable = (self.cia402, self.pdo_set, self.used, self.configure_pdos, self.dc_mode, self.pd, self.st,
                    self.pd_add_object, self.pd_remove, self.pd_add_pdo, self.pd_add_missing)
        for w in editable:
            w.setEnabled(s is not None)
        if s is None:
            self.again.setEnabled(False)
            for w in (self.assign, self.sync0_cycles, self.sync0_shift):
                w.setEnabled(False)
            self.dc_hint.setText("")
            self.dc_warn.hide()
            self.pd.setRowCount(0)
            self.st.setRowCount(0)
            self.notes.hide()
            self.homing_card.hide()
            self.serial_card.hide()
            self.eoe_card.hide()
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
            self.show_mode_marks(s, p)

        self.used.setChecked(s.get("unused") is not True)
        self.configure_pdos.setChecked(bool(s.get("configure-pdos", True)))
        self.pdo_set.clear()
        self.pdo_set.addItem("PDO assignment: the ESI file's default" if p and p.pdo_sets
                             else "PDO assignment: the slave's own", None)
        for name in (p.pdo_sets if p else []):
            self.pdo_set.addItem("PDO assignment: " + name, name)
        self.pdo_set.setCurrentIndex(max(self.pdo_set.findData(pdo_set_in_use(s, p)), 0))
        self.again.setEnabled(b is not None and not win.busy)

        self.show_dc(s)
        notes = list(p.notes) if p else []
        self.notes.setVisible(bool(notes))
        self.notes_text.setText("\n".join(notes))

        self.show_process_data(s)
        self.show_settings(s)
        self.show_homing(s)
        self.show_serial(s)
        self.show_eoe(s)

    # -- Ethernet over EtherCAT

    # key, label, example (shown in the empty field), tooltip
    EOE_FIELDS = (
        ("ip", "The slave's address", "192.168.10.2/24",
         "The slave's IPv4 address with the prefix length (/24 is the mask 255.255.255.0)"),
        ("mask", "Subnet mask", "255.255.255.0",
         "Instead of the prefix length in the slave's address: not both"),
        ("host-ip", "The computer's address", "192.168.10.1",
         "This computer's own address in the slave's subnet. The master gives it to the "
         "slave's network device and brings the device up. Empty: the device is left down, "
         "yours to configure"),
        ("gateway", "Gateway", "none", "An IPv4 address, sent to the slave"),
        ("dns", "DNS server", "none", "An IPv4 address, sent to the slave"),
        ("hostname", "Host name", "none", "At most 31 characters, sent to the slave"),
        ("mac", "MAC address", "the slave's own", "Such as 02:00:00:00:10:21"),
    )

    def show_eoe(self, s):
        b = self.win.session.bus.get(self.position)
        eoe = s.get("eoe")
        self.eoe_card.setVisible(eoe is not None or bool(b and getattr(b.proposal, "has_eoe", False)))
        if self.eoe_card.isHidden():
            return
        on = isinstance(eoe, dict)
        was, self.loading = self.loading, True
        self.eoe_on.setChecked(on)
        self.loading = was
        for key, box in self.eoe_fields.items():
            box.setText(str(eoe[key]) if on and key in eoe else "")
            box.setEnabled(on)
        # the mask is another way to write the prefix length: shown where the file has it
        mask = self.eoe_fields["mask"]
        show_mask = on and "mask" in eoe
        if show_mask != (self.eoe_form.indexOf(mask) >= 0):
            if show_mask:
                self.eoe_form.insertRow(1, self.eoe_mask_label, mask)
            else:
                self.eoe_form.takeRow(mask)     # a hidden row would leave a gap
            mask.setVisible(show_mask)
            self.eoe_mask_label.setVisible(show_mask)
        self.show_eoe_messages(s)

    def show_eoe_messages(self, s):
        eoe = s.get("eoe")
        errors, warnings, hint = [], [], ""
        if eoe is not None:
            errors = config.eoe_errors(eoe)
            cfg = self.win.session.cfg
            who = "slave %s (%s): " % (s.get("position"), s.get("name"))
            networks = config.host_networks() if isinstance(eoe, dict) and "host-ip" in eoe else ()
            warnings = [t[len(who):] for t in config.eoe_warnings(cfg, networks) if t.startswith(who)]
            b = self.win.session.bus.get(self.position)
            if b is not None and not getattr(b.proposal, "has_eoe", True):
                warnings.append("the slave's SII does not list the EoE mailbox protocol: the master "
                                "cannot set its IP address")
        net = config.eoe_network(eoe) if isinstance(eoe, dict) and not errors else None
        if net is not None:
            device = config.eoe_device(s)
            if "host-ip" in eoe:
                hint = ("The computer reaches the slave through its own network device %s, which the "
                        "master sets up while LinuxCNC or a test runs. Try it then: ping %s, or "
                        "http://%s/ in a browser. Only this computer reaches the slave."
                        % (device, net.ip, net.ip))
            else:
                hint = ("The slave gets its address, but the computer's network device %s is left "
                        "down: give the computer's address to reach the slave from here." % device)
        self.eoe_hint.setText(hint)
        self.eoe_hint.setVisible(bool(hint))
        self.eoe_errors.setText("\n".join("Error: " + t for t in errors))
        self.eoe_errors.setVisible(bool(errors))
        self.eoe_warn.setText("\n".join("Warning: " + t for t in warnings))
        self.eoe_warn.setVisible(bool(warnings))

    def eoe_toggled(self, on):
        s = self.win.session.slave(self.position)
        if self.loading or s is None:
            return
        if on:
            s["eoe"] = self.eoe_kept.pop(self.position, None) or config.eoe_suggestion(
                self.win.session.cfg, s, config.host_networks())
        else:
            if isinstance(s.get("eoe"), dict):
                self.eoe_kept[self.position] = s["eoe"]
            s.pop("eoe", None)
        self.show_eoe(s)
        self.win.changed()

    def eoe_edited(self, key):
        s = self.win.session.slave(self.position)
        eoe = (s or {}).get("eoe")
        if self.loading or not isinstance(eoe, dict):
            return
        text = self.eoe_fields[key].text().strip()
        if text == str(eoe.get(key, "")):
            return
        if text:
            eoe[key] = text
        else:
            eoe.pop(key, None)
        self.show_eoe(s)
        self.win.changed()

    # -- serial channels

    SERIAL_COLUMNS = ("Channel", "Port", "Baud rate", "Frame", "Send continuous", "RS485 (half duplex)")

    def show_serial(self, s):
        chans = s.get("serial") or []
        self.serial_card.setVisible(bool(chans))
        while self.serial_grid.count():
            w = self.serial_grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.serial_widgets = []
        if not chans:
            return
        for c, text in enumerate(self.SERIAL_COLUMNS):
            self.serial_grid.addWidget(label(text, "hint"), 0, c)
        for k, ch in enumerate(chans):
            row = k + 1
            w = {}
            name = label("%s  (0x%s/0x%s)" % (ch.get("name"), str(ch.get("output-pdo", ""))[2:],
                                               str(ch.get("input-pdo", ""))[2:]))
            self.serial_grid.addWidget(name, row, 0)
            w["port"] = QLineEdit(str(ch.get("port", "")))
            w["port"].setFont(mono())
            w["port"].setToolTip("The port's path: a program opens it like /dev/ttyUSB0. It is a link "
                                 "that the ecat component makes when it is loaded")
            w["port"].editingFinished.connect(lambda k=k: self.serial_edited(k, "port"))
            self.serial_grid.addWidget(w["port"], row, 1)
            w["baud"] = QuietCombo()
            for b in sorted(config.SERIAL_BAUDS):
                w["baud"].addItem(str(b), b)
            w["baud"].setCurrentIndex(max(w["baud"].findData(ch.get("baud", 9600)), 0))
            w["baud"].setToolTip("At start, until a program sets its own on the port. 300-1200 only on "
                                 "the EL6002/EL6022; 12000 and 14400 on the EL6001/EL6021 from "
                                 "firmware 07")
            w["baud"].activated.connect(lambda *_, k=k: self.serial_edited(k, "baud"))
            self.serial_grid.addWidget(w["baud"], row, 2)
            w["frame"] = QuietCombo()
            for f in config.SERIAL_FRAMES:
                w["frame"].addItem(f, f)
            w["frame"].setCurrentIndex(max(w["frame"].findData(str(ch.get("frame", "8N1"))), 0))
            w["frame"].setToolTip("Data bits, parity, stop bits: the device's. A program on the port "
                                  "cannot change parity and data bits (a pty does not pass them on); "
                                  "its stop bits are used")
            w["frame"].activated.connect(lambda *_, k=k: self.serial_edited(k, "frame"))
            self.serial_grid.addWidget(w["frame"], row, 3)
            w["continuous"] = QCheckBox()
            w["continuous"].setChecked(bool(ch.get("send-continuous")))
            w["continuous"].setToolTip("The terminal sends what the program wrote as a whole, without "
                                       "a pause (0x8000:04). For Modbus RTU above 38400 baud")
            w["continuous"].toggled.connect(lambda *_, k=k: self.serial_edited(k, "continuous"))
            self.serial_grid.addWidget(w["continuous"], row, 4)
            index = self._serial_index(ch, k)
            w["half"] = QCheckBox()
            have_half = bool(self.win.session.object_name(self.position, index, 0x06))
            w["half"].setEnabled(have_half)
            w["half"].setChecked(any(self._is_obj(st, index, 0x06) and st.get("value") for st in
                                     s.get("settings") or []))
            w["half"].setToolTip("0x%04X:06 \"Enable half duplex\" as a setting: for a two-wire RS485 "
                                 "bus" % index if have_half else
                                 "This terminal has no half duplex (or its dictionary is not read)")
            w["half"].toggled.connect(lambda *_, k=k: self.serial_edited(k, "half"))
            self.serial_grid.addWidget(w["half"], row, 5)
            self.serial_widgets.append(w)
        self.show_serial_lines(s)

    @staticmethod
    def _serial_index(ch, k):
        try:
            return int(str(ch.get("settings-index", "0x%04X" % (0x8000 + 0x10 * k))), 16)
        except ValueError:
            return 0x8000 + 0x10 * k

    @staticmethod
    def _is_obj(st, index, sub):
        try:
            return config.parse_obj(st.get("object")) == (index, sub)
        except ValueError:
            return False

    def show_serial_lines(self, s):
        out = []
        chans = s.get("serial") or []
        for ch in chans:
            f = str(ch.get("frame", "8N1"))
            out.append("%sLINK_TYPE=serial\nSERIAL_PORT=%s\nSERIAL_BAUD=%s\nSERIAL_BITS=%s\n"
                       "SERIAL_PARITY=%s\nSERIAL_STOP=%s"
                       % ("# %s.%s\n" % (s.get("name"), ch.get("name")) if len(chans) > 1 else "",
                          ch.get("port", ""), ch.get("baud", 9600), f[0],
                          {"N": "none", "E": "even", "O": "odd"}.get(f[1:2], "none"), f[2:3]))
        text = "\n\n".join(out)
        self.serial_lines.setPlainText(text)
        # as high as the lines: the block spacing is a little more than the font's
        self.serial_lines.setFixedHeight(int(self.serial_lines.fontMetrics().lineSpacing() * 1.25
                                             * (text.count("\n") + 1)) + 16)
        self.serial_hint.setText("Several transactions on one port: give these in the first; the next "
                                 "ones take them over. The HAL check warns when mb2hal's frame "
                                 "differs from the one here.")

    def serial_edited(self, k, what):
        s = self.win.session.slave(self.position)
        chans = (s or {}).get("serial") or []
        if self.loading or k >= len(chans) or k >= len(self.serial_widgets):
            return
        ch, w = chans[k], self.serial_widgets[k]
        if what == "port":
            ch["port"] = w["port"].text().strip()
        elif what == "baud":
            ch["baud"] = w["baud"].currentData()
        elif what == "frame":
            ch["frame"] = w["frame"].currentData()
        elif what == "continuous":
            if w["continuous"].isChecked():
                ch["send-continuous"] = True
            else:
                ch.pop("send-continuous", None)
        elif what == "half":
            index = self._serial_index(ch, k)
            sts = [st for st in s.get("settings") or [] if not self._is_obj(st, index, 0x06)]
            if w["half"].isChecked():
                sts.append({"object": config.obj(index, 0x06), "type": "BOOL", "value": 1})
            s["settings"] = sts
            self.show_settings(s)
        self.show_serial_lines(s)
        self.win.changed()

    # -- distributed clocks

    def show_dc(self, s):
        """The Sync row: free run, the slave's DC modes, or DC mode with user settings
        (any AssignActivate); what the slave says about DC under it."""
        d = self.win.session.dc_info(self.position)
        dc = s.get("dc")
        self.dc_mode.clear()
        self.dc_mode.addItem("Free run (no DC)", DC_FREE)
        for m in (d.dc_modes() if d else []):
            self.dc_mode.addItem("DC mode %s · %s" % (m.label(), m.source), m)
        self.dc_mode.addItem("DC mode with user settings", DC_OWN)
        self.dc_mode.setItemData(self.dc_mode.count() - 1,
                                 "Any AssignActivate, typed in the field", Qt.ToolTipRole)
        if dc is None:
            i = 0
        else:
            assign = dcinfo.parse_assign(dc.get("assign-activate", ""))
            i = next((k for k in range(1, self.dc_mode.count() - 1)
                      if self.dc_mode.itemData(k).assign == assign), self.dc_mode.count() - 1)
        self.dc_mode.setCurrentIndex(i)
        dc = dc or {}
        self.assign.setText(str(dc.get("assign-activate", "")))
        self.sync0_cycles.setValue(int(dc.get("sync0-cycles") or 1))
        self.sync0_shift.setValue(int(dc.get("sync0-shift-ns") or 0))
        self.sync1_cycles.setValue(int(dc.get("sync1-cycles") or 0))
        self.sync1_shift.setValue(int(dc.get("sync1-shift-ns") or 0))
        self.enable_dc_fields()
        self.dc_hint.setText("Distributed clocks, as the slave describes them: " +
                             "; ".join(d.lines()) if d else
                             "Read the bus to see which DC modes the slave has.")
        self.show_dc_warnings(s)

    def enable_dc_fields(self):
        choice = self.dc_mode.currentData()
        on = self.dc_mode.isEnabled()
        self.assign.setEnabled(on and choice == DC_OWN)
        for w in (self.sync0_cycles, self.sync0_shift, self.sync1_cycles, self.sync1_shift):
            w.setEnabled(on and choice != DC_FREE)
        # the SYNC1 fields where SYNC1 is on (AssignActivate 0x0400) or the file has a cycle
        assign = dcinfo.parse_assign(self.assign.text()) or 0
        self.sync1_row.setVisible(choice != DC_FREE and bool(assign & 0x0400 or self.sync1_cycles.value()))

    def dc_cycle_text(self, s):
        """The SYNC0 cycle this slave gets (the servo period times its factor), and what was
        measured on this kind of device at servo periods that were tried ("" for none)."""
        period = self.win.session.period
        shown = period or 1000000          # as the title bar: 1 ms until an INI file or a typed period
        how = "" if period else " (the default: no INI file chosen, none typed on the Master page)"
        dc = s.get("dc")
        out = []
        if dc is not None and dcinfo.parse_assign(dc.get("assign-activate", "")):
            n = int(dc.get("sync0-cycles") or 1)
            out.append("SYNC0 every %s: the servo period %s%s times cycle %d."
                       % (_us_text(shown * n), _us_text(shown), how, n))
        else:
            out.append("Servo period %s%s; this slave runs without SYNC0." % (_us_text(shown), how))
        m = measured.line(s, period) if period else ""
        if m:
            out.append(m + ".")
        return "\n".join(out)

    def show_dc_warnings(self, s):
        self.dc_cycle.setText(self.dc_cycle_text(s))
        found = dcinfo.problems(s, self.win.session.dc_info(self.position), self.win.session.period)
        if s.get("dc"):
            found = found + measured.problems(s, self.win.session.period)
        self.dc_warn.setText("\n".join(("Warning: " if lv == "warning" else "Note: ") + t
                                        for lv, t in found))
        self.dc_warn.setVisible(bool(found))

    def dc_mode_chosen(self, *_):
        if self.loading:
            return
        m = self.dc_mode.currentData()
        if isinstance(m, dcinfo.Mode):
            self.assign.setText("0x%04X" % m.assign)
            self.loading = True
            self.sync0_cycles.setValue(max(int(m.sync0_factor or 1), 1))
            on = bool(m.assign & 0x0400)      # SYNC1 is on: the mode's cycle, else none
            self.sync1_cycles.setValue(int(m.sync1_cycles or 0) if on else 0)
            self.sync1_shift.setValue(int(m.sync1_shift or 0) if on else 0)
            self.loading = False
        elif m == DC_OWN and not self.assign.text().strip():
            self.assign.setFocus()
        self.general_edited()

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
        self.fit_pd_columns()
        self.loading = False
        self.show_pd_status(s)

    PROFILE_TIP = ("How ecat runs the slave. CiA 402: a drive, with the profile's enable, fault "
                   "handling and modes. Modular device: I/O terminals, with encoder and serial "
                   "channels. Generic: every entry a pin. Changing it proposes the slave's PDOs again "
                   "for the profile (asks first; name and settings are kept).")

    def show_profile(self, s, p, b):
        """The profile choice: the one recognised (CoE 0x1000 or the ESI file), the one in
        the file, and generic, which is always there."""
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        if s is None:
            self.profile_box.setEnabled(False)
            self.profile_hint.setText("")
            self.profile_box.blockSignals(False)
            return
        dev = self.win.session.esi_device(self.position)
        recognised = {402: "cia402", 5001: "mdp"}.get(getattr(p, "recognised", None)) or \
            {402: "cia402", 5001: "mdp"}.get(getattr(dev, "profile", None))
        names = [n for n in ("cia402", "mdp", "generic")
                 if n == "generic" or n == recognised or n == s.get("profile")]
        for n in names:
            self.profile_box.addItem(PROFILE_TEXT[n] + ("  (recognised)" if n == recognised else ""), n)
        self.profile_box.setCurrentIndex(max(self.profile_box.findData(s.get("profile")), 0))
        self.profile_box.setEnabled(b is not None and not self.win.busy)
        self.profile_box.setToolTip(self.PROFILE_TIP if b is not None else
                                    "Read the bus to change the profile: the slave's PDOs are proposed again")
        hint = ""
        if recognised:
            where = "the slave's 0x1000" if p is not None and p.profile_source == "CoE 0x1000" else "its ESI file"
            hint = "recognised as %s from %s" % (PROFILE_TEXT[recognised], where)
            if s.get("profile") != recognised:
                hint += "; %s chosen" % PROFILE_TEXT.get(s.get("profile"), s.get("profile"))
        self.profile_hint.setText(hint)
        self.profile_hint.setVisible(bool(self.profile_hint.text()))
        self.profile_box.blockSignals(False)

    def profile_chosen(self, _index):
        s = self.win.session.slave(self.position)
        new = self.profile_box.currentData()
        if s is None or new == s.get("profile"):
            return
        if not self.win.change_profile(self.position, new):
            # cancelled: the box back to the profile in use
            self.profile_box.setCurrentIndex(max(self.profile_box.findData(s.get("profile")), 0))

    def show_mode_marks(self, s, p):
        """Per mode, whether the device's PDOs can give it (from the ESI file)."""
        dev = self.win.session.esi_device(self.position)
        c = s.get("cia402") or {}
        if dev is None or c.get("mode") not in ("csp", "csv", "pv", "pp"):
            self.mode_marks.setText("no ESI file: not known" if dev is None else "")
            return
        supported = None
        if p and p.drive_modes:
            supported = sum(1 << b for b, n in propose.CIA402_MODE_BITS.items() if n in p.drive_modes)
        _whole, each = pdoreq.cia402_verdicts(dev, c["mode"], [], len(c.get("axes") or [1]), supported)
        self.mode_marks.setText(" · ".join("%s %s" % (m.upper(), v.status) for m, v in each.items()))
        self.mode_marks.setToolTip("\n".join("%s: %s" % (m.upper(), "; ".join(v.problems))
                                             for m, v in each.items() if v.problems) or
                                   self.mode_marks.toolTip())

    def show_ways(self, s):
        """The ways to get what the profile and its choices need, the recommended first."""
        while self.pd_ways_lay.count():
            it = self.pd_ways_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
            elif it.layout():
                while it.layout().count():
                    w = it.layout().takeAt(0).widget()
                    if w:
                        w.deleteLater()
        dev = self.win.session.esi_device(self.position)
        ways = pdoreq.ways_for(s, dev) if s.get("configure-pdos", True) is not False else []
        ready = [w for w in ways if w.kind == pdoreq.READY][:3]
        build = [w for w in ways if w.kind == pdoreq.BUILD][:1]
        ways = ready + build
        self.pd_ways.setVisible(bool(ways))
        if not ways:
            return
        self.pd_ways_lay.addWidget(label("Ways to get this (choose one: the PDOs are proposed again "
                                         "with it, the rest of the slave's entry stays):", "hint", wrap=True))
        bus = self.win.session.bus.get(self.position) is not None
        for n, w in enumerate(ways):
            row = QHBoxLayout()
            text = "%s%s: %s" % ("Recommended: " if n == 0 else "", w.title, w.why)
            row.addWidget(label(text, wrap=True), 1)
            use = QPushButton("Use this")
            use.setEnabled(bus and not self.win.busy)
            use.setToolTip("Reads the slave and takes this PDO mapping" if bus else
                           "Read the bus first: the slave is read for this")
            use.clicked.connect(lambda _c=False, way=w: self.win.apply_way(self.position, way.title, way.set_name))
            row.addWidget(use)
            self.pd_ways_lay.addLayout(row)

    def show_pd_status(self, s):
        """The lines under the PDOs: what the mode set lacks, and what the ESI file says
        about the PDOs in the file."""
        errors, warns, notes = [], [], []
        self.pd_adds = []
        if s.get("profile") == "cia402":
            c = s.get("cia402") or {}
            have = set()
            for d in (s.get("process-data") or {}).values():
                for p in d or []:
                    for e in p.get("entries") or []:
                        try:
                            have.add(config.parse_obj(e.get("object")))
                        except ValueError:
                            pass        # config.check names it
            for st in pdoreq.cia402_status(c.get("mode"), [m for m in c.get("modes") or []
                                                           if m != c.get("mode")],
                                           len(c.get("axes") or [1]), have,
                                           mode_settings=pdoreq.mode_settings(s)):
                (errors if st.level == "error" else notes).append(st.text)
                self.pd_adds += st.add if st.level == "error" or any(d == "setting" for _i, d in st.add) else []
        for level, text in self.win.session.pdo_problems(self.position):
            if level == "note" and text in notes:
                continue
            {"error": errors, "warning": warns}.get(level, notes).append(
                text.split("): ", 1)[-1])
        for box, lines in ((self.pd_errors, errors), (self.pd_warn, warns), (self.pd_notes, notes)):
            box.setText("\n".join(lines))
            box.setVisible(bool(lines))
        self.pd_add_missing.setVisible(bool(self.pd_adds))
        self.show_ways(s)

    def _pd_edit(self, action):
        """Run an edit of the slave's process data; show the reason when it is refused."""
        s = self.win.session.slave(self.position)
        if s is None:
            return
        try:
            done = action(s, self.win.session.esi_device(self.position),
                          self.win.session.dictionary(self.position))
        except pdoedit.EditError as e:
            QMessageBox.warning(self, TITLE, str(e))
            return
        if done is None:
            return
        if self.win.session.esi_device(self.position) is None:
            done += " (no ESI file: whether the device takes this mapping is not known)"
        self.win.changed()
        self.show_process_data(s)
        self.win.status(done)

    def add_pd_object(self):
        def action(s, dev, d):
            found = pdoedit.candidates(s, dev, d)
            if not found:
                text, ok = QInputDialog.getText(self, TITLE, "Object to map (such as 0x60F4):")
                if not ok or not text.strip():
                    return None
                try:
                    index = int(text.strip(), 16)
                except ValueError:
                    raise pdoedit.EditError("%s is not an object number" % text)
                direction = None
            else:
                names = ["0x%04X  %s  (%s)" % (i, n or "?", {"rx": "output", "tx": "input"}.get(w, "output or input"))
                         for i, _sub, n, w in found]
                text, ok = QInputDialog.getItem(self, TITLE, "Object to map:", names, 0, False)
                if not ok:
                    return None
                index, _sub, _n, way = found[names.index(text)]
                direction = None
                if way == "rt":
                    kind, ok = QInputDialog.getItem(self, TITLE, "As:", ["output (RX)", "input (TX)"], 0, False)
                    if not ok:
                        return None
                    direction = "rx" if kind.startswith("output") else "tx"
            e = pdoedit.add_object(s, dev, d, index, 0, direction)
            return "Mapped %s" % e["object"]
        self._pd_edit(action)

    def remove_pd_entry(self):
        row = self.pd.currentRow()
        if row < 0 or row >= len(self.pd_rows):
            self.win.status("Select the entry to remove")
            return

        def action(s, dev, _d):
            direction, pi, ei = self.pd_rows[row]
            gone = pdoedit.remove_entry(s, dev, direction, pi, ei)
            return "Removed %s" % gone.get("object")
        self._pd_edit(action)

    def add_pd_pdo(self):
        def action(s, dev, _d):
            found = pdoedit.addable_pdos(s, dev)
            if not found:
                raise pdoedit.EditError("no other PDO can be added: the device's ESI file lists none "
                                        "that is free of the PDOs in use, or it is not known")
            names = ["0x%04X  %s  (%s, %d entries)" % (p.index, p.name, "output" if p.direction == "rx"
                                                       else "input", len(p.entries)) for p in found]
            text, ok = QInputDialog.getItem(self, TITLE, "PDO to add:", names, 0, False)
            if not ok:
                return None
            pdoedit.add_pdo(s, dev, found[names.index(text)].index)
            return "Added PDO 0x%04X" % found[names.index(text)].index
        self._pd_edit(action)

    def add_pd_missing(self):
        adds = list(self.pd_adds)

        def action(s, dev, d):
            added, failed = pdoedit.add_missing(s, dev, d, adds)
            if failed:
                QMessageBox.warning(self, TITLE, "Could not add all:\n\n" + "\n\n".join(failed))
            return "Added %s" % (", ".join(e["object"] for e in added) or "nothing")
        self._pd_edit(action)

    def fit_pd_columns(self):
        """The columns that few entries use are shown where one does (or when asked
        for): a narrow window shows the rest of the row."""
        for c in (self.SCALE, self.OFFSET, self.SPLIT, self.INVERT):
            used = any(self.pd.item(r, c) is not None and self.pd.item(r, c).text()
                       for r in range(self.pd.rowCount()))
            self.pd.setColumnHidden(c, not (used or self.pd_more.isChecked()))
        fit_columns(self.pd, stretch=self.PIN, wide=(self.SPLIT,))
        fit_table(self.pd)

    def pd_more_toggled(self, _on):
        s = self.win.session.slave(self.position)
        if s is not None and not self.loading:
            self.show_process_data(s)

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
        cells = ["%s %s" % ("out" if direction == "outputs" else "in", pdo.get("pdo")),
                 e.get("object"), name, "%s %s" % (e.get("type", ""), e.get("bits")), use]
        for c, v in enumerate(cells):
            self.pd.setItem(r, c, item(v, colour=GREY if v in ("profile", "gap") else None,
                                       font=mono() if c in (0, 1) else None,
                                       tip=str(v) if v and c == 2 else None))
        pin_text = e.get("pin", "")
        if use == "profile":
            ser = next((c.get("name") for c in s.get("serial") or []
                        if str(pdo.get("pdo")).upper() in (str(c.get("output-pdo")).upper(),
                                                           str(c.get("input-pdo")).upper())), None)
            pin_text = "handled by the %s" % ("CiA 402 profile" if s.get("profile") == "cia402"
                                              else "serial channel %s" % ser if ser
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

    def add_init_settings(self):
        added = self.win.session.add_init_settings(self.position)
        s = self.win.session.slave(self.position)
        if s is None or not added:
            return
        self.show_settings(s)
        self.win.changed()
        self.win.status("Added %s" % ", ".join(st["object"] for st in added))

    def show_settings(self, s):
        missing = self.win.session.missing_init_settings(self.position)
        self.init_button.setVisible(bool(missing))
        self.init_button.setToolTip("Adds %s (from the device's ESI file, or the drive's mode)"
                                    % ", ".join("%s = %s" % (st["object"], st.get("value", st.get("data")))
                                                for st in missing) if missing else "")
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
        self.ferror_button.setVisible(s.get("profile") == "cia402")
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
            config.sync_mode_setting(s)
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
        if self.used.isChecked():
            s.pop("unused", None)
        else:
            s["unused"] = True
        choice = self.dc_mode.currentData()
        if choice != DC_FREE:
            dc = s.setdefault("dc", {})
            dc["assign-activate"] = self.assign.text().strip()
            dc["sync0-cycles"] = self.sync0_cycles.value()
            dc["sync0-shift-ns"] = self.sync0_shift.value()
            if self.sync1_cycles.value() and (dcinfo.parse_assign(dc["assign-activate"]) or 0) & 0x0400:
                dc["sync1-cycles"] = self.sync1_cycles.value()
                dc["sync1-shift-ns"] = self.sync1_shift.value()
            else:
                dc.pop("sync1-cycles", None)
                dc.pop("sync1-shift-ns", None)
        else:
            s.pop("dc", None)
        self.enable_dc_fields()
        self.show_dc_warnings(s)
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
            self.pd.setColumnHidden(self.SPLIT, False)
            self.pd.setCurrentCell(row, self.SPLIT)
            self.pd.editItem(self.pd.item(row, self.SPLIT))

    def _refresh_row(self, row, e):
        self.loading = True
        self.pd.item(row, self.USE).setText(self._use(e))
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
        self.fit_pd_columns()
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

    def suggest_following_error(self):
        """Add (or update) the drive's following-error window as a setting."""
        s = self.win.session.slave(self.position)
        if s is None:
            return
        got = integrate.following_error_suggestion(self.win.session.ini_path,
                                                   self.win.session.cfg, s)
        if "object" not in got:
            self.win.status("No following-error window suggested: %s" % got["why"])
            return
        settings = s.setdefault("settings", [])
        old = next((st for st in settings if st.get("object") == got["object"]), None)
        if old is None:
            old = {"object": got["object"]}
            settings.append(old)
        index, sub = config.parse_obj(got["object"])
        d = self.win.session.dictionary(self.position)
        name = d.name(index, sub)
        if name:
            old["name"] = name
        old.update(type=got["type"], value=got["value"])
        self.show_settings(s)
        self.win.status("%s = %d (%s)" % (got["object"], got["value"], got["why"]))
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

    def pdo_set_chosen(self, _index):
        session = self.win.session
        b = session.bus.get(self.position)
        if self.pdo_set.currentData() != pdo_set_in_use(session.slave(self.position),
                                                        b.proposal if b else None):
            self.propose_again()

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
        self.capture = QPushButton("Save capture...")
        self.capture.setToolTip(
            "For a device the developer does not have: one file with every slave's identity, "
            "SII, object dictionary and values, PDOs and registers, the report, the ESI "
            "descriptions and, if tcpdump may run as root, a few seconds of frames. Takes about "
            "a minute; only reads. Run it during the I/O test to have the frames of a bus in OP.")
        self.capture.clicked.connect(win.save_capture)
        row.addWidget(self.capture)
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
        self.master_says = Card("Messages from the master", "Oldest first: what the master said "
                                "about this slave while ecat ran (the I/O test or LinuxCNC), "
                                "from the event log.")
        self.master_table = table(["Time", "Message"], 1)
        self.master_says.add(self.master_table)
        self.master_none = self.master_says.add(label("", "hint", wrap=True))
        body.addWidget(self.master_says)
        lay.addWidget(self.body)

        # the flight recorder: events of the bus while LinuxCNC ran
        self.events = Card("Event log", "What the ecat component recorded: link, states, "
                                        "working counter, drive faults, the stop guard, and the "
                                        "master's own messages (\"master: ...\"). It is kept "
                                        "after LinuxCNC stops; the run before is kept too.")
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
        # what the log means on this computer (explain.py), when a symptom shows
        self.meaning = QFrame()
        self.meaning.setObjectName("meaning")
        ml = QVBoxLayout(self.meaning)
        ml.setContentsMargins(0, 0, 0, 8)
        ml.addWidget(label("WHAT THIS MEANS", "cardTitle"))
        self.meaning_text = label("", wrap=True)
        self.meaning_text.setTextFormat(Qt.RichText)
        self.meaning_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ml.addWidget(self.meaning_text)
        mr = QHBoxLayout()
        self.meaning_timing = QPushButton("Timing tab")
        self.meaning_timing.setToolTip("Measure how late the servo thread runs on this computer.")
        self.meaning_timing.clicked.connect(lambda: win.go("timing"))
        mr.addWidget(self.meaning_timing)
        self.meaning_examine = QPushButton("Examine all")
        self.meaning_examine.setToolTip("Read every slave's port error counters (cable, connectors).")
        self.meaning_examine.clicked.connect(lambda: win.examine(None))
        mr.addWidget(self.meaning_examine)
        mr.addStretch(1)
        ml.addLayout(mr)
        self.meaning.hide()
        self.meaning_key = None
        self.events.add(self.meaning)
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
        said = events.folded(log.texts or [])
        pos = self.win.current if isinstance(self.win.current, int) else None
        self.show_meaning(log, names, path)
        if self.events_slave.isChecked() and pos is not None:
            shown = [e for e in shown if e.slave in (-1, pos)]
            said = [(x, n) for x, n in said if x.slave in (None, pos)]
        self.events_info.setText("ecat %s since %s, %s%s%s" % (
            "runs" if log.running else "ran", events.stamp(log.start), log.config or "?",
            "; %d older events were overwritten" % log.lost if log.lost > 0 else "",
            "" if log.texts is not None else "; this ecat is older and does not record the "
            "master's messages"))
        rows = list(reversed(events.merged(log, shown, said)[-200:]))
        t = self.events_table
        t.setRowCount(len(rows))
        colours = {"info": None, "warn": AMBER, "error": RED}
        marks = {"info": "", "warn": "WARN", "error": "ERROR"}
        texts = events.annotate(log.events, names)
        for r, (kind, x) in enumerate(rows):
            if kind == "event":
                sev, msg = texts[x.index]
                when = x.time
            else:
                sev, msg, when = x[0].severity, events.text_line(*x), x[0].time
            t.setItem(r, 0, item(events.stamp(when), font=mono()))
            t.setItem(r, 1, item(marks[sev], colour=colours[sev]))
            t.setItem(r, 2, item(msg, colour=colours[sev] if kind == "event" else colours[sev] or GREY,
                                 font=mono() if kind == "text" else None))
        fit_table(t, minimum=1)

    def show_meaning(self, log, names, path):
        """What the log means on this computer: judged again when the log or the
        examined slaves change, or after 30 s (the masters, the computer)."""
        win = self.win
        key = (path, log.start, log.total, log.texts_total, id(win.reports), len(win.reports),
               win.session.ini_path, int(time.time() // 30))
        if key == self.meaning_key:
            return
        self.meaning_key = key
        from . import report as report_mod, rtcheck
        ini = win.session.ini_path if win.session.ini_path and os.path.exists(win.session.ini_path) else None
        try:
            ctx = explain.context(log, names, masters=report_mod.master_facts(),
                                  inv=rtcheck.inventory(ini=ini),
                                  interface=((win.session.cfg or {}).get("master") or {}).get("interface"),
                                  reports=list(win.reports.values()))
            found = explain.findings(ctx)
        except Exception as x:      # a bad symptom file must not break the page
            self.meaning_text.setText("The explanation could not be made: %s" % x)
            self.meaning.show()
            return
        self.meaning.setVisible(bool(found))
        if not found:
            return
        self.meaning_text.setText(explain.html(found, {
            "likely": RED.name(), "possible": AMBER.name(), "ruled out": GREEN.name(),
            "not checked": GREY.name()}))
        looks = {c["look"] for f in found for c in f.causes if c["state"] != "ruled out"}
        self.meaning_timing.setVisible("timing" in looks)
        self.meaning_examine.setVisible("examine" in looks)
        self.meaning_examine.setEnabled(not win.busy)

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
            self.headline.setText("The master stopped bringing the slave up")
            self.explain.setText("The slave reports no AL status code: either the master gave up "
                                 "configuring it (such as a DC setting the slave cannot take) or "
                                 "the slave refused and acknowledging the error cleared its code. "
                                 "The master's messages below say which.")
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
            full = sorted({"%d %s" % (p.number, k) for p in r.ports for k in p.saturated})
            self.cable_verdict.setText(
                "No errors grew in %g s; counts since power-on can be old." % r.seconds +
                (" Port 1 is open: this is the last slave." if last else "") +
                ((" The counts at their limit (255+: port %s) cannot show whether the fault is "
                  "still there: write to the counter's register (0x0300, 0x0310 for lost "
                  "links) or power the slave off, then look again." % ", ".join(full))
                 if full else ""))
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

        # the master's messages about it
        path, said = events.slave_messages(pos)
        self.master_table.setRowCount(0)
        for t, n in said[-30:]:
            row = self.master_table.rowCount()
            self.master_table.insertRow(row)
            colour = {"error": RED, "warn": AMBER}.get(t.severity)
            self.master_table.setItem(row, 0, item(events.stamp(t.time), font=mono()))
            self.master_table.setItem(row, 1, item(t.text + (" (%d times)" % n if n > 1 else ""),
                                                   colour=colour, font=mono()))
        fit_table(self.master_table)
        self.master_table.setVisible(bool(said))
        self.master_none.setText(
            ("From the run before (%s)." % path if path and path.endswith(".prev") else "")
            if said else "The master said nothing about this slave in the last two runs of ecat "
            "(its messages are recorded by ecat 0.16 and later).")
        self.master_none.setVisible(not said or bool(path and path.endswith(".prev")))


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
    @property
    def COLOURS(self):
        return {"position": THEMES[_current["theme"]]["bluetext"] if _current["theme"] == "dark"
                else "#2563eb", "following error": RED.name(), "velocity": GREEN.name()}

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
        if getattr(self.win.test_page, "response", None) is not None:
            self.win.test_page.response.show_drive(slave)

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


class RecordingPlot(QWidget):
    """A response recording over its whole length: the command and the
    position, the following error (LinuxCNC's, and the drive's own when
    recorded), and the torque; the previous recording's following error in
    grey behind the new one."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(220)
        self.rec = self.prev = None

    def show_recording(self, rec, prev=None):
        self.rec, self.prev = rec, prev
        self.update()

    @staticmethod
    def lanes(rec):
        """[(title, unit, [(name, values, colour)])] of a recording."""
        d = rec.data
        err = [c - f for c, f in zip(d["cmd"], d["fb"])]
        start = d["cmd"][0] if d["cmd"] else 0.0
        blue = THEMES[_current["theme"]]["bluetext"] if _current["theme"] == "dark" else "#2563eb"
        out = [("position", "mm", [("command", [x - start for x in d["cmd"]], "#9ca3af"),
                                    ("position", [x - start for x in d["fb"]], blue)]),
               ("following error", "mm", [("LinuxCNC", err, RED.name())])]
        if "drive-error" in d:
            sc = float(rec.meta.get("scale") or 1.0) or 1.0
            out[1][2].append(("drive (0x60F4)", [x / sc for x in d["drive-error"]], AMBER.name()))
        if "torque" in d:
            out.append(("torque", "%", [("0x6077", [x / 10.0 for x in d["torque"]], GREEN.name())]))
        return out

    def paintEvent(self, _event):
        if self.rec is None or not len(self.rec):
            return
        p = QPainter(self)
        p.setFont(mono())
        lanes = self.lanes(self.rec)
        prev_err = None
        if self.prev is not None and len(self.prev):
            pd = self.prev.data
            prev_err = [c - f for c, f in zip(pd["cmd"], pd["fb"])]
        n = len(lanes)
        h, w = self.height() / n, self.width()
        period = (self.rec.meta.get("period-ns") or 1000000) / 1e9
        length = max(len(self.rec), len(prev_err or [])) or 1
        for i, (title, unit, series) in enumerate(lanes):
            top = i * h
            values = [v for _, vals, _ in series for v in vals]
            if title == "following error" and prev_err:
                values += prev_err
            lo, hi = min(values), max(values)
            if hi - lo < 1e-9:
                lo, hi = lo - 0.5, hi + 0.5
            pad = (hi - lo) * 0.08
            lo, hi = lo - pad, hi + pad
            p.setPen(QPen(QColor("#9ca3af"), 1))
            p.drawLine(0, int(top + h - 1), w, int(top + h - 1))
            if lo < 0 < hi:
                y0 = top + 4 + hi / (hi - lo) * (h - 10)
                p.setPen(QPen(QColor("#9ca3af"), 1, Qt.DotLine))
                p.drawLine(0, int(y0), w, int(y0))
            drawn = ([("previous", prev_err, "#9ca3af")] if title == "following error" and prev_err
                     else []) + series
            for _name, vals, colour in drawn:
                p.setPen(QPen(QColor(colour), 1.5))
                step = max(len(vals) // max(w, 1), 1)
                prev = None
                for k in range(0, len(vals), step):
                    x = k / length * w
                    y = top + 4 + (hi - vals[k]) / (hi - lo) * (h - 10)
                    if prev:
                        p.drawLine(int(prev[0]), int(prev[1]), int(x), int(y))
                    prev = (x, y)
            p.setPen(QPen(QColor(series[-1][2])))
            p.drawText(6, int(top + 14), "%s (%s): %s" % (title, unit, ", ".join(
                s[0] for s in drawn)))
            p.drawText(6, int(top + h - 5), "%+.4f..%+.4f" % (lo + pad, hi - pad))
        p.setPen(QPen(QColor("#9ca3af")))
        p.drawText(int(w - 70), int(self.height() - 5), "%.2f s" % (length * period))
        p.end()


class ResponseCard(Card):
    """Measuring a CSP drive's response (response.py): a test move recorded
    every servo cycle, and what the following error is made of. Changes
    nothing in the drive; the move runs only while the button is held."""

    FIELDS = [("distance", "Distance (mm, ±)", -1000, 1000, 2),
              ("velocity", "Velocity (mm/s)", 0.1, 5000, 1),
              ("acceleration", "Acceleration (mm/s²)", 1, 100000, 0),
              ("repeats", "Back and forth (times)", 1, 20, 0),
              ("dwell", "Dwell at each end (s)", 0.1, 5, 1)]

    def __init__(self, win):
        super().__init__("Response", "A test move recorded every servo cycle: how far the drive "
                         "lags and why. Nothing in the drive is changed.")
        self.win = win
        self.measurement = None
        self.last = None        # the last Recording, for the next one's grey line
        self.for_drive = None
        top = QHBoxLayout()
        form = QFormLayout()
        self.fields = {}
        for key, text, lo, hi, dec in self.FIELDS:
            box = QDoubleSpinBox()
            box.setRange(lo, hi)
            box.setDecimals(dec)
            box.setValue(response.DEFAULTS[key])
            form.addRow(text, box)
            self.fields[key] = box
        top.addLayout(form)
        side = QVBoxLayout()
        self.objects = label("", "hint", wrap=True)
        side.addWidget(self.objects)
        self.add_button = QPushButton("Add them to the process data")
        self.add_button.setToolTip("Adds the objects to the drive's first input PDO in the "
                                   "configuration; they are recorded once the test is started again")
        self.add_button.clicked.connect(self.add_objects)
        side.addWidget(self.add_button)
        self.measure = QPushButton("Measure (hold)")
        self.measure.setObjectName("hold")
        self.measure.setToolTip("Hold alone: enables the drive and moves it out by the distance and "
                                "back, at the velocity and acceleration above, recording every "
                                "cycle; releasing stops it")
        self.measure.pressed.connect(self.start)
        self.measure.released.connect(self.release)
        side.addWidget(self.measure)
        self.progress = label("", "hint", wrap=True)
        side.addWidget(self.progress)
        side.addStretch(1)
        top.addLayout(side, 1)
        self.add(top)
        self.add(label("The drive's limits on the Move card hold here too: raise its following "
                       "error and max torque for a fast move, or the drive stops it. The travel "
                       "limits the distance.", "hint", wrap=True))
        self.plot = RecordingPlot()
        self.add(self.plot)
        self.result = label("", "mono", wrap=True)
        self.result.setFont(mono())
        self.result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.add(self.result)
        self.saved = label("", "hint", wrap=True)
        self.saved.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.add(self.saved)
        # LinuxCNC's feedforward (0x60B1 / 0x60B2): tried in the test's HAL, kept in the file
        self.ff_box = QWidget()
        fl = QVBoxLayout(self.ff_box)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(label("Feedforward from LinuxCNC", "rule"))
        frow = QHBoxLayout()
        self.ff_fields = {}
        for key, text, tip in (
                ("velocity", "Velocity", "ff-velocity-gain: the share of the command's velocity "
                 "written to 0x60B1 (1.0 for all of it)"),
                ("torque", "Torque per acceleration (% per mm/s²)",
                 "ff-torque-per-accel: written to 0x60B2 times the command's acceleration; a "
                 "measurement with the torque (0x6077) recorded fills it in"),
                ("torque-max", "Limit (%)", "ff-torque-max: the torque offset's limit, % of rated")):
            frow.addWidget(QLabel(text))
            box = QLineEdit()
            box.setMaximumWidth(110)
            box.setToolTip(tip)
            frow.addWidget(box)
            self.ff_fields[key] = box
        frow.addStretch(1)
        fl.addLayout(frow)
        brow = QHBoxLayout()
        self.ff_try = QPushButton("Try in the test")
        self.ff_try.setToolTip("Sets the drive's ff-* parameters in the test's HAL; measure again "
                               "to see the difference. The drive must be set to use 0x60B1 / 0x60B2 "
                               "(a vendor setting in its Settings).")
        self.ff_try.clicked.connect(lambda: self.try_feedforward())   # not clicked's bool
        brow.addWidget(self.ff_try)
        self.ff_off = QPushButton("Off")
        self.ff_off.setToolTip("Feedforward 0 in the test")
        self.ff_off.clicked.connect(lambda: self.feedforward_off())
        brow.addWidget(self.ff_off)
        self.ff_keep = QPushButton("Keep in the configuration")
        self.ff_keep.setToolTip("Writes the values as cia402.feedforward of the slave (save the "
                                "file); Apply then sets the joint's velo-scale for it")
        self.ff_keep.clicked.connect(lambda: self.keep_feedforward())
        brow.addWidget(self.ff_keep)
        brow.addStretch(1)
        fl.addLayout(brow)
        self.ff_now = label("", "hint", wrap=True)
        fl.addWidget(self.ff_now)
        self.ff_box.hide()
        self.add(self.ff_box)
        # the drive's settings that may explain the result (tuning.py)
        row = QHBoxLayout()
        self.look = QPushButton("Look for settings in the drive")
        self.look.setToolTip("Reads the drive's settings its quirk file names and those whose names "
                             "suggest a filter, a gain or feedforward; what the measurement points "
                             "at comes first. Nothing is written.")
        self.look.clicked.connect(self.look_for_settings)
        row.addWidget(self.look)
        row.addStretch(1)
        self.add(row)
        # trying the chosen one: written in RAM, the drive's own value kept;
        # above the table, which can be taller than the window (it was under it, out of sight)
        self.try_row = QWidget()
        tr = QHBoxLayout(self.try_row)
        tr.setContentsMargins(0, 0, 0, 0)
        self.try_label = label("", "hint")
        tr.addWidget(self.try_label)
        self.try_value = QLineEdit()
        self.try_value.setMaximumWidth(140)
        self.try_value.returnPressed.connect(self.try_setting)
        tr.addWidget(self.try_value)
        self.try_button = QPushButton("Try it (write to the drive's RAM)")
        self.try_button.setToolTip("Writes the value to the drive with the drive disabled; its own "
                                   "value is kept and put back by Undo, or when the test stops")
        self.try_button.clicked.connect(self.try_setting)
        tr.addWidget(self.try_button)
        tr.addStretch(1)
        self.try_row.hide()
        self.add(self.try_row)
        self.trials_title = label("Being tried in the drive (RAM). Measure again to see the "
                                  "difference; keep a value that helps, undo one that does not.",
                                  "hint", wrap=True)
        self.trials_title.hide()
        self.add(self.trials_title)
        self.trials_table = QTableWidget(0, 5)
        self.trials_table.setHorizontalHeaderLabels(["Object", "Name", "Drive's own", "Trying", ""])
        self.trials_table.verticalHeader().hide()
        self.trials_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.trials_table.horizontalHeader().setStretchLastSection(True)
        self.trials_table.hide()
        self.add(self.trials_table)
        self.settings_table = QTableWidget(0, 4)
        self.settings_table.setHorizontalHeaderLabels(["Object", "Name", "Now", "Why look at it"])
        self.settings_table.verticalHeader().hide()
        self.settings_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.settings_table.horizontalHeader().setStretchLastSection(True)
        self.settings_table.setWordWrap(True)
        self.settings_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.settings_table.setSelectionMode(QTableWidget.SingleSelection)
        self.settings_table.itemSelectionChanged.connect(self.setting_chosen)
        self.settings_table.hide()
        self.add(self.settings_table)
        self.found = []         # the candidates shown in the settings table
        self.last_res = None    # the analysis of self.last
        self.settings_hint = label("Try one at a time: write it in the SDO card on this page (the "
                                   "drive keeps it in RAM until it is switched off), then measure "
                                   "again; the last measurement stays grey in the plot. One that "
                                   "helps belongs in the slave's Settings, which ecat writes at "
                                   "every start.", "hint", wrap=True)
        self.settings_hint.hide()
        self.add(self.settings_hint)
        self.kinds = []         # what the last result points at, most first

    def drive(self):
        return self.win.test_page.move.test()

    def show_drive(self, slave):
        dt = self.drive()
        ok = dt is not None and dt.prepared and dt.mode == "csp" and not self.win.test.watching \
            if self.win.test is not None else False
        self.setVisible(bool(ok))
        if not ok:
            return
        if self.for_drive != dt.name:
            self.for_drive, self.last = dt.name, None
            self.plot.show_recording(None)
            self.result.setText("")
            self.saved.setText("")
            self.kinds = []
            self.last_res = None
            self.found = []
            self.settings_table.setRowCount(0)
            self.settings_table.hide()
            self.settings_hint.hide()
            self.try_row.hide()
        self.show_trials()
        have = response.measure_entries(slave, dt.axis)
        missing = response.missing_objects(slave, self.win.session.dictionary(slave["position"]),
                                           dt.axis)
        missing = [m for m in missing if m[1] not in have]
        text = "Recorded: pos-cmd, pos-fb%s." % "".join(
            ", %s" % response.MEASURE_OBJECTS[i - dt.axis * config.AXIS_OFFSET][1]
            for i, _e in (have[k] for k in sorted(have)))
        # the test runs the configuration as it was when it started
        running = {c.key for c in response.columns(self.win.test.cfg,
                                                   self.win.test.drives.index(dt.name), dt.name)}
        if set(have) - running:
            text += " Added since the test started: stop the test and start it again to record them."
        if missing:
            text += " Not in the process data: %s." % ", ".join(w for _i, _c, w in missing)
        self.objects.setText(text)
        self.add_button.setVisible(bool(missing))
        self.show_feedforward(slave, dt)

    # -- LinuxCNC's feedforward

    def show_feedforward(self, slave, dt):
        """The feedforward row, for a drive with 0x60B1 or 0x60B2: the fields from
        the test's HAL when they are empty, and what the test runs with now."""
        can = response.feedforward_objects(slave)
        self.ff_box.setVisible(any(can.values()))
        if not any(can.values()):
            return
        self.ff_fields["velocity"].setEnabled(can["velocity"])
        for k in ("torque", "torque-max"):
            self.ff_fields[k].setEnabled(can["torque"])
        now = response.feedforward_now(dt.name)
        for key, box in self.ff_fields.items():
            if not box.text().strip():
                v = now.get(key, response.FF_TORQUE_MAX if key == "torque-max" else 0.0)
                box.setText("%g" % v)
        self.show_feedforward_now(dt, now)

    def show_feedforward_now(self, dt, now=None):
        now = response.feedforward_now(dt.name) if now is None else now
        if not now:
            self.ff_now.setText("")
            return
        ff = (self.win.session.slave(dt.position) or {}).get("cia402", {}).get("feedforward") or {}
        self.ff_now.setText("In the test now: velocity %g, torque %g %% per mm/s², limit %g %%. "
                            "In the configuration: %s." % (
                                now.get("velocity", 0.0), now.get("torque", 0.0),
                                now.get("torque-max", response.FF_TORQUE_MAX),
                                ", ".join("%s %g" % (k, v) for k, v in ff.items()) or "none"))

    def feedforward_values(self):
        """The fields as numbers, or None (a status message says which is wrong)."""
        out = {}
        for key, box in self.ff_fields.items():
            if not box.isEnabled():
                continue
            text = box.text().strip().replace(",", ".")
            try:
                v = float(text) if text else 0.0
            except ValueError:
                self.win.status("Feedforward: %r is not a number" % box.text())
                return None
            if key == "velocity" and not 0.0 <= v <= 2.0:
                self.win.status("Feedforward: the velocity share is 0 to 2 (1.0 for all of it)")
                return None
            if key == "torque-max" and not 0.0 <= v <= 300.0:
                self.win.status("Feedforward: the limit is 0 to 300 % of rated torque")
                return None
            out[key] = v
        return out

    def try_feedforward(self, values=None):
        dt = self.drive()
        if dt is None or (self.measurement is not None and self.measurement.active):
            return
        values = self.feedforward_values() if values is None else values
        if values is None:
            return
        try:
            self.win.test.setp_many(response.feedforward_setp(dt.name, values))
        except testmode.TestError as e:
            self.win.status("Feedforward not set: %s" % e)
            return
        self.win.status("Feedforward in the test: %s; measure again to compare"
                        % ", ".join("%s %g" % kv for kv in values.items()))
        self.show_feedforward_now(dt)

    def feedforward_off(self):
        values = {k: 0.0 for k, box in self.ff_fields.items() if box.isEnabled() and k != "torque-max"}
        self.try_feedforward(values)

    def keep_feedforward(self):
        dt = self.drive()
        values = self.feedforward_values()
        if dt is None or values is None:
            return
        s = self.win.session.slave(dt.position)
        if s is None:
            return
        ff = response.keep_feedforward(s, values)
        self.win.changed()
        self.win.status("cia402.feedforward of %s: %s; save the configuration, then Apply writes "
                        "the joint's velo-scale" % (s.get("name"), ", ".join(
                            "%s %g" % kv for kv in (ff or {}).items()) or "removed"))
        self.show_feedforward_now(dt)

    def spec(self):
        out = {k: box.value() for k, box in self.fields.items()}
        out["repeats"] = int(out["repeats"])
        return out

    def add_objects(self):
        dt = self.drive()
        if dt is None:
            return
        s = dt.slave
        d = self.win.session.dictionary(s["position"])
        missing = response.missing_objects(s, d, dt.axis)
        dev = self.win.session.esi_device(s["position"])
        fixed = [x.index for x in (dev.pdos if dev else []) if x.fixed]
        try:
            added = response.add_objects(s, [i for i, _c, _w in missing], d, fixed)
        except ValueError as e:
            QMessageBox.warning(self, TITLE, str(e))
            return
        self.win.changed()
        self.win.status("Added %s to %s's inputs; save, then stop and start the test to record them"
                        % (", ".join(e["object"] for e in added), s.get("name")))
        self.show_drive(s)

    def look_for_settings(self):
        dt = self.drive()
        if dt is None or (self.measurement is not None and self.measurement.active):
            return
        s, pos = dt.slave, dt.slave["position"]
        d = self.win.session.dictionary(pos)
        kinds = self.kinds or list(response.KINDS)

        def read(index, sub, ttype):
            try:
                return tool.upload(pos, index, sub, ttype)
            except (tool.ToolError, OSError):
                return None

        def work(progress):
            progress("Reading %s's settings..." % s.get("name"))
            return tuning.candidates(s, d, read, kinds)
        self.win.run("Reading the drive's settings...", work, self.show_settings)

    def show_settings(self, found):
        self.found = list(found)
        self.try_row.hide()
        t = self.settings_table
        t.setRowCount(0)
        for c in found:
            r = t.rowCount()
            t.insertRow(r)
            now = "?" if c.value is None else ("%d %s" % (c.value, c.unit)).strip()
            for col, text in enumerate((c.obj, c.name, now, c.why() or response.KINDS.get(c.kind, ""))):
                item = QTableWidgetItem(text)
                if col == 3:
                    item.setToolTip(response.KINDS.get(c.kind, c.kind))
                if c.edge or (c.kind in self.kinds[:1] and c.from_quirk):
                    item.setForeground(AMBER)
                if not c.applies:
                    item.setForeground(GREY)
                t.setItem(r, col, item)
        t.resizeColumnsToContents()
        t.setColumnWidth(1, min(t.columnWidth(1), 280))
        t.resizeRowsToContents()
        t.setMinimumHeight(min(60 + 28 * len(found), 420))
        t.setVisible(bool(found))
        self.settings_hint.setVisible(bool(found))
        if not found:
            self.win.status("No settings found that look like a filter, a gain or feedforward")

    def chosen(self):
        rows = self.settings_table.selectionModel().selectedRows() if self.settings_table.selectionModel() else []
        return self.found[rows[0].row()] if rows and rows[0].row() < len(self.found) else None

    def setting_chosen(self):
        c = self.chosen()
        self.try_row.setVisible(c is not None)
        if c is not None:
            rng = "" if c.minimum is None and c.maximum is None else " (%s..%s)" % (
                "" if c.minimum is None else c.minimum, "" if c.maximum is None else c.maximum)
            self.try_label.setText("New value for %s %s%s:" % (c.obj, c.name, rng))
            self.try_value.setText("" if c.value is None else str(c.value))

    def try_setting(self):
        dt, c = self.drive(), self.chosen()
        if dt is None or c is None or (self.measurement is not None and self.measurement.active):
            return
        pos = dt.slave["position"]
        try:
            e = tuning.try_value(dt.slave, c, self.try_value.text().strip(),
                                 lambda i, sub, t: self._read(pos, i, sub, t),
                                 lambda i, sub, v, t: tool.download(pos, i, sub, v, t))
        except tuning.TrialError as x:
            QMessageBox.warning(self, TITLE, str(x))
            return
        c.value = e["value"]
        self.win.status("%s = %d in %s's RAM (its own %d is kept); measure again"
                        % (c.obj, e["value"], dt.slave.get("name"), e["original"]))
        self.show_settings(self.found)
        self.show_trials()

    @staticmethod
    def _read(pos, index, sub, ttype):
        try:
            return tool.upload(pos, index, sub, ttype)
        except (tool.ToolError, OSError):
            return None

    def show_trials(self):
        dt = self.drive()
        pos = dt.slave.get("position") if dt is not None else None
        entries = [e for e in tuning.pending() if e["position"] == pos]
        t = self.trials_table
        t.setRowCount(0)
        for e in entries:
            r = t.rowCount()
            t.insertRow(r)
            for col, text in enumerate((config.obj(e["index"], e["subindex"]), e.get("name", ""),
                                        str(e["original"]), str(e["value"]))):
                t.setItem(r, col, QTableWidgetItem(text))
            cell = QWidget()
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0)
            keep = QPushButton("Keep as setting")
            keep.setToolTip("Adds it to the slave's Settings in the configuration (written at "
                            "every start); save the file to keep it. Nothing is stored in the "
                            "drive's EEPROM.")
            keep.clicked.connect(lambda _c=False, x=e: self.keep_trial(x))
            back = QPushButton("Undo")
            back.setToolTip("Writes the drive's own value back")
            back.clicked.connect(lambda _c=False, x=e: self.undo_trial(x))
            cl.addWidget(keep)
            cl.addWidget(back)
            t.setCellWidget(r, 4, cell)
        t.resizeColumnsToContents()
        t.resizeRowsToContents()
        t.setMinimumHeight(min(40 + 34 * len(entries), 240))
        t.setVisible(bool(entries))
        self.trials_title.setVisible(bool(entries))

    def keep_trial(self, e):
        s = self.win.session.slave(e["position"])
        if s is None:
            return
        st = tuning.keep(e, s)
        self.win.changed()
        self.win.status("%s = %s is now a setting of %s: save the configuration to keep it"
                        % (st["object"], st["value"], s.get("name")))
        self.show_trials()

    def undo_trial(self, e):
        if self.measurement is not None and self.measurement.active:
            return
        try:
            tuning.undo(e, lambda i, sub, v, t: tool.download(e["position"], i, sub, v, t))
        except (tool.ToolError, OSError) as x:
            QMessageBox.warning(self, TITLE, "Not written back (kept in %s): %s"
                                % (tuning.TRIALS_FILE, x))
            return
        self.win.status("%s back to the drive's own %d" % (config.obj(e["index"], e["subindex"]),
                                                          e["original"]))
        for c in self.found:
            if (c.index, c.subindex) == (e["index"], e["subindex"]):
                c.value = e["original"]
        self.show_settings(self.found)
        self.show_trials()

    def start(self):
        dt = self.drive()
        if dt is None or (self.measurement is not None and self.measurement.active):
            return
        ini = response.ini_values(self.win.session.ini_path, self.win.session.cfg, dt.name)
        try:
            self.measurement = response.Measurement(dt, self.spec(), ini)
            self.measurement.start()
        except (testmode.TestError, OSError) as e:
            self.measurement = None
            self.win.status(str(e))
            return
        self.win.tested_drives.add(dt.name)
        self.progress.setText("Moving: hold the button for about %.0f s, until it says done"
                              % self.measurement.duration())

    def release(self):
        m = self.measurement
        if m is not None and m.active:
            m.abort()

    def stop(self):
        """The test stops: no halsampler is left running."""
        m = self.measurement
        if m is not None and m.active:
            m.abort("the test stopped")
            m._finish()

    def tick(self):
        """From the window's 25 ms timer while a measurement runs."""
        m = self.measurement
        if m is None or not m.active:
            return
        try:
            state = m.step()
        except testmode.TestError as e:
            m.abort(str(e))
            state = m.state
        if state in ("moving", "dwell"):
            self.progress.setText("Moving: %d of %d strokes; keep holding"
                                  % (m.step_no + 1, len(m.targets)))
        elif state == "draining":
            self.progress.setText("Reading the samples...")
        elif state in ("done", "failed"):
            self.finished(m)

    def finished(self, m):
        rec = m.recording
        if rec is None or not len(rec):
            self.progress.setText("Nothing was recorded%s" % (": %s" % m.ended if m.ended else ""))
            return
        res = response.analyse(rec)
        self.kinds = [k for k, _t in res.findings if k in response.KINDS]
        self.progress.setText("Done%s" % (" (ended early: %s)" % m.ended if m.ended else
                                          ": you may release the button"))
        self.plot.show_recording(rec, self.last)
        lines = response.report(res, rec)
        if self.last is not None and self.last_res is not None:
            lines = response.compare(self.last, self.last_res, rec, res) + [""] + lines
        self.result.setText("\n".join(lines))
        # the torque per acceleration measured, for feedforward from LinuxCNC (not set yet)
        box = self.ff_fields["torque"]
        if res.torque_per_accel and box.isEnabled() and m.recording.meta.get("feedforward", {}).get(
                "torque", 0.0) == 0.0:
            box.setText("%.4g" % res.torque_per_accel)
            self.ff_now.setText(self.ff_now.text() + " Torque per acceleration measured: %.4g "
                                "(in the field; Try in the test to use it)." % res.torque_per_accel)
        self.last_res = res
        self.saved.setText("Samples: %s (ecatsetup --response FILE shows this again)" % m.path)
        self.last = rec


def _writer(position):
    """write(index, subindex, value text, type) for a slave, with the ethercat tool."""
    return lambda i, sub, v, t: tool.download(position, i, sub, v, t)


def _duration_text(seconds):
    s = int(seconds)
    return "%d min %d s" % (s // 60, s % 60) if s >= 60 else "%d s" % s


def _us_text(ns):
    """A time in ns as microseconds, or milliseconds from 1 ms."""
    if ns >= 1000000:
        return "%g ms" % (ns / 1e6)
    return "%g us" % (ns / 1000.0)


def _cpu_text(cpus):
    """{0, 1, 2, 3} -> "0-3", {3} -> "3"."""
    out, run = [], []
    for c in sorted(cpus) + [None]:
        if run and (c is None or c != run[-1] + 1):
            out.append("%d" % run[0] if len(run) == 1 else "%d-%d" % (run[0], run[-1]))
            run = []
        if c is not None:
            run.append(c)
    return ",".join(out)


def _cpu_set(text):
    """"0-2,5" -> {0, 1, 2, 5}."""
    out = set()
    for part in (text or "").strip().split(","):
        a, _, b = part.partition("-")
        if a.strip().isdigit():
            out |= set(range(int(a), int(b if b.strip().isdigit() else a) + 1))
    return out


def irq_row(interface, proc="/proc", sys="/sys"):
    """(ok, text) about where the network card's interrupts are handled
    against the CPUs that are isolated for real time, or None when they cannot
    be found. proc and sys are the roots to read (for the tests)."""
    if not interface:
        return None
    pat = re.compile(r"(^|\s)%s(-\S+)?\s*$" % re.escape(interface))
    cpus = set()
    try:
        with open(os.path.join(proc, "interrupts")) as f:
            irqs = [ln.split(":")[0].strip() for ln in f if pat.search(ln.rstrip()) and ln.split(":")[0].strip().isdigit()]
        for n in irqs:
            with open(os.path.join(proc, "irq", n, "smp_affinity_list")) as f:
                cpus |= _cpu_set(f.read())
    except OSError:
        return None
    if not cpus:
        return None
    try:
        with open(os.path.join(sys, "devices", "system", "cpu", "isolated")) as f:
            isolated = _cpu_set(f.read())
    except OSError:
        isolated = set()
    name = "NIC interrupts (%s)" % interface
    if isolated and cpus <= isolated:
        return True, "%s are on CPU %s, isolated for real time (the servo thread's)" % (name, _cpu_text(cpus))
    if isolated:
        return False, ("%s are on CPU %s, while CPU %s is the isolated one: a frame's arrival waits behind "
                       "the GUI and other work (master: irq-cpu: %d pins them there)"
                       % (name, _cpu_text(cpus), _cpu_text(isolated), min(isolated)))
    return None, "%s are on CPU %s; no CPU is isolated for real time (isolcpus=)" % (name, _cpu_text(cpus))


def room_for_two(period, offsets, margin, input_margin):
    """The room the automatic SYNC0 shift has for two cycles of turn-around, in ns:
    the period less both margins less the spread of the frames' send time (negative
    when it has to place SYNC0 for three)."""
    return period - margin - input_margin - (max(offsets) - min(offsets))


def sync_rows(cfg, get, offsets, period, pll=(), irq=None):
    """[(ok, text)] about the bus's synchronisation while the test runs.
    get(name): an ecat.0 pin's value as text, or None; offsets: send-offset
    samples (ns); period: the servo period (ns); pll: pll-err samples (ns);
    irq: irq_row's result."""
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
        rows.append((phased, "The servo thread follows the reference clock%s (%s)"
                     % ("" if phased else ": not locked yet", pll_text(get, pll))))
    else:
        rows.append((True, "The slaves follow the servo thread (the servo period is not adjusted; "
                     "%s is only measured)" % pll_text(get, pll)))
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
        # the room the automatic shift has: what decided "three cycles" on the plasma cutter at 125 us
        try:
            margin = int(get("auto-shift-margin"))
            input_margin = int(get("auto-shift-input-margin"))
        except (TypeError, ValueError):
            margin = input_margin = min(50000, period // 6)
        room = room_for_two(period, offsets, margin, input_margin)
        rows.append((room >= period // 10, "Room for two cycles: %+d us (%g + %g us of margin, the frames' send time "
                     "spreads %g us)%s" % (room // 1000, margin / 1000, input_margin / 1000,
                                           (max(offsets) - min(offsets)) / 1000,
                                           "" if room >= period // 10 else
                                           ": with less than a tenth of the period left, a late frame puts SYNC0 in the three-cycle position")))
    if irq is not None:
        rows.append(irq)
    # what was measured on the kinds of device that run with SYNC0 here
    seen = {}
    for s in dc_slaves:
        res = measured.results(s)
        v = measured.verdict(res, period)
        if v is not None:
            seen.setdefault((s.get("device") or s.get("name"), v), []).append(s.get("name"))
    for (dev, v), names in seen.items():
        rows.append(({"ok": True, "unknown": None, "bad": False}[v[0]],
                     "%s (%s), measured on the bus: %s" % (dev, ", ".join(n for n in names if n), v[1])))
    return rows


def pll_text(get, pll):
    """"pll-err 120 ns, -800..900 ns over the last 50 samples"."""
    now = get("pll-err") or "?"
    if len(pll) >= 5:
        return "pll-err %s ns, %d..%d ns over the last %d samples" % (now, min(pll), max(pll), len(pll))
    return "pll-err %s ns" % now


class TestPage(QWidget):
    def show_sync(self, get, period):
        """Fill the synchronisation card from the ecat.0 pins (get)."""
        try:
            self.offsets = (self.offsets + [int(get("send-offset"))])[-50:]
        except (TypeError, ValueError):
            pass
        try:
            self.pll = (self.pll + [int(get("pll-err"))])[-50:]
        except (TypeError, ValueError):
            pass
        iface = ((self.win.session.cfg or {}).get("master") or {}).get("interface")
        rows = sync_rows(self.win.session.cfg, get, self.offsets, period, self.pll, irq_row(iface))
        for (mark, text), row in zip(self.sync_rows, rows + [None] * len(self.sync_rows)):
            mark.setVisible(row is not None)
            text.setVisible(row is not None)
            if row is None:
                continue
            ok, t = row
            set_pill(mark, "✓" if ok else "!" if ok is False else "–",
                     "green" if ok else "red" if ok is False else "grey")
            text.setText(t)
        # how long the bus has run well: all slaves in OP, no DC failure, two cycles
        good = (get("all-op") == "TRUE" and get("dc-sync-failed") != "TRUE"
                and (get("turnaround-cycles") in ("2", "0", None) or
                     ((self.win.session.cfg or {}).get("master", {}).get("dc", {}).get("sync0-shift-ns") != "auto")))
        now = time.monotonic()
        if not good:
            self.ok_since = None
        elif self.ok_since is None:
            self.ok_since = now
        self.remember_text.setText(
            "Running well for %s at %s." % (_duration_text(now - self.ok_since), _us_text(period)) if self.ok_since
            and period else "Not running well yet (all slaves in OP, two cycles)." if period else
            "The servo period is not known.")
        self.sync_card.show()

    def remember_period(self):
        """Record what this run shows about the drives that use SYNC0 (measured.py)."""
        win = self.win
        cfg = win.session.cfg or {}
        period = win.session.period
        slaves = [s for s in cfg.get("slaves") or [] if s.get("dc") and not s.get("unused")
                  and quirks.identity(s) and s.get("position") is not None]
        if not period or not slaves:
            QMessageBox.information(self, TITLE, "There is no servo period, or no slave that uses SYNC0.")
            return
        states = {}
        for s in slaves:
            try:
                st, _, code = diagnose.al_status(s["position"])
            except Exception as e:                      # the master tool or a register read failed
                QMessageBox.warning(self, TITLE, "Could not read slave %s's state: %s" % (s["position"], e))
                return
            if st != 8 and not code:
                # the master acknowledged the slave's error, which cleared its own code: the master keeps it
                try:
                    code = int(tool.slave_info(s["position"]).get("al_status_code") or 0)
                except Exception:
                    code = 0
            states[s["position"]] = (st, code)
        # one entry per kind of device
        kinds = {}
        for s in slaves:
            kinds.setdefault(quirks.identity(s), []).append(s)
        notin = [s for s in slaves if states[s["position"]][0] != 8]
        if not notin:
            secs = time.monotonic() - self.ok_since if self.ok_since else 0
            if secs < 60:
                QMessageBox.information(self, TITLE, "The bus has run well for %s. Let it run for a minute or "
                                        "more first: a result is worth more after a while under the load you use."
                                        % _duration_text(secs))
                return
            names = ", ".join("%s (%s)" % (g[0].get("device") or g[0].get("name"), ", ".join(x["name"] for x in g))
                              for g in kinds.values())
            if QMessageBox.question(self, TITLE, "Keep that %s works at the servo period %s: all slaves in OP for %s?"
                                    % (names, _us_text(period), _duration_text(secs)),
                                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            note = "Test page: all slaves in OP for %s, turn-around %s" % (_duration_text(secs), "2 cycles")
            for g in kinds.values():
                path = measured.record(g[0], period, "ok", note=note)
        else:
            dc_codes = (0x0030, 0x0035, 0x0036, 0x0037)
            refused = [s for s in notin if states[s["position"]][1] in dc_codes]
            other = [s for s in notin if s not in refused]
            if not refused:
                QMessageBox.information(self, TITLE, "%s not in OP, with AL status %s: that is not a SYNC0 cycle "
                                        "problem, so nothing is kept." % (
                                            ", ".join(s["name"] for s in notin),
                                            ", ".join("0x%04X" % states[s["position"]][1] for s in notin)))
                return
            names = ", ".join("%s (%s, AL 0x%04X)" % (s.get("device") or s.get("name"), s["name"], states[s["position"]][1])
                              for s in refused)
            if QMessageBox.question(self, TITLE, "Keep that %s refused the servo period %s?%s" % (
                    names, _us_text(period), ("\n\nNot kept: %s (another reason)." % ", ".join(s["name"] for s in other)) if other else ""),
                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            seen = set()
            for s in refused:
                if quirks.identity(s) in seen:
                    continue
                seen.add(quirks.identity(s))
                path = measured.record(s, period, "refused", al_status=states[s["position"]][1],
                                       note="Test page: did not go to OP")
        win.status("Kept in %s" % path)
        if win.slave_page.position is not None:
            win.slave_page.show_slave(win.slave_page.position)        # its measured line

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
        for _ in range(12):
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
        self.pll = []
        self.ok_since = None         # monotonic time since which the bus has run well (OP, two cycles)
        brow = QHBoxLayout()
        self.remember = QPushButton("Remember this period for the drives...")
        self.remember.setToolTip(
            "Keep what this run shows about the drives' SYNC0 cycle: after a minute or more with all "
            "slaves in OP and the turn-around at two cycles, that the servo period works; when a drive "
            "does not go to OP because it does not take the cycle (AL status 0x0030, 0x0035, 0x0036), "
            "that it refused it. Written to your own file, ~/.config/ecatsetup/measured/sync0.yaml, and "
            "shown on the slave's page; only the periods that were tried count.")
        self.remember.clicked.connect(self.remember_period)
        brow.addWidget(self.remember)
        self.remember_text = label("", "hint", wrap=True)
        brow.addWidget(self.remember_text, 1)
        self.sync_card.add(brow)
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
        self.response = ResponseCard(win)
        self.response.hide()
        dl.addWidget(self.response)
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
        # a text field, not a spin box: the value is kept and shown with all its
        # digits (66.6666666666666), and typed freely; taken when you leave it
        self.scale = QLineEdit()
        self.scale.setFont(mono())
        self.scale.setMinimumWidth(160)
        self.scale.setToolTip("The drive's pos-scale: counts per machine unit (mm or degree), "
                              "with . as the decimal point")
        self.scale.editingFinished.connect(self.scale_edited)
        lay.addWidget(self.scale)
        self.value = 1.0          # the scale shown, as a number
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
        self.show_scale(float(j.get("scale", 1.0)) if j else 1.0)
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
        self.show_scale(value)
        self.edited()

    @staticmethod
    def scale_text(value):
        """A scale as typed: all its digits, . as the decimal point, no needless .0."""
        text = repr(float(value))
        return text[:-2] if text.endswith(".0") else text

    def show_scale(self, value):
        self.value = float(value)
        self.scale.setText(self.scale_text(value))
        self.scale.setCursorPosition(0)

    def scale_edited(self):
        text = self.scale.text().strip()
        try:
            value = float(text)
            if value != value or abs(value) == float("inf"):
                raise ValueError
        except ValueError:
            self.page.win.status("joint.%d: %r is not a number (use . as the decimal point)"
                                 % (self.joint, text))
            self.show_scale(self.value)
            return
        if value == self.value:
            return
        self.value = value
        self.edited()

    def edited(self, *_):
        cfg = self.page.win.session.cfg
        if cfg is None:
            return
        name = self.drive.currentData()
        scale = self.value
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
        # always shown: the INI file in use, and a button to choose another
        # one or to read the same one again
        self.ini_row = QWidget()
        nl = QHBoxLayout(self.ini_row)
        nl.setContentsMargins(0, 0, 0, 0)
        self.ini_text = label("", "hint", wrap=True)
        nl.addWidget(self.ini_text, 1)
        self.ini_button = QPushButton("INI file...")
        self.ini_button.setToolTip("Choose the machine's INI file, or the same one to read it again")
        self.ini_button.clicked.connect(win.choose_ini)
        nl.addWidget(self.ini_button)
        self.axes.add(self.ini_row)
        # LinuxCNC writes the .expanded file again at every start
        self.expanded_warning = self.axes.add(label(wrap=True))
        self.expanded_warning.setObjectName("warnText")
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

        # an lcec configuration: the old pins and what takes their place
        self.lcec_card = Card("From lcec",
                              "Your HAL files use lcec. Each pin of lcec (and of the cia402 "
                              "component, where the profile does not replace it) needs the ecat "
                              "pin that takes its place, or to be taken out; writing then "
                              "comments those lines out and puts them back with the new pins.")
        self.lcec_text = self.lcec_card.add(label("", "hint", wrap=True))
        self.lcec_problems = self.lcec_card.add(label("", "problemText", wrap=True))
        row = QHBoxLayout()
        self.lcec_names = QPushButton("Name the slaves as lcec did")
        self.lcec_names.clicked.connect(self.do_lcec_names)
        row.addWidget(self.lcec_names)
        self.lcec_suggest = QPushButton("Use the suggestions")
        self.lcec_suggest.setToolTip("Choose the suggested pin for every old pin that has no "
                                     "choice yet")
        self.lcec_suggest.clicked.connect(self.do_lcec_suggestions)
        row.addWidget(self.lcec_suggest)
        row.addStretch(1)
        self.lcec_card.lay.addLayout(row)
        self.lcec_table = table(["Old pin", "Used in", "New pin", "", "Suggested because"], 4)
        self.lcec_table.setWordWrap(True)
        self.lcec_table.setTextElideMode(Qt.ElideNone)
        self.lcec_card.add(self.lcec_table)
        self.lcec_settings_text = self.lcec_card.add(label("", "hint", wrap=True))
        self.lcec_settings = table(["Slave", "Object", "Name", "lcec wrote", "Now", ""], 2)
        self.lcec_card.add(self.lcec_settings)
        row = QHBoxLayout()
        self.lcec_add_all = QPushButton("Add all these settings")
        self.lcec_add_all.clicked.connect(lambda: self.add_lcec_settings(self.lcec_hints))
        row.addWidget(self.lcec_add_all)
        row.addStretch(1)
        self.lcec_card.lay.addLayout(row)
        self.lcec_found = None
        self.lcec_rows = []
        self.lcec_hints = []
        lay.addWidget(self.lcec_card)

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
        if self.win.tabs.currentWidget() is not self:
            # built when the page is shown (show_current): its pin table is
            # the slowest part of the window to fill
            return
        self.yaml.setPlainText(s.dumps())
        self.show_pins()
        self.show_params()
        if True:
            self.show_joints()
            self.show_lcec()
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
            colour = RED if f.severity == "error" else AMBER
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
        self.hal_signals, self.io_proposal, self.hal_uses, self.hal_sigs = {}, {}, {}, {}
        if ini and s.cfg is not None:
            try:
                self.hal_signals = integrate.signals(integrate.user_hal_files(ini), ini)
                self.io_proposal = integrate.propose_nets(ini, s.cfg)
                self.hal_uses, self.hal_sigs = halcheck.uses(ini)
            except OSError:
                pass
        own = integrate.slave_pins(s.cfg)
        chosen = fromlcec.choices(s.cfg)
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
            elsewhere = [u for u in self.hal_uses.get(pin, []) if u.signal and
                         u.file not in (integrate.LOAD_FILE, integrate.MAIN_FILE)]
            if net is None and elsewhere:
                # connected by the HAL files as they are now (by hand, or by the From lcec
                # card): shown, not edited here, since a net here would be a second signal
                self.pins.setItem(r, 5, hal_connection_item(pin, elsewhere, self.hal_sigs))
                continue
            olds = sorted(o for o, n in chosen.items() if n == pin)
            planned = {sig: sorted(chosen.get(x, x) or "" for x in pins if x not in olds)
                       for sig, pins in self.hal_signals.items() if any(o in pins for o in olds)}
            if net is None and planned:
                # chosen on the From lcec card, in the HAL files once applied
                it = item("  ".join("%s: %s" % (sig, " ".join(x for x in ps if x)) if any(ps)
                                    else sig for sig, ps in sorted(planned.items())),
                          font=mono(), colour=GREY)
                it.setToolTip("Takes the place of %s (From lcec card); written into your HAL "
                              "files by Apply, on this page." % ", ".join(olds))
                self.pins.setItem(r, 5, it)
                continue
            others = [x for x in (net or {}).get("pins", []) if x != pin]
            box = QuietCombo()
            box.setEditable(True)
            box.setInsertPolicy(QComboBox.NoInsert)
            box.addItem("")
            for x in offered:
                k = integrate.known_pin(x)
                if k and k[0] == htype and (k[1] == "io" if way == "io" else k[1] not in (way, "io")):
                    box.addItem(x)
            for sig, sig_pins in sorted(self.hal_signals.items()):
                # a signal of the HAL files that has a LinuxCNC pin of the right kind on it
                if any((k := integrate.known_pin(x)) and k[0] == htype and
                       (k[1] == "io" if way == "io" else k[1] not in (way, "io"))
                       for x in sig_pins):
                    box.addItem(sig)
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

    TAKE_OUT = "(take it out)"

    def show_lcec(self):
        """The card From lcec: the old pins of an lcec configuration, and lcec's settings."""
        s, ini = self.win.session, self.ini()
        self.lcec_found, self.lcec_rows, self.lcec_hints = None, [], []
        t = self.lcec_table
        t.setRowCount(0)
        if ini is not None and s.cfg is not None:
            try:
                self.lcec_found = fromlcec.find(ini)
            except OSError:
                self.lcec_found = None
        f = self.lcec_found
        self.lcec_card.setVisible(f is not None)
        if f is None:
            return
        cfg = s.cfg
        allrows = fromlcec.rows(f, cfg)
        self.lcec_rows = rows = [r for r in allrows if not r.auto]
        undecided = [r for r in rows if not r.decided]
        auto = len(allrows) - len(rows)
        text = []
        if f.xml_path and f.xml is not None:
            text.append("Suggestions from %s." % os.path.basename(f.xml_path))
        text += [n[0].upper() + n[1:] + "." for n in f.notes]
        text += [n + "." for n in fromlcec.glue_notes(f, cfg)]
        if auto:
            text.append("%d pin%s of the cia402 component and the lcec pins it connects to %s "
                        "taken care of by the profile." % (auto, "" if auto == 1 else "s",
                                                         "is" if auto == 1 else "are"))
        text.append("%d of %d old pins still need a choice." % (len(undecided), len(rows))
                    if undecided else "Every old pin has its new pin, or is taken out.")
        self.lcec_text.setText(" ".join(text))
        self.lcec_problems.setText("\n".join("\u2717 " + x for x in f.problems))
        self.lcec_problems.setVisible(bool(f.problems))
        self.lcec_suggest.setVisible(any(r.suggestion for r in undecided))
        names = fromlcec.names(f, cfg)
        self.lcec_names.setVisible(bool(names))
        self.lcec_names.setToolTip("Rename " + ", ".join("%s to %s" % kv for kv in names.items()))
        pins = sorted(p for p in halpins.pins(cfg) if not p.startswith("ecat.")) + \
            sorted(p for p in halpins.pins(cfg) if p.startswith("ecat."))
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            colour = None if r.decided else RED
            t.setItem(i, 0, item(r.pin, font=mono(), colour=colour,
                                 tip=None if r.decided else "No new pin chosen yet"))
            t.setItem(i, 1, item(", ".join("%s:%d" % u for u in r.uses)))
            box = QuietCombo()
            box.setEditable(True)
            box.setInsertPolicy(QComboBox.NoInsert)
            box.addItem("")
            box.addItem(self.TAKE_OUT)
            if r.suggestion:
                box.addItem(r.suggestion)
            for x in pins:
                if x != r.suggestion:
                    box.addItem(x)
            box.setEditText("" if not r.decided else r.new or self.TAKE_OUT)
            box.lineEdit().setPlaceholderText(r.suggestion or "")
            box.setFont(mono())
            box.setMinimumWidth(260)
            box.setToolTip("The ecat pin that takes the place of %s in your HAL files, or %s: "
                           "its lines go" % (r.pin, self.TAKE_OUT))
            box.lineEdit().editingFinished.connect(lambda r=r, b=box: self.lcec_edited(r, b))
            box.activated.connect(lambda _i, r=r, b=box: self.lcec_edited(r, b))
            t.setCellWidget(i, 2, box)
            if r.suggestion and not r.decided:
                use = QPushButton("Use")
                use.setToolTip("Use the suggestion %s for %s" % (r.suggestion, r.pin))
                use.clicked.connect(lambda _c=False, r=r: self.lcec_use(r))
                t.setCellWidget(i, 3, use)
            else:
                t.setItem(i, 3, item(""))
            t.setItem(i, 4, item(r.why, tip=r.why))
        t.setVisible(bool(rows))
        t.resizeRowsToContents()
        fit_table(t, minimum=1)
        # lcec's settings, as suggestions
        dicts = {x.idx: s.dictionary(x.idx) for x in (f.xml.slaves if f.xml else [])}
        self.lcec_hints = hints = fromlcec.setting_hints(f, cfg, dicts)
        st = self.lcec_settings
        st.setRowCount(len(hints))
        for i, h in enumerate(hints):
            st.setItem(i, 0, item(h.slave, font=mono()))
            if h.zero_at_start:
                st.setItem(i, 1, item("cia402.zero-at-start", font=mono()))
                st.setItem(i, 2, item(h.what, tip=h.what))
                st.setItem(i, 3, item("true"))
                st.setItem(i, 4, item("false"))
            else:
                st.setItem(i, 1, item(h.setting["object"], font=mono()))
                st.setItem(i, 2, item(h.setting.get("name", ""), tip=h.what))
                v = h.setting.get("value", h.setting.get("data"))
                ttype = h.setting.get("type")
                st.setItem(i, 3, item(show_value(v, ttype, self.win.hex_values), font=mono(),
                                      tip=h.what))
                st.setItem(i, 4, item("not set" if h.now is None else
                                      show_value(h.now, ttype, self.win.hex_values), font=mono()))
            add = QPushButton("Add")
            add.clicked.connect(lambda _c=False, h=h: self.add_lcec_settings([h]))
            st.setCellWidget(i, 5, add)
        st.setVisible(bool(hints))
        fit_table(st, minimum=1)
        self.lcec_add_all.setVisible(len(hints) > 1)
        self.lcec_settings_text.setText(
            "lcec wrote these settings at every start; the configuration does not have them, or "
            "has another value. A drive that does not keep them has its own values again after "
            "a power cycle. Add those that are still wanted." if hints else
            "" if f.xml is None else "lcec's settings are all in the configuration.")

    def lcec_edited(self, row, box):
        cfg = self.win.session.cfg
        if cfg is None:
            return
        text = box.currentText().strip()
        if text == "":
            if row.decided:
                fromlcec.set_choice(cfg, row.pin, None, decided=False)
                QTimer.singleShot(0, self.win.changed)
            return
        new = None if text == self.TAKE_OUT else text
        if new is not None and new not in halpins.pins(cfg):
            self.win.statusBar().showMessage("%s is not a pin or parameter of this configuration"
                                             % new, 10000)
            QTimer.singleShot(0, self.show_lcec)
            return
        if row.decided and row.new == new and not row.auto:
            return
        fromlcec.set_choice(cfg, row.pin, new)
        QTimer.singleShot(0, self.win.changed)

    def lcec_use(self, row):
        """Take one row's suggestion."""
        cfg = self.win.session.cfg
        if cfg is None or not row.suggestion:
            return
        fromlcec.set_choice(cfg, row.pin, row.suggestion)
        QTimer.singleShot(0, self.win.changed)

    def do_lcec_suggestions(self):
        cfg = self.win.session.cfg
        if cfg is None:
            return
        for r in self.lcec_rows:
            if not r.decided and r.suggestion:
                fromlcec.set_choice(cfg, r.pin, r.suggestion)
        self.win.changed()

    def do_lcec_names(self):
        cfg = self.win.session.cfg
        if cfg is None or self.lcec_found is None:
            return
        renamed = fromlcec.take_names(self.lcec_found, cfg)
        self.win.status("Renamed " + ", ".join("%s to %s" % kv for kv in renamed.items()))
        self.win.changed()

    def add_lcec_settings(self, hints):
        cfg = self.win.session.cfg
        if cfg is None or not hints:
            return
        for h in hints:
            fromlcec.add_setting(cfg, h)
        self.win.changed()

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
            colour = (GREEN if r.status == "connected" else
                      AMBER if r.status == "simulated" else RED)
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
        signal = None
        ini = self.ini()
        if ini and others:
            # a signal's name stands for the pins on it in the HAL files
            try:
                g = wiring.graph(ini, cfg)
                pins, signal, dropped, conflicts = wiring.expand_signals(
                    g, integrate.slave_pins(cfg).get(pin, (None, "out"))[1], others)
            except OSError:
                pins, signal, dropped, conflicts = others, None, [], []
            if conflicts:
                QMessageBox.information(
                    self, "Signal with another writer",
                    "%s is written by %s in your HAL files. A signal has one writer: take that "
                    "line out of your HAL files, or choose another signal." % (
                        signal or "The signal", ", ".join(conflicts)))
                QTimer.singleShot(0, self.win.changed)
                return
            if dropped:
                self.win.statusBar().showMessage(
                    "%s: %s gives way (it is a simulation)" % (signal or "signal",
                                                               ", ".join(dropped)), 10000)
            others = pins
        net = integrate.net_of(cfg, pin)
        if others == [x for x in (net or {}).get("pins", []) if x != pin]:
            return
        integrate.set_net(cfg, pin, others, signal)
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
        self.ini_text.setText(
            "INI file: %s" % ini if ini else
            "Choose the machine's INI file to connect its joints to drives.")
        self.ini_button.setText("Other INI file or read again..." if ini else "INI file...")
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
        # the proposal follows the configuration (a drive renamed, a profile chosen)
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
                # an lcec configuration's cia402 component had the scale
                for name, scale in self.proposal.values():
                    self.known_scales.setdefault(name, scale)
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
        if QMessageBox.question(
                self, TITLE, "Write these files in %s?\n\n%s\n\nEvery existing file is kept as "
                ".bak. LinuxCNC must not run with this configuration now." % (
                    d, write_summary(self.yaml_name(), self.plan)),
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

# -- the computer's timing (the real-time check)

RT_WAYS = [("attach", "Beside LinuxCNC that runs"), ("threads", "The threads only (halrun, no I/O)")]
RT_LOADS = [("as-used", "As the computer is used"), ("rest", "At rest"), ("stress", "With load (stress-ng)")]
RT_LENGTHS = [(60, "1 min"), (300, "5 min"), (900, "15 min"), (3600, "1 h")]


def rtflow_item(finding):
    from . import rtflow
    return rtflow.item_key(finding)


class FindingView(QFrame):
    """One finding: what was seen, why, what to do, and in Improve mode Try."""

    def __init__(self, page, f, n):
        super().__init__()
        self.setObjectName("finding")
        self.f = f
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 10)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.addWidget(label("%d. %s" % (n, f.title), "rowTitle"), 1)
        kind = {"runtime": "run-time change", "permanent": "needs a restart", "bios": "in the BIOS",
                "advice": "advice", "measure": "measure again"}
        # plain text, not pills: they looked like buttons
        head.addWidget(label("%s · gain %s" % (kind.get(f.kind, f.kind), f.gain), "hint"))
        if f.kind == "runtime" and getattr(f, "commands", None):
            head.addWidget(label("no restart", "hint"))
        self.try_button = QPushButton("Try it")
        self.try_button.setToolTip("Run the command(s) below as root now (sudo), and measure "
                                   "again the same way to see what it did. Undo is one press; "
                                   "no restart is needed.")
        self.try_button.clicked.connect(lambda: page.try_finding(f))
        head.addWidget(self.try_button)
        lay.addLayout(head)
        rule = getattr(f, "rule", None)

        def line(name, text):
            if text:
                lab = label("<b>%s</b> %s" % (name, html.escape(" ".join(str(text).split()))), wrap=True)
                lab.setTextFormat(Qt.RichText)
                lay.addWidget(lab)
        line("Seen:", f.evidence)
        line("Why:", getattr(f, "cause", None))
        line("What to do:", getattr(f, "action", None))
        cmds = list(getattr(f, "commands", []) or [])
        kp = getattr(f, "kernel_param", None)
        if cmds or kp:
            code = QPlainTextEdit("\n".join(["# Try it runs, as root (no restart):"] + cmds if cmds else []) +
                                  ("\n" if cmds and kp else "") +
                                  (("# optional, later, to keep it for good (a kernel parameter: needs a "
                                    "restart; not done here): %s" if f.kind == "runtime" else
                                    "# kernel parameter (needs a restart; not done here): %s") % kp
                                   if kp else ""))
            code.setReadOnly(True)
            code.setFont(mono())
            code.setFixedHeight(22 * (len(cmds) + (2 if cmds else 0) + (1 if kp else 0)) + 8)
            lay.addWidget(code)
        line("Risk:", getattr(f, "risk", None))
        line("Cost:", getattr(f, "cost", None))
        line("Check:", getattr(f, "check", None))
        undo = getattr(f, "undo", None) or "; ".join(getattr(f, "undo_commands", []) or [])
        line("Undo:", undo)
        if rule is not None:
            src = "; ".join(("<a href=\"%s\">%s</a>" % (html.escape(s["url"]), html.escape(s.get("text", "")))
                             if s.get("url") else html.escape(s.get("text", ""))) for s in f.source)
            lab = label("<b>Source:</b> %s &nbsp; <b>Seen on our computers:</b> %s" % (
                src, html.escape(f.seen or "no, from the documentation")), "hint", wrap=True)
            lab.setTextFormat(Qt.RichText)
            lab.setOpenExternalLinks(True)
            lay.addWidget(lab)


class TimingPage(QWidget):
    """The real-time check: the computer, a measurement, its budget, the
    findings and the changes, in the guided order of rtflow.Flow."""

    def __init__(self, win):
        super().__init__()
        from . import rtflow
        self.win = win
        self.flow = rtflow.Flow()
        self.worker = None
        self.cancel = None
        self.sudo_ok = None
        self.last_result = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        lay = page_layout(content)
        self.scroller = scroll(content)
        outer.addWidget(self.scroller)

        row = QHBoxLayout()
        row.addWidget(label("Computer timing", "bigTitle"))
        self.mode = pill("Look", "grey")
        row.addWidget(self.mode)
        row.addStretch(1)
        # the mode's state; the two buttons below set it
        self.improve = QCheckBox()
        self.improve.toggled.connect(self.improve_toggled)
        self.improve.hide()
        self.save_report = QPushButton("Save report...")
        self.save_report.setToolTip("One file with the computer, every run, the changes and what is "
                                    "left: what you saw, did and got. Only reads.")
        self.save_report.clicked.connect(self.write_report)
        row.addWidget(self.save_report)
        lay.addLayout(row)

        # the two modes
        self.mode_card = Card("Mode", None)
        mrow0 = QHBoxLayout()
        self.look_button = QPushButton("Look: measure only")
        self.improve_button = QPushButton("Improve: also try changes (sudo)")
        for b in (self.look_button, self.improve_button):
            b.setCheckable(True)
            mrow0.addWidget(b)
        self.look_button.setChecked(True)
        self.look_button.clicked.connect(lambda: self.improve.setChecked(False))
        self.improve_button.clicked.connect(lambda: self.improve.setChecked(True))
        self.mode_text = label("", "hint", wrap=True)
        mrow0.addWidget(self.mode_text, 1)
        self.mode_card.add(mrow0)
        lay.addWidget(self.mode_card)

        # where the work is
        self.step_card = Card("Where you are", "The check leads and moves on by itself as you work; "
                              "you can stop anywhere: the change log and its undo commands stay.")
        # every step in order, the present one marked; they only show, they are not buttons
        from . import rtflow
        strip = QHBoxLayout()
        strip.setSpacing(6)
        self.step_labels = []
        for n, name in enumerate(rtflow.STEPS):
            if n:
                strip.addWidget(label("\u203a", "hint"))
            lab = label("%d %s" % (n + 1, name), "hint")
            self.step_labels.append(lab)
            strip.addWidget(lab)
        strip.addStretch(1)
        self.step_card.add(strip)
        self.step_pill = self.step_labels[0]
        srow = QHBoxLayout()
        self.step_text = label("", wrap=True)
        srow.addWidget(self.step_text, 1)
        self.keep_button = QPushButton("Keep the change")
        self.keep_button.clicked.connect(self.keep)
        srow.addWidget(self.keep_button)
        self.undo_button = QPushButton("Undo the change")
        self.undo_button.clicked.connect(lambda: self.undo())
        srow.addWidget(self.undo_button)
        self.again = QPushButton("Start again")
        self.again.setToolTip("A new baseline (the change log stays).")
        self.again.clicked.connect(self.start_again)
        srow.addWidget(self.again)
        self.step_card.add(srow)
        lay.addWidget(self.step_card)

        # measure
        self.measure_card = Card("Measure", "How late the real-time threads start, every cycle, "
                                 "and what comes with the late ones.")
        form = QFormLayout()
        self.way = QuietCombo()
        for key, text in RT_WAYS:
            self.way.addItem(text, key)
        form.addRow("Way", self.way)
        self.load = QuietCombo()
        for key, text in RT_LOADS:
            self.load.addItem(text, key)
        form.addRow("Load", self.load)
        self.load_warn = label("", "warnText", wrap=True)
        self.load_warn.hide()
        self.load.currentIndexChanged.connect(self.show_load_warning)
        form.addRow("", self.load_warn)
        self.length = QuietCombo()
        for secs, text in RT_LENGTHS:
            self.length.addItem(text, secs)
        self.length.currentIndexChanged.connect(self.show_bound)
        form.addRow("Length", self.length)
        self.root = QCheckBox("As root (sudo): count SMIs; with the threads only, also run rtla timerlat")
        self.root.setToolTip("rtla is never run beside LinuxCNC: starting and stopping it can delay the "
                             "machine's real-time threads.")
        form.addRow("", self.root)
        self.measure_card.add(form)
        self.bound = label("", "hint", wrap=True)
        self.measure_card.add(self.bound)
        mrow = QHBoxLayout()
        self.measure_button = QPushButton("Measure")
        self.measure_button.setObjectName("primary")
        self.measure_button.clicked.connect(self.measure)
        mrow.addWidget(self.measure_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop)
        mrow.addWidget(self.stop_button)
        self.progress = label("", "hint", wrap=True)
        mrow.addWidget(self.progress, 1)
        self.measure_card.add(mrow)
        # the latest run in a few lines, and where its findings are
        self.summary = QFrame()
        self.summary.setObjectName("summaryBox")
        sl = QVBoxLayout(self.summary)
        sl.setContentsMargins(10, 8, 10, 8)
        self.summary_text = label("", wrap=True)
        self.summary_text.setTextFormat(Qt.RichText)
        sl.addWidget(self.summary_text)
        grow = QHBoxLayout()
        self.goto_findings = QPushButton("Go to the findings")
        self.goto_findings.clicked.connect(lambda: self.scroll_to(self.find_card))
        grow.addWidget(self.goto_findings)
        self.goto_result = QPushButton("The whole result")
        self.goto_result.clicked.connect(lambda: self.scroll_to(self.result_card))
        grow.addWidget(self.goto_result)
        self.summary_todo = QPushButton("To do yourself...")
        self.summary_todo.setToolTip("The lasting changes, as a list to save (the same as in Findings).")
        self.summary_todo.clicked.connect(lambda: self.show_todo())
        grow.addWidget(self.summary_todo)
        grow.addStretch(1)
        sl.addLayout(grow)
        self.summary.hide()
        self.measure_card.add(self.summary)
        lay.addWidget(self.measure_card)

        # the result
        self.result_card = Card("The latest run", "Its budget, the spikes and the counters that "
                                "came with them, and the comparison with the run before.")
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setFont(mono())
        self.result.setMinimumHeight(260)
        self.result_card.add(self.result)
        lay.addWidget(self.result_card)

        # findings
        self.find_card = Card("Findings", "Most to gain first. Run-time changes can be tried "
                              "here in Improve mode; kernel parameters, the BIOS and advice are "
                              "for you to do (a restart).")
        trow = QHBoxLayout()
        self.todo_button = QPushButton("To do yourself: the lasting changes...")
        self.todo_button.setToolTip(
            "A list to save: kernel parameters (one new GRUB line), sysctl, a start-up service and the BIOS, "
            "from the changes you kept and the findings; each with how to take it back. EtherCAT Setup itself "
            "changes nothing that lasts after a restart.")
        self.todo_button.clicked.connect(self.show_todo)
        trow.addWidget(self.todo_button)
        trow.addStretch(1)
        self.find_card.add(trow)
        self.find_box = QVBoxLayout()
        self.find_card.add(self.find_box)
        lay.addWidget(self.find_card)

        # changes
        self.change_card = Card("History: changes made to this computer", "")
        self.change_table = table(["Change", "State", "Measured", "When"], stretch=0)
        self.change_card.add(self.change_table)
        crow = QHBoxLayout()
        self.undo_selected = QPushButton("Undo the selected change")
        self.undo_selected.clicked.connect(self.undo_chosen)
        crow.addWidget(self.undo_selected)
        crow.addStretch(1)
        self.change_card.add(crow)
        lay.addWidget(self.change_card)

        # the computer: one line, the whole table on request
        self.inv_card = Card("The computer", "What it is, each fact with where it was read (the "
                             "tooltip). A fact matters only when a finding says so.")
        irow = QHBoxLayout()
        self.inv_summary = label("", wrap=True)
        irow.addWidget(self.inv_summary, 1)
        self.inv_toggle = QPushButton("Show all")
        self.inv_toggle.setCheckable(True)
        self.inv_toggle.toggled.connect(self.show_inventory)
        irow.addWidget(self.inv_toggle)
        self.read_button = QPushButton("Read again")
        self.read_button.setToolTip("Read what the computer is again (after a change made elsewhere).")
        self.read_button.clicked.connect(self.read_computer)
        irow.addWidget(self.read_button)
        self.inv_card.add(irow)
        self.inv_table = table(["", "Value"], stretch=1)
        self.inv_card.add(self.inv_table)
        self.inv_table.hide()
        lay.addWidget(self.inv_card)
        lay.addStretch(1)
        self.finding_views = []
        self.show_bound()

    # -- showing

    def show_load_warning(self, *_):
        """With load chosen and stress-ng missing, say so before the run: the
        run would otherwise be measured at rest. Looked up again each time, so
        it goes away once stress-ng is installed."""
        import shutil
        missing = self.load.currentData() == "stress" and shutil.which("stress-ng") is None
        self.load_warn.setText("\u26a0 stress-ng is not installed, so there is no load to measure with. "
                               "Install it (sudo apt install stress-ng) or choose another load."
                               if missing else "")
        self.load_warn.setVisible(missing)
        return missing

    def show_bound(self, *_):
        from . import rtcheck
        secs = self.length.currentData() or 60
        self.bound.setText(
            "A run with no spike over a limit says there are fewer than %s per hour (95 %%); "
            "to claim fewer than one per hour, a run of %s is needed." % (
                rtcheck.number_text(rtcheck.rate_bound(0, secs)),
                rtcheck.duration_text(rtcheck.seconds_needed(1))))

    def refresh(self):
        from . import rtcheck, rtflow
        f = self.flow
        running = self.worker is not None
        hal = rtcheck.hal_running() if not running else True
        # beside LinuxCNC when it runs, else the threads only
        if not running:
            self.way.model().item(0).setEnabled(hal)
            if not hal and self.way.currentData() == "attach":
                self.way.setCurrentIndex(1)
            elif hal and not f.baseline and self.way.currentData() != "attach":
                self.way.setCurrentIndex(0)
        lock = bool(f.baseline) and not running
        for w in (self.way, self.load, self.length):
            w.setEnabled(not running and not lock)
        if lock:
            ref = f.run(f.baseline[0])
            self.way.setCurrentIndex(max(0, self.way.findData(ref.way)))
            self.load.setCurrentIndex(max(0, self.load.findData(ref.load)))
            i = min(range(len(RT_LENGTHS)), key=lambda k: abs(RT_LENGTHS[k][0] - ref.seconds))
            self.length.setCurrentIndex(i)
            self.measure_card.hint.setText("The same way as the baseline, so that runs can be compared "
                                           "(Start again to choose another).")
        if self.sudo_ok is None:
            self.sudo_ok = rtflow.can_sudo()
        self.root.setEnabled(bool(self.sudo_ok) and not running and not lock)
        self.show_load_warning()
        if not self.sudo_ok:
            self.root.setChecked(False)
            self.root.setToolTip("sudo would ask for a password, which the window cannot give: "
                                 "use ecatsetup --rt-check --rt-root in a terminal")
        self.measure_button.setEnabled(not running and f.inventory is not None)
        self.stop_button.setEnabled(running)
        self.read_button.setEnabled(not running)
        on = self.improve.isChecked()
        set_pill(self.mode, "Improve" if on else "Look", "amber" if on else "grey")
        self.look_button.setChecked(not on)
        self.improve_button.setChecked(on)
        self.look_button.setObjectName("" if on else "primary")
        self.improve_button.setObjectName("primary" if on else "")
        restyle(self.look_button)
        restyle(self.improve_button)
        self.mode_text.setText(
            "Improve: a run-time change in the findings can be tried with Try it (it asks first). "
            "No restart is ever needed for it; Undo takes it back, and a restart does too." if on else
            "Look reads and measures, and changes nothing. Choose Improve to try a finding's "
            "run-time change with one press.")

        step, text = f.step()
        for n, lab in enumerate(self.step_labels):
            name = rtflow.STEPS[n]
            if n == step:
                lab.setObjectName("")
                set_pill(lab, "Step %d of %d · %s" % (n + 1, len(rtflow.STEPS), name),
                         "green" if step == 5 else "blue")
            else:
                lab.setText("\u2713 %s" % name if n < step else "%d %s" % (n + 1, name))
                lab.setProperty("pill", None)
                lab.setObjectName("hint")
                restyle(lab)
            lab.setToolTip("Where the check is now." if n == step else "Done." if n < step
                           else "Comes later; the check moves on by itself.")
        self.step_pill = self.step_labels[step]
        self.step_text.setText(text)
        ch = f.log.find(f.pending) if f.pending else None
        self.keep_button.setVisible(ch is not None)
        self.undo_button.setVisible(ch is not None)
        self.keep_button.setEnabled(ch is not None and ch.run_after is not None and not running)
        self.undo_button.setEnabled(ch is not None and not running)
        self.again.setEnabled(not running)

        inv = f.inventory
        self.inv_table.setRowCount(0)
        for fact in (inv.facts if inv else []):
            r = self.inv_table.rowCount()
            self.inv_table.insertRow(r)
            self.inv_table.setItem(r, 0, item(fact.label, tip=fact.source))
            self.inv_table.setItem(r, 1, item(fact.value, tip=fact.source))
        fit_table(self.inv_table)
        if inv is None:
            self.inv_summary.setText("Not read yet.")
        else:
            def v(key):
                x = inv.fact(key)
                return x.value if x else "?"
            parts = [v("cpu_model"), "kernel %s%s" % (v("kernel"), ", PREEMPT_RT" if inv.data.get("preempt_rt")
                                                       else ", not PREEMPT_RT"),
                     "isolated CPU %s" % v("isolated")]
            if inv.fact("threads"):
                parts.append(v("threads"))
            if inv.fact("io"):
                parts.append("I/O " + v("io"))
            self.inv_summary.setText(" · ".join(parts))

        latest = f.run(f.latest) if f.latest else None
        text = latest.text() if latest else "No run yet."
        if self.last_result:
            text += "\n\n" + rtflow.comparison_text(*self.last_result)
        self.result.setPlainText(text)
        self.show_findings()
        self.show_summary(latest)
        self.show_changes()

    def scroll_to(self, card):
        """Scroll so that the card's top is at the top of the page (as far as
        the page goes)."""
        bar = self.scroller.verticalScrollBar()
        bar.setValue(min(bar.maximum(), max(0, card.y() - 12)))

    def show_summary(self, latest):
        """Under Measure: the latest run's budget per thread, the comparison
        with the run before, and how many findings there are, with the most
        to gain named."""
        from html import escape
        from .rtcheck import period_text
        if latest is None:
            self.summary.hide()
            return
        lines = []
        for b in latest.budgets():
            colour = RED if b.share_left < 0.2 else AMBER if b.share_left < 0.5 else GREEN
            lines.append("<b>%s</b>: <span style='color:%s'>%s</span>, %.0f us of %s left (%.0f %%); "
                         "started at most %.1f us late" % (
                             escape(b.thread), colour.name(), escape(b.verdict()), b.left_us,
                             escape(period_text(b.period)), 100 * b.share_left, b.late_us))
        if not lines:
            lines.append("No cycles were recorded.")
        if self.last_result:
            lines.append("Compared with the run before: <b>%s</b>" % escape(self.last_result[1]))
        found, _ = self.flow.findings()
        if found:
            now = [x for x in found if x.kind == "runtime" and x.commands]
            later = len(found) - len(now)
            parts = []
            if now:
                parts.append("%d can be tried now, no restart" % len(now))
            if later:
                parts.append("%d need a restart, the BIOS or are advice" % later)
            lines.append("<b>%d finding%s</b> (%s). Most to gain: %s." % (
                len(found), "" if len(found) == 1 else "s", "; ".join(parts), escape(found[0].title)))
        else:
            lines.append("No finding: no rule applies to this run.")
        self.summary_text.setText("<br>".join(lines))
        self.goto_findings.setVisible(bool(found))
        self.summary.show()

    def show_findings(self):
        for v in self.finding_views:
            self.find_box.removeWidget(v)
            v.hide()
            v.setParent(None)
            v.deleteLater()
        self.finding_views = []
        f = self.flow
        pending = f.log.find(f.pending) if f.pending else None
        if pending is not None:
            v = QFrame()
            v.setObjectName("finding")
            vl = QVBoxLayout(v)
            vl.setContentsMargins(0, 6, 0, 10)
            head = QHBoxLayout()
            head.addWidget(label("Being tried: %s" % pending.title, "rowTitle"), 1)
            undo = QPushButton("Undo it")
            undo.setToolTip("\n".join(["Runs as root:"] + pending.undo_commands))
            undo.clicked.connect(lambda: self.undo())
            undo.setEnabled(self.worker is None)
            head.addWidget(undo)
            keep = QPushButton("Keep it")
            keep.setEnabled(pending.run_after is not None and self.worker is None)
            keep.setToolTip("Measure again first, to see what it did." if pending.run_after is None
                            else "Keep it in force (until the next restart).")
            keep.clicked.connect(self.keep)
            head.addWidget(keep)
            vl.addLayout(head)
            what = ("Done as root: %s. Now measure again (Measure, above), the same way, to see what it "
                    "did; then keep it or undo it." % "; ".join(pending.commands) if pending.run_after is None
                    else "Measured after it: %s. Keep it or undo it." % pending.verdict)
            vl.addWidget(label(what, wrap=True))
            v.undo_button, v.keep_button = undo, keep
            self.find_box.addWidget(v)
            self.finding_views.append(v)
            self.pending_view = v
        else:
            self.pending_view = None
        found, ctx = f.findings()
        if pending is not None:
            found = [x for x in found if not (x.id == pending.rule and rtflow_item(x) == (pending.item or ""))]
        for n, fd in enumerate(found, 1):
            v = FindingView(self, fd, n)
            v.try_button.setVisible(fd.kind == "runtime" and bool(fd.commands))
            v.try_button.setEnabled(self.improve.isChecked() and self.flow.tryable(fd) and self.worker is None)
            if not self.improve.isChecked():
                v.try_button.setToolTip("Tick Improve at the top first: then this runs the command(s) "
                                        "below as root, and you measure again to see what it did.")
            elif not self.flow.tryable(fd):
                v.try_button.setToolTip("One change at a time: keep or undo the change being tried "
                                        "first (or this one is in force already).")
            self.find_box.addWidget(v)
            self.finding_views.append(v)
        if not found and pending is None:
            v = QFrame()
            QVBoxLayout(v).addWidget(label("Read the computer first." if self.flow.inventory is None
                                           else "No rule applies.", "hint"))
            self.find_box.addWidget(v)
            self.finding_views.append(v)

    def show_changes(self):
        from . import rtflow
        log = self.flow.log
        t = self.change_table
        t.setRowCount(0)
        for c in log.changes:
            r = t.rowCount()
            t.insertRow(r)
            tip = "\n".join(["$ " + x for x in c.commands] + ["undo: " + x for x in c.undo_commands])
            t.setItem(r, 0, item(c.title, tip=tip))
            t.setItem(r, 1, item(c.state + (", by hand" if c.by_hand else ""),
                                 colour=AMBER if c.in_force else GREY))
            t.setItem(r, 2, item(c.verdict or "-"))
            t.setItem(r, 3, item(time.strftime("%Y-%m-%d %H:%M", time.localtime(c.tried or 0))))
            t.item(r, 0).setData(Qt.UserRole, c.id)
        fit_table(t, 2)
        self.change_card.hint.setText(
            "What was already done, here or with ecatsetup --rt-check --improve; changes to try are "
            "the findings above. \"undone\" means taken back. Each change with its undo command is in "
            "%s; the undo script %s has every undo command of what is still in force, newest first "
            "(read it, then sudo sh it)." % (rtflow.changes_path(), rtflow.undo_script_path()))
        self.undo_selected.setEnabled(self.worker is None and any(c.in_force for c in log.changes))

    def show_inventory(self, on):
        self.inv_table.setVisible(on)
        self.inv_toggle.setText("Hide" if on else "Show all")

    # -- actions

    def read_computer(self):
        ini = self.win.session.ini_path if self.win.session.ini_path and \
            os.path.exists(self.win.session.ini_path) else None
        self.flow.take_inventory(ini)
        self.win.status("Read the computer%s" % (" and %s" % os.path.basename(ini) if ini else ""))
        self.refresh()

    def improve_toggled(self, on):
        if on and QMessageBox.question(
                self, TITLE, "Improve mode lets you change this computer from the findings, one change "
                "at a time, with sudo. Every change is logged with the command that takes it back, and "
                "run-time changes are gone after a restart anyway.\n\nTurn Improve mode on?") != QMessageBox.Yes:
            self.improve.blockSignals(True)
            self.improve.setChecked(False)
            self.improve.blockSignals(False)
        self.refresh()

    def threads(self):
        f = self.flow
        th = (f.inventory.data.get("threads") if f.inventory else None) or []
        return th or [("servo-thread", 1000000)]

    def measure(self):
        import threading
        from . import rtcheck
        if self.worker is not None or self.flow.inventory is None:
            return
        if self.show_load_warning():
            return
        way, load, secs = self.way.currentData(), self.load.currentData(), self.length.currentData()
        root = self.root.isChecked() and self.root.isEnabled()
        inv = self.flow.inventory
        threads = self.threads()
        self.cancel = threading.Event()
        cancel = self.cancel

        def work(progress):
            if way == "attach":
                return rtcheck.measure_attach(secs, load, inv, progress, root, cancel)
            return rtcheck.measure_threads(threads, secs, load, inv, progress, root, cancel)
        self.worker = Worker(work, self)
        self.worker.progress.connect(self.progress.setText)
        self.worker.done.connect(self.measured)
        self.worker.failed.connect(self.measure_failed)
        self.progress.setText("Starting...")
        self.worker.start()
        self.refresh()

    def stop(self):
        if self.cancel is not None:
            self.cancel.set()
            self.progress.setText("Stopping...")

    def measured(self, run):
        self.worker = None
        self.last_result = self.flow.add_run(run)
        self.progress.setText("Done: %s" % os.path.basename(self.flow.latest))
        self.win.status("Measured; the run is saved in %s" % self.flow.latest)
        self.refresh()

    def measure_failed(self, text):
        self.worker = None
        self.progress.setText(text)
        self.refresh()

    def try_finding(self, fd):
        from . import rtflow
        f = self.flow
        if QMessageBox.question(
                self, TITLE, "Try \"%s\"?\n\nThis runs as root now:\n  %s\n\nNo restart is needed. "
                "Undo it takes it back at once (it runs: %s), and a restart takes it back too.\n\nThen "
                "measure again the same way to see what it did." % (
                    fd.title, "\n  ".join(fd.commands), "; ".join(fd.undo_commands))) != QMessageBox.Yes:
            return
        try:
            ch = rtflow.try_change(f.log, fd, f.reference)
        except rtflow.SudoError as e:
            if not e.needs_password:
                QMessageBox.warning(self, TITLE, "The change could not be made: %s\n\nNothing was "
                                    "changed (what went before it was undone)." % e)
                return
            text = "\n".join(rtflow.shell_line(c) for c in fd.commands)
            box = QMessageBox(self)
            box.setWindowTitle(TITLE)
            box.setText("sudo needs a password, which the window cannot give. Run this in a "
                        "terminal, then press \"I ran it\":")
            box.setDetailedText(text)
            box.setInformativeText(text)
            ran = box.addButton("I ran it", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec_()
            if box.clickedButton() is not ran:
                return
            ch = rtflow.record_by_hand(f.log, fd, f.reference)
        f.tried(ch)
        self.last_result = None
        self.win.status("Tried: %s. Measure again the same way." % ch.title)
        self.refresh()

    def keep(self):
        self.flow.keep()
        self.last_result = None
        self.refresh()

    def undo(self, change=None):
        from . import rtflow
        f = self.flow
        pending = change is None
        ch = f.log.find(f.pending) if pending else change
        if ch is None:
            return
        try:
            if pending:
                f.undo()
            else:
                rtflow.undo_change(f.log, ch)
        except rtflow.SudoError as e:
            if not e.needs_password:
                QMessageBox.warning(self, TITLE, "Undo failed: %s" % e)
                return
            text = "\n".join(rtflow.shell_line(c) for c in ch.undo_commands)
            if QMessageBox.question(self, TITLE, "sudo needs a password. Run this in a terminal, then "
                                    "press Yes:\n\n%s" % text) != QMessageBox.Yes:
                return
            if pending:
                f.undo(by_hand=True)
            else:
                rtflow.undo_change(f.log, ch, by_hand=True)
        self.flow.refresh_inventory()
        self.last_result = None
        self.refresh()

    def undo_chosen(self):
        r = self.change_table.currentRow()
        if r < 0:
            return
        ch = self.flow.log.find(self.change_table.item(r, 0).data(Qt.UserRole))
        if ch is None or not ch.in_force:
            return
        self.undo(None if ch.id == self.flow.pending else ch)

    def start_again(self):
        self.flow.reset()
        self.last_result = None
        self.refresh()

    def show_todo(self):
        from . import rttodo
        try:
            text = rttodo.text(self.flow)
        except Exception as e:      # a list that cannot be made must not end the window
            QMessageBox.warning(self, TITLE, "The list could not be made: %s: %s\n\nPlease tell the "
                                "developers, with Save report." % (type(e).__name__, e))
            return
        TodoDialog(self, text).exec_()

    def write_report(self):
        from . import rtflow
        path, _ = QFileDialog.getSaveFileName(self, "Save report - %s" % TITLE,
                                              os.path.join(os.path.expanduser("~"), rtflow.default_report_name()),
                                              "Report (*.md);;All files (*)")
        if not path:
            return
        with open(path, "w") as fh:
            fh.write(rtflow.report(self.flow))
        self.win.status("Wrote %s; look it through before sending it" % path)


class TodoDialog(QDialog):
    """The real-time check's lasting changes for the user to make
    (rttodo.text): shown, copied or saved."""

    def __init__(self, parent, text):
        super().__init__(parent)
        self.setWindowTitle("To do yourself - %s" % TITLE)
        self.resize(900, 700)
        lay = QVBoxLayout(self)
        lay.addWidget(label("EtherCAT Setup changes nothing that lasts after a restart. These are the changes "
                            "in the forms that do last, for you to make one group at a time, measuring again "
                            "after each. Save the list or copy it.", "hint", wrap=True))
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(mono())
        self.text.setPlainText(text)
        lay.addWidget(self.text, 1)
        row = QHBoxLayout()
        self.save_button = QPushButton("Save...")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        row.addWidget(self.save_button)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self.text.toPlainText()))
        row.addWidget(copy)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.saved = None

    def save(self, path=None):
        from . import rttodo
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "Save the list - %s" % TITLE,
                                                  os.path.join(os.path.expanduser("~"), rttodo.default_name()),
                                                  "Markdown (*.md);;Text (*.txt);;All files (*)")
        if not path:
            return
        with open(path, "w") as fh:
            fh.write(self.text.toPlainText() + "\n")
        self.saved = path
        self.parent().win.status("Wrote %s" % path)


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
        use_point()
        super().__init__()
        self.session = session
        self.reports = {}       # position -> diagnose.Report
        self.worker = None
        self.test = None        # testmode.TestRun while the I/O test runs
        self.current = None     # the chosen slave's position, MASTER_ITEM, or None
        self.messages_by_slave = {}
        self.master_offer_off = False       # "Don't ask again this session"
        self.master_restart_asked = False   # a master that does not fit was offered a restart
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
        self.theme = theme_setting()
        apply_theme(self.theme)
        self.setStyleSheet(style(self.theme))

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
        self.timing_page = TimingPage(self)
        self.tabs.addTab(self.start_page, "Start")
        self.tabs.addTab(self.bus_page, "Bus")
        self.tabs.addTab(self.trouble_page, "Slave status")
        self.tabs.addTab(self.test_page, "Test")
        self.tabs.addTab(self.hal_page, "HAL && LinuxCNC")
        self.tabs.addTab(self.timing_page, "Timing")
        self.tabs.currentChanged.connect(lambda _: self.show_current())
        lay.addWidget(self.tabs.strip)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.lcnc_label = QLabel("LinuxCNC: " + lcnc_env.describe())
        self.lcnc_label.setToolTip("The LinuxCNC the tests (halrun) and the HAL files' library "
                                   "come from; set `linuxcnc:` in %s" % settings.path())
        self.statusBar().addPermanentWidget(self.lcnc_label)
        self._actions()
        # the start page when there is nothing yet, else where the work is
        if session.cfg is not None or session.bus:
            self.tabs.setCurrentWidget(self.bus_page)
        self.refresh()

    def go(self, target):
        """A start page button: a page or an action."""
        pages = {"bus": self.bus_page, "status": self.trouble_page, "test": self.test_page,
                 "hal": self.hal_page, "timing": self.timing_page}
        actions = {"scan": self.read_bus, "open": self.open_file, "update": self.update_from_bus,
                   "esi": self.add_esi, "watch": self.start_watch, "ini": self.choose_ini}
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
        self.a_ini = act("Machine INI file...", self.choose_ini)
        menu.addSeparator()
        self.a_test = act("Start the I/O test", self.start_test)
        self.a_watch = act("Watch LinuxCNC that runs (read only)", self.start_watch)
        self.a_test_stop = act("Stop the test or the watching", self.stop_test)
        menu.addSeparator()
        look = menu.addMenu("Appearance")
        group = QActionGroup(self)
        self.a_theme = {}
        for key, name in THEME_NAMES.items():
            a = QAction(name, self, checkable=True)
            a.setChecked(key == self.theme)
            a.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            group.addAction(a)
            look.addAction(a)
            self.a_theme[key] = a
        menu.addSeparator()
        act("Quit", self.close, QKeySequence.Quit)
        self.header.menu_button.setMenu(menu)

    def set_theme(self, theme, save=True):
        """Light or dark mode, at once, and kept in settings.yaml."""
        if theme not in THEMES:
            return
        self.theme = theme
        apply_theme(theme)
        self.setStyleSheet(style(theme))
        if theme in getattr(self, "a_theme", {}):
            self.a_theme[theme].setChecked(True)
        if save:
            try:
                data = settings.load()
                data["theme"] = theme
                settings.save(data)
            except OSError as e:
                self.status("Could not save the mode: %s" % e)
        # table text and messages take the new colours when drawn again
        self.refresh()
        self.update()

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
            m.get("transport"), "cycle %s%s" % (period_text(s.period or 1000000),
                                                " (from INI)" if s.period_from_ini else
                                                "" if s.period else " (default)")) if x))
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
        elif page is self.timing_page:
            if self.timing_page.flow.inventory is None:
                self.timing_page.read_computer()
            else:
                self.timing_page.refresh()

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
        if p.needs_interface:
            # no file names the card, nor ethercat-uspace.conf: ask, or the master
            # would not start (and the bus could not be read to make a file)
            nic = self.ask_master_interface()
            if nic is None:
                return
            p = masterstart.plan(self.session.cfg, interface=nic)
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

    def ask_master_interface(self):
        """The network card to start the master on, chosen by the user; None if
        cancelled or there is none."""
        cards = masterstart.wired()
        if not cards:
            QMessageBox.warning(self, TITLE, "No master is running, and no wired network card "
                                "was found to start one on.")
            return None
        items = ["%s  (%s)" % (n, "link" if up else "no link") for n, up in cards]
        choice, ok = QInputDialog.getItem(
            self, TITLE, "No master is running, and nothing names its network card (no "
            "configuration file yet, and no INTERFACE in ethercat-uspace.conf).\n\n"
            "Which network card are the slaves connected to?", items, 0, False)
        if not ok:
            return None
        return cards[items.index(choice)][0]

    def offer_master_restart(self):
        """The master that runs has no link, sees no slaves or sits on another
        network card than the file's: show the commands and restart it on the
        file's card if the user confirms. Once a session, never while LinuxCNC
        runs or a test is on, and never for a master a program has activated."""
        if self.busy or self.test is not None or self.master_restart_asked \
                or masterstart.linuxcnc_running():
            return
        try:
            why = masterstart.stale_reason(
                masterstart.master_status(tool.run("master")), self.session.cfg)
        except tool.ToolError:
            return
        if not why:
            return
        self.master_restart_asked = True
        p = masterstart.restart_plan(self.session.cfg)
        box = QMessageBox(self)
        box.setWindowTitle(TITLE)
        box.setIcon(QMessageBox.Question)
        box.setText("The master that runs %s. Restart it?" % why)
        box.setInformativeText("\n".join(p.notes + ([""] if p.notes else []) + [
            masterstart.text(p), "",
            "The window itself does not run as root; sudo must work without a password "
            "here, or you run the commands in a terminal."]))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec_() != QMessageBox.Yes:
            return

        def work(progress):
            progress("Restarting the master...")
            return masterstart.run(p, noninteractive=True)

        def done(result):
            ok, message = result
            if not ok:
                self.set_messages([(RED, "", message.splitlines())])
                self.status(message.splitlines()[0])
                return
            self.master_just_started = True
            self.read_bus()

        self.run("Restarting the master...", work, done)

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
            QTimer.singleShot(0, self.offer_master_restart)
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
            self.refresh(keep_page=True)     # the PDO assignment box back to the one in use
            return
        self.run("Reading slave %d..." % pos,
                 lambda progress: self.session.propose_again(pos, mode, pdo_set),
                 lambda notes: self.check(extra=["slave %d: proposed again: %s" % (pos, n)
                                                 for n in notes] or
                                          ["slave %d: proposed again" % pos]))

    def apply_way(self, pos, way, pdo_set=None):
        """The user chose a way to get the PDOs: the slave is read and only its PDOs replaced."""
        self.run("Reading slave %d..." % pos,
                 lambda progress: self.session.apply_pdos(pos, way=None if pdo_set else way,
                                                          pdo_set=pdo_set),
                 lambda notes: (self.changed(), self.check(extra=["slave %d: PDOs: %s" % (pos, way)])))

    def change_profile(self, pos, profile):
        if QMessageBox.question(
                self, TITLE, "Run slave %d as %s? Its PDOs are proposed again for that profile "
                "(pins may change); its name, settings and DC stay." % (pos, PROFILE_TEXT[profile]),
                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return False
        self.run("Reading slave %d..." % pos,
                 lambda progress: self.session.change_profile(pos, profile),
                 lambda notes: (self.changed(), self.check(extra=["slave %d: profile %s" % (pos, PROFILE_TEXT[profile])]
                                                           + ["slave %d: %s" % (pos, n) for n in notes])))
        return True

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
        # the PDOs against the ESI files: what the device would refuse. Shown apart from the
        # file's errors, since they do not stop a save (an ESI file can be wrong too)
        pdo = self.session.pdo_problems()
        esi_errors = [t for lv, t in pdo if lv == "error"]
        by_slave = {}
        for m in errors + esi_errors + problems:
            by_slave.setdefault(slave_of(m), []).append(m)
        self.messages_by_slave = by_slave
        # DC settings the slaves do not describe: warned about, not errors
        dc = self.session.dc_problems()
        groups = [(RED, "Error: ", errors), (RED, "ESI file: ", esi_errors), (RED, "Bus: ", problems),
                  (AMBER, "Warning: ", [t for lv, t in dc + pdo if lv == "warning"]),
                  (GREY, "Bus: ", notes), (GREY, "Note: ", [t for lv, t in dc + pdo if lv == "note"]),
                  (GREY, "", list(extra))]
        if not quiet:
            if self.session.cfg is not None and not errors and not problems and not esi_errors:
                groups.append((GREEN, "", ["No errors in the file" +
                                           (", and the bus matches it" if self.session.bus else
                                            "; the bus was not read")]))
            elif self.session.cfg is not None and not errors and not problems:
                groups.append((GREY, "", ["No errors in the file itself: it can be saved; the ESI "
                                          "file's objections above are what the device may refuse"]))
            self.status("%d errors" % len(errors + problems + esi_errors)
                        if errors or problems or esi_errors else "Checked")
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

    def save_capture(self):
        """Write a capture of the bus for a developer (capture.py)."""
        from . import capture
        s = self.session
        base = os.path.dirname(os.path.abspath(s.ini_path or s.path or os.path.expanduser("~/x")))
        path, _ = QFileDialog.getSaveFileName(self, "Save capture - %s" % TITLE,
                                              os.path.join(base, capture.default_name()),
                                              "Capture (*.tar.gz);;All files (*)")
        if not path:
            return
        cfg_path = s.path if s.path and os.path.exists(s.path) else None
        ini_path = s.ini_path if s.ini_path and os.path.exists(s.ini_path) else None

        def work(progress):
            progress("Capturing the bus (about a minute)...")
            notes = capture.record(path, s.esi_paths, cfg_path, ini_path, progress=progress)
            return path, notes

        def done(result):
            p, notes = result
            self.status("Wrote %s" % p)
            text = "\n".join(capture.describe(p))
            QMessageBox.information(
                self, TITLE, "Wrote %s.\n\n%s\n\nIt holds bus data, the configuration and the "
                "INI lines the report holds, and no other files. Look it through before sending "
                "it." % (p, text))
        self.run("Capturing the bus...", work, done)

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
        self.test_page.response.stop()
        run = self.test
        tests = list(self.drive_tests.values())

        def work(progress):
            failed = []
            run.disable_drives()
            for dt in tests:
                progress("Writing the drive's own limits back to %s" % dt.name)
                failed += ["%s %s" % (dt.name, f) for f in dt.restore()]
            if tuning.pending():
                progress("Writing back the settings that were being tried")
                failed += tuning.undo_all(_writer)[1]
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
            self.test_page.pll = []
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
        if self.test is not None and not self.test.watching:
            self.test_page.response.tick()
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
        """Limits a crashed drive test left in drives, and settings being tried
        (the Response card): offer to put them back."""
        if tuning.pending() and not testmode.test_holder():
            text = "\n".join("slave %s (%s): %s back to %s" % (
                e["position"], e.get("slave"), config.obj(e["index"], e["subindex"]), e["original"])
                for e in tuning.pending())
            if QMessageBox.question(
                    self, TITLE, "Settings were being tried in drives when EtherCAT Setup ended:\n\n"
                    "%s\n\nWrite the drives' own values back now? (A master must run.)" % text,
                    QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
                done, failed = tuning.undo_all(_writer)
                if failed:
                    QMessageBox.warning(self, TITLE, "Not written back (kept in %s):\n%s"
                                        % (tuning.TRIALS_FILE, "\n".join(failed)))
                else:
                    self.status("Written back: %s" % "; ".join(done))
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
            self.test_page.pll = []
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
        period = servo_period(path)
        self.session.ini_path = path
        self.session.period_from_ini = period is not None
        if period is not None:
            self.session.period = period
        self.refresh(keep_page=True)
        self.bus_page.master.show_config()

    def closeEvent(self, event):
        if self.busy or not self.ask_save():
            event.ignore()
            return
        tp = self.timing_page
        if tp.worker is not None:
            # a measurement runs: stop it, so that halrun and the HAL lines it added go
            tp.stop()
            tp.worker.wait(30000)
        if self.test is not None:
            self.test_timer.stop()
            self.test.disable_drives()
            for dt in self.drive_tests.values():
                dt.restore()
            self.drive_tests = {}
            tuning.undo_all(_writer)
            self.test.stop()
            self.heartbeat.stop()
            self.pending_timer.stop()
            self.test = None
            self.test_page.sync_card.hide()
            self.test_page.offsets = []
            self.test_page.pll = []
        event.accept()


def main(path=None, esi_paths=(), ini_path=None, mode="csp", read_bus=True):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    use_point()
    app.setApplicationName(TITLE)
    # the ESI folders given before, then those on the command line
    esi_paths = list(dict.fromkeys(settings.esi_folders() + list(esi_paths)))
    # ESI files not read before take a while (Beckhoff's set: about a minute, once;
    # then they come from the cache): say so instead of showing nothing
    notice = None
    n, size = esi.uncached(esi_paths)
    if size > 20 * 1024 * 1024:
        notice = QLabel("EtherCAT Setup: reading %d ESI files (%d MB) for the first time.\n"
                        "This takes about %d s once; later starts take them from a cache."
                        % (n, size // (1024 * 1024), max(5, size // (25 * 1024 * 1024))))
        notice.setWindowTitle(TITLE)
        notice.setContentsMargins(24, 18, 24, 18)
        notice.show()
        app.processEvents()
    win = MainWindow(Session(esi_paths, ini_path, mode))
    if notice is not None:
        notice.close()
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
