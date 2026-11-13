"""What is still to connect: the LinuxCNC pins a machine gets from the
outside or gives to it (KNOWN_PINS in integrate.py: E-stop, switches,
probe, spindle, plasmac's torch and sensors ...) and what feeds them now,
read from all of the configuration's HAL files (POSTGUI and .tcl ones too).

A pin is followed back through debounce components (dbounce, debounce:
their in -> out), never through one that changes the value (not, or2):
an E-stop is never inverted by a guess. What is found at the end is:

- "connected": a slave's pin of this configuration;
- "simulated": a component of the simulation: named so (sim-torch,
  qtplasmac_sim, sim-joint.0.pid.output, sim_home_switch) or loaded in one
  of LinuxCNC's simulation files (simulated_home.hal ...). Logic between
  (not, or2, and2 ...) is followed to decide that, but the slave's pin is
  then connected to the LinuxCNC pin itself;
- "open": nothing;
- "yours": anything else (your own logic), left alone and not listed.

For an output (LinuxCNC writes it): "simulated" when only simulation
components read it, "connected" when a slave's pin does. For a pin both
sides drive (index-enable): "simulated" when only simulation components
are on its signal, "connected" when a slave's pin is.

Each row names the pin to connect a slave's pin to (target): the one that
read the simulation, so that a debounce stays in the chain.
"""

import os
import re
from dataclasses import dataclass

from . import integrate

ARROWS = ("=>", "<=", "<=>")
PASS_THROUGH = {"dbounce", "debounce"}     # in -> out, the same value
LOGIC = {"not", "or2", "and2", "xor2", "nand2", "nor2", "logic", "edge", "toggle", "timedelay",
         "oneshot", "flipflop", "select8", "mux2", "lut5"}
# the pins of LinuxCNC's own joints: the Joints card's
JOINT_OWN = re.compile(r"joint\.\d+\.(motor-pos-cmd|motor-pos-fb|amp-enable-out|amp-fault-in)$")


@dataclass
class Row:
    pin: str            # the LinuxCNC pin
    htype: str          # its HAL type
    way: str            # "in": LinuxCNC reads it; "out": LinuxCNC writes it; "io": both
    status: str         # "simulated", "open", "connected"
    now: str = ""       # what feeds it (in) or reads it (out) now
    target: str = ""    # the pin a slave's pin is connected to
    where: str = ""     # file:line of the line that simulates it
    tcl: bool = False   # that line is in a .tcl file (changed by hand only)
    slave: str = ""     # the slave's pin, when connected

    @property
    def todo(self):
        return self.status in ("simulated", "open")


@dataclass
class Graph:
    writers: dict       # signal -> [pin]
    readers: dict       # signal -> [pin]
    unknown: dict       # signal -> [pin] (a net without arrows)
    signal: dict        # pin -> signal
    where: dict         # (signal, pin) -> (file, line, .tcl)
    instance: dict      # component instance -> (its type, file it was loaded in)
    tokens: set         # every word of every statement, for "does it appear"


def _statements(path, ini_path):
    """(line, tokens) of a HAL file; a .tcl one through haltcl."""
    if integrate.is_tcl(path):
        from . import haltcl
        x = haltcl.expand(path, ini_path)
        for a, _, t in integrate.statements(x.text):
            yield (x.lines[a] if a < len(x.lines) else 0), t
    else:
        for a, _, t in integrate.statements(integrate.original(integrate.read_text(path))):
            yield a + 1, t


def _loaded(t):
    """[(instance, type)] a loadrt or loadusr statement makes."""
    if t[0] == "loadrt" and len(t) > 1:
        comp = t[1]
        for a in t[2:]:
            if a.startswith("names="):
                return [(n, comp) for n in a[6:].split(",") if n]
            if a.startswith("count="):
                try:
                    return [("%s.%d" % (comp, i), comp) for i in range(int(a[6:]))]
                except ValueError:
                    pass
        return [(comp, comp), (comp + ".0", comp)]
    if t[0] == "loadusr":
        args = t[1:]
        name = None
        i = 0
        while i < len(args) and args[i].startswith("-"):
            if "n" in args[i][1:] and i + 1 < len(args):
                name = args[i + 1]
                i += 2
                continue
            i += 1
        prog = args[i] if i < len(args) else None
        return [(name or os.path.basename(prog), prog)] if (name or prog) else []
    return []


