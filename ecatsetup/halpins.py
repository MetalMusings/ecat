"""The HAL pins and parameters the ecat component makes for a
configuration, without running it.

The lists mirror the C tables (component/lcec_main.c, devices/ecat_cia402.c,
devices/ecat_encoder.c); tests/test_halcheck.py reads those tables and
fails when the two differ.
"""

from . import config

# (name, HAL type, direction): "in", "out", "io" (pins), "rw", "ro" (parameters)
MASTER_GLOBAL = [
    ("slaves-responding", "u32", "out"), ("state-init", "bit", "out"),
    ("state-preop", "bit", "out"), ("state-safeop", "bit", "out"), ("state-op", "bit", "out"),
    ("link-up", "bit", "out"), ("all-op", "bit", "out"), ("dc-sync-failed", "bit", "out"),
    ("take-down", "bit", "in"),
]
MASTER = [
    ("pll-err", "s32", "out"), ("pll-out", "s32", "out"), ("pll-reset-count", "u32", "out"),
    ("dc-phased", "bit", "out"), ("send-offset", "s32", "out"), ("shift-time", "u32", "out"),
    ("turnaround-cycles", "s32", "out"), ("guard-state", "s32", "out"),
    ("guard-cycles", "u32", "out"),
    ("pll-step", "u32", "rw"), ("pll-max-err", "u32", "rw"), ("dc-phase-max-err", "u32", "rw"),
    ("send-offset-max", "s32", "rw"), ("auto-shift-margin", "u32", "rw"),
    ("auto-shift-input-margin", "u32", "rw"),
]
SLAVE = [
    ("slave-online", "bit", "out"), ("slave-oper", "bit", "out"),
    ("slave-state-init", "bit", "out"), ("slave-state-preop", "bit", "out"),
    ("slave-state-safeop", "bit", "out"), ("slave-state-op", "bit", "out"),
    ("slave-dc-sync-failed", "bit", "out"),
]
CIA402 = [
    ("enable", "bit", "in"), ("pos-cmd", "float", "in"), ("velocity-cmd", "float", "in"),
    ("fault-reset", "bit", "in"), ("watchdog", "u32", "in"),
    ("pos-fb", "float", "out"), ("velocity-fb", "float", "out"), ("drv-fault", "bit", "out"),
    ("fault-reason", "s32", "out"), ("cia-state", "s32", "out"), ("statusword", "u32", "out"),
    ("controlword", "u32", "out"), ("opmode", "s32", "out"), ("opmode-display", "s32", "out"),
    ("error-code", "u32", "out"), ("drv-actual-position", "s32", "out"),
    ("drv-actual-velocity", "s32", "out"), ("drv-target-position", "s32", "out"),
    ("drv-target-velocity", "s32", "out"), ("stat-switchon-ready", "bit", "out"),
    ("stat-switched-on", "bit", "out"), ("stat-op-enabled", "bit", "out"),
    ("stat-fault", "bit", "out"), ("stat-voltage-enabled", "bit", "out"),
    ("stat-quick-stop", "bit", "out"), ("stat-switchon-disabled", "bit", "out"),
    ("stat-warning", "bit", "out"), ("stat-remote", "bit", "out"),
    ("stat-target-reached", "bit", "out"), ("opmode-no-mode", "bit", "out"),
    ("opmode-homing", "bit", "out"), ("opmode-cyclic-position", "bit", "out"),
    ("opmode-cyclic-velocity", "bit", "out"),
    ("pos-scale", "float", "rw"), ("velo-scale", "float", "rw"),
    ("auto-fault-reset", "bit", "rw"), ("enable-timeout-ms", "u32", "rw"),
    ("watchdog-ms", "u32", "rw"), ("mode", "s32", "ro"),
]
CIA402_HOMING = [
    ("index-enable", "bit", "io"), ("home", "bit", "in"), ("stat-homing", "bit", "out"),
    ("stat-homed", "bit", "out"), ("home-error", "bit", "out"), ("home-timeout-ms", "u32", "rw"),
]
CIA402_PV = [("at-speed", "bit", "out"), ("zero-speed", "bit", "out")]
CIA402_PP = [
    ("pp-target", "float", "in"), ("pp-start", "bit", "in"), ("pp-relative", "bit", "in"),
    ("pp-halt", "bit", "in"), ("pp-done", "bit", "out"), ("pp-timeout-ms", "u32", "rw"),
    ("pp-immediate", "bit", "rw"), ("pp-hold-start", "bit", "rw"),
]
CIA402_PP_VELOCITY = [("pp-velocity", "float", "in")]
CIA402_STOP = [("stop-timeout-ms", "u32", "rw")]
CIA402_ZERO = [("pos-zero", "s32", "out")]
CIA402_MODE = [("opmode-cmd", "s32", "in")]
ENCODER = [
    ("reset", "bit", "in"), ("counts", "s32", "out"), ("raw-count", "s32", "out"),
    ("pos", "float", "out"), ("velocity", "float", "out"), ("ok", "bit", "out"),
    ("pos-scale", "float", "rw"), ("velocity-window-ms", "u32", "rw"),
]
ENCODER_INDEX = [("index-enable", "bit", "io")]
ENCODER_LATCH = [("latch-enable-pos", "bit", "io"), ("latch-enable-neg", "bit", "io"),
                 ("latch-pos", "float", "out")]
