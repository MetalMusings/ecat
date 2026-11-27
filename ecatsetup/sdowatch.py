"""SDO values watched live: the Workbench's SDO card reads each watched object
again about once a second, whatever master runs (the workbench, the free master,
or LinuxCNC's), and shows its value and which bits are on. For anything the
process data does not carry, such as a drive's physical inputs (0x60FD bits 16
and up) or a maker's own status object. Nothing is written.

No Qt here: the card shows what read_all() gives.
"""

import time
from dataclasses import dataclass

from . import tool

MAX = 16                    # rows; each is one SDO read per round
CHANGED_S = 2.0             # a value that changed is marked this long


@dataclass
class Entry:
    position: int
    index: int
    sub: int
    dtype: str = None       # the tool's type ("uint32"), None: the slave's
    name: str = ""
    text: str = ""          # as last read; "" before the first read
    value: int = None       # the integer, when it is one
    error: str = ""
    changed: float = 0.0    # time.monotonic() of the last change

    @property
    def key(self):
        return (self.position, self.index, self.sub)


def parse(text):
    """The tool's upload text as (int or None, shown text). "0x0010 16" -> (16,
    ...); an unsigned value from the hex part, so that its bits are the slave's."""
    parts = text.split()
    if len(parts) == 2 and parts[0].lower().startswith("0x"):
        try:
            return int(parts[0], 16), text
        except ValueError:
            pass
    try:
        return int(text, 0), text
    except ValueError:
        return None, text


def shown(e, hex_values):
    """The value as the card shows it."""
    if e.error:
        return "–"
    parts = e.text.split()
    if len(parts) == 2 and parts[0].lower().startswith("0x"):
        return parts[0] if hex_values else parts[1]
    return e.text


def bits_on(value):
    """"2, 20" for a value with bits 2 and 20 on; "" for none or not a number."""
    if value is None or value < 0:
        return ""
    return ", ".join(str(b) for b in range(value.bit_length()) if value >> b & 1)


class Watch:
    def __init__(self):
        self.entries = []

    def add(self, position, index, sub, dtype=None, name=""):
        """A row for the object (once); False when the list is full."""
        for e in self.entries:
            if e.key == (position, index, sub):
                e.dtype = dtype or e.dtype
                return True
        if len(self.entries) >= MAX:
            return False
        self.entries.append(Entry(position, index, sub, dtype, name))
        return True

    def remove(self, key):
        self.entries = [e for e in self.entries if e.key != key]

    def read_all(self, read=None):
        """Read every row once: [(key, text or None, error)] (read(pos, index,
        sub, dtype) gives the tool's text or raises tool.ToolError). Safe in a
        thread: nothing of the entries is changed."""
        read = read or tool.upload_text
        out = []
        for e in list(self.entries):
            try:
                out.append((e.key, read(e.position, e.index, e.sub, e.dtype), ""))
            except tool.ToolError as x:
                lines = str(x).strip().splitlines()
                out.append((e.key, None, lines[-1] if lines else "no answer"))
        return out

    def take(self, results, now=None):
        """Put a round's results into the rows; the keys whose value changed."""
        now = time.monotonic() if now is None else now
        by_key = {e.key: e for e in self.entries}
        changed = []
        for key, text, error in results:
            e = by_key.get(key)
            if e is None:       # removed meanwhile
                continue
            if error:
                e.error = error
                continue
            value, text = parse(text.strip())
            if e.text and text != e.text:
                e.changed = now
                changed.append(key)
            e.text, e.value, e.error = text, value, ""
        return changed