def graph(ini_path, cfg=None):
    """The HAL files' signals, with writers and readers from the arrows of
    each net (and the slaves' and LinuxCNC's known pins where a net has
    none)."""
    own = integrate.slave_pins(cfg) if cfg else {}
    g = Graph({}, {}, {}, {}, {}, {}, set())
    paths = [p for p, _ in _hal_files(ini_path)]
    for path in paths:
        name = os.path.basename(path)
        try:
            stmts = list(_statements(path, ini_path))
        except OSError:
            continue
        for line, t in stmts:
            g.tokens.update(t)
            for inst, typ in _loaded(t):
                g.instance.setdefault(inst, (typ, name))
            if t[0] != "net" or len(t) < 3:
                continue
            sig = t[1]
            groups, arrow, cur = [], None, []
            for x in t[2:]:
                if x in ARROWS:
                    groups.append(cur)
                    arrow, cur = arrow or x, []
                else:
                    cur.append(x)
            groups.append(cur)
            if arrow == "=>":
                w, r, u = groups[0], sum(groups[1:], []), []
            elif arrow == "<=":
                r, w, u = groups[0], sum(groups[1:], []), []
            else:
                w, r, u = [], [], sum(groups, [])
            for pin in list(u):
                d = (own.get(pin) or integrate.known_pin(pin) or (None, None))[1]
                if d == "out":
                    w.append(pin)
                    u.remove(pin)
                elif d == "in":
                    r.append(pin)
                    u.remove(pin)
            for k, pins in ((g.writers, w), (g.readers, r), (g.unknown, u)):
                k.setdefault(sig, [])
                k[sig] += [p for p in pins if p not in k[sig]]
            for pin in w + r + u:
                g.signal.setdefault(pin, sig)
                g.where.setdefault((sig, pin), (name, line, integrate.is_tcl(path)))
    return g


def _hal_files(ini_path):
    from . import halcheck
    return [(p, post) for p, post in halcheck.hal_files(ini_path) if os.path.exists(p)]


def _instance(g, pin):
    """(instance, type, file) of the component a pin belongs to."""
    best = None
    for inst in g.instance:
        if pin.startswith(inst + ".") and (best is None or len(inst) > len(best)):
            best = inst
    if best is None:
        head = pin.split(".", 1)[0]
        return head, None, None
    typ, f = g.instance[best]
    return best, typ, f


_SIM_NAME = re.compile(r"(^sim[-_.]|^sim$|[-_]sim($|[-_.]))", re.I)


def is_sim(g, pin):
    inst, typ, f = _instance(g, pin)
    return bool(_SIM_NAME.search(inst) or (typ and _SIM_NAME.search(typ))
                or f in integrate.SIM_FILES)


def _pass_through(g, pin):
    """The input of a debounce whose output this pin is, or None."""
    inst, typ, _ = _instance(g, pin)
    if typ in PASS_THROUGH and pin == inst + ".out":
        return inst + ".in"
    return None


def _endpoints(ini_path, g):
    """The LinuxCNC pins to look at: those of KNOWN_PINS the HAL files use,
    the E-stop input always, and the home switch of every joint that homes
    to a switch."""
    pins = {x for x in g.tokens if integrate.known_pin(x)}
    pins.add("iocontrol.0.emc-enable-in")
    try:
        e = integrate.read_ini(ini_path)
        for j, _, _ in integrate.ini_joints(ini_path):
            v = integrate.ini_get(e, "JOINT_%d" % j, "HOME_SEARCH_VEL") or "0"
            try:
                if float(v) != 0:
                    pins.add("joint.%d.home-sw-in" % j)
            except ValueError:
                pass
    except OSError:
        pass
    return sorted(pins)


