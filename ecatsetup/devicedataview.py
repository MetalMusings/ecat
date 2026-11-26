"""The window's side of shared device data (PROTOTYPE; devicedata.py has the
rest): remembering a fact for a device type where it is learnt, and the
Device data dialog (what is in use and where it comes from, Share, Fetch,
going through a shared file's facts one by one, Try / Undo for a setting,
Report)."""

import html
import os
import urllib.parse

from PyQt5.QtCore import QTimer, Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                             QHBoxLayout, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                             QRadioButton, QScrollArea, QTextBrowser, QVBoxLayout)

from . import devicedata, measured, quirks, settings, testmode, tool, tuning
from .gui import TITLE, label

# where each card is, from the main window: (the tab's page, the card, the card shown
# instead while it is hidden, when it shows). devicedata.KINDS names a fact's card by
# these ids
_BENCH = "It shows on the Workbench while the workbench runs: Start the workbench."
CARDS = {
    "settings": (lambda w: w.bus_page, lambda w: w.slave_page.settings_card, None, ""),
    "process-data": (lambda w: w.bus_page, lambda w: w.slave_page.pd_card, None, ""),
    "homing": (lambda w: w.bus_page, lambda w: w.slave_page.homing_card, lambda w: w.slave_page.op_card,
               "It shows for a CiA 402 drive with homing among its modes: tick Homing (hm) under "
               "Operation and sync."),
    "sdo": (lambda w: w.test_page, lambda w: w.test_page.sdo_card, None, ""),
    "switches": (lambda w: w.test_page, lambda w: w.test_page.switches,
                 lambda w: w.test_page.start_card, _BENCH),
    "move": (lambda w: w.test_page, lambda w: w.test_page.move, lambda w: w.test_page.start_card, _BENCH),
    "response": (lambda w: w.test_page, lambda w: w.test_page.response,
                 lambda w: w.test_page.start_card, _BENCH),
    "sync": (lambda w: w.live_page, lambda w: w.live_page.sync, lambda w: w.live_page.source,
             "It shows on the Live tab while something runs: the workbench, or a watched LinuxCNC."),
    "from-lcec": (lambda w: w.hal_page, lambda w: w.hal_page.lcec_card, None,
                  "It shows when the INI file's configuration is an lcec one."),
}
CARD_NAMES = {"settings": "Settings", "process-data": "Process data", "homing": "Homing by the drive",
              "sdo": "SDO", "switches": "Switches", "move": "Move", "response": "Response",
              "sync": "Synchronisation", "from-lcec": "From lcec"}
UNKNOWNS_SETTING = "device-data-unknowns"

KINDS = [("tried", "Tried on this machine"), ("manual", "The device's manual"),
         ("other", "Somewhere else")]
URL_MAX = 7000      # longer prefilled links are cut by browsers: the post is copied instead


def device(win, position):
    """(slave, (vendor, product), name, revision text) of a configured slave, or None."""
    s = win.session.slave(position) if isinstance(position, int) and position >= 0 else None
    ident = quirks.identity(s) if s else None
    if not ident:
        return None
    rev = (s.get("identity") or {}).get("revision")
    b = win.session.bus.get(position) if getattr(win.session, "bus", None) else None
    if b is not None and getattr(b, "info", None) and b.info.get("revision") is not None:
        rev = "0x%08X" % b.info["revision"]
    return s, ident, str(s.get("device") or s.get("name") or "device"), rev


