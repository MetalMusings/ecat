"""The HAL pins and parameters the ecat component makes for a
configuration, without running it.

The lists mirror the C tables (component/lcec_main.c, devices/ecat_cia402.c,
devices/ecat_encoder.c); tests/test_halcheck.py reads those tables and
fails when the two differ.
"""

from . import config

# (name, HAL type, direction): "in", "out", "io" (pins), "rw", "ro" (parameters)
MASTER_GLOBAL = [
    ("slaves-responding", "uint", "out"), ("state-init", "bool", "out"),
    ("state-preop", "bool", "out"), ("state-safeop", "bool", "out"), ("state-op", "bool", "out"),
    ("link-up", "bool", "out"), ("all-op", "bool", "out"), ("dc-sync-failed", "bool", "out"),
    ("take-down", "bool", "in"),
]
MASTER = [
    ("pll-err", "sint", "out"), ("pll-out", "sint", "out"), ("pll-reset-count", "uint", "out"),
    ("dc-phased", "bool", "out"), ("send-offset", "sint", "out"), ("shift-time", "uint", "out"),
    ("turnaround-cycles", "sint", "out"), ("guard-state", "sint", "out"),
    ("guard-cycles", "uint", "out"),
    ("pll-step", "uint", "rw"), ("pll-max-err", "uint", "rw"), ("dc-phase-max-err", "uint", "rw"),
    ("send-offset-max", "sint", "rw"), ("auto-shift-margin", "uint", "rw"),
    ("auto-shift-input-margin", "uint", "rw"),
]
SLAVE = [
    ("slave-online", "bool", "out"), ("slave-oper", "bool", "out"),
    ("slave-state-init", "bool", "out"), ("slave-state-preop", "bool", "out"),
    ("slave-state-safeop", "bool", "out"), ("slave-state-op", "bool", "out"),
    ("slave-dc-sync-failed", "bool", "out"),
]
CIA402 = [
    ("enable", "bool", "in"), ("pos-cmd", "real", "in"), ("velocity-cmd", "real", "in"),
    ("fault-reset", "bool", "in"), ("watchdog", "uint", "in"),
    ("pos-fb", "real", "out"), ("velocity-fb", "real", "out"), ("drv-fault", "bool", "out"),
    ("fault-reason", "sint", "out"), ("cia-state", "sint", "out"), ("statusword", "uint", "out"),
    ("controlword", "uint", "out"), ("opmode", "sint", "out"), ("opmode-display", "sint", "out"),
    ("error-code", "uint", "out"), ("drv-actual-position", "sint", "out"),
    ("drv-actual-velocity", "sint", "out"), ("drv-target-position", "sint", "out"),
    ("drv-target-velocity", "sint", "out"), ("stat-switchon-ready", "bool", "out"),
    ("stat-switched-on", "bool", "out"), ("stat-op-enabled", "bool", "out"),
    ("stat-fault", "bool", "out"), ("stat-voltage-enabled", "bool", "out"),
    ("stat-quick-stop", "bool", "out"), ("stat-switchon-disabled", "bool", "out"),
    ("stat-warning", "bool", "out"), ("stat-remote", "bool", "out"),
    ("stat-target-reached", "bool", "out"), ("opmode-no-mode", "bool", "out"),
    ("opmode-homing", "bool", "out"), ("opmode-cyclic-position", "bool", "out"),
    ("opmode-cyclic-velocity", "bool", "out"),
    ("pos-scale", "real", "rw"), ("velo-scale", "real", "rw"),
    ("auto-fault-reset", "bool", "rw"), ("enable-timeout-ms", "uint", "rw"),
    ("watchdog-ms", "uint", "rw"), ("mode", "sint", "ro"),
]
CIA402_HOMING = [
    ("index-enable", "bool", "io"), ("home", "bool", "in"), ("stat-homing", "bool", "out"),
    ("stat-homed", "bool", "out"), ("home-error", "bool", "out"), ("home-timeout-ms", "uint", "rw"),
]
CIA402_PV = [("at-speed", "bool", "out"), ("zero-speed", "bool", "out")]
CIA402_PP = [
    ("pp-target", "real", "in"), ("pp-start", "bool", "in"), ("pp-relative", "bool", "in"),
    ("pp-halt", "bool", "in"), ("pp-done", "bool", "out"), ("pp-timeout-ms", "uint", "rw"),
    ("pp-immediate", "bool", "rw"),
]
CIA402_PP_VELOCITY = [("pp-velocity", "real", "in")]
CIA402_STOP = [("stop-timeout-ms", "uint", "rw")]
CIA402_ZERO = [("pos-zero", "sint", "out")]
CIA402_MODE = [("opmode-cmd", "sint", "in")]
ENCODER = [
    ("reset", "bool", "in"), ("counts", "sint", "out"), ("raw-count", "sint", "out"),
    ("pos", "real", "out"), ("velocity", "real", "out"), ("ok", "bool", "out"),
    ("pos-scale", "real", "rw"), ("velocity-window-ms", "uint", "rw"),
]
ENCODER_INDEX = [("index-enable", "bool", "io")]
ENCODER_LATCH = [("latch-enable-pos", "bool", "io"), ("latch-enable-neg", "bool", "io"),
                 ("latch-pos", "real", "out")]
FUNCTIONS = ["ecat.read-all", "ecat.write-all"]

# Defaults of the parameters a user may set (docs/component.md); a parameter
# not listed has no default shown.
DEFAULTS = {
    "pos-scale": 1.0, "velo-scale": 1.0, "auto-fault-reset": True,
    "enable-timeout-ms": 2000, "watchdog-ms": 0, "stop-timeout-ms": 10000,
    "pp-timeout-ms": 60000, "pp-immediate": False, "home-timeout-ms": 60000,
    "velocity-window-ms": 10,
}


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
                            out["%s.%s" % (name, p)] = ("bool", way)
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


def parameters(cfg):
    """{name: (HAL type, default)} of the parameters of the slaves, their axes
    and encoder channels that the configuration can set (`linuxcnc.params`):
    not the master's, and not the scale of a drive that a joint sets."""
    from . import integrate
    scaled = {"%s.pos-scale" % j.get("slave") for j in integrate.joints(cfg)}
    out = {}
    for pin, (htype, d) in pins(cfg).items():
        if d == "rw" and not pin.startswith("ecat.") and pin not in scaled:
            out[pin] = (htype, DEFAULTS.get(pin.rsplit(".", 1)[1]))
    return out


def encoder_scales(cfg):
    """The `pos-scale` parameters of the encoder channels."""
    return [p for p in parameters(cfg) if p.endswith(".pos-scale") and
            any(p == "%s.%s.pos-scale" % (s.get("name"), ch.get("name"))
                for s in (cfg or {}).get("slaves") or [] for ch in s.get("encoders") or [])]