def rows(ini_path, cfg):
    """[Row] of the LinuxCNC pins to connect to the machine, see the module
    docstring; joint pins a drive takes (Joints) are left out."""
    g = graph(ini_path, cfg)
    own = integrate.slave_pins(cfg)
    joint_taken = set(integrate.joint_pins(cfg))
    out = []
    for pin in _endpoints(ini_path, g):
        if pin in joint_taken or JOINT_OWN.match(pin):
            continue
        htype, way = integrate.known_pin(pin)
        # a slave's pin connected here (by this configuration's nets)
        net = next((n for n in integrate.nets(cfg) if pin in (n.get("pins") or [])
                    or any(x in (n.get("pins") or []) for x in _chain_heads(g, pin))), None)
        slave = next((x for x in (net or {}).get("pins") or [] if x in own), "")
        if way == "in":
            row = _trace_in(g, own, pin, htype)
        elif way == "io":
            row = _trace_io(g, own, pin, htype)
        else:
            row = _trace_out(g, own, pin, htype)
        if row is None:
            continue
        if slave:
            row.status, row.slave = "connected", slave
        out.append(row)
    out.sort(key=lambda r: (not r.todo, r.way, r.pin))
    return out


def _chain_heads(g, pin):
    """The pins a debounce chain into pin starts at (pin itself too)."""
    heads, cur = [pin], pin
    for _ in range(8):
        sig = g.signal.get(cur)
        w = [x for x in g.writers.get(sig, []) if x != cur] if sig else []
        nxt = _pass_through(g, w[0]) if len(w) == 1 else None
        if not nxt:
            break
        heads.append(nxt)
        cur = nxt
    return heads


def _sources(g, own, pin, seen=None):
    """What finally writes pin, through logic: [(kind, pin)] with kind
    "own", "sim", "open" or "other"."""
    seen = seen if seen is not None else set()
    if pin in seen:
        return []
    seen.add(pin)
    sig = g.signal.get(pin)
    w = [x for x in g.writers.get(sig, []) if x != pin] if sig else []
    if sig and not w:
        w = [x for x in g.unknown.get(sig, []) if x != pin]
    if not w:
        return [("open", pin)]
    out = []
    for x in w:
        inst, typ, _ = _instance(g, x)
        if x in own:
            out.append(("own", x))
        elif is_sim(g, x):
            out.append(("sim", x))
        elif typ in LOGIC or typ in PASS_THROUGH:
            ins = sorted({p for s in g.readers for p in g.readers[s] if p.startswith(inst + ".")} |
                         {p for s in g.unknown for p in g.unknown[s] if p.startswith(inst + ".")})
            got = [k for i in ins for k in _sources(g, own, i, seen)]
            # an input of the logic that nothing connects
            out += got or [("open", "the inputs of %s" % inst)]
        else:
            out.append(("other", x))
    return out


def _trace_in(g, own, pin, htype):
    cur = pin
    for _ in range(8):
        sig = g.signal.get(cur)
        if sig is None:
            return Row(pin, htype, "in", "open", target=cur)
        w = [x for x in g.writers.get(sig, []) if x != cur]
        if not w:
            w = [x for x in g.unknown.get(sig, []) if x != cur]
        if not w:
            return Row(pin, htype, "in", "open", now="signal %s, nothing writes it" % sig,
                       target=cur)
        if any(x in own for x in w):
            return Row(pin, htype, "in", "connected", now=w[0], target=cur,
                       slave=next(x for x in w if x in own))
        nxt = _pass_through(g, w[0]) if len(w) == 1 else None
        if nxt:
            cur = nxt
            continue
        src = _sources(g, own, cur)
        kinds = {k for k, _ in src}
        if kinds <= {"sim", "open"} and kinds:
            f, line, tcl = g.where.get((sig, w[0])) or g.where.get((sig, cur)) or ("", 0, False)
            via = "" if cur == pin else " (through %s)" % cur.rsplit(".", 1)[0]
            now = ", ".join(w) + via
            if "sim" not in kinds:
                now += ": nothing connected at %s" % ", ".join(p for _, p in src)
            return Row(pin, htype, "in", "simulated" if "sim" in kinds else "open", now=now,
                       target=cur, where="%s:%d" % (f, line) if f else "", tcl=tcl)
        return None             # your own logic
    return None


