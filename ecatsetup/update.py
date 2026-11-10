"""Update an ecat configuration from the bus, keeping the user's choices.

Everything in a slave's entry in the file is a choice or was accepted as
one: its name, pins, bit splits, scaling, settings, mapping, profile mode
and DC. So a slave that is still on the bus keeps its entry as it is. The
bus only changes which slaves there are:

- a slave at the same position with the same identity is kept;
- a slave of the same kind at another position, because slaves were put
  in or taken out before it, is taken to have moved and keeps its entry
  with the new position (slaves report no serial numbers, so this goes by
  kind and order in the chain);
- a slave not in the file gets the proposal made from its own data;
- a slave in the file but not on the bus is left out.

Every change is reported as a note.
"""

import copy
from dataclasses import dataclass, field


def _kind(s):
    ident = s.get("identity") or {}
    return (int(str(ident.get("vendor")), 16), int(str(ident.get("product")), 16))


def _same_modules(s, b):
    """A modular device's modules in the file (s) and on the bus (b); a
    file without them matches."""
    a, c = s.get("modules"), b.get("modules")
    if a is None or c is None:
        return True
    return [int(str(x), 16) for x in a] == [int(str(x), 16) for x in c]


def _describe(s):
    return "%s (%s)" % (s.get("name"), s.get("device") or "unknown device")


def _align(a, b):
    """Pairs (i, j) of slaves of the same kind, in order in both chains:
    the most pairs, and of those the most at the same position."""
    n, m = len(a), len(b)
    # best[i][j]: (pairs, same position) for a[i:], b[j:]
    best = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            score = max(best[i + 1][j], best[i][j + 1])
            if _kind(a[i]) == _kind(b[j]):
                p, q = best[i + 1][j + 1]
                score = max(score, (p + 1, q + (a[i]["position"] == b[j]["position"])))
            best[i][j] = score
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if _kind(a[i]) == _kind(b[j]):
            p, q = best[i + 1][j + 1]
            if best[i][j] == (p + 1, q + (a[i]["position"] == b[j]["position"])):
                pairs.append((i, j))
                i, j = i + 1, j + 1
                continue
        if best[i][j] == best[i + 1][j]:
            i += 1
        else:
            j += 1
    return pairs


@dataclass
class Changes:
    """How the bus differs from a configuration, slave by slave."""
    moved: list = field(default_factory=list)     # [(name, device, old position, new position)]
    new: list = field(default_factory=list)       # [(position, device)]
    gone: list = field(default_factory=list)      # [(name, device, position)]
    revision: list = field(default_factory=list)  # [(name, position, file's, bus's)]
    modules: list = field(default_factory=list)   # [(name, position)] whose modules changed

    @property
    def any(self):
        return bool(self.moved or self.new or self.gone or self.revision or self.modules)

    def summary(self):
        parts = []
        for n, what in ((len(self.moved), "moved"), (len(self.new), "new"),
                        (len(self.gone), "missing"), (len(self.revision), "of another revision"),
                        (len(self.modules), "with other modules")):
            if n:
                parts.append("%d %s" % (n, what))
        return ", ".join(parts)


def compare(old, fresh):
    """Changes between the configuration and one proposed from the bus,
    matched as merge() matches them."""
    ch = Changes()
    old_slaves = sorted(old.get("slaves") or [], key=lambda s: s["position"])
    bus = sorted(fresh.get("slaves") or [], key=lambda s: s["position"])
    pairs = _align(old_slaves, bus)
    seen_old = {i for i, _ in pairs}
    seen_bus = {j for _, j in pairs}
    for i, j in pairs:
        s, b = old_slaves[i], bus[j]
        if s["position"] != b["position"]:
            ch.moved.append((s.get("name"), s.get("device"), s["position"], b["position"]))
        old_rev = (s.get("identity") or {}).get("revision")
        new_rev = (b.get("identity") or {}).get("revision")
        if old_rev is not None and new_rev is not None and int(str(old_rev), 16) != int(str(new_rev), 16):
            ch.revision.append((s.get("name"), b["position"], old_rev, new_rev))
        if not _same_modules(s, b):
            ch.modules.append((s.get("name"), b["position"]))
    for j, b in enumerate(bus):
        if j not in seen_bus:
            ch.new.append((b["position"], b.get("device")))
    for i, s in enumerate(old_slaves):
        if i not in seen_old:
            ch.gone.append((s.get("name"), s.get("device"), s["position"]))
    return ch


