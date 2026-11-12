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
    minimum: int = None   # from an ESI file, when it gives them
    maximum: int = None
    default: int = None

    @property
    def writable(self):
        return "w" in self.access

    @property
    def writable_preop(self):
        """Whether the slave accepts a write in PREOP, where settings are written."""
        return len(self.access) > 1 and self.access[1] == "w"


class Dictionary:
    def __init__(self, text=""):
        self.objects = {}   # index -> object name
        self.entries = {}   # (index, subindex) -> CoeEntry
        self.source = "slave"   # "slave", "ESI" or "slave and ESI"
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

    def enriched(self, esi_dict):
        """This dictionary (read from the slave) with the ESI's minimum,
        maximum and default values added to the entries both have. Without
        entries of its own (no bus, or a slave that lists none) the ESI's
        dictionary is used as it is."""
        if not self.entries:
            return esi_dict
        out = Dictionary()
        out.objects = dict(self.objects)
        for key, e in self.entries.items():
            x = esi_dict.entries.get(key)
            if x and (x.minimum, x.maximum, x.default) != (None, None, None):
                e = CoeEntry(e.index, e.subindex, e.access, e.data_type, e.bitlen, e.name,
                             x.minimum, x.maximum, x.default)
            out.entries[key] = e
        out.source = "slave and ESI" if esi_dict.entries else "slave"
        return out
