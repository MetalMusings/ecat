"""Measuring a CSP drive's response: a test move recorded every servo cycle,
and what the following error is made of.

This is part 1 of the tuning idea (docs/ecatsetup.md "Measuring a drive's
response"): it measures and changes nothing in the drive. It runs in the
drive test (drivetest.DriveTest, prepared, in CSP) while the workbench
(testmode.TestRun) runs, so every safety layer of the drive test holds:
hold-to-enable, the heartbeat, the drive's travel, following-error and torque
limits, and the limit3 in front of pos-cmd.

- The test's HAL file has a sampler channel per CSP drive (sampler_hal()),
  disabled until a measurement: LinuxCNC's pos-cmd and pos-fb, whether the
  drive is in "Operation enabled", and the drive's own following error
  (0x60F4) and torque (0x6077) where they are in its process data.
- A measurement (Measurement) moves the drive back and forth through the
  limit3 at a chosen velocity and acceleration, with a dwell at each end,
  while halsampler writes every cycle to a file. The window steps it from a
  timer; the drive moves only while the button is held.
- analyse() splits LinuxCNC's following error (pos-cmd - pos-fb) into
  T * v + K * a + c over the samples in motion: T is the lag in time, made of
  the bus (the frames: the turn-around, about two cycles), what the drive
  adds before its loop (a command filter: its own target, pos-fb + 0x60F4,
  is behind pos-cmd by more than the bus), and its position loop's lag
  (0x60F4 per velocity: 1 / its effective position gain, when it has no or
  too little velocity feedforward); K the part that follows the
  acceleration. Without 0x60F4 the drive's part is not split. diagnose()
  says what each part points at in the drive (KINDS). Also: overshoot and settling at each
  stop, torque (servo drives), the margin to the drive's following-error
  window (stepper drives lose steps there), and FERROR / MIN_FERROR for
  LinuxCNC from the result.
"""

import json
import math
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field

from . import config, lcnc_env, quirks, testmode

HALSAMPLER = os.environ.get("ECAT_HALSAMPLER") or lcnc_env.program("halsampler")
DEPTH = 16384           # samples in each sampler channel's FIFO
SAVE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                        "ecatsetup", "responses")

# CiA 402 objects recorded when the drive has them in its process data:
# object -> (column, what)
MEASURE_OBJECTS = {0x60F4: ("drive-error", "the drive's own following error (0x60F4)"),
                   0x6077: ("torque", "the drive's torque (0x6077)")}
SAMPLER_LETTER = {"real": "f", "bool": "b", "sint": "s", "uint": "u"}

# defaults of a measurement, in machine units and seconds
DEFAULTS = {"distance": 10.0, "velocity": 20.0, "acceleration": 200.0, "repeats": 2,
            "dwell": 0.5}


# -- LinuxCNC's feedforward (cia402.feedforward, the profile's ff-* parameters)

FF_OBJECTS = {"velocity": 0x60B1, "torque": 0x60B2}
FF_PARAMS = {"velocity": "ff-velocity-gain", "torque": "ff-torque-per-accel",
             "torque-max": "ff-torque-max"}
FF_TORQUE_MAX = 30.0    # % of rated torque: the profile's default limit


def feedforward_objects(slave):
    """Which of LinuxCNC's feedforward the slave can take: {"velocity": bool,
    "torque": bool}, 0x60B1 / 0x60B2 with use: profile in its outputs for
    every axis (config.feedforward_errors asks the same)."""
    owned = set()
    for pdo in (slave.get("process-data") or {}).get("outputs") or []:
        for e in pdo.get("entries") or []:
            if e.get("use") == "profile":
                try:
                    index, sub = config.parse_obj(e.get("object"))
                except ValueError:
                    continue
                if sub == 0:
                    owned.add(index)
    n = len(config.axes(slave))
    return {k: all(i + a * config.AXIS_OFFSET in owned for a in range(n))
            for k, i in FF_OBJECTS.items()}


def feedforward_now(axis_name):
    """The profile's feedforward parameters of an axis in the running HAL:
    {"velocity", "torque", "torque-max"} (those it has), or {}."""
    try:
        params = testmode.parse_pins(testmode.halcmd("-s", "show", "param", "%s.ff-" % axis_name,
                                                     check=False))
    except (OSError, testmode.TestError):
        return {}
    out = {}
    for key, name in FF_PARAMS.items():
        p = params.get("%s.%s" % (axis_name, name))
        try:
            if p is not None:
                out[key] = float(p.value)
        except ValueError:
            pass
    return out


def feedforward_setp(axis_name, values):
    """{parameter: value text} that sets an axis's feedforward in HAL: values
    {"velocity", "torque", "torque-max"}, those given."""
    return {"%s.%s" % (axis_name, FF_PARAMS[k]): repr(float(v))
            for k, v in values.items() if k in FF_PARAMS and v is not None}


def keep_feedforward(slave, values):
    """Writes values {"velocity", "torque", "torque-max"} into the slave's
    cia402.feedforward, for what it has objects for. A velocity or torque of
    0 is left out, and so is a torque-max at the default with no torque;
    with neither left the key is removed. Returns what is in the file now
    (a dict, or None)."""
    can = feedforward_objects(slave)
    c = slave.setdefault("cia402", {})
    ff = {}
    for key in ("velocity", "torque"):
        v = values.get(key)
        if can.get(key) and v:
            ff[key] = float(v)
    tmax = values.get("torque-max")
    if "torque" in ff and tmax is not None and float(tmax) != FF_TORQUE_MAX:
        ff["torque-max"] = float(tmax)
    if ff:
        c["feedforward"] = ff
    else:
        c.pop("feedforward", None)
    return c.get("feedforward")


# -- the test's HAL

@dataclass
class Column:
    key: str        # cmd, fb, enabled, drive-error, torque
    signal: str     # the HAL signal the sampler pin is put on
    pin: str        # the pin the signal comes from (a new signal), or None (an existing one)
    letter: str     # the sampler's type letter


