"""The CiA 402 homing methods (0x6098): what each one does, which of them a
drive supports, and the settings a chosen method needs.

The catalog is the same for every drive. What a drive supports comes from the
drive itself (0x60E3, if it has that object). What a drive's own methods do,
and the settings of its own they need (the ECT60's torque homing), are in its
manual and the notes on the device. See docs/ecatsetup.md.
"""

from . import config, tool

# What a method needs from the machine
LIMIT_NEG, LIMIT_POS, HOME_SWITCH, INDEX, STOP = \
    "negative limit switch", "positive limit switch", "home switch", "index pulse", "mechanical stop"


class Method:
    def __init__(self, number, text, needs=(), moves=True, uses_index=False):
        self.number = number
        self.text = text
        self.needs = tuple(needs)
        self.moves = moves
        self.uses_index = uses_index

    def __repr__(self):
        return "Method(%d)" % self.number


def _build():
    m = {}

    def add(n, text, needs=(), moves=True, index=False):
        m[n] = Method(n, text, needs, moves, index)

    add(1, "Negative limit switch, then the index pulse", (LIMIT_NEG, INDEX), index=True)
    add(2, "Positive limit switch, then the index pulse", (LIMIT_POS, INDEX), index=True)
    for n in (3, 4):
        add(n, "Home switch (positive side), then the index pulse", (HOME_SWITCH, INDEX), index=True)
    for n in (5, 6):
        add(n, "Home switch (negative side), then the index pulse", (HOME_SWITCH, INDEX), index=True)
    for n in range(7, 11):
        add(n, "Home switch, starting positive, reversing at the positive limit switch, "
               "then the index pulse", (HOME_SWITCH, LIMIT_POS, INDEX), index=True)
    for n in range(11, 15):
        add(n, "Home switch, starting negative, reversing at the negative limit switch, "
               "then the index pulse", (HOME_SWITCH, LIMIT_NEG, INDEX), index=True)
    add(17, "Negative limit switch", (LIMIT_NEG,))
    add(18, "Positive limit switch", (LIMIT_POS,))
    for n in (19, 20):
        add(n, "Home switch (positive side)", (HOME_SWITCH,))
    for n in (21, 22):
        add(n, "Home switch (negative side)", (HOME_SWITCH,))
    for n in range(23, 27):
        add(n, "Home switch, starting positive, reversing at the positive limit switch",
            (HOME_SWITCH, LIMIT_POS))
    for n in range(27, 31):
        add(n, "Home switch, starting negative, reversing at the negative limit switch",
            (HOME_SWITCH, LIMIT_NEG))
    add(33, "Index pulse, negative direction", (INDEX,), index=True)
    add(34, "Index pulse, positive direction", (INDEX,), index=True)
    add(35, "The current position is home; the axis does not move", moves=False)
    add(36, "Manufacturer specific (the drive's manual says what it does)")
    add(37, "Manufacturer specific, or in newer drives the current position "
            "(the drive's manual says which)")
    return m


CATALOG = _build()
# The numbers below 0 are manufacturer specific, and 15, 16, 31, 32 are reserved.


def method(number, slave=None):
    """A catalog entry; a number that is not in it gives a manufacturer-specific
    entry (the drive's manual, and the notes on the device, say what it does)."""
    base = CATALOG.get(number)
    if base is None:
        base = Method(number, "Manufacturer specific (the drive's manual says what it does)")
    return base


def supported(slave, axis=0, read=None):
    """(methods, where) for a slave: a sorted list of numbers and "drive (0x60E3)";
    or (None, None) when the drive does not say, and every method in the catalog
    may be tried. `read(index, sub, type)` reads an SDO (None if the drive
    refuses); tool.upload by default."""
    read = read or (lambda i, s, t: tool.upload(slave["position"], i, s, t))
    base = 0x60E3 + axis * config.AXIS_OFFSET
    count = read(base, 0, "uint8")
    if not count:
        return None, None
    # Each sub-index of 0x60E3 holds a supported method number (CiA 402).
    # Not tried on a drive: the ECT60 has no 0x60E3.
    found = set()
    for sub in range(1, count + 1):
        v = read(base, sub, "int16")
        if v:
            found.add(v)
    return (sorted(found), "drive (0x60E3)") if found else (None, None)