class SourceDialog(QDialog):
    """Where a fact comes from (and, for a command, what it does)."""

    def __init__(self, parent, what, value, revision, note=None):
        super().__init__(parent)
        self.setWindowTitle("Remember for this device type - %s" % TITLE)
        self.revision = revision
        lay = QVBoxLayout(self)
        lay.addWidget(label("<b>%s</b>%s" % (what, "" if value is None else ": %s" % value), wrap=True))
        lay.addWidget(label("Kept in your own quirk file for the device (vendor and product), so that "
                            "the next one of these devices starts from it, here or anywhere you share it. "
                            "Say where it comes from: others weigh it by that.", "hint", wrap=True))
        form = QFormLayout()
        self.note = None
        if note is not None:
            self.note = QLineEdit(note)
            self.note.setPlaceholderText("what it does, such as: sets the position to zero")
            form.addRow("What it does:", self.note)
        self.kind = QComboBox()
        for k, text in KINDS:
            self.kind.addItem(text, k)
        form.addRow("Where it comes from:", self.kind)
        self.ref = QLineEdit()
        self.ref.setPlaceholderText("the manual's name, version and section; or what you did")
        form.addRow("Reference:", self.ref)
        lay.addLayout(form)
        ok = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok.button(QDialogButtonBox.Ok).setText("Remember")
        ok.accepted.connect(self.accept)
        ok.rejected.connect(self.reject)
        lay.addWidget(ok)

    def source(self):
        return {"kind": self.kind.currentData(), "ref": self.ref.text().strip(),
                "date": devicedata.today(), "revision": self.revision}


def remember(win, parent, position, path, value, what, note=None, ask=True):
    """Remember one fact of a slave's device type, asking where it comes from.
    For a command, value None takes the note typed. Returns the file or None."""
    d = device(win, position)
    if d is None:
        win.status("This slave has no vendor and product: nothing to remember it for")
        return None
    _, ident, name, rev = d
    dlg = SourceDialog(parent, what, None if note is not None else devicedata.text(value), rev, note)
    if ask and dlg.exec_() != QDialog.Accepted:
        return None
    if note is not None:
        value = dlg.note.text().strip() or note or "a command"
    try:
        path_ = devicedata.remember(ident, name, path, value, dlg.source())
    except devicedata.Error as e:
        QMessageBox.warning(parent, TITLE, str(e))
        return None
    win.status("Remembered for %s: %s (%s)" % (name, devicedata.label(path), path_))
    return path_


class ReportDialog(QDialog):
    """A report on a shared file: worked / partly / did not work, own words."""

    def __init__(self, parent, entry, used, revision):
        super().__init__(parent)
        self.setWindowTitle("Report - %s" % TITLE)
        self.entry, self.used, self.revision = entry, used, revision
        lay = QVBoxLayout(self)
        lay.addWidget(label("How did the shared file #%s work for you?" % entry.get("discussion"), wrap=True))
        self.buttons = []
        for r in devicedata.RESULTS:
            b = QRadioButton(r[0].upper() + r[1:])
            b.result = r
            self.buttons.append(b)
            lay.addWidget(b)
        self.buttons[0].setChecked(True)
        form = QFormLayout()
        self.firmware = QLineEdit()
        self.firmware.setPlaceholderText("if you know it")
        form.addRow("Firmware:", self.firmware)
        lay.addLayout(form)
        self.words = QPlainTextEdit()
        self.words.setPlaceholderText("Your own words: the machine, what you used, what happened")
        lay.addWidget(self.words, 1)
        lay.addWidget(label("The report is copied and the thread opens in your browser: paste it there as "
                            "a comment (GitHub does not take a comment from a link). You can edit it "
                            "later, or add another.", "hint", wrap=True))
        ok = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok.button(QDialogButtonBox.Ok).setText("Copy and open the thread")
        ok.accepted.connect(self.accept)
        ok.rejected.connect(self.reject)
        lay.addWidget(ok)

    def text(self):
        result = next(b.result for b in self.buttons if b.isChecked())
        return devicedata.report(self.entry["path"], result, self.used, self.revision,
                                 self.firmware.text().strip(), self.words.toPlainText())


def open_url(url):
    QDesktopServices.openUrl(QUrl(url))


