"""The window's side of shared device data (PROTOTYPE; devicedata.py has the
rest): remembering a fact for a device type where it is learnt, and the
Device data dialog (what is in use and where it comes from, Share, Fetch,
going through a shared file's facts one by one, Try / Undo for a setting,
Report)."""

import html
import urllib.parse

from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                             QHBoxLayout, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                             QRadioButton, QVBoxLayout)

from . import devicedata, quirks, testmode, tool, tuning
from .gui import GREY, TITLE, item, label, mono, table

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


class DeviceDataDialog(QDialog):
    """A device type's facts: in use (shipped and yours, each with its source),
    Share yours, Fetch shared ones and go through a file's facts one by one."""

    def __init__(self, win, position):
        super().__init__(win)
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

        lay.addWidget(label("<b>In use</b>: what EtherCAT Setup knows about this device beyond its ESI "
                            "file, and where each fact comes from.", wrap=True))
        self.facts = table(["Fact", "Value", "From", "Source"], 1)
        self.facts.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.facts, 2)
        row = QHBoxLayout()
        self.share_button = QPushButton("Share yours...")
        self.share_button.setToolTip("Post your facts for this device (not your pin names or scales) so that "
                                     "others can use them; needs a GitHub account")
        self.share_button.clicked.connect(self.share)
        self.forget_button = QPushButton("Forget")
        self.forget_button.setToolTip("Take the chosen fact out of your own file (a shipped one comes back)")
        self.forget_button.clicked.connect(self.forget)
        row.addWidget(self.share_button)
        row.addWidget(self.forget_button)
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

    def show_facts(self):
        own = devicedata.read_raw(devicedata.own_path(self.ident)) or {}
        now = devicedata.current_raw(self.ident)
        srcs = now.get("sources") or {}
        self.in_use = devicedata.facts(now)
        self.facts.setRowCount(0)
        for p in self.in_use:
            r = self.facts.rowCount()
            self.facts.insertRow(r)
            mine = devicedata.get(own, p) is not None
            self.facts.setItem(r, 0, item(devicedata.label(p)))
            self.facts.setItem(r, 1, item(devicedata.text(devicedata.get(now, p)), font=mono()))
            self.facts.setItem(r, 2, item("yours" if mine else "shipped", colour=None if mine else GREY))
            src = srcs.get(devicedata.key(p))
            self.facts.setItem(r, 3, item(devicedata.source_text(src) if src else
                                          ("" if mine else "came with the program"),
                                          colour=None if src else GREY))
        self.share_button.setEnabled(bool(own))

    def chosen_fact(self):
        rows = self.facts.selectionModel().selectedRows() if self.facts.selectionModel() else []
        return self.in_use[rows[0].row()] if rows and rows[0].row() < len(self.in_use) else None

    def forget(self):
        p = self.chosen_fact()
        own = devicedata.read_raw(devicedata.own_path(self.ident)) or {}
        if p is None or devicedata.get(own, p) is None:
            self.win.status("Choose one of your own facts to forget")
            return
        devicedata.remember(self.ident, self.name, p, None, None)
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