def choices(slave, axis=0, read=None):
    """The list for the window: [(Method, supported)] for every method of the
    catalog plus those the drive lists; supported is True, False, or None when
    nothing says."""
    nums, _ = supported(slave, axis, read)
    every = sorted(set(CATALOG) | set(nums or ()))
    return [(method(n, slave), None if nums is None else n in nums) for n in every]


# Parameters of a method (drive units), and the objects they go to
PARAMS = (("fast", 0x6099, 1, "uint32", "speed towards the switch"),
          ("slow", 0x6099, 2, "uint32", "speed from the switch to the index or the edge"),
          ("accel", 0x609A, 0, "int32", "acceleration"),
          ("offset", 0x607C, 0, "int32", "home offset"))


def parameters(number, slave=None):
    """Which of fast, slow, accel, offset a method uses."""
    m = method(number, slave)
    if not m.moves:
        return ["offset"]
    return ["fast", "slow", "accel", "offset"]


def plan(slave, number, params=None, axis=0):
    """The settings that make the drive home by a method: [{"object", "type",
    "value", "name"}] for the slave's `settings`. params: {"fast": ...} for
    those the method uses; the others are left out."""
    params = params or {}
    off = axis * config.AXIS_OFFSET
    out = [{"object": "0x%04X:%02X" % (0x6098 + off, 0), "type": "SINT", "value": number,
            "name": "Homing method"}]
    used = parameters(number, slave)
    for key, index, sub, dtype, text in PARAMS:
        if key in used and params.get(key) is not None:
            out.append({"object": "0x%04X:%02X" % (index + off, sub),
                        "type": {"uint32": "UDINT", "int32": "DINT"}[dtype],
                        "value": int(params[key]), "name": text})
    return out


def choice(slave, axis=0):
    """The homing chosen for an axis of a slave: {"method": n, "fast": ...} or None.
    `homing` of a slave is that mapping for a drive with one axis, or a list of them
    (None for an axis without) for a drive with several."""
    h = (slave or {}).get("homing")
    if isinstance(h, list):
        h = h[axis] if axis < len(h) else None
    elif axis:
        h = None
    return dict(h) if h and h.get("method") is not None else None


def _owned(slave, number, axis):
    """The objects (as the strings of `settings`) a chosen method wrote."""
    return {s["object"].upper().replace("X", "x") for s in plan(slave, number, {k: 0 for k in
            ("fast", "slow", "accel", "offset")}, axis)}


def _norm(text):
    index, _, sub = str(text).partition(":")
    return (int(index, 16), int(sub or "0", 16))


def _cia402_keys(slave):
    """The profile's homing keys from the slave's choices (one per axis):
    home-enable-again if a chosen method needs it, and home-speed, the
    fastest homing speed (1 for methods that do not move), so that the
    profile tells the drive setting its position from travel."""
    n = max(len(config.axes(slave)), 1)
    chosen = [c for c in (choice(slave, a) for a in range(n)) if c]
    c402 = slave.setdefault("cia402", {})
    again = any(c.get("enable-again") for c in chosen)
    if again:
        c402["home-enable-again"] = True
    else:
        c402.pop("home-enable-again", None)
    speeds = []
    for c in chosen:
        if not method(c["method"], slave).moves:
            speeds.append(1)
        else:
            speeds += [abs(int(c[k])) for k in ("fast", "slow") if c.get(k)]
    if chosen and speeds and all(speeds):
        c402["home-speed"] = max(speeds)
    else:
        c402.pop("home-speed", None)


def apply(slave, axis, chosen):
    """Make the choice (or None for no choice) the slave's homing: the `homing`
    block, and its settings in `settings` (those of an earlier choice go). Other
    settings, also those for the same objects that the user wrote, are kept
    unless the new choice writes them: then the choice's value wins."""
    old = choice(slave, axis)
    gone = set()
    if old:
        gone |= {_norm(o) for o in _owned(slave, old["method"], axis)}
    if chosen:
        gone |= {_norm(o) for o in _owned(slave, chosen["method"], axis)}
    settings = [st for st in slave.get("settings") or [] if _norm(st["object"]) not in gone]
    if chosen:
        settings += plan(slave, chosen["method"], chosen, axis)
    if settings:
        slave["settings"] = settings
    else:
        slave.pop("settings", None)
    n = len(config.axes(slave))
    block = ({k: v for k, v in chosen.items() if v is not None} if chosen else None)
    if n <= 1:
        if block:
            slave["homing"] = block
        else:
            slave.pop("homing", None)
    else:
        h = slave.get("homing")
        h = list(h) if isinstance(h, list) else [None] * n
        h += [None] * (n - len(h))
        h[axis] = block
        if any(h):
            slave["homing"] = h
        else:
            slave.pop("homing", None)
    if slave.get("profile") == "cia402" or "cia402" in slave:
        _cia402_keys(slave)


