"""Parse a CoE object dictionary as listed by "ethercat sdos"."""

import re
from dataclasses import dataclass

_OBJ = re.compile(r'^SDO 0x([0-9a-f]{4}), "(.*)"')
_ENTRY = re.compile(r'^\s+0x([0-9a-f]{4}):([0-9a-f]{2}), (\S{6}), ([^,]+), (\d+) bit, "(.*)"')


@dataclass
class CoeEntry:
    index: int
    subindex: int
    access: str      # six characters: read/write in PREOP, SAFEOP, OP
    data_type: str   # as the tool names it: uint16, int32, float, string, ...
    bitlen: int
    name: str

    @property
    def writable(self):
        return "w" in self.access


class Dictionary:
    def __init__(self, text=""):
        self.objects = {}   # index -> object name
        self.entries = {}   # (index, subindex) -> CoeEntry
        for line in text.splitlines():
            m = _OBJ.match(line)
            if m:
                self.objects[int(m.group(1), 16)] = m.group(2).strip()
                continue
            m = _ENTRY.match(line)
            if m:
                e = CoeEntry(int(m.group(1), 16), int(m.group(2), 16), m.group(3),
                             m.group(4).strip(), int(m.group(5)), m.group(6).strip())
                self.entries[(e.index, e.subindex)] = e

    def __bool__(self):
        return bool(self.objects)

    def name(self, index, subindex=None):
        """A readable name: the entry's, else the object's."""
        e = self.entries.get((index, subindex))
        obj = self.objects.get(index, "")
        if e and e.name and not e.name.startswith("SubIndex"):
            return e.name if not obj or e.name == obj else "%s: %s" % (obj, e.name)
        return obj

    def subentries(self, index):
        """Entries of an object above subindex 0, in order."""
        return [e for (i, s), e in sorted(self.entries.items()) if i == index and s > 0]
