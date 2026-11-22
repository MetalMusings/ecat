"""The documentation as the window's Help tab shows it: the Markdown files split
at their headings, a search over them, and which section belongs to which part
of the window (docs/development.md "The Help tab").

No Qt here, so that the tests run without PyQt5. Anchors are GitHub's, so that a
link that works on github.com works in the tab too.
"""

import os
import re
from dataclasses import dataclass, field

# The documents, in the order of the contents: the reference last.
FILES = ("getting-started.md", "ecatsetup.md", "reference.md")

_HERE = os.path.dirname(os.path.abspath(__file__))


def docs_dir():
    """The folder with the documents: $ECATSETUP_DOCS, a copy installed among the
    program's own files (ecatsetup/docs), a checkout's docs/, or the package's
    documentation folder. None if none has them."""
    if os.environ.get("ECATSETUP_DOCS"):
        return os.environ["ECATSETUP_DOCS"]
    for d in (os.path.join(_HERE, "docs"), os.path.join(_HERE, "..", "docs"),
              "/usr/share/doc/linuxcnc-ecat/docs"):
        if os.path.exists(os.path.join(d, "ecatsetup.md")):
            return os.path.normpath(d)
    return None


def slug(title):
    """GitHub's anchor for a heading (without the -1, -2 of a repeated one)."""
    t = title.strip().lower()
    t = re.sub(r"[^\w\- ]", "", t)
    return t.replace(" ", "-")


@dataclass
class Section:
    file: str
    level: int
    title: str
    anchor: str
    first: int                  # line index of the heading
    end: int = 0                # line index after its own text (the next heading)
    last: int = 0               # line index after its subsections too
    parent: int = -1            # index in Docs.sections, -1 at the top
    children: list = field(default_factory=list)


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


class Docs:
    """The documents of a folder, split at their headings."""

    def __init__(self, folder=None, files=FILES):
        self.folder = folder or docs_dir()
        self.lines = {}         # file -> its lines
        self.sections = []
        self.by_anchor = {}     # (file, anchor) -> index
        if self.folder is None:
            return
        for name in files:
            path = os.path.join(self.folder, name)
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.read().split("\n")
            except OSError:
                continue
            self.lines[name] = lines
            self._split(name, lines)

    def _split(self, name, lines):
        seen = {}
        stack = []              # indexes of the open sections, by level
        inside = False
        mine = []
        for n, line in enumerate(lines):
            if line.lstrip().startswith("```"):
                inside = not inside
                continue
            m = None if inside else _HEADING.match(line)
            if not m:
                continue
            level, title = len(m.group(1)), m.group(2)
            a = slug(title)
            k = seen.get(a, 0)
            seen[a] = k + 1
            anchor = a if k == 0 else "%s-%d" % (a, k)
            while stack and self.sections[stack[-1]].level >= level:
                stack.pop()
            s = Section(name, level, title, anchor, n, parent=stack[-1] if stack else -1)
            i = len(self.sections)
            self.sections.append(s)
            if stack:
                self.sections[stack[-1]].children.append(i)
            stack.append(i)
            mine.append(i)
            self.by_anchor[(name, anchor)] = i
        for j, i in enumerate(mine):
            s = self.sections[i]
            s.end = self.sections[mine[j + 1]].first if j + 1 < len(mine) else len(lines)
            s.last = next((self.sections[k].first for k in mine[j + 1:]
                           if self.sections[k].level <= s.level), len(lines))

    def find(self, file, anchor=None):
        """The index of a section: a file's anchor, or the file's first heading."""
        if anchor:
            return self.by_anchor.get((file, anchor))
        return next((i for i, s in enumerate(self.sections) if s.file == file), None)

    def tops(self):
        """The top sections of every file, in order."""
        return [i for i, s in enumerate(self.sections) if s.parent < 0]

    def text(self, i, whole=True):
        """A section's Markdown: its own text, and with `whole` its subsections too.
        A file's title (level 1) shows its own text only: the file is long."""
        s = self.sections[i]
        stop = s.last if whole and s.level > 1 else s.end
        return "\n".join(self.lines[s.file][s.first:stop])

    def path(self, i):
        """The headings from the file's title down to a section."""
        out = []
        while i >= 0:
            out.append(self.sections[i].title)
            i = self.sections[i].parent
        return list(reversed(out))

    def search(self, query, limit=60):
        """The sections whose own text has every word of `query` (any case), the
        ones with a word in the title first."""
        words = [w for w in query.lower().split() if w]
        if not words:
            return []
        hits = []
        for i, s in enumerate(self.sections):
            own = "\n".join(self.lines[s.file][s.first:s.end]).lower()
            if all(w in own for w in words):
                title = s.title.lower()
                hits.append((-sum(w in title for w in words), i))
        return [i for _, i in sorted(hits)[:limit]]

    def link(self, href, here):
        """Where a link inside the documents goes: a section index, or None for a
        link out (a web page, a file that is not one of the documents). `here` is
        the file the link is in."""
        if not href or re.match(r"^[a-z]+:", href):
            return None
        target, _, anchor = href.partition("#")
        target = os.path.basename(target) if target else here
        if target not in self.lines:
            return None
        return self.find(target, anchor or None)