def measure_entries(slave, axis=0):
    """{column: (object, entry)} of MEASURE_OBJECTS in the slave's inputs
    with a pin, for the axis (its objects at index + axis * 0x800)."""
    out = {}
    off = axis * config.AXIS_OFFSET
    for pdo in (slave.get("process-data") or {}).get("inputs") or []:
        for e in pdo.get("entries") or []:
            try:
                index, sub = config.parse_obj(e.get("object"))
            except ValueError:
                continue
            col = MEASURE_OBJECTS.get(index - off)
            if index - off in quirks.measure_ignore(slave):
                continue
            if col and sub == 0 and e.get("pin") and e.get("hal-type") in SAMPLER_LETTER:
                out[col[0]] = (index, e)
    return out


def columns(cfg, i, axis_name):
    """The sampler columns of CSP drive i (testmode.csp_drives order)."""
    jog, _sel, _not = testmode.jog_names(i)
    cols = [Column("cmd", "%s-cmd" % jog, None, "f"),
            Column("fb", "%s-fb" % jog, None, "f"),
            Column("enabled", "%s-enabled" % jog, None, "b")]
    slave, axis = slave_of(cfg, axis_name)
    if slave is not None:
        for key, (_index, e) in sorted(measure_entries(slave, axis).items()):
            cols.append(Column(key, "ecatresp%d-%s" % (i, key), "%s.%s" % (slave["name"], e["pin"]),
                               SAMPLER_LETTER[e["hal-type"]]))
    return cols


def slave_of(cfg, axis_name):
    """(slave, axis index) of a cia402 axis's pin prefix."""
    for s in (cfg or {}).get("slaves") or []:
        names = config.axes(s)
        if axis_name in names:
            return s, names.index(axis_name)
    return None, 0


def sampler_hal(cfg):
    """(loadrt lines, addf lines, net lines) of the sampler channels, one per
    CSP drive, all disabled. The addf goes after the drives' limit3 (pos-cmd
    of this cycle) and before ecat.write-all."""
    drives = testmode.csp_drives(cfg)
    if not drives:
        return [], [], []
    chans = [columns(cfg, i, name) for i, name in enumerate(drives)]
    load = ["loadrt sampler depth=%s cfg=%s" % (",".join(str(DEPTH) for _ in chans),
                                                ",".join("".join(c.letter for c in cols)
                                                         for cols in chans))]
    addf = ["addf sampler.%d servo-thread" % i for i in range(len(chans))]
    nets = []
    for i, cols in enumerate(chans):
        for n, c in enumerate(cols):
            if c.pin:
                nets.append("net %s %s => sampler.%d.pin.%d" % (c.signal, c.pin, i, n))
            else:
                nets.append("net %s => sampler.%d.pin.%d" % (c.signal, i, n))
        nets.append("setp sampler.%d.enable 0" % i)
    return load, addf, nets


# -- a recording

