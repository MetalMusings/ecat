"""The Help tab: the documentation in the window (docs/development.md "The Help tab").

A self-contained widget, so that it can later move into a dock or a window of its
own: a contents tree (hideable), a search, and the section shown with Qt's own
Markdown. The text changes only when the user asks (F1, a card's ?, a link, the
contents or the search); the line "Where you are" is a suggestion, never a jump.
"""

import os

from PyQt5.QtCore import QUrl, Qt, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QImage, QKeySequence, QTextCursor, QTextImageFormat
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton, QShortcut, QSplitter,
                             QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import helpdocs


class HelpView(QWidget):
    """The documents, one section at a time."""

    back = pyqtSignal()             # "Back to <tab>" pressed (or Esc)

    def __init__(self, docs=None, parent=None):
        super().__init__(parent)
        self.docs = docs if docs is not None else helpdocs.Docs()
        self.current = None         # the section shown (an index), kept while away
        self.suggested = None       # the section "Where you are" offers
        self.history = []           # sections shown before, for Alt+Left
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        bar = QHBoxLayout()
        self.back_button = QPushButton("Back")
        self.back_button.setToolTip("Back to where you were (F1 or Esc)")
        self.back_button.clicked.connect(self.back.emit)
        self.back_button.hide()
        bar.addWidget(self.back_button)
        self.contents_button = QPushButton("Contents")
        self.contents_button.setCheckable(True)
        self.contents_button.setChecked(True)
        self.contents_button.toggled.connect(lambda on: self.tree.setVisible(on))
        bar.addWidget(self.contents_button)
        self.where = QPushButton("")
        self.where.setObjectName("link")
        self.where.setFlat(True)
        self.where.setCursor(Qt.PointingHandCursor)
        self.where.clicked.connect(lambda: self.suggested is not None and self.show_section(self.suggested))
        self.where.hide()
        bar.addWidget(self.where)
        bar.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search the help")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(260)
        self.search.textChanged.connect(self.search_changed)
        bar.addWidget(self.search)
        lay.addLayout(bar)

        self.split = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemActivated.connect(self.tree_chosen)
        self.tree.itemClicked.connect(self.tree_chosen)
        self.split.addWidget(self.tree)
        self.text = QTextBrowser()
        self.text.setOpenLinks(False)
        self.text.anchorClicked.connect(self.link_clicked)
        self.text.document().setDocumentMargin(14)
        if self.docs.folder:
            self.text.setSearchPaths([self.docs.folder])
        self.split.addWidget(self.text)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([300, 900])
        lay.addWidget(self.split, 1)
        QShortcut(QKeySequence("Alt+Left"), self, self.go_back_in_help)
        self.fill_contents()
        if not self.docs.sections:
            self.text.setMarkdown("The documentation was not found. It is in the `docs` folder of "
                                  "the program ([on GitHub](https://github.com/MetalMusings/ecat/tree/main/docs)); "
                                  "set `ECATSETUP_DOCS` to a folder that has `ecatsetup.md`.")
        else:
            self.show_section(self.docs.find("ecatsetup.md") or 0, remember=False)

    # contents and search

    def fill_contents(self, only=None):
        """The tree: every heading down to level 4, or the search's hits as a list."""
        self.tree.clear()
        self.items = {}
        if only is not None:
            for i in only:
                s = self.docs.sections[i]
                it = QTreeWidgetItem([s.title])
                it.setToolTip(0, " > ".join(self.docs.path(i)))
                it.setData(0, Qt.UserRole, i)
                self.tree.addTopLevelItem(it)
                self.items[i] = it
            if not only:
                it = QTreeWidgetItem(["Nothing found"])
                it.setDisabled(True)
                self.tree.addTopLevelItem(it)
            return

        def add(i, parent):
            s = self.docs.sections[i]
            it = QTreeWidgetItem([s.title])
            it.setData(0, Qt.UserRole, i)
            if parent is None:
                self.tree.addTopLevelItem(it)
            else:
                parent.addChild(it)
            self.items[i] = it
            for c in s.children:
                if self.docs.sections[c].level <= 4:
                    add(c, it)
        for i in self.docs.tops():
            add(i, None)
        for i in self.docs.tops():
            self.items[i].setExpanded(self.docs.sections[i].file == "ecatsetup.md")

    def search_changed(self, text):
        if text.strip():
            self.fill_contents(self.docs.search(text))
        else:
            self.fill_contents()
            self.mark_current()

    def tree_chosen(self, it, _column=0):
        i = it.data(0, Qt.UserRole)
        if i is not None:
            self.show_section(i)

    def mark_current(self):
        it = self.items.get(self.current)
        if it is not None:
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(it)
            self.tree.scrollToItem(it)
            self.tree.blockSignals(False)

    # the text

    def show_section(self, i, remember=True):
        if i is None or not 0 <= i < len(self.docs.sections):
            return
        if remember and self.current is not None and self.current != i:
            self.history.append(self.current)
            del self.history[:-50]
        self.current = i
        s = self.docs.sections[i]
        md = self.docs.text(i)
        if s.level == 1:
            # a file's title: its own text and the list of its chapters
            md += "\n\n" + "\n".join("- [%s](#%s)" % (self.docs.sections[c].title, self.docs.sections[c].anchor)
                                     for c in s.children)
        self.text.setMarkdown(md)
        self.text.document().setDocumentMargin(14)
        self.fit_images()
        self.text.verticalScrollBar().setValue(0)
        self.mark_current()

    def show_topic(self, key):
        """A place of the window (helpdocs.TOPICS) or a (file, anchor)."""
        f, anchor = helpdocs.TOPICS.get(key, key) if not isinstance(key, tuple) else key
        self.show_section(self.docs.find(f, anchor))

    def go_back_in_help(self):
        if self.history:
            i = self.history.pop()
            self.show_section(i, remember=False)

    def link_clicked(self, url):
        href = url.toString()
        here = self.docs.sections[self.current].file if self.current is not None else "ecatsetup.md"
        i = self.docs.link(href, here)
        if i is not None:
            self.show_section(i)
        elif url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
        elif self.docs.folder:
            # another file of the docs folder (development.md, an image): open it outside
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.join(self.docs.folder, href.partition("#")[0])))

    def fit_images(self):
        """Screenshots no wider than the text."""
        room = max(200, self.text.viewport().width() - 40)
        doc = self.text.document()
        cur = QTextCursor(doc)
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                fmt = frag.charFormat()
                if fmt.isImageFormat():
                    img = fmt.toImageFormat()
                    w, h = self.image_size(img.name())
                    want = min(w, room)
                    if w and abs(img.width() - want) > 1:
                        new = QTextImageFormat(img)
                        new.setWidth(want)
                        new.setHeight(h * want / w)
                        cur.setPosition(frag.position())
                        cur.setPosition(frag.position() + frag.length(), QTextCursor.KeepAnchor)
                        cur.setCharFormat(new)
                it += 1
            block = block.next()

    def image_size(self, name):
        """An image's own size (cached), (0, 0) if it cannot be read."""
        cache = self.__dict__.setdefault("_sizes", {})
        if name not in cache:
            img = QImage(os.path.join(self.docs.folder or "", name)) if not os.path.isabs(name) else QImage(name)
            cache[name] = (img.width(), img.height()) if not img.isNull() else (0, 0)
        return cache[name]

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.current is not None:
            self.fit_images()

    # where the user came from

    def set_back(self, text):
        """The Back button's text ("Back to Bus"), or None to hide it."""
        self.back_button.setVisible(bool(text))
        if text:
            self.back_button.setText(text)

    def suggest(self, where, key):
        """The "Where you are" line: a section for what the user works with, to click."""
        f, anchor = helpdocs.TOPICS.get(key, key) if not isinstance(key, tuple) else key
        i = self.docs.find(f, anchor)
        self.suggested = i
        if i is None or i == self.current:
            self.where.hide()
            return
        self.where.setText("Where you are: %s  →  %s" % (where, self.docs.sections[i].title))
        self.where.show()