def consequences(cfg, changes, ini_path=None):
    """What depends on the slaves that are gone: joints, connected pins
    and lines in the HAL files. Readable strings."""
    from . import config as cfgmod, integrate
    out = []
    gone = {name for name, _, _ in changes.gone}
    prefixes = set()
    for s in (cfg or {}).get("slaves") or []:
        if s.get("name") in gone:
            prefixes |= {x + "." for x in [s["name"]] + cfgmod.axes(s)}
    gone_axes = {p[:-1] for p in prefixes}
    for j in integrate.joints(cfg):
        if j.get("slave") in gone_axes:
            out.append("joint %d is connected to %s, which is missing: the connection is "
                       "removed" % (j["joint"], j["slave"]))
    for n in integrate.nets(cfg):
        hit = [p for p in n.get("pins") or [] if p.startswith(tuple(prefixes))]
        if hit:
            out.append("signal %s connects %s, which will not exist" % (n.get("signal"),
                                                                       ", ".join(hit)))
    if ini_path and prefixes:
        from . import halcheck
        try:
            uses = halcheck.uses(ini_path)[0]
        except OSError:
            uses = {}
        for pin, us in sorted(uses.items()):
            if pin.startswith(tuple(prefixes)):
                for u in us:
                    if u.file not in (integrate.LOAD_FILE, integrate.MAIN_FILE):
                        out.append("%s:%d uses %s, which will not exist" % (u.file, u.line, pin))
    return out


def merge(old, fresh):
    """old: the configuration in the file; fresh: one proposed from the bus
    (config.from_proposals). Returns (configuration, notes)."""
    notes = []
    cfg = {"ecat": fresh["ecat"], "master": copy.deepcopy(old.get("master") or {})}

    if "dc" in fresh["master"] and "dc" not in cfg["master"]:
        cfg["master"]["dc"] = copy.deepcopy(fresh["master"]["dc"])
        notes.append("master: added DC settings, since slaves use distributed clocks")

    old_slaves = sorted(old.get("slaves") or [], key=lambda s: s["position"])
    bus = sorted(fresh["slaves"], key=lambda s: s["position"])
    # The slaves form a chain: a slave put in or taken out shifts the ones
    # after it. Match the two chains by kind, keeping their order, as many
    # slaves as possible, and of those the matching that moves the fewest.
    matched = {}  # bus position -> old slave
    for i, j in _align(old_slaves, bus):
        s, b = old_slaves[i], bus[j]
        matched[b["position"]] = s
        if s["position"] != b["position"]:
            notes.append("slave %s: moved from position %d to %d"
                         % (_describe(s), s["position"], b["position"]))
    kept = {id(s) for s in matched.values()}
    gone = [s for s in old_slaves if id(s) not in kept]

    names = {s["name"] for s in matched.values()}
    slaves = []
    for b in bus:
        s = matched.get(b["position"])
        if s is None:
            new = copy.deepcopy(b)
            base, n = new["name"], 2
            while new["name"] in names:
                new["name"] = "%s-%d" % (base, n)
                n += 1
            names.add(new["name"])
            slaves.append(new)
            notes.append("slave %d: new, %s, proposed from its own data" % (b["position"], _describe(new)))
            continue
        s = copy.deepcopy(s)
        s["position"] = b["position"]
        old_rev = s["identity"].get("revision")
        new_rev = b["identity"].get("revision")
        if new_rev is not None and old_rev is not None and int(old_rev, 16) != int(new_rev, 16):
            s["identity"]["revision"] = new_rev
            notes.append("slave %d %s: revision %s, the file had %s; check its process data"
                         % (b["position"], s["name"], new_rev, old_rev))
        if s.get("device") != b.get("device") and b.get("device"):
            s["device"] = b["device"]
        if not _same_modules(s, b):
            notes.append("slave %d %s: its modules changed (%s, the file had %s); propose it "
                         "again, since its process data depends on them"
                         % (b["position"], s["name"], ", ".join(b["modules"]) or "none",
                            ", ".join(s["modules"]) or "none"))
        elif "modules" in b and "modules" not in s:
            s["modules"] = list(b["modules"])
        slaves.append(s)

    for s in gone:
        notes.append("slave %s at position %d: not on the bus, left out" % (_describe(s), s["position"]))

    cfg["slaves"] = slaves
    # the LinuxCNC connections, without those of slaves that are gone
    lc = copy.deepcopy(old.get("linuxcnc"))
    if lc:
        from . import config as cfgmod
        prefixes = tuple(x + "." for s in gone for x in [s.get("name")] + cfgmod.axes(s) if x)
        axes = {p[:-1] for p in prefixes}
        own = tuple(x + "." for s in slaves for x in [s.get("name")] + cfgmod.axes(s) if x)
        js = []
        for j in lc.get("joints") or []:
            if j.get("slave") in axes:
                notes.append("joint %s: its drive %s is not on the bus; the connection is removed"
                             % (j.get("joint"), j.get("slave")))
            else:
                js.append(j)
        ns = []
        for n in lc.get("nets") or []:
            pins = [p for p in n.get("pins") or [] if not p.startswith(prefixes)]
            if len(pins) != len(n.get("pins") or []):
                notes.append("signal %s: pins of slaves not on the bus removed" % n.get("signal"))
            if len(pins) >= 2 and any(p.startswith(own) for p in pins):
                ns.append(dict(n, pins=pins))
        for key, v in (("joints", js), ("nets", ns)):
            if v:
                lc[key] = v
            else:
                lc.pop(key, None)
        if lc:
            cfg["linuxcnc"] = lc
    return cfg, notes