@dataclass
class Recording:
    """Samples of one measurement: columns -> list of values, one per cycle."""
    meta: dict
    data: dict = field(default_factory=dict)
    lost: int = 0       # overruns: samples lost because the FIFO was full

    def __len__(self):
        return len(self.data.get("cmd") or [])

    def save(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        keys = list(self.data)
        with open(path, "w") as f:
            f.write("# ecatsetup response recording\n")
            f.write("# meta: %s\n" % json.dumps(dict(self.meta, lost=self.lost), sort_keys=True))
            f.write("\t".join(keys) + "\n")
            for row in zip(*(self.data[k] for k in keys)):
                f.write("\t".join(repr(v) for v in row) + "\n")
        return path

    @classmethod
    def load(cls, path):
        meta, keys, data = {}, None, {}
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("# meta: "):
                    meta = json.loads(line[8:])
                elif line.startswith("#") or not line.strip():
                    continue
                elif keys is None:
                    keys = line.split("\t")
                    data = {k: [] for k in keys}
                else:
                    for k, v in zip(keys, line.split("\t")):
                        data[k].append(float(v))
        rec = cls(meta, data, int(meta.pop("lost", 0) or 0))
        return rec


def parse_samples(text, keys):
    """halsampler -t output -> ({key: [values]}, samples lost)."""
    data = {k: [] for k in keys}
    lost, last = 0, None
    for line in text.splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "overrun":
            continue
        try:
            n = int(f[0])
            values = [float(x) for x in f[1:1 + len(keys)]]
        except ValueError:
            continue
        if len(values) != len(keys):
            continue
        if last is not None and n != last + 1:
            lost += max(n - last - 1, 0)
        last = n
        for k, v in zip(keys, values):
            data[k].append(v)
    return data, lost


# -- a measurement

def plan_targets(start, spec, clamp):
    """The targets of a measurement: out by the distance and back, repeats
    times; clamp keeps a target within the drive test's travel."""
    far = clamp(start + spec["distance"])
    return [far, start] * max(int(spec["repeats"]), 1)


def move_time(distance, velocity, acceleration):
    """Seconds a trapezoidal move of this length takes (limit3's profile)."""
    d, v, a = abs(distance), abs(velocity), abs(acceleration)
    if d == 0 or v == 0 or a == 0:
        return 0.0
    if d >= v * v / a:
        return d / v + v / a
    return 2 * math.sqrt(d / a)


class Measurement:
    """One measurement of a prepared CSP drive (drivetest.DriveTest). The
    window calls start() when the button is pressed, step() from a timer
    while it is held, and abort() when it is released before the end."""

    ENABLE_TIMEOUT = 3.0    # s for the drive to reach "Operation enabled"
    SETTLE = 0.3            # s of standstill recorded before the first move
    DRAIN_TIMEOUT = 3.0     # s for halsampler to read what is left in the FIFO

    def __init__(self, dt, spec, ini=None, clock=time.monotonic):
        if dt.mode != "csp" or not dt.prepared:
            raise testmode.TestError("only a prepared CSP drive can be measured")
        self.dt, self.run = dt, dt.run
        self.spec = dict(DEFAULTS, **(spec or {}))
        if not self.spec["distance"] or self.spec["velocity"] <= 0 or self.spec["acceleration"] <= 0:
            raise testmode.TestError("give a distance, a velocity and an acceleration")
        self.ini = ini or {}
        self.clock = clock
        self.index = self.run.drives.index(dt.name)
        self.cols = columns(self.run.cfg, self.index, dt.name)
        self.jog, self.sel, _ = testmode.jog_names(self.index)
        self.state = "idle"     # idle, enabling, settle, moving, dwell, draining, done, failed
        self.ended = None       # why it ended early, or None
        self.proc = self.out = self.path = None
        self.targets, self.step_no, self.since = [], 0, 0.0
        self.recording = None

    @property
    def active(self):
        return self.state not in ("idle", "done", "failed")

    def duration(self):
        """Seconds the measurement takes, about."""
        one = move_time(self.spec["distance"], self.spec["velocity"], self.spec["acceleration"])
        return self.SETTLE + 2 * max(int(self.spec["repeats"]), 1) * (one + self.spec["dwell"])

    def start(self):
        dt = self.dt
        self.targets = plan_targets(dt.start, self.spec, dt._clamp)
        if self.targets[0] == dt.start:
            raise testmode.TestError("the distance is outside the travel: nothing to move")
        os.makedirs(SAVE_DIR, exist_ok=True)
        self.path = os.path.join(SAVE_DIR, "%s-%s.samples" % (dt.name, time.strftime("%Y%m%d-%H%M%S")))
        self.out = open(self.path + ".raw", "w")
        self.proc = subprocess.Popen([HALSAMPLER, "-c", str(self.index), "-t"], stdout=self.out,
                                     stderr=subprocess.DEVNULL, env=lcnc_env.env())
        # back to where the drive is, then the measurement's velocity and acceleration
        dt.enable(True)
        self.run.setp_many({"%s.maxv" % self.jog: repr(float(abs(self.spec["velocity"]))),
                            "%s.maxa" % self.jog: repr(float(abs(self.spec["acceleration"]))),
                            "sampler.%d.enable" % self.index: "TRUE"})
        self.state, self.since = "enabling", self.clock()

    def _pin(self, name):
        p = self.run.pins(name).get(name)
        return p.value if p else None

    def step(self):
        """Advance the measurement; returns its state."""
        now = self.clock()
        if self.state in ("enabling", "settle", "moving", "dwell"):
            if self.proc is not None and self.proc.poll() is not None:
                return self._end("halsampler stopped")
            enabled = self.dt.pin("stat-op-enabled") == "TRUE"
            if self.state == "enabling":
                if enabled:
                    self.state, self.since = "settle", now
                elif now - self.since > self.ENABLE_TIMEOUT:
                    return self._end("the drive did not reach \"Operation enabled\"")
            elif not enabled:
                return self._end("the drive left \"Operation enabled\" (fault-reason %s)"
                                 % (self.dt.pin("fault-reason") or "?"))
            elif self.state == "settle" and now - self.since >= self.SETTLE:
                self._go(now)
            elif self.state == "moving":
                try:
                    at = float(self._pin("%s.out" % self.jog))
                except (TypeError, ValueError):
                    at = None
                if at is not None and abs(at - self.targets[self.step_no]) < 1e-9:
                    self.state, self.since = "dwell", now
            elif self.state == "dwell" and now - self.since >= self.spec["dwell"]:
                self.step_no += 1
                if self.step_no >= len(self.targets):
                    self._stop_sampling(now)
                else:
                    self._go(now)
        elif self.state == "draining":
            depth = self._pin("sampler.%d.curr-depth" % self.index)
            try:
                empty = int(depth, 0) == 0
            except (TypeError, ValueError):
                empty = True
            if empty or now - self.since > self.DRAIN_TIMEOUT:
                self._finish()
        return self.state

    def _go(self, now):
        target = self.targets[self.step_no]
        self.dt.target = target
        self.run.setp_many({"%s.in1" % self.sel: repr(target)})
        self.state, self.since = "moving", now

    def _stop_sampling(self, now):
        self.run.setp_many({"sampler.%d.enable" % self.index: "FALSE"})
        self.dt.enable(False)
        self._limit3_back()
        self.state, self.since = "draining", now

    def _limit3_back(self):
        s = self.dt.settings
        try:
            self.run.setp_many({"%s.maxv" % self.jog: repr(float(abs(s["max-velocity"]))),
                                "%s.maxa" % self.jog: repr(float(abs(s["max-acceleration"])))})
        except testmode.TestError:
            pass

    def _end(self, why):
        """Ended early: what was recorded is kept and analysed."""
        self.ended = why
        try:
            self.run.setp_many({"sampler.%d.enable" % self.index: "FALSE"})
        except testmode.TestError:
            pass
        self.dt.enable(False)
        self._limit3_back()
        self.state, self.since = "draining", self.clock()
        return self.state

    def abort(self, why="the button was released"):
        if self.active and self.state != "draining":
            self._end(why)

    def _finish(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
                try:
                    self.proc.wait(2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(2)
            self.proc = None
        if self.out is not None:
            self.out.close()
            self.out = None
        try:
            with open(self.path + ".raw") as f:
                text = f.read()
            os.remove(self.path + ".raw")
        except OSError:
            text = ""
        data, lost = parse_samples(text, [c.key for c in self.cols])
        lim = {lim.name: [lim.value, lim.original] for lim in self.dt.limits}
        meta = {"drive": self.dt.name, "slave": self.dt.slave.get("name"),
                "period-ns": self.run.period, "scale": self.dt.settings["scale"],
                "spec": self.spec, "ended": self.ended, "limits": lim,
                "turnaround-cycles": self._turnaround(), "ini": self.ini,
                "trials": self._trials(), "feedforward": self._feedforward(),
                "when": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.recording = Recording(meta, data, lost)
        if len(self.recording):
            self.recording.save(self.path)
        self.state = "done" if len(self.recording) else "failed"

    def _trials(self):
        """The settings being tried in this drive now (tuning.py): {object: [original, value]}."""
        from . import tuning
        pos = self.dt.slave.get("position")
        return {config.obj(e["index"], e["subindex"]): [e["original"], e["value"]]
                for e in tuning.pending() if e["position"] == pos}

    def _feedforward(self):
        """The profile's feedforward parameters while measuring (cia402.feedforward,
        set between measurements): {"velocity": gain, "torque": % per unit/s2},
        or {} when the drive has none."""
        now = feedforward_now(self.dt.name)
        return {k: now[k] for k in ("velocity", "torque") if k in now}

    def _turnaround(self):
        try:
            return int(self._pin("ecat.0.turnaround-cycles") or 0, 0)
        except (TypeError, ValueError):
            return 0


# -- analysis

def lstsq(rows, ys):
    """Least squares: coefficients x of rows @ x = ys (small, dense), or None."""
    n = len(rows[0]) if rows else 0
    if not rows or len(rows) < n:
        return None
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(n)] for i in range(n)]
    aty = [sum(r[i] * y for r, y in zip(rows, ys)) for i in range(n)]
    # Gauss-Jordan with partial pivoting
    m = [row[:] + [b] for row, b in zip(ata, aty)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12 * max(1.0, max(abs(x) for x in m[p][:n])):
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [a - f * b for a, b in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def _r2(rows, ys, coef):
    pred = [sum(a * b for a, b in zip(r, coef)) for r in rows]
    mean = sum(ys) / len(ys)
    ss = sum((y - mean) ** 2 for y in ys)
    res = sum((y - p) ** 2 for y, p in zip(ys, pred))
    return (1 - res / ss) if ss > 0 else 1.0, math.sqrt(res / len(ys))


def nice(x):
    """Round up to 1, 2 or 5 times a power of ten."""
    if x <= 0:
        return 0.0
    e = math.floor(math.log10(x))
    for m in (1, 2, 5, 10):
        if m * 10 ** e >= x * (1 - 1e-9):
            return m * 10 ** e
    return 10 ** (e + 1)


@dataclass
class Stop:
    at: float           # s from the start of the recording
    direction: int
    overshoot: float    # mm past the target, 0 if none
    settle: float       # s from the end of the command until within the band; None: never
    final: float        # mm, the error at the end of the dwell


@dataclass
class Result:
    samples: int
    seconds: float
    period: float               # s
    lost: int
    ended: str
    vmax: float                 # mm/s, of the command
    amax: float                 # mm/s^2
    lag: float = None           # T, s: LinuxCNC's error per velocity
    accel_term: float = None    # K, s^2
    offset: float = None        # c, mm
    fit_r2: float = None
    fit_rms: float = None
    bus: float = None           # s: the frames, pos-cmd to the drive and back (the turn-around)
    target_delay: float = None  # s: how far the drive's own target (pos-fb + 0x60F4) is behind pos-cmd
    command_delay: float = None  # s: of that, what the drive adds before its loop (a command filter)
    drive_lag: float = None     # s: the drive's position loop (0x60F4), or all the drive adds
    drive_lag_measured: bool = False
    peak: float = 0.0           # mm, LinuxCNC's error
    peak_accel: float = 0.0     # mm, while the command accelerates
    const: dict = field(default_factory=dict)    # direction -> mean error at constant speed, mm
    cruise: float = 0.0         # s, the longest stretch at constant speed
    drive_peak: float = None    # mm, 0x60F4
    band: float = 0.0           # mm, for settling
    stops: list = field(default_factory=list)
    torque_peak: float = None   # % of rated
    torque_const: dict = field(default_factory=dict)  # direction -> mean %
    torque_per_accel: float = None  # % per mm/s^2
    torque_limit: float = None  # % (the test's 0x6072)
    window: dict = field(default_factory=dict)  # "test"/"drive" -> mm (following-error window)
    ferror: dict = field(default_factory=dict)  # suggestions for LinuxCNC
    findings: list = field(default_factory=list)  # [(kind, text)]: what to look at, most first
    notes: list = field(default_factory=list)


def _diff(x, p):
    v = [0.0] * len(x)
    a = [0.0] * len(x)
    for n in range(1, len(x) - 1):
        v[n] = (x[n + 1] - x[n - 1]) / (2 * p)
        a[n] = (x[n + 1] - 2 * x[n] + x[n - 1]) / (p * p)
    return v, a


def _box(x, w):
    """x averaged over 2w+1 samples, centred (the ends as they are)."""
    n = len(x)
    if w < 1 or n < 2 * w + 1:
        return list(x)
    out = list(x)
    run = sum(x[:2 * w + 1])
    for i in range(w, n - w):
        out[i] = run / (2 * w + 1)
        if i + w + 1 < n:
            run += x[i + w + 1] - x[i - w]
    return out


def _enabled_span(rec):
    en = rec.data.get("enabled") or []
    idx = [n for n, e in enumerate(en) if e]
    return (idx[0], idx[-1] + 1) if idx else (0, 0)


def target_delay(cmd, tgt, idx, cruise, v, p):
    """How many cycles the drive's own target (pos-fb + 0x60F4) is behind
    pos-cmd: first from the mean gap at constant speed (gap / velocity), then
    the whole number of cycles around it that fits best. (cycles, rms mm)."""
    est = [(cmd[n] - tgt[n]) / v[n] for n in cruise if abs(v[n]) > 0]
    guess = int(round(sum(est) / len(est) / p)) if est else 0
    best = None
    for k in range(max(guess - 3, 0), guess + 4):
        pts = [n for n in idx if n - k >= 0]
        if not pts:
            continue
        rms = math.sqrt(sum((tgt[n] - cmd[n - k]) ** 2 for n in pts) / len(pts))
        if best is None or rms < best[1]:
            best = (k, rms)
    return best


def analyse(rec, ini=None):
    """What the recording shows. ini: {"MAX_VELOCITY", "MAX_ACCELERATION",
    "FERROR", "MIN_FERROR", "joint"} of the joint the drive is on, where known
    (else the recording's own meta "ini")."""
    meta = rec.meta
    ini = ini if ini is not None else (meta.get("ini") or {})
    p = (meta.get("period-ns") or 1000000) / 1e9
    scale = abs(float(meta.get("scale") or 1.0)) or 1.0
    start, end = _enabled_span(rec)
    cmd, fb = rec.data["cmd"], rec.data["fb"]
    err = [c - f for c, f in zip(cmd, fb)]
    v, a = _diff(cmd, p)
    res = Result(samples=end - start, seconds=(end - start) * p, period=p, lost=rec.lost,
                 ended=meta.get("ended"), vmax=0.0, amax=0.0)
    if end - start < 10:
        res.notes.append("too few samples with the drive enabled to say anything")
        return res
    lo, hi = start + 1, end - 1
    res.vmax = max(abs(v[n]) for n in range(lo, hi))
    # the acceleration the command keeps, not the odd sample where it turns
    acc_values = sorted(abs(a[n]) for n in range(lo, hi) if abs(a[n]) > 1e-9)
    res.amax = acc_values[int(0.95 * (len(acc_values) - 1))] if acc_values else 0.0
    res.band = max(3.0 / scale, 0.002)
    vtol = max(res.vmax * 0.02, 1e-6)
    moving = [False] * len(cmd)
    for n in range(lo, hi):
        moving[n] = abs(v[n]) > vtol
    idx = [n for n in range(lo, hi) if moving[n]]
    if len(idx) < 10:
        res.notes.append("the command hardly moved: no fit")
        return res
    res.peak = max(abs(err[n]) for n in range(lo, hi))
    acc = [n for n in idx if abs(a[n]) > res.amax * 0.5]
    res.peak_accel = max((abs(err[n]) for n in acc), default=0.0)
    rows = [[v[n], a[n], 1.0] for n in idx]
    ys = [err[n] for n in idx]
    coef = lstsq(rows, ys)
    if coef:
        res.lag, res.accel_term, res.offset = coef
        res.fit_r2, res.fit_rms = _r2(rows, ys, coef)
    # constant speed: the command at its velocity, not accelerating
    cruise = [n for n in idx if abs(v[n]) > res.vmax * 0.98 and abs(a[n]) < res.amax * 0.01]
    for sign in (1, -1):
        cs = [err[n] for n in cruise if sign * v[n] > 0]
        if len(cs) >= 5:
            res.const[sign] = sum(cs) / len(cs)
    if len(res.const) == 2:
        # at constant speed both ways: the lag and the offset without the fit's
        # assumptions (a drive that has not settled at the end of a short move)
        res.lag = (res.const[1] - res.const[-1]) / (2 * res.vmax)
        res.offset = (res.const[1] + res.const[-1]) / 2
    # the drive's own error: its loop's lag, and how far its target is behind pos-cmd
    turn = int(meta.get("turnaround-cycles") or 0) or 2
    res.bus = turn * p
    derr = None
    if "drive-error" in rec.data:
        derr = [x / float(meta.get("scale") or 1.0) for x in rec.data["drive-error"]]
        res.drive_peak = max(abs(derr[n]) for n in range(lo, hi))
        dconst = {}
        for sign in (1, -1):
            cs = [derr[n] for n in cruise if sign * v[n] > 0]
            if len(cs) >= 5:
                dconst[sign] = sum(cs) / len(cs)
        dc = lstsq(rows, [derr[n] for n in idx])
        if len(dconst) == 2:
            res.drive_lag, res.drive_lag_measured = (dconst[1] - dconst[-1]) / (2 * res.vmax), True
        elif dc:
            res.drive_lag, res.drive_lag_measured = dc[0], True
        tgt = [f + d for f, d in zip(fb, derr)]
        k = target_delay(cmd, tgt, idx, cruise, v, p)
        if k is not None:
            res.target_delay = k[0] * p
            res.command_delay = max(res.target_delay - res.bus, 0.0)
            if k[1] > max(res.band, 0.1 * res.peak):
                res.notes.append("the drive's target (pos-fb + 0x60F4) is not pos-cmd a whole "
                                 "number of cycles later (rms %.4f mm left): it is smoothed, not "
                                 "only delayed" % k[1])
    if res.lag is not None and not res.drive_lag_measured:
        # without 0x60F4: all the drive adds to the bus, filter and loop together
        res.drive_lag = res.lag - res.bus
    # a soft drive needs a few of its time constants at constant speed to settle
    run, longest, in_cruise = 0, 0, set(cruise)
    for n in range(lo, hi):
        run = run + 1 if n in in_cruise else 0
        longest = max(longest, run)
    res.cruise = longest * p
    if res.drive_lag and res.drive_lag > 0 and res.cruise < 5 * res.drive_lag:
        res.notes.append("constant speed lasted %.0f ms, less than five times the drive's lag "
                         "(%.0f ms): the error had not settled, so the lag is too small; measure "
                         "with a longer distance or a lower velocity"
                         % (res.cruise * 1000, res.drive_lag * 1000))
    # the stops: where the command comes to stand still (limit3 holds its target exactly)
    still = [n + 1 < len(cmd) and cmd[n + 1] == cmd[n] for n in range(len(cmd))]
    n, before = lo, cmd[lo]
    while n < hi:
        if still[n] and not still[n - 1]:
            e = n
            nxt = e
            while nxt < hi and still[nxt]:
                nxt += 1
            if nxt - e >= 5 and cmd[e] != before:
                target = cmd[e]
                direction = 1 if cmd[e] > before else -1
                before = target
                past = [direction * (fb[i] - target) for i in range(e, nxt)]
                tail = [fb[i] - target for i in range(nxt - max((nxt - e) // 5, 1), nxt)]
                # settled: within the band of where it ends up (a stepper may stay a few
                # counts off the target; that is the final error, not a failure to settle)
                rest = target + sum(tail) / len(tail)
                outside = [i for i in range(e, nxt) if abs(fb[i] - rest) > res.band]
                settle = None if outside and outside[-1] >= nxt - 1 else \
                    ((outside[-1] + 1 - e) * p if outside else 0.0)
                res.stops.append(Stop((e - start) * p, direction, max(max(past), 0.0), settle,
                                      -sum(tail) / len(tail)))
            n = nxt
        else:
            n += 1
    # torque (servo drives)
    if "torque" in rec.data:
        tq = [x / 10.0 for x in rec.data["torque"]]
        res.torque_peak = max(abs(tq[n]) for n in range(lo, hi))
        for sign in (1, -1):
            cs = [tq[n] for n in idx if sign * v[n] > res.vmax * 0.98 and abs(a[n]) < res.amax * 0.01]
            if len(cs) >= 5:
                res.torque_const[sign] = sum(cs) / len(cs)
        # torque per acceleration: against the motor's acceleration (pos-fb), which the
        # torque makes, not the command's, which a drive with lag follows late and
        # smeared (fitted against the command it came out a quarter low on a drive
        # lagging 25 ms). pos-fb is in whole counts, and noise in what a fit is made
        # against pulls the result towards 0: both are smoothed alike (two boxes of
        # 2w+1 samples) so that a count's step is at most 1 % of the acceleration
        # measured (w 5 at 1000 counts/mm, 1000 mm/s2, 1 ms; at most 50 ms)
        need = math.sqrt((1.0 / scale) / (0.01 * max(res.amax, 1e-9)))   # (2w+1) * p
        w = min(max(1, int(math.ceil((need / p - 1) / 2))), max(1, int(round(0.05 / p))))
        fbs = _box(_box(fb, w), w)
        afb = [0.0] * len(fb)
        for n in range(1, len(fb) - 1):
            afb[n] = (fbs[n + 1] - 2 * fbs[n] + fbs[n - 1]) / (p * p)
        tqs = _box(_box(tq, w), w)
        inner = [n for n in idx if lo + 2 * w <= n < hi - 2 * w]
        tc = lstsq([[afb[n], 1.0 if v[n] > 0 else -1.0, 1.0] for n in inner], [tqs[n] for n in inner])
        if tc:
            res.torque_per_accel = tc[0]
    limits = meta.get("limits") or {}
    if "max-torque" in limits and limits["max-torque"][0] is not None:
        res.torque_limit = limits["max-torque"][0] / 10.0
        if res.torque_peak is not None and res.torque_peak >= 0.95 * res.torque_limit:
            res.notes.append("the torque reached the test's limit (%g %%, 0x6072): the drive "
                             "could not accelerate as asked, so the error is larger than it "
                             "would be; raise Max torque or lower the acceleration"
                             % res.torque_limit)
    if "following-error" in limits:
        value, original = limits["following-error"]
        sc = float(meta.get("scale") or 1.0)
        if value is not None:
            res.window["test"] = abs(value / sc)
        if original is not None:
            res.window["drive"] = abs(original / sc)
    if rec.lost:
        res.notes.append("%d samples were lost (the program did not read them in time): the "
                         "times around the gap are off" % rec.lost)
    if res.ended:
        res.notes.append("ended early: %s" % res.ended)
    _suggest_ferror(res, ini)
    res.findings = diagnose(res)
    return res


# what a finding points at, for the search of the drive's settings (tuning.py)
KINDS = {
    "command-filter": "a command filter or smoothing in the drive (filter time, smoothing, "
                      "position command filter, moving average)",
    "position-loop": "the drive's position loop (position gain, Kp, integral, stiffness)",
    "feedforward": "velocity feedforward in the drive (speed feedforward, Kvff)",
    "torque": "torque feedforward or the load inertia ratio in the drive",
    "gain-high": "a gain too high, or a resonance (notch filters)",
}


def diagnose(res):
    """[(kind, text)]: what the result points at in the drive, the largest
    part of the following error first. kind is one of KINDS, or "ok"."""
    if res.lag is None:
        return []
    ms = 1000.0
    out = []
    v = res.vmax or 1.0
    if res.command_delay is not None and res.command_delay >= max(2 * res.period, 0.001):
        out.append((res.command_delay, "command-filter",
                    "The drive starts on a new position %.1f ms after it arrives (its target is "
                    "%.0f cycles behind pos-cmd, the bus accounts for %.0f): a command filter or "
                    "smoothing in the drive. At %g mm/s that alone is %.3f mm. In CSP LinuxCNC's "
                    "path is already smooth, so such a filter can mostly be small."
                    % (res.command_delay * ms, res.target_delay / res.period,
                       res.bus / res.period, v, res.command_delay * v)))
    if res.drive_lag is not None and res.drive_lag >= max(0.001, 0.25 * abs(res.lag)):
        if res.drive_lag_measured:
            out.append((res.drive_lag, "feedforward",
                        "The drive's position loop lags %.1f ms (0x60F4: %.3f mm at %g mm/s, an "
                        "effective gain of %.0f /s): velocity feedforward, or a higher position "
                        "gain. Feedforward can also come from LinuxCNC: cia402.feedforward.velocity, "
                        "if the drive has 0x60B1 (velocity offset)."
                        % (res.drive_lag * ms, res.drive_lag * v, v, 1.0 / res.drive_lag)))
        else:
            out.append((res.drive_lag, "command-filter",
                        "The drive adds %.1f ms to the bus's %.1f ms (%.3f mm at %g mm/s): a "
                        "command filter in the drive, or its position loop without feedforward. "
                        "Add 0x60F4 to the process data to tell which."
                        % (res.drive_lag * ms, res.bus * ms, res.drive_lag * v, v)))
    acc = abs(res.accel_term or 0.0) * (res.amax or 0.0)
    if acc >= max(0.25 * res.peak, 2 * res.band):
        out.append((acc / v, "torque",
                    "%.3f mm of the error follows the acceleration: torque feedforward or the "
                    "load inertia ratio in the drive%s." % (acc, "; or from LinuxCNC: "
                    "cia402.feedforward.torque: %.4g (measured here), if the drive has 0x60B2"
                    % res.torque_per_accel if res.torque_per_accel else "")))
    over = max((st.overshoot for st in res.stops), default=0.0)
    unsettled = any(st.settle is None for st in res.stops)
    if over > max(2 * res.band, 0.05 * res.peak) or (unsettled and over > res.band):
        out.append((over / v, "gain-high",
                    "At the stops the axis overshoots by up to %.3f mm%s: a gain too high or a "
                    "resonance." % (over, " and does not settle within the dwell"
                                    if unsettled else "")))
    if abs(res.offset or 0.0) >= 3 * res.band:
        out.append((abs(res.offset) / v, "position-loop",
                    "The error has a constant %+.3f mm in both directions: the drive's integral "
                    "gain, or friction it does not make up for." % res.offset))
    out.sort(key=lambda x: -x[0])
    found = [(kind, text) for _w, kind, text in out]
    if not found:
        found.append(("ok", "The drive follows within the bus's delay (%.1f ms): nothing in it "
                      "to tune for this move." % (res.bus * ms)))
    return found


def _suggest_ferror(res, ini):
    """FERROR and MIN_FERROR for LinuxCNC, twice the error the fit predicts at
    the joint's MAX_VELOCITY / MAX_ACCELERATION (or the test's)."""
    if res.lag is None:
        return
    try:
        vmax = float(ini.get("MAX_VELOCITY"))
        amax = float(ini.get("MAX_ACCELERATION"))
        source = "the joint's MAX_VELOCITY %g and MAX_ACCELERATION %g" % (vmax, amax)
    except (TypeError, ValueError):
        vmax, amax = res.vmax, res.amax
        source = "the test's velocity and acceleration (no joint in the INI file)"
    at_speed = abs(res.lag) * vmax + abs(res.accel_term or 0) * amax + abs(res.offset or 0)
    at_rest = abs(res.accel_term or 0) * amax + abs(res.offset or 0) + res.band
    res.ferror = {"FERROR": nice(2 * at_speed), "MIN_FERROR": nice(2 * at_rest),
                  "predicted": at_speed, "source": source,
                  "now": {k: ini.get(k) for k in ("FERROR", "MIN_FERROR") if ini.get(k) is not None},
                  "extrapolated": vmax > 1.5 * res.vmax or amax > 1.5 * res.amax}


def report(res, rec=None):
    """The result as lines of text."""
    ms = 1000.0
    out = []
    spec = (rec.meta.get("spec") if rec else None) or {}
    if spec:
        out.append("Move %g mm at %g mm/s, %g mm/s², %d times back and forth: %d samples, %.2f s"
                   % (spec.get("distance"), spec.get("velocity"), spec.get("acceleration"),
                      spec.get("repeats"), res.samples, res.seconds))
    if res.lag is None:
        out += res.notes
        return out
    out.append("Following error in LinuxCNC (pos-cmd - pos-fb): peak %.4f mm, %.4f mm while "
               "accelerating" % (res.peak, res.peak_accel))
    for sign in (1, -1):
        if sign in res.const:
            out.append("  at constant speed %+g mm/s: %.4f mm" % (sign * res.vmax, res.const[sign]))
    out.append("It is %.2f ms x velocity %+.5f s² x acceleration %+.4f mm (fit: %.0f %% of the "
               "variation, %.4f mm rms left)" % (res.lag * ms, res.accel_term, res.offset,
                                                   100 * res.fit_r2, res.fit_rms))
    out.append("  the bus: %d cycles of %g ms (%.2f ms), pos-cmd to the drive and back; no "
               "setting removes it" % (round(res.bus / res.period), res.period * ms, res.bus * ms))
    if res.target_delay is not None:
        out.append("  the drive's target (pos-fb + 0x60F4) is %.2f ms behind pos-cmd: %.2f ms "
                   "more than the bus, added in the drive before its loop"
                   % (res.target_delay * ms, res.command_delay * ms))
    if res.drive_lag is not None:
        if res.drive_lag_measured:
            out.append("  the drive's position loop lags %.2f ms (0x60F4)%s"
                       % (res.drive_lag * ms, ": an effective position gain of %.0f /s"
                          % (1.0 / res.drive_lag) if res.drive_lag > 0.0002 else ""))
        else:
            out.append("  the drive adds %.2f ms to the bus (a command filter and its loop "
                       "together; 0x60F4 would tell them apart)" % (res.drive_lag * ms))
    if res.drive_peak is not None:
        out.append("The drive's own following error (0x60F4): peak %.4f mm" % res.drive_peak)
    for name, text in (("test", "the test's window"), ("drive", "the drive's own window")):
        if name in res.window and res.window[name]:
            peak = res.drive_peak if res.drive_peak is not None else res.peak
            out.append("  %.0f %% of %s (%.3f mm)%s" % (100 * peak / res.window[name], text,
                                                       res.window[name],
                                                       "" if res.drive_peak is not None else
                                                       ", counting LinuxCNC's error with its delay"))
    if res.stops:
        over = max(s.overshoot for s in res.stops)
        settles = [s.settle for s in res.stops]
        out.append("At the stops (%d): overshoot up to %.4f mm, %s, error after the dwell up to "
                   "%.4f mm (band %.4f mm)" % (
                       len(res.stops), over,
                       "within the band after %.0f ms at most" % (max(settles) * ms)
                       if None not in settles else "not within the band before the next move",
                       max(abs(s.final) for s in res.stops), res.band))
    if res.torque_peak is not None:
        line = "Torque: peak %.1f %% of rated" % res.torque_peak
        if res.torque_const:
            line += ", at constant speed %s" % ", ".join(
                "%.1f %% going %s" % (res.torque_const[s], "+" if s > 0 else "-")
                for s in (1, -1) if s in res.torque_const)
        if res.torque_per_accel is not None:
            line += ", %.2f %% per 1000 mm/s²" % (res.torque_per_accel * 1000)
        out.append(line)
        if res.torque_per_accel:
            out.append("  as feedforward from LinuxCNC (0x60B2 torque offset): cia402: "
                       "{feedforward: {torque: %.4g}}" % res.torque_per_accel)
    if res.ferror:
        f = res.ferror
        out.append("For LinuxCNC, from %s: about %.4f mm at full speed, so FERROR %g and "
                   "MIN_FERROR %g (twice that)%s%s" % (
                       f["source"], f["predicted"], f["FERROR"], f["MIN_FERROR"],
                       "; now %s" % ", ".join("%s %s" % kv for kv in sorted(f["now"].items()))
                       if f["now"] else "",
                       "; extrapolated far beyond the test: measure nearer those values"
                       if f["extrapolated"] else ""))
    if res.findings:
        # first, after the line about the move: what to look at
        at = 1 if spec else 0
        out[at:at] = ["What it points at:"] + ["  - %s" % text for _kind, text in res.findings]
    out += res.notes
    return out


def compare(prev, prev_res, rec, res):
    """Lines comparing a measurement with the one before it: what was changed
    in the drive between them (the settings being tried, tuning.py) and how
    the numbers moved."""
    out = []
    ps, s = prev.meta.get("spec") or {}, rec.meta.get("spec") or {}
    same = all(ps.get(k) == s.get(k) for k in ("distance", "velocity", "acceleration"))
    pt, t = prev.meta.get("trials") or {}, rec.meta.get("trials") or {}
    changes = []
    for obj in sorted(set(pt) | set(t)):
        before = pt[obj][1] if obj in pt else t[obj][0]
        after = t[obj][1] if obj in t else pt[obj][0]
        if before != after:
            changes.append("%s %d -> %d%s" % (obj, before, after, "" if obj in t else " (put back)"))
    pf, f = prev.meta.get("feedforward") or {}, rec.meta.get("feedforward") or {}
    for key in ("velocity", "torque"):
        if key in pf and key in f and pf[key] != f[key]:
            changes.append("feedforward %s %g -> %g (cia402.feedforward)" % (key, pf[key], f[key]))
    out.append("Since the last measurement: %s" % ("; ".join(changes) if changes else
                                                   "no setting changed in the drive"))
    if not same:
        out.append("  the move is not the same (%g mm, %g mm/s, %g mm/s² before): the numbers "
                   "are not comparable" % (ps.get("distance"), ps.get("velocity"),
                                           ps.get("acceleration")))
        return out
    if prev_res.lag is None or res.lag is None:
        return out
    ms = 1000.0

    def mean_const(r):
        return sum(abs(x) for x in r.const.values()) / len(r.const) if r.const else None

    def stop(r, f):
        return max((f(st) for st in r.stops), default=None)
    rows = [("lag", prev_res.lag * ms, res.lag * ms, "ms"),
            ("error at constant speed", mean_const(prev_res), mean_const(res), "mm"),
            ("peak error", prev_res.peak, res.peak, "mm"),
            ("added before the drive's loop", None if prev_res.command_delay is None
             else prev_res.command_delay * ms, None if res.command_delay is None
             else res.command_delay * ms, "ms"),
            ("the drive's loop", None if prev_res.drive_lag is None else prev_res.drive_lag * ms,
             None if res.drive_lag is None else res.drive_lag * ms, "ms"),
            ("overshoot at the stops", stop(prev_res, lambda st: st.overshoot),
             stop(res, lambda st: st.overshoot), "mm"),
            ("error after the dwell", stop(prev_res, lambda st: abs(st.final)),
             stop(res, lambda st: abs(st.final)), "mm")]
    for name, a, b, unit in rows:
        if a is None or b is None:
            continue
        digits = 2 if unit == "ms" else 4
        change = " (%+.0f %%)" % (100.0 * (b - a) / a) if abs(a) > 1e-9 else ""
        out.append("  %s %.*f -> %.*f %s%s" % (name, digits, a, digits, b, unit, change))
    return out


def ini_values(ini_path, cfg, axis_name):
    """{"joint", "MAX_VELOCITY", ...} of the joint the axis is connected to,
    from the INI file; {} if none. The joint is the configuration's
    (linuxcnc.joints), else the one the HAL files connect to the axis's
    pos-cmd (a configuration written by hand, not by Apply)."""
    from . import integrate
    if not ini_path:
        return {}
    joint = next((j for j in integrate.joints(cfg) if j.get("slave") == axis_name), None)
    if joint is None:
        try:
            found = integrate.propose_joints(ini_path, cfg)
        except (OSError, ValueError):
            found = {}
        n = next((j for j, (name, _scale) in sorted(found.items()) if name == axis_name), None)
        joint = {"joint": n} if n is not None else None
    if joint is None:
        return {}
    try:
        e = integrate.read_ini(ini_path)
    except OSError:
        return {}
    sec = "JOINT_%d" % joint["joint"]
    out = {"joint": joint["joint"]}
    for key in ("MAX_VELOCITY", "MAX_ACCELERATION", "FERROR", "MIN_FERROR"):
        try:
            out[key] = float(integrate.resolve(integrate.ini_get(e, sec, key), e))
        except (TypeError, ValueError):
            pass
    return out


# -- what to add to the process data

def missing_objects(slave, dictionary, axis=0):
    """[(object, column, what)] of MEASURE_OBJECTS the drive has in its
    dictionary but not in its process data."""
    have = measure_entries(slave, axis)
    off = axis * config.AXIS_OFFSET
    out = []
    ignored = quirks.measure_ignore(slave)
    for index, (col, what) in sorted(MEASURE_OBJECTS.items()):
        if index in ignored:
            continue
        if col not in have and dictionary is not None and (index + off, 0) in dictionary.entries:
            out.append((index + off, col, what))
    return out


def add_objects(slave, objects, dictionary, fixed_pdos=()):
    """Add the objects to the slave's first input PDO (the drive must map
    them: its PDO must not be fixed). Returns the entries added; raises
    ValueError with the reason when it cannot."""
    from . import propose
    inputs = (slave.get("process-data") or {}).get("inputs") or []
    if not inputs:
        raise ValueError("the drive has no input PDO in the configuration")
    pdo = inputs[0]
    try:
        pdo_index = int(str(pdo.get("pdo")), 16)
    except ValueError:
        pdo_index = None
    if pdo_index in set(fixed_pdos):
        raise ValueError("PDO %s is fixed in the drive's ESI file: choose a PDO assignment with "
                         "these objects instead (Process data)" % pdo.get("pdo"))
    added = []
    for index in objects:
        e = dictionary.entries.get((index, 0))
        if e is None:
            raise ValueError("0x%04X is not in the drive's dictionary" % index)
        ctype = propose.COE_TYPES.get(e.data_type)
        if ctype is None:
            raise ValueError("0x%04X has a type this program cannot map (%s)" % (index, e.data_type))
        # axis n's objects are at index + n * 0x800 (0x6000..0x9FFF)
        axis = (index - 0x6000) // config.AXIS_OFFSET if 0x6000 <= index < 0xA000 else 0
        base = index - axis * config.AXIS_OFFSET
        name = propose.CIA402_PINS.get(base, (MEASURE_OBJECTS[base][0],))[0]
        pin = name if axis == 0 else "%s-%d" % (name, axis)
        entry = {"object": config.obj(index, 0), "bits": e.bitlen, "type": ctype,
                 "pin": pin, "hal-type": propose.HAL_TYPES.get(ctype, "sint")}
        pdo.setdefault("entries", []).append(entry)
        added.append(entry)
    return added