def ini_help(cfg, slave, axis=0):
    """The LinuxCNC INI lines for a joint that homes through the drive, with
    what each is for: [(text, is_setting)]. None when no method is chosen.
    The joint is the one of the configuration's `linuxcnc: joints` that uses the
    axis; JOINT_N when there is none."""
    chosen = choice(slave, axis)
    if not chosen:
        return None
    names = config.axes(slave)
    drive = names[axis] if axis < len(names) else slave.get("name")
    joint = next((j["joint"] for j in ((cfg or {}).get("linuxcnc") or {}).get("joints") or []
                  if j.get("slave") == drive), None)
    m = method(chosen["method"], slave)
    if _homecomp(cfg, joint):
        out = [("[EMCMOT]", True),
               ("# Written by Apply: the homing module that hands homing to the drive (a test).", False),
               ("HOMEMOD = cia402_homecomp", True),
               ("[JOINT_%s]" % joint, True),
               ("# The drive homes by method %d (%s), started by cia402_homecomp." % (m.number, m.text),
                False),
               ("# Motion does not search: no search, latch or index.", False),
               ("HOME_SEARCH_VEL = 0", True),
               ("HOME_LATCH_VEL = 0", True),
               ("HOME_USE_INDEX = NO", True),
               ("# HOME_OFFSET is the machine coordinate of where the drive stands when it reports", False),
               ("# homing done. HOME is where the joint goes after homing, at HOME_FINAL_VEL.", False),
               ("# Coming from index-enable homing (HOME_OFFSET where the drive's position is 0):", False),
               ("# measure HOME_OFFSET again; it differs where the drive does not stand at its", False),
               ("# position 0 then (a springy stop: about 1 mm on a plasma cutter's ECT60).", False),
               ("HOME_OFFSET = 0", True),
               ("HOME = 0", True),
               ("HOME_FINAL_VEL = 10", True),
               ("# Joints with the same number home together, lower numbers first.", False),
               ("HOME_SEQUENCE = 0", True)]
        if m.moves:
            out.insert(6, ("# The axis MOVES at the drive's speeds (0x6099); motion does not see the "
                           "move (the HAL lines limit it).", False))
        return out
    out = [("[JOINT_%s]" % ("N" if joint is None else joint), True),
           ("# The drive homes by method %d (%s)." % (m.number, m.text), False),
           ("# Motion does not search itself: it only waits for the drive, as for an index pulse.",
            False),
           ("HOME_SEARCH_VEL = 0", True),
           ("HOME_LATCH_VEL = 1", True),
           ("HOME_USE_INDEX = YES", True),
           ("# HOME_OFFSET is the machine coordinate where the drive's position is 0 after", False),
           ("# homing; the drive gives the home point the position of its home offset", False),
           ("# (0x607C) or 0. HOME is where the joint goes after homing.", False),
           ("# Coming from cia402_homecomp: measure HOME_OFFSET again, and take its lines", False),
           ("# (joint.N.cia-*) out of your HAL files: LinuxCNC does not start with them.", False),
           ("HOME_OFFSET = 0", True),
           ("HOME = 0", True),
           ("# Joints with the same number home together, lower numbers first.", False),
           ("HOME_SEQUENCE = 0", True)]
    if m.moves:
        out.insert(3, ("# The axis MOVES at the drive's speeds (0x6099) and must reach its "
                       "end within home-timeout-ms (60 s). Motion does not see the move:", False))
        out.insert(4, ("# limit it with %s.home-max-travel in HAL (the HAL lines)." % drive, False))
    else:
        out.insert(3, ("# This method does not move the axis.", False))
    return out


def _homecomp(cfg, joint):
    """Whether the joint (number or None) homes through cia402_homecomp."""
    from . import integrate
    return joint is not None and integrate.joint_homing(cfg, joint) == "homecomp"