def _trace_out(g, own, pin, htype):
    sig = g.signal.get(pin)
    if sig is None:
        return None
    r = [x for x in g.readers.get(sig, []) + g.unknown.get(sig, []) if x != pin]
    if any(x in own for x in r):
        return Row(pin, htype, "out", "connected", now=", ".join(r), target=pin,
                   slave=next(x for x in r if x in own))
    if r and all(is_sim(g, x) for x in r):
        f, line, tcl = g.where.get((sig, r[0])) or ("", 0, False)
        return Row(pin, htype, "out", "simulated", now=", ".join(r), target=pin,
                   where="%s:%d" % (f, line) if f else "", tcl=tcl)
    return None


def _trace_io(g, own, pin, htype):
    """A pin both sides read and write (index-enable): what else is on its signal."""
    sig = g.signal.get(pin)
    if sig is None:
        return Row(pin, htype, "io", "open", target=pin)
    others = list(dict.fromkeys(x for k in (g.writers, g.readers, g.unknown)
                                for x in k.get(sig, []) if x != pin))
    if any(x in own for x in others):
        return Row(pin, htype, "io", "connected", now=", ".join(others), target=pin,
                   slave=next(x for x in others if x in own))
    if not others:
        return Row(pin, htype, "io", "open", now="signal %s, nothing else on it" % sig, target=pin)
    if all(is_sim(g, x) for x in others):
        f, line, tcl = g.where.get((sig, others[0])) or ("", 0, False)
        return Row(pin, htype, "io", "simulated", now=", ".join(others), target=pin,
                   where="%s:%d" % (f, line) if f else "", tcl=tcl)
    return None                 # your own logic


def connect(cfg, row, slave_pin):
    """Connect a slave's pin to a row's target (slave_pin empty: take the
    row's slave pin off it). Another slave pin on the target is taken off."""
    for n in list(integrate.nets(cfg)):
        pins = n.get("pins") or []
        if row.target in pins:
            first = pins[0]
            integrate.set_net(cfg, first, [x for x in pins[1:] if x != row.target], n.get("signal"))
    if slave_pin:
        net = integrate.net_of(cfg, slave_pin)
        others = [x for x in (net or {}).get("pins", []) if x != slave_pin]
        integrate.set_net(cfg, slave_pin, others + [row.target])


def signals(g):
    """Every signal name of the HAL files."""
    return set(g.writers) | set(g.readers) | set(g.unknown)


def expand_signals(g, way, tokens):
    """What a slave's pin is connected to when the words typed for it are pins
    and signal names of the HAL files. way: its direction as in
    integrate.slave_pins ("out": it writes the signal, "in": it reads it, "io").

    Returns (pins, signal, dropped, conflicts): the pins to connect it with (a
    signal's other pins, so that the HAL files' signal keeps its readers or
    its writer), the signal name when one signal was typed (so that it keeps
    its name), the simulated pins that give way, and the real pins that also
    write a signal this pin writes (to be asked about, not connected)."""
    pins, named, dropped, conflicts = [], [], [], []
    names = signals(g)
    for t in tokens:
        if t in g.signal or t not in names:
            pins.append(t)
            continue
        named.append(t)
        w, r, u = (g.writers.get(t, []), g.readers.get(t, []), g.unknown.get(t, []))
        sim = [x for x in w + r + u if is_sim(g, x)]
        if way == "out":
            pins += [x for x in r + u if x not in sim]
            conflicts += [x for x in w if x not in sim]
        elif way == "in":
            pins += [x for x in w if x not in sim]
        else:
            pins += [x for x in w + r + u if x not in sim]
        dropped += [x for x in sim if x in w or way == "io"]
    pins = list(dict.fromkeys(pins))
    return pins, (named[0] if len(named) == 1 else None), list(dict.fromkeys(dropped)), \
        list(dict.fromkeys(conflicts))