# Which section the window's parts point to: a tab (by its key) or a card (by its
# title, as Card shows it). tests/test_helpdocs.py checks that each one exists, so
# a renamed heading turns a test red.
TOPICS = {
    # tabs
    "tab:start": ("ecatsetup.md", "start"),
    "tab:bus": ("ecatsetup.md", "bus"),
    "tab:status": ("ecatsetup.md", "troubleshooting-a-slave"),
    "tab:test": ("ecatsetup.md", "test-mode"),
    "tab:hal": ("ecatsetup.md", "writing-the-configuration-into-linuxcnc"),
    "tab:timing": ("ecatsetup.md", "the-computers-timing"),
    "tab:master": ("ecatsetup.md", "starting-the-master"),
    # what the selected slave is
    "profile:cia402": ("ecatsetup.md", "cia-402-drives"),
    "profile:encoder": ("ecatsetup.md", "encoder-channels"),
    "profile:serial": ("ecatsetup.md", "serial-channels"),
    "profile:mdp": ("ecatsetup.md", "bringing-up-a-new-slave"),
    "profile:modules": ("ecatsetup.md", "modular-devices"),
    "profile:generic": ("ecatsetup.md", "bringing-up-a-new-slave"),
    # cards
    "Bus": ("ecatsetup.md", "bus"),
    "Move": ("ecatsetup.md", "moving-a-drive"),
    "Response": ("ecatsetup.md", "measuring-a-drives-response"),
    "Master": ("ecatsetup.md", "bus"),
    "Operation and sync": ("ecatsetup.md", "bus"),
    "Process data": ("ecatsetup.md", "bus"),
    "Settings": ("ecatsetup.md", "bus"),
    "Distributed clocks": ("ecatsetup.md", "distributed-clocks"),
    "Synchronisation": ("ecatsetup.md", "synchronisation-in-the-test-mode"),
    "Homing by the drive": ("ecatsetup.md", "homing-by-the-drive"),
    "Network (EoE)": ("ecatsetup.md", "a-devices-web-page-over-eoe"),
    "Serial ports": ("ecatsetup.md", "a-modbus-vfd-on-a-serial-terminal-el6021"),
    "Written at start by the device's description (InitCmds)": ("ecatsetup.md", "bus"),
    "Notes from the proposal": ("ecatsetup.md", "where-the-proposal-comes-from"),
    "Cable and link": ("ecatsetup.md", "step-3-cable-and-connectors"),
    "What the slave says": ("ecatsetup.md", "step-2-why-it-is-not-in-op"),
    "Messages from the master": ("ecatsetup.md", "step-2-why-it-is-not-in-op"),
    "Drive state (CiA 402)": ("ecatsetup.md", "cia-402-drives"),
    "I/O test": ("ecatsetup.md", "io"),
    "Inputs (from the slave)": ("ecatsetup.md", "io"),
    "Outputs (to the slave)": ("ecatsetup.md", "io"),
    "SDO (CoE object)": ("ecatsetup.md", "io"),
    "Live values": ("ecatsetup.md", "watching-linuxcnc"),
    "Event log": ("ecatsetup.md", "the-event-log"),
    "Symptoms": ("ecatsetup.md", "what-the-event-log-means"),
    "Joints": ("ecatsetup.md", "joints"),
    "Parameters": ("ecatsetup.md", "parameters"),
    "Inputs and outputs": ("ecatsetup.md", "inputs-and-outputs"),
    "Still to connect": ("ecatsetup.md", "from-a-simulated-configuration"),
    "From lcec": ("ecatsetup.md", "from-an-lcec-configuration"),
    "HAL check": ("ecatsetup.md", "checking-the-hal-files"),
    "Apply to LinuxCNC": ("ecatsetup.md", "what-is-written"),
    "Measure": ("ecatsetup.md", "the-computers-timing"),
    "Findings": ("ecatsetup.md", "the-computers-timing"),
    "The computer": ("ecatsetup.md", "what-the-check-looks-at"),
    "History: changes made to this computer": ("ecatsetup.md", "the-computers-timing"),
}