FUNCTIONS = ["ecat.read-all", "ecat.write-all"]


def _mapped(slave):
    out = set()
    for kind in ("outputs", "inputs"):
        for pdo in (slave.get("process-data") or {}).get(kind) or []:
            for e in pdo.get("entries") or []:
                try:
                    out.add(config.parse_obj(e.get("object")))
                except ValueError:
                    pass
    return out


def pins(cfg):
    """{name: (HAL type, direction)} of every pin and parameter."""
    out = {}

    def add(prefix, table):
        for name, t, d in table:
            out["%s.%s" % (prefix, name)] = (t, d)
    add("ecat", MASTER_GLOBAL)
    add("ecat.0", MASTER_GLOBAL + MASTER)
    for s in (cfg or {}).get("slaves") or []:
        name = s.get("name")
        if not name:
            continue
        add(name, SLAVE)
        for kind in ("outputs", "inputs"):
            way = "in" if kind == "outputs" else "out"
            for pdo in (s.get("process-data") or {}).get(kind) or []:
                for e in pdo.get("entries") or []:
                    if "pin" in e:
                        out["%s.%s" % (name, e["pin"])] = (e.get("hal-type"), way)
                    for p in e.get("split") or []:
                        if p:
                            out["%s.%s" % (name, p)] = ("bit", way)
        if s.get("profile") == "cia402":
            c = s.get("cia402") or {}
            mode, modes = c.get("mode"), list(c.get("modes") or [])
            allowed = {mode} | set(modes)
            mapped = _mapped(s)
            for n, axis in enumerate(config.axes(s)):
                off = n * config.AXIS_OFFSET
                add(axis, CIA402)
                if "hm" in allowed:
                    add(axis, CIA402_HOMING)
                if "pv" in allowed:
                    add(axis, CIA402_PV)
                if "pp" in allowed:
                    add(axis, CIA402_PP)
                    if (0x6081 + off, 0) in mapped:
                        add(axis, CIA402_PP_VELOCITY)
                if allowed & {"pv", "pp"}:
                    add(axis, CIA402_STOP)
                if set(modes) - {"hm", mode}:
                    add(axis, CIA402_MODE)
                if c.get("zero-at-start"):
                    add(axis, CIA402_ZERO)
        mapped = _mapped(s)
        for ch in s.get("encoders") or []:
            prefix = "%s.%s" % (name, ch.get("name"))
            add(prefix, ENCODER)
            try:
                index = int(str(ch.get("index")), 16)
            except ValueError:
                continue
            # as ecat_encoder.c: the latch value, its valid bit and its enable
            o = index + 0x1000
            if {(index, 0x01), (index, 0x12), (o, 0x01)} <= mapped:
                add(prefix, ENCODER_INDEX)
            if {(index, 0x02), (index, 0x12)} <= mapped and mapped & {(o, 0x02), (o, 0x04)}:
                add(prefix, ENCODER_LATCH)
    return out