def hal_help(cfg, slave, axis=0, ini_path=None):
    """The HAL lines for a joint that homes through the drive, with what each
    is for: [(text, is_setting)]. None when no method is chosen. The
    index-enable net is written by Apply (shown, not to copy); the guards
    home-max-travel and home-timeout-ms for a method that moves the axis,
    with values from the INI file's axis limits and the chosen speed where
    those are known (docs/ecatsetup.md, "From LinuxCNC's homing to the
    drive's", step 6)."""
    import math
    import os
    from . import integrate
    chosen = choice(slave, axis)
    if not chosen:
        return None
    names = config.axes(slave)
    drive = names[axis] if axis < len(names) else slave.get("name")
    joint = next((j for j in ((cfg or {}).get("linuxcnc") or {}).get("joints") or []
                  if j.get("slave") == drive), None)
    n = "N" if joint is None else joint["joint"]
    letter, sig = None, None
    if ini_path and os.path.exists(ini_path):
        try:
            letter = next((x for j, x, _ in integrate.ini_joints(ini_path) if j == n), None)
        except (OSError, ValueError):
            letter = None
        main = os.path.join(os.path.dirname(os.path.abspath(ini_path)), integrate.MAIN_FILE)
        try:
            with open(main) as f:
                text = f.read()
            for line in text.splitlines():
                t = line.split()
                if t[:1] == ["net"] and "%s.index-enable" % drive in t:
                    sig = t[1]
        except OSError:
            pass
    sig = sig or "%s-index-enable" % ((letter or "j%s" % n).lower())
    comp = _homecomp(cfg, n if joint else None)
    if comp:
        out = [("# Written by Apply into ethercat.hal (nothing to do here): the nets between", False),
               ("# joint.%s, cia402_homecomp's joint.%s.cia-* pins and %s, and" % (n, n, drive), False),
               ("#   setp joint.%s.request-custom-homing 1" % n, False),
               ("#   setp %s.home-hold 1" % drive, False)]
    else:
        out = [("# Written by Apply into ethercat.hal (nothing to do here):", False),
               ("#   net %s joint.%s.index-enable <=> %s.index-enable" % (sig, n, drive), False)]
    m = method(chosen["method"], slave)
    if not m.moves:
        out.append(("# Method %d does not move the axis: no guard is needed." % m.number, False))
        return out
    length = None
    if letter and ini_path:
        try:
            e = integrate.read_ini(ini_path)
            lo = float(integrate.ini_get(e, "AXIS_%s" % letter.upper(), "MIN_LIMIT"))
            hi = float(integrate.ini_get(e, "AXIS_%s" % letter.upper(), "MAX_LIMIT"))
            length = abs(hi - lo)
        except (TypeError, ValueError, OSError):
            length = None
    out.append(("# Optional, recommended for a method that moves the axis: motion does not see", False))
    out.append(("# the axis while the drive homes. Put these in one of your own HAL files.", False))
    if length:
        travel = int(math.ceil(length * 1.05 / 10.0) * 10)
        out.append(("# The axis is %g long ([AXIS_%s] MAX_LIMIT - MIN_LIMIT), and a margin:"
                    % (length, letter.upper()), False))
        out.append(("setp %s.home-max-travel %d" % (drive, travel), True))
    else:
        out.append(("# A little more than the axis length (no INI file or axis limits to work it out from):",
                    False))
        out.append(("setp %s.home-max-travel <axis length + a margin>" % drive, True))
    scale = abs(float((joint or {}).get("scale") or 0))
    fast = abs(float(chosen.get("fast") or 0))
    if length and scale and fast:
        secs = length / (fast / scale)
        ms = max(60000, int(math.ceil(2 * secs / 10.0) * 10000))
        out.append(("# The whole axis at the homing speed takes %.0f s (%g counts/s at %g counts per unit);"
                    % (secs, fast, scale), False))
        out.append(("# twice that, and at least the default 60 s:", False))
        out.append(("setp %s.home-timeout-ms %d" % (drive, ms), True))
        if comp:
            out.append(("# cia402_homecomp's own limit (30 s by default), the same:", False))
            out.append(("setp joint.%s.cia-timeout-ms %d" % (n, ms), True))
    else:
        out.append(("# 60 s by default: raise it if a homing can take longer (the whole axis at the homing speed):",
                    False))
        out.append(("#setp %s.home-timeout-ms 60000" % drive, False))
        if comp:
            out.append(("# cia402_homecomp's own limit is 30 s by default:", False))
            out.append(("#setp joint.%s.cia-timeout-ms 60000" % n, False))
    return out