def show_card(win, position, card):
    """Bring a card into view, for the chosen slave: the card owns the fact. A card
    hidden now (no homing mode, no workbench running) is not there to show: the card
    that leads to it is shown instead, and the status line says when it shows.
    Returns (the card shown, why the card itself is not, or "")."""
    if card not in CARDS:
        return None, ""
    page, widget, instead, when = CARDS[card]
    if isinstance(position, int):
        win.select_slave(position)
    tab = page(win)
    win.show_tab(tab)
    QApplication.processEvents()
    w = widget(win)
    why = ""
    if not w.isVisibleTo(tab):
        why = "The %s card is not shown now. %s" % (CARD_NAMES.get(card, card), when)
        w = instead(win) if instead else tab
        win.status(why)
    win.raise_()
    win.activateWindow()
    # after the tab is laid out: scrolling at once uses the old geometry
    reveal(w)
    QTimer.singleShot(50, lambda: reveal(w))
    return w, why


def reveal(w):
    try:
        p = w.parentWidget()
        while p is not None and not isinstance(p, QScrollArea):
            p = p.parentWidget()
        if p is not None:
            p.ensureWidgetVisible(w, 0, 0)
    except RuntimeError:        # the window went meanwhile
        pass


def _mark(src, mine):
    """Where a fact comes from, short, for the page."""
    if not mine:
        return "came with the program"
    if not src:
        return "yours"
    return devicedata.source_text(src)


def page_html(name, ident, revision, esi_line, now, own, unknown=(), timing=None, other=None):
    """The device's page: its facts grouped by section, each with its value, where
    it comes from, its card and (yours) Forget. other: (label, raw) of a shared
    file shown the same way, each fact marked against what is in use."""
    raw = other[1] if other else now
    srcs = (raw or {}).get("sources") or {}
    rows = {sec: [] for sec in devicedata.SECTIONS + ("Timing",)}
    for p in devicedata.facts(raw):
        k = devicedata.kind(p)
        if other and not k.share:
            continue
        rows[k.section].append((p, k))
    out = ["<h3 style='margin-bottom:2px'>%s</h3>" % _html(name),
           "<p style='margin-top:0'>vendor 0x%08X, product 0x%08X%s<br>%s</p>" % (
               ident[0], ident[1], ", revision %s" % _html(revision) if revision else "", _html(esi_line))]
    if other:
        out.append("<p><b>%s</b>: as it would change what is in use. Choose its facts one by one "
                   "below.</p>" % _html(other[0]))
    for sec in devicedata.SECTIONS + ("Timing",):
        lines = []
        for p, k in rows.get(sec, []):
            v = devicedata.get(raw, p)
            key = devicedata.key(p)
            mine = devicedata.get(own, p) is not None
            links = []
            if k.card:
                links.append("<a href='card:%s'>%s card</a>" % (k.card, CARD_NAMES.get(k.card, k.card)))
            if mine and not other:
                links.append("<a href='forget:%s'>forget</a>" % _html(key))
            if other:
                was = devicedata.get(now, p)
                state = "same as in use" if was == v else ("new" if was is None else
                                                          "in use now: %s" % devicedata.text(was))
                mark = "%s; <i>%s</i>" % (_html(devicedata.source_text(srcs.get(key))
                                                if srcs.get(key) else "no source given"), _html(state))
            else:
                mark = _html(_mark(srcs.get(key), mine))
            lines.append("<tr><td>%s</td><td><tt>%s</tt></td><td>%s</td><td>%s</td></tr>" % (
                _html(k.label.format(*p)), _html(devicedata.text(v)), mark, " &middot; ".join(links)))
        if sec == "Timing" and timing and not other:
            lines.append("<tr><td>Measured SYNC0 periods</td><td><tt>%s</tt></td><td>%s</td><td>"
                         "<a href='card:sync'>Synchronisation card</a></td></tr>" % (_html(timing), "measured"))
        for usec, text, card in unknown if not other else ():
            if usec == sec:
                lines.append("<tr><td colspan='3'><i style='color:#b45309'>%s</i></td><td>"
                             "<a href='card:%s'>%s card</a></td></tr>" % (
                                 _html(text), card, CARD_NAMES.get(card, card)))
        if lines:
            out.append("<h4 style='margin-bottom:2px'>%s</h4><table cellspacing='0' cellpadding='3' "
                       "width='100%%'>%s</table>" % (_html(sec), "".join(lines)))
    return "".join(out)


class DeviceDataDialog(QDialog):
    """A device type's facts: in use (shipped and yours, each with its source),
    Share yours, Fetch shared ones and go through a file's facts one by one."""

    def __init__(self, win, position):
        # a window of its own, not kept above the main window: its card links bring a card
        # into view there (MainWindow keeps the reference and closes it with itself)
        super().__init__(None)
        self.setWindowFlags(Qt.Window)
        self.win, self.position = win, position
        self.slave, self.ident, self.name, self.revision = device(win, position)
        self.setWindowTitle("Device data - %s" % TITLE)
        self.resize(980, 760)
        self.files, self.rows, self.at, self.used = [], [], 0, []
        lay = QVBoxLayout(self)
        lay.addWidget(label("<b>%s</b> &nbsp; vendor 0x%08X, product 0x%08X%s &nbsp; "
                            "<span style='color:#b45309'>prototype</span>" % (
                                self.name, self.ident[0], self.ident[1],
                                ", revision %s" % self.revision if self.revision else ""), wrap=True))

        row = QHBoxLayout()
        row.addWidget(label("<b>What is known about this device type</b>, grouped; each fact is changed "
                            "on its card.", wrap=True), 1)
        self.unknown_box = QCheckBox("Show what is not known yet")
        try:
            self.unknown_box.setChecked(bool(settings.load().get(UNKNOWNS_SETTING, True)))
        except (OSError, ValueError, AttributeError):
            self.unknown_box.setChecked(True)
        self.unknown_box.toggled.connect(self.unknown_toggled)
        row.addWidget(self.unknown_box)
        lay.addLayout(row)
        self.page = QTextBrowser()
        self.page.setOpenLinks(False)
        self.page.anchorClicked.connect(self.link)
        lay.addWidget(self.page, 3)
        row = QHBoxLayout()
        self.share_button = QPushButton("Share yours...")
        self.share_button.setToolTip("Post your facts for this device (not your pin names or scales) so that "
                                     "others can use them; needs a GitHub account")
        self.share_button.clicked.connect(self.share)
        row.addWidget(self.share_button)
        self.whole_button = QPushButton("Show the whole shared file")
        self.whole_button.setCheckable(True)
        self.whole_button.setToolTip("Show the chosen shared file above, grouped like yours, each fact marked "
                                     "against what is in use")
        self.whole_button.toggled.connect(lambda _: self.show_facts())
        row.addWidget(self.whole_button)
        row.addStretch(1)
        lay.addLayout(row)

        lay.addWidget(label("<b>Shared by others</b>: fetched only when you ask; nothing is used until you "
                            "choose it, fact by fact.", wrap=True))
        row = QHBoxLayout()
        self.fetch_button = QPushButton("Fetch shared data")
        self.fetch_button.clicked.connect(self.fetch)
        row.addWidget(self.fetch_button)
        self.fetched = label("", "hint")
        row.addWidget(self.fetched, 1)
        lay.addLayout(row)
        row = QHBoxLayout()
        self.file_box = QComboBox()
        self.file_box.currentIndexChanged.connect(self.file_chosen)
        row.addWidget(self.file_box, 1)
        self.thread_button = QPushButton("Open the thread")
        self.thread_button.clicked.connect(lambda: open_url(self.entry()["url"]) if self.entry() else None)
        row.addWidget(self.thread_button)
        self.report_button = QPushButton("Report...")
        self.report_button.clicked.connect(self.report)
        row.addWidget(self.report_button)
        lay.addLayout(row)
        self.file_info = label("", "hint", wrap=True)
        lay.addWidget(self.file_info)

        # one fact at a time
        self.step = label("", None, wrap=True)
        self.step.setTextFormat(Qt.RichText)
        lay.addWidget(self.step)
        row = QHBoxLayout()
        self.use_button = QPushButton("Use it")
        self.use_button.setToolTip("Put this fact into your own quirk file, with where it came from")
        self.use_button.clicked.connect(self.use)
        self.try_button = QPushButton("Try it")
        self.try_button.setToolTip("Write the setting into the drive's RAM now (the drive's own value is kept "
                                   "and put back with Undo, or when the workbench stops)")
        self.try_button.clicked.connect(self.try_it)
        self.undo_button = QPushButton("Undo")
        self.undo_button.clicked.connect(self.undo)
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(lambda: self.go(self.at - 1))
        self.skip_button = QPushButton("Skip")
        self.skip_button.clicked.connect(lambda: self.go(self.at + 1))
        for b in (self.use_button, self.try_button, self.undo_button, self.back_button, self.skip_button):
            row.addWidget(b)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.show_facts()
        self.show_files()

    # -- in use

    def esi_line(self):
        try:
            dev = self.win.session.esi_device(self.position)
        except (AttributeError, KeyError, TypeError, ValueError):
            dev = None
        if dev is None:
            return "No ESI file for it."
        return "ESI file: %s (%s)%s" % (dev.name, os.path.basename(dev.path),
                                        ", profile %d" % dev.profile if dev.profile else "")

    def show_facts(self):
        own = devicedata.read_raw(devicedata.own_path(self.ident)) or {}
        now = devicedata.current_raw(self.ident)
        res = measured.results(self.ident)
        unknown = devicedata.unknowns(self.slave, now, res) if self.unknown_box.isChecked() else []
        e = self.entry() if self.whole_button.isChecked() else None
        other = ("Shared file #%s by %s" % (e.get("discussion"), e.get("author") or "?"), e["raw"]) \
            if e is not None and not e["problems"] else None
        bar = self.page.verticalScrollBar().value()
        self.page.setHtml(page_html(self.name, self.ident, self.revision, self.esi_line(), now, own,
                                    unknown, measured.describe(res), other))
        self.page.verticalScrollBar().setValue(bar)
        self.share_button.setEnabled(bool(own))

    def unknown_toggled(self, on):
        try:
            data = settings.load()
            data[UNKNOWNS_SETTING] = bool(on)
            settings.save(data)
        except (OSError, ValueError, AttributeError):
            pass
        self.show_facts()

    def link(self, url):
        """card:<id> brings the card into view; forget:<fact> takes one of yours away."""
        text = url.toString()
        what, _, rest = text.partition(":")
        if what == "card":
            show_card(self.win, self.position, rest)
        elif what == "forget":
            self.forget(tuple(rest.split(" ")))

    def forget(self, path):
        own = devicedata.read_raw(devicedata.own_path(self.ident)) or {}
        if devicedata.get(own, path) is None:
            return
        devicedata.remember(self.ident, self.name, path, None, None)
        self.win.status("Forgotten: %s" % devicedata.label(path))
        self.show_facts()
        self.go(self.at)

    def share(self, confirm=True):
        own = devicedata.read_raw(devicedata.own_path(self.ident))
        if not own:
            return None
        s = devicedata.strip(own)
        problems = devicedata.check(s)
        title, body, url = devicedata.share(own)
        if problems:
            QMessageBox.warning(self, TITLE, "Not shared: the check refuses it:\n\n%s"
                                % "\n".join("- " + p for p in problems))
            return None
        long_ = len(url) > URL_MAX
        if confirm and QMessageBox.question(
                self, TITLE, "Share %d fact%s about %s?\n\nThe browser opens a new post in %s with "
                "them (your pin names and scales are left out). Send it from there; it needs a GitHub "
                "account. It is checked automatically and shared under CC0: anyone may use it.%s"
                % (len(devicedata.facts(s)), "" if len(devicedata.facts(s)) == 1 else "s", self.name,
                   devicedata.REPO, "\n\nThe post is long: it is copied, paste it into the post."
                   if long_ else ""), QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return None
        if long_:
            QApplication.clipboard().setText(body)
            url = "https://github.com/%s/discussions/new?%s" % (devicedata.REPO, urllib.parse.urlencode(
                {"category": devicedata.CATEGORY, "title": title}, quote_via=urllib.parse.quote))
        open_url(url)
        self.win.status("Opened the post in your browser: send it from there")
        return url

    # -- shared

    def fetch(self):
        self.win.status("Fetching shared device data...")
        QApplication.processEvents()
        try:
            n = devicedata.fetch()
        except devicedata.Error as e:
            QMessageBox.warning(self, TITLE, str(e))
            return
        self.win.status("Fetched %d files of shared device data" % n)
        self.show_files()

    def show_files(self):
        f = devicedata.fetched()
        self.fetched.setText("Fetched %s." % f["time"].replace("T", " ") if f else "Not fetched yet.")
        self.files = devicedata.shared_for(self.ident)
        self.file_box.blockSignals(True)
        self.file_box.clear()
        for e in self.files:
            self.file_box.addItem("#%s by %s: %d up, %s%s" % (
                e.get("discussion"), e.get("author") or "?", e.get("upvotes") or 0,
                devicedata.summary(e.get("reports")), " (REFUSED by the check)" if e["problems"] else ""))
        self.file_box.blockSignals(False)
        for w in (self.file_box, self.thread_button, self.report_button):
            w.setVisible(bool(self.files))
        if not self.files:
            self.file_info.setText("Nothing shared for this device%s." % ("" if f else " yet: fetch first"))
        self.file_chosen()

    def entry(self):
        i = self.file_box.currentIndex()
        return self.files[i] if 0 <= i < len(self.files) else None

    def file_chosen(self, *_):
        e = self.entry()
        self.rows, self.at = [], 0
        self.whole_button.setVisible(e is not None and not e["problems"])
        if self.whole_button.isChecked():
            self.show_facts()
        if e is not None:
            if e["problems"]:
                self.file_info.setText("The check here refuses this file, so none of it can be used:\n%s"
                                       % "\n".join("- " + p for p in e["problems"]))
            else:
                self.rows = [r for r in devicedata.compare(self.ident, e["raw"]) if not r["same"]]
                same = len(devicedata.facts(e["raw"])) - len(self.rows)
                words = [r.get("words") for r in e.get("reports") or [] if r.get("words")]
                self.file_info.setText("%d fact%s differ from what is in use%s.%s" % (
                    len(self.rows), "" if len(self.rows) == 1 else "s",
                    ", %d are the same" % same if same else "",
                    (" Latest report: \"%s\"" % words[-1][:200]) if words else ""))
        self.go(0)

    def go(self, at):
        self.at = max(0, min(at, len(self.rows)))
        have = self.at < len(self.rows)
        r = self.rows[self.at] if have else None
        for b in (self.use_button, self.skip_button):
            b.setEnabled(have)
        self.back_button.setEnabled(self.at > 0)
        setting = have and r["path"][0] == "settings"
        self.try_button.setVisible(setting)
        self.undo_button.setVisible(setting)
        self.undo_button.setEnabled(bool(setting and self.trial(r["path"])))
        if not self.rows:
            self.step.setText("")
            return
        if not have:
            self.step.setText("<i>That was the last of them.</i>%s" % (
                " Tell how it went with <b>Report...</b>." if self.used else ""))
            return
        src = devicedata.source_text(r["source"]) if r["source"] else "no source given"
        self.step.setText(
            "<b>%d of %d: %s</b><br>In use: <tt>%s</tt><br>Shared: <tt>%s</tt> &nbsp;(%s)" % (
                self.at + 1, len(self.rows), r["label"], _html(devicedata.text(r["now"])),
                _html(devicedata.text(r["shared"])), _html(src)))

    def use(self):
        r = self.rows[self.at] if self.at < len(self.rows) else None
        if r is None:
            return
        try:
            devicedata.take(self.ident, self.name, self.entry(), r["path"])
        except devicedata.Error as e:
            QMessageBox.warning(self, TITLE, str(e))
            return
        self.used.append(devicedata.key(r["path"]))
        self.win.status("In your own quirk file now: %s" % r["label"])
        self.show_facts()
        r["now"], r["same"] = r["shared"], True
        self.go(self.at + 1)

    # -- trying a shared setting

    def candidate(self, path):
        st = devicedata.get(self.entry()["raw"], path)
        index, _, sub = path[1].partition(":")
        typ = str(st.get("type", "UDINT"))
        return tuning.Candidate(int(index, 16), int(sub or "0", 16), st.get("name") or path[1], "other",
                                testmode.TOOL_TYPES.get(typ.upper(), typ.lower())), st["value"]

    def trial(self, path):
        index, _, sub = path[1].partition(":")
        return next((e for e in tuning.pending() if e["position"] == self.position
                     and e["index"] == int(index, 16) and e["subindex"] == int(sub or "0", 16)), None)

    def try_it(self):
        r = self.rows[self.at] if self.at < len(self.rows) else None
        if r is None or r["path"][0] != "settings":
            return
        why = testmode.sdo_write_refused(self.slave, *[int(x, 16) for x in r["path"][1].split(":")])
        if why:
            QMessageBox.warning(self, TITLE, "Not tried: %s" % why)
            return
        c, value = self.candidate(r["path"])
        d = self.win.session.dictionary(self.position) if hasattr(self.win.session, "dictionary") else None
        if d is not None and d.entries and (c.index, c.subindex) not in d.entries:
            QMessageBox.warning(self, TITLE, "Not tried: %s is not in this drive's dictionary (another "
                                "revision or firmware?)" % r["path"][1])
            return
        pos = self.position

        def read(i, sub, t):
            try:
                return tool.upload(pos, i, sub, t)
            except (tool.ToolError, OSError):
                return None
        try:
            e = tuning.try_value(self.slave, c, value, read,
                                 lambda i, sub, v, t: tool.download(pos, i, sub, v, t))
        except tuning.TrialError as x:
            QMessageBox.warning(self, TITLE, str(x))
            return
        self.win.status("%s = %s in the drive's RAM now (its own %s is kept): try it, then Use it or Undo"
                        % (r["path"][1], e["value"], e["original"]))
        self.go(self.at)

    def undo(self):
        r = self.rows[self.at] if self.at < len(self.rows) else None
        e = self.trial(r["path"]) if r else None
        if e is None:
            return
        try:
            tuning.undo(e, lambda i, sub, v, t: tool.download(e["position"], i, sub, v, t))
        except tool.ToolError as x:
            QMessageBox.warning(self, TITLE, "Could not put it back: %s" % x)
            return
        self.win.status("%s back to the drive's own %s" % (r["path"][1], e["original"]))
        self.go(self.at)

    def report(self, dlg=None):
        e = self.entry()
        if e is None or not e.get("url"):
            return None
        dlg = dlg or ReportDialog(self, e, self.used, self.revision)
        if dlg.exec_() != QDialog.Accepted:
            return None
        text = dlg.text()
        QApplication.clipboard().setText(text)
        open_url(e["url"])
        self.win.status("The report is copied: paste it as a comment in the thread")
        return text


def _html(text):
    return html.escape(str(text))
