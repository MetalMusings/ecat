"""The real-time check, step 1 (docs/development.md "Real-time check: where
jitter comes from"): what the computer is (the inventory), how its real-time
threads keep time (a Look measurement), the period's budget, and numbers that
are honest about how long they were measured.

Nothing here needs root or changes the computer. Everything read from /proc
and /sys goes through Roots, so that tests point it at a fake tree.

Two ways to measure:

- threads only: halrun with the INI's threads at their periods, a timedelta
  first in each and a sampler channel recording it every cycle; no I/O;
- attach: the same timedelta and sampler added to a LinuxCNC (or halrun)
  that runs, first in its threads, and taken out again at the end. It loads
  nothing that talks to the user's hardware, so it works for any I/O card.

halsampler's output is read as it comes and kept as a histogram (1 us bins),
so that a fast base thread over a long run takes little memory.
"""

import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time

from . import lcnc_env

# names of what the check adds to a HAL; the prefix tells them from the user's
PREFIX = "ecatsetup-rt"
SAMPLER_DEPTH = 16384

# spike thresholds, us: counted over the run (cycle-to-cycle error, either way)
THRESHOLDS_US = (10, 20, 50, 100, 200, 500)

STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                         "ecatsetup", "rt-runs")


class RtError(Exception):
    pass


# --------------------------------------------------------------------------
# reading the computer

class Roots:
    """Where /proc and /sys are, and how a program is run (tests replace all
    three)."""

    def __init__(self, proc="/proc", sys="/sys", run=None):
        self.proc = proc
        self.sys = sys
        self._run = run

    def read(self, base, *parts):
        try:
            with open(os.path.join(base, *parts), errors="replace") as f:
                return f.read().strip()
        except OSError:
            return None

    def p(self, *parts):
        return self.read(self.proc, *parts)

    def s(self, *parts):
        return self.read(self.sys, *parts)

    def ls(self, base, *parts):
        try:
            return sorted(os.listdir(os.path.join(base, *parts)))
        except OSError:
            return []

    def link(self, base, *parts):
        path = os.path.join(base, *parts)
        return os.path.basename(os.path.realpath(path)) if os.path.lexists(path) else None

    def run(self, argv):
        """A program's output, or None when it is missing or fails."""
        if self._run is not None:
            return self._run(argv)
        prog = shutil.which(argv[0]) or shutil.which(argv[0], path="/usr/sbin:/sbin")
        if prog is None:
            return None
        try:
            r = subprocess.run([prog] + list(argv[1:]), capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None


def cpu_list(text):
    """"0-2,5" -> [0, 1, 2, 5]; "" or None -> []."""
    out = []
    for part in (text or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                out.extend(range(int(a), int(b) + 1))
            except ValueError:
                pass
        else:
            try:
                out.append(int(part))
            except ValueError:
                pass
    return sorted(set(out))


def list_text(cpus):
    """[0, 1, 2, 5] -> "0-2,5"."""
    cpus = sorted(set(cpus))
    out, i = [], 0
    while i < len(cpus):
        j = i
        while j + 1 < len(cpus) and cpus[j + 1] == cpus[j] + 1:
            j += 1
        out.append(str(cpus[i]) if i == j else "%d-%d" % (cpus[i], cpus[j]))
        i = j + 1
    return ",".join(out)


def cmdline_params(cmdline):
    """The kernel command line as {name: value} (value None for a bare flag)."""
    out = {}
    for tok in (cmdline or "").split():
        k, _, v = tok.partition("=")
        out[k] = v if _ else None
    return out


def parse_interrupts(text):
    """/proc/interrupts -> (cpus, [{irq, counts, chip, name}]). counts has a
    number per CPU column; chip and name are what the kernel prints after them
    (the name is the driver's or the device's)."""
    lines = (text or "").splitlines()
    if not lines:
        return [], []
    cpus = [int(c[3:]) for c in lines[0].split() if c.startswith("CPU")]
    rows = []
    for line in lines[1:]:
        head, _, rest = line.partition(":")
        f = rest.split()
        counts = []
        for x in f[:len(cpus)]:
            if not x.isdigit():
                break
            counts.append(int(x))
        tail = f[len(counts):]
        irq = head.strip()
        if len(counts) < len(cpus):
            # ERR:, MIS: have one number
            rows.append({"irq": irq, "counts": counts, "chip": "", "name": " ".join(tail)})
            continue
        if irq.isdigit() and len(tail) >= 2:
            # chip, hw irq and type, then the names (may contain spaces and commas)
            chip = tail[0]
            name = " ".join(tail[2:]) if len(tail) > 2 else tail[-1]
        else:
            chip, name = "", " ".join(tail)
        rows.append({"irq": irq, "counts": counts, "chip": chip, "name": name})
    return cpus, rows


def processes(roots, names):
    """{name: [pid]} of running processes whose comm is one of names."""
    out = {}
    for pid in roots.ls(roots.proc):
        if not pid.isdigit():
            continue
        comm = roots.p(pid, "comm")
        if comm in names:
            out.setdefault(comm, []).append(int(pid))
    return out


def task_stat(text):
    """/proc/<pid>/task/<tid>/stat -> (comm, last cpu, rt priority, policy)."""
    if not text or ")" not in text:
        return None
    comm = text[text.index("(") + 1:text.rindex(")")]
    f = text[text.rindex(")") + 2:].split()
    # f[0] is field 3 (state); processor is field 39, rt_priority 40, policy 41
    try:
        return comm, int(f[36]), int(f[37]), int(f[38])
    except (IndexError, ValueError):
        return None


def rt_threads(roots):
    """[(tid, name, cpu, priority)] of rtapi_app's real-time (SCHED_FIFO or RR)
    threads: where LinuxCNC's threads run now."""
    out = []
    for pid in processes(roots, ("rtapi_app",)).get("rtapi_app", []):
        for tid in roots.ls(roots.proc, str(pid), "task"):
            st = task_stat(roots.p(str(pid), "task", tid, "stat"))
            if st and st[3] in (1, 2):
                out.append((int(tid), st[0], st[1], st[2]))
    return out


class Fact:
    def __init__(self, key, label, value, source):
        self.key, self.label, self.value, self.source = key, label, value, source

    def __repr__(self):
        return "Fact(%s=%r)" % (self.key, self.value)


class Inventory:
    """What the computer is: facts to show (each with where it was read) and
    the data behind them, for the analysis."""

    def __init__(self):
        self.facts = []
        self.data = {}

    def add(self, key, label, value, source, data=None):
        self.facts.append(Fact(key, label, value, source))
        self.data[key] = value if data is None else data

    def fact(self, key):
        for f in self.facts:
            if f.key == key:
                return f
        return None

    def to_json(self):
        return {"facts": [[f.key, f.label, f.value, f.source] for f in self.facts], "data": self.data}

    @classmethod
    def from_json(cls, d):
        inv = cls()
        inv.facts = [Fact(*f) for f in d.get("facts", [])]
        inv.data = d.get("data", {})
        return inv


IO_MODULES = (
    ("hm2_eth", "Mesa Ethernet (hm2_eth)"),
    ("hm2_pci", "Mesa PCI (hm2_pci)"),
    ("hm2_spi", "Mesa SPI (hm2_spi)"),
    ("hm2_rpspi", "Mesa SPI on a Raspberry Pi (hm2_rpspi)"),
    ("hm2_spix", "Mesa SPI (hm2_spix)"),
    ("ecat", "EtherCAT (ecat)"),
    ("lcec", "EtherCAT (lcec)"),
    ("hal_parport", "parallel port (hal_parport)"),
    ("hal_ppmc", "Pico Systems (hal_ppmc)"),
    ("hostmot2", None),
)

VM_VENDORS = ("qemu", "vmware", "virtualbox", "innotek", "xen", "microsoft corporation",
              "parallels", "bochs", "kvm")


def ini_threads(ini):
    """[(name, period ns)] of the INI's threads, fastest first, or []."""
    from . import session
    try:
        servo = int(float(session.ini_value(ini, "EMCMOT", "SERVO_PERIOD")))
    except (OSError, TypeError, ValueError):
        return []
    out = [("servo-thread", servo)]
    try:
        base = int(float(session.ini_value(ini, "EMCMOT", "BASE_PERIOD") or 0))
    except (OSError, TypeError, ValueError):
        base = 0
    if 0 < base < servo:
        out.insert(0, ("base-thread", base))
    return out


def io_kinds(ini):
    """What the configuration's I/O is, from the modules its HAL files load."""
    from . import integrate
    mods = set()
    try:
        files = integrate.user_hal_files(ini)
    except OSError:
        files = []
    d = os.path.dirname(os.path.abspath(ini))
    for name in ("ethercat-load.hal",):
        p = os.path.join(d, name)
        if os.path.exists(p):
            files.append(p)
    for path in files:
        try:
            text = integrate.read_hal(path, ini)
        except Exception:      # a .tcl file that cannot be expanded: not this check's business
            continue
        for _, _, tok in integrate.statements(text):
            if tok[0] == "loadrt" and len(tok) > 1:
                mods.add(tok[1])
    kinds = [label for mod, label in IO_MODULES if label and mod in mods]
    return kinds or (["none found (a simulated configuration?)"] if files else [])


def inventory(roots=None, ini=None):
    """The inventory: facts, each with where it was read. Nothing is judged
    here; a fact gets a verdict only when a measurement shows it matters."""
    r = roots or Roots()
    inv = Inventory()
    P, S = r.proc, r.sys

    rel = r.p("sys", "kernel", "osrelease")
    version = r.p("version") or ""
    inv.add("kernel", "Kernel", rel or "?", "/proc/sys/kernel/osrelease")
    rt = r.s("kernel", "realtime") == "1" or "PREEMPT_RT" in version
    inv.add("preempt_rt", "PREEMPT_RT", "yes" if rt else "no", "/sys/kernel/realtime, /proc/version",
            data=rt)
    cmd = r.p("cmdline") or ""
    inv.add("cmdline", "Kernel command line", cmd, "/proc/cmdline")
    params = cmdline_params(cmd)

    # CPUs
    model = None
    flags = ""
    for line in (r.p("cpuinfo") or "").splitlines():
        k, _, v = line.partition(":")
        if k.strip() == "model name" and model is None:
            model = v.strip()
        if k.strip() == "flags" and not flags:
            flags = v
    inv.add("cpu_model", "CPU", model or "?", "/proc/cpuinfo")
    online = cpu_list(r.s("devices", "system", "cpu", "online"))
    inv.add("cpus", "CPUs online", list_text(online), "/sys/devices/system/cpu/online", data=online)
    cores = {}
    for c in online:
        sib = cpu_list(r.s("devices", "system", "cpu", "cpu%d" % c, "topology", "thread_siblings_list"))
        cores[c] = sib or [c]
    smt = any(len(v) > 1 for v in cores.values())
    groups = sorted({tuple(v) for v in cores.values()})
    inv.add("smt", "SMT (hyperthreading)",
            ("on: " + "  ".join("[%s]" % list_text(g) for g in groups if len(g) > 1)) if smt else "off",
            "/sys/devices/system/cpu/cpu*/topology/thread_siblings_list",
            data={str(k): v for k, v in cores.items()})

    isolated = cpu_list(r.s("devices", "system", "cpu", "isolated"))
    inv.add("isolated", "Isolated CPUs (isolcpus)", list_text(isolated) or "none",
            "/sys/devices/system/cpu/isolated", data=isolated)
    iso_flags = (params.get("isolcpus") or "")
    inv.add("isolcpus_flags", "isolcpus flags",
            ",".join(x for x in iso_flags.split(",") if x and not x[0].isdigit()) or "none",
            "/proc/cmdline", data=[x for x in iso_flags.split(",") if x and not x[0].isdigit()])
    nohz = cpu_list(r.s("devices", "system", "cpu", "nohz_full"))
    inv.add("nohz_full", "nohz_full CPUs", list_text(nohz) or "none",
            "/sys/devices/system/cpu/nohz_full", data=nohz)
    nocbs = cpu_list(params.get("rcu_nocbs"))
    inv.add("rcu_nocbs", "rcu_nocbs CPUs", list_text(nocbs) or "none", "/proc/cmdline", data=nocbs)
    irqaff = r.p("irq", "default_smp_affinity")
    inv.add("irqaffinity", "Default IRQ affinity", params.get("irqaffinity") or "not set",
            "/proc/cmdline", data=cpu_list(params.get("irqaffinity")))

    # frequency
    govs = {}
    for c in online:
        g = r.s("devices", "system", "cpu", "cpu%d" % c, "cpufreq", "scaling_governor")
        if g:
            govs[c] = g
    gv = sorted(set(govs.values()))
    inv.add("governor", "Frequency governor",
            ("/".join(gv) if len(gv) <= 1 else ", ".join("cpu%d %s" % kv for kv in sorted(govs.items())))
            or "no cpufreq", "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
            data={str(k): v for k, v in govs.items()})
    nt = r.s("devices", "system", "cpu", "intel_pstate", "no_turbo")
    boost = r.s("devices", "system", "cpu", "cpufreq", "boost")
    if nt is not None:
        inv.add("turbo", "Turbo", "off" if nt == "1" else "on",
                "/sys/devices/system/cpu/intel_pstate/no_turbo", data=nt != "1")
    elif boost is not None:
        inv.add("turbo", "Boost", "on" if boost == "1" else "off",
                "/sys/devices/system/cpu/cpufreq/boost", data=boost == "1")
    else:
        inv.add("turbo", "Turbo/boost", "not known", "no intel_pstate/no_turbo or cpufreq/boost",
                data=None)
    pstate = r.s("devices", "system", "cpu", "intel_pstate", "status") or \
        r.s("devices", "system", "cpu", "amd_pstate", "status")
    if pstate:
        inv.add("pstate", "P-state driver status", pstate, "/sys/devices/system/cpu/*_pstate/status")

    # idle states
    drv = r.s("devices", "system", "cpu", "cpuidle", "current_driver")
    idle = {}
    for c in online:
        states = []
        base = ("devices", "system", "cpu", "cpu%d" % c, "cpuidle")
        for st in r.ls(S, *base):
            if not st.startswith("state"):
                continue
            def g(n):
                return r.s(*(base + (st, n)))
            try:
                states.append({"state": st, "name": g("name"), "latency_us": int(g("latency") or 0),
                               "disabled": g("disable") == "1", "usage": int(g("usage") or 0),
                               "time_us": int(g("time") or 0)})
            except ValueError:
                pass
        idle[str(c)] = sorted(states, key=lambda x: int(x["state"][5:] or 0))
    deepest = {}
    for c, states in idle.items():
        on = [x for x in states if not x["disabled"]]
        if on:
            deepest[c] = max(on, key=lambda x: x["latency_us"])
    if deepest:
        txt = ", ".join(sorted({"%s (%d us)" % (x["name"], x["latency_us"]) for x in deepest.values()}))
    else:
        txt = "none"
    inv.add("cpuidle", "Idle driver, deepest enabled state", "%s; %s" % (drv or "none", txt),
            "/sys/devices/system/cpu/cpu*/cpuidle/state*/{name,latency,disable}",
            data={"driver": drv, "states": idle,
                  "max_cstate": {k: v for k, v in params.items()
                                 if k in ("intel_idle.max_cstate", "processor.max_cstate", "idle")}})

    # thermal throttling
    thr = {}
    for c in online:
        v = r.s("devices", "system", "cpu", "cpu%d" % c, "thermal_throttle", "core_throttle_count")
        if v is not None and v.isdigit():
            thr[str(c)] = int(v)
    if thr:
        inv.add("throttle", "Thermal throttle events since boot", str(sum(thr.values())),
                "/sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count", data=thr)

    # interrupts
    cols, rows = parse_interrupts(r.p("interrupts"))
    irqs = []
    for row in rows:
        if row["irq"].isdigit():
            aff = r.p("irq", row["irq"], "effective_affinity_list") or \
                r.p("irq", row["irq"], "smp_affinity_list")
            row["affinity"] = cpu_list(aff)
        irqs.append(row)
    inv.add("interrupts", "Interrupt lines", "%d (%d numbered)" % (len(irqs), sum(
        1 for x in irqs if x["irq"].isdigit())), "/proc/interrupts, /proc/irq/*/effective_affinity_list",
        data={"cpus": cols, "rows": irqs})
    procs = processes(r, ("irqbalance", "rtapi_app", "linuxcnc", "milltask", "NetworkManager"))
    inv.add("irqbalance", "irqbalance", "running" if "irqbalance" in procs else "not running",
            "/proc/*/comm", data="irqbalance" in procs)
    inv.add("networkmanager", "NetworkManager", "running" if "NetworkManager" in procs else "not running",
            "/proc/*/comm", data="NetworkManager" in procs)

    # network cards
    nics = []
    for name in r.ls(S, "class", "net"):
        if name == "lo" or not os.path.exists(os.path.join(S, "class", "net", name, "device")):
            continue
        nic = {"name": name, "driver": r.link(S, "class", "net", name, "device", "driver"),
               "operstate": r.s("class", "net", name, "operstate"),
               "speed": r.s("class", "net", name, "speed"),
               "pci": r.link(S, "class", "net", name, "device")}
        aspm = {}
        for k in ("l0s_aspm", "l1_aspm", "l1_1_aspm", "l1_2_aspm"):
            v = r.s("class", "net", name, "device", "link", k)
            if v is not None:
                aspm[k] = v == "1"
        nic["aspm"] = aspm
        nic["irqs"] = [x["irq"] for x in irqs if x["irq"].isdigit() and name in x["name"]]
        coal = r.run(["ethtool", "-c", name])
        if coal:
            m = re.search(r"^rx-usecs:\s*(\S+)", coal, re.M)
            nic["rx_usecs"] = m.group(1) if m else None
        eee = r.run(["ethtool", "--show-eee", name])
        if eee:
            m = re.search(r"EEE status:\s*(.+)", eee)
            nic["eee"] = m.group(1).strip() if m else None
        nics.append(nic)

    def nic_text(n):
        bits = ["%s %s" % (n["name"], n["driver"] or "?")]
        if n["operstate"] == "up" and n["speed"] and n["speed"] != "-1":
            bits.append("up %s Mb/s" % n["speed"])
        else:
            bits.append(n["operstate"] or "?")
        if n.get("rx_usecs") is not None:
            bits.append("rx-usecs %s" % n["rx_usecs"])
        if n.get("eee"):
            bits.append("EEE %s" % n["eee"])
        if any(n["aspm"].values()):
            bits.append("ASPM " + ",".join(k[:-5] for k, v in n["aspm"].items() if v))
        return " ".join(bits)
    inv.add("nics", "Network cards", "; ".join(nic_text(n) for n in nics) or "none",
            "/sys/class/net, ethtool -c / --show-eee", data=nics)
    inv.add("aspm_policy", "PCIe ASPM policy",
            (re.search(r"\[(\w+)\]", r.s("module", "pcie_aspm", "parameters", "policy") or "") or
             [None, "not known"])[1], "/sys/module/pcie_aspm/parameters/policy")

    gpus = []
    for card in r.ls(S, "class", "drm"):
        if re.fullmatch(r"card\d+", card):
            gpus.append(r.link(S, "class", "drm", card, "device", "driver") or "?")
    inv.add("graphics", "Graphics driver", ", ".join(gpus) or "none", "/sys/class/drm/card*/device/driver",
            data=gpus)

    dmi = {k: r.s("class", "dmi", "id", k) for k in
           ("sys_vendor", "product_name", "board_vendor", "board_name", "bios_vendor", "bios_version",
            "bios_date")}
    inv.add("board", "Computer", " ".join(x for x in (dmi["sys_vendor"], dmi["product_name"]) if x) or
            " ".join(x for x in (dmi["board_vendor"], dmi["board_name"]) if x) or "not given by the BIOS",
            "/sys/class/dmi/id")
    inv.add("bios", "BIOS", " ".join(x for x in (dmi["bios_vendor"], dmi["bios_version"], dmi["bios_date"])
                                     if x) or "?", "/sys/class/dmi/id", data=dmi)
    hyp = r.s("hypervisor", "type")
    vm = bool(hyp) or " hypervisor" in " " + flags + " " or \
        any(v in (dmi["sys_vendor"] or "").lower() for v in VM_VENDORS)
    inv.add("vm", "Virtual machine", ("yes" + (" (%s)" % hyp if hyp else "")) if vm else "no",
            "cpuinfo flags, /sys/hypervisor, /sys/class/dmi/id", data=vm)

    battery = None
    for ps in r.ls(S, "class", "power_supply"):
        if r.s("class", "power_supply", ps, "type") == "Mains":
            battery = r.s("class", "power_supply", ps, "online") == "0"
    if battery is not None:
        inv.add("battery", "On battery", "yes" if battery else "no", "/sys/class/power_supply/*/online",
                data=battery)

    if ini:
        th = ini_threads(ini)
        inv.add("threads", "LinuxCNC threads (INI)",
                ", ".join("%s %s" % (n, period_text(p)) for n, p in th) or "no SERVO_PERIOD",
                "[EMCMOT] of %s" % ini, data=th)
        io = io_kinds(ini)
        inv.add("io", "I/O", ", ".join(io) or "?", "loadrt lines of the INI's HAL files", data=io)
    rts = rt_threads(r)
    if rts:
        inv.add("rt_cpu", "Real-time threads run on",
                ", ".join("%s on CPU %d (prio %d)" % (n, c, p) for _, n, c, p in rts),
                "/proc/<rtapi_app>/task/*/stat", data=sorted({c for _, _, c, _ in rts}))
    else:
        inv.add("rt_cpu", "Real-time threads run on", "LinuxCNC is not running",
                "/proc/*/comm", data=[])
    return inv


def period_text(ns):
    if ns >= 1000000 and ns % 1000000 == 0:
        return "%d ms" % (ns // 1000000)
    if ns >= 1000 and ns % 1000 == 0:
        return "%d us" % (ns // 1000)
    return "%d ns" % ns


def inventory_text(inv):
    w = max([len(f.label) for f in inv.facts] + [10])
    return "\n".join("%-*s  %s   [%s]" % (w, f.label, f.value, f.source) for f in inv.facts)


# --------------------------------------------------------------------------
# statistics

def poisson_upper(k, conf=0.95):
    """The upper bound of a Poisson mean with k events seen (one-sided,
    conf): 0 -> 3.0 at 95 %. Bisection on the cumulative distribution."""
    def cdf(lam):
        term = math.exp(-lam)
        s = term
        for i in range(1, k + 1):
            term *= lam / i
            s += term
        return s
    lo, hi = 0.0, max(10.0, 4.0 * (k + 1) + 10)
    for _ in range(100):
        mid = (lo + hi) / 2
        if cdf(mid) > 1 - conf:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def rate_bound(k, seconds, conf=0.95):
    """Events per hour that k events in `seconds` rule out (upper bound)."""
    return poisson_upper(k, conf) * 3600.0 / seconds if seconds > 0 else float("inf")


def seconds_needed(per_hour, conf=0.95):
    """How long a run with no event must be to claim fewer than per_hour."""
    return poisson_upper(0, conf) * 3600.0 / per_hour


class CycleStats:
    """Every cycle's time since the one before (timedelta.out), kept as a
    histogram of the error against the period in 1 us bins, with the worst
    cycles and the counts over thresholds."""

    WORST = 20

    def __init__(self, thread, period):
        self.thread = thread
        self.period = period
        self.n = 0
        self.hist = {}          # error in whole us (rounded toward -inf) -> count
        self.min = None
        self.max = None
        self.sum = 0
        self.worst = []         # [(abs error ns, error ns, sample index)]
        self.over = {t: 0 for t in THRESHOLDS_US}
        self.overruns = 0       # halsampler's "overrun" lines: samples lost
        self.started = None
        self.ended = None

    def add(self, delta):
        """One cycle: delta = ns since the previous run (0 = first, skipped)."""
        if delta <= 0:
            return
        e = delta - self.period
        idx = self.n
        self.n += 1
        b = e // 1000
        self.hist[b] = self.hist.get(b, 0) + 1
        if self.min is None or e < self.min:
            self.min = e
        if self.max is None or e > self.max:
            self.max = e
        self.sum += e
        a = abs(e)
        for t in THRESHOLDS_US:
            if a > t * 1000:
                self.over[t] += 1
            else:
                break
        if len(self.worst) < self.WORST or a > self.worst[-1][0]:
            self.worst.append((a, e, idx))
            self.worst.sort(key=lambda x: -x[0])
            del self.worst[self.WORST:]

    def feed(self, text):
        """halsampler's lines (a value, or "<n> <value>" with -t)."""
        for line in text.splitlines():
            f = line.split()
            if not f:
                continue
            if f[0].startswith("overrun"):
                self.overruns += 1
                continue
            try:
                self.add(int(f[-1]))
            except ValueError:
                pass

    @property
    def seconds(self):
        return self.n * self.period / 1e9

    def abs_percentile(self, q):
        """The |error| (us) that q of the cycles stay within (from the
        histogram: an upper bound good to 1 us)."""
        if not self.n:
            return None
        ab = {}
        for b, c in self.hist.items():
            k = b + 1 if b >= 0 else -b       # |e| < k us
            ab[k] = ab.get(k, 0) + c
        need = q * self.n
        s = 0
        for k in sorted(ab):
            s += ab[k]
            if s >= need:
                return k
        return max(ab)

    def summary(self):
        if not self.n:
            return {"thread": self.thread, "period": self.period, "cycles": 0}
        hours = self.seconds / 3600.0
        return {
            "thread": self.thread, "period": self.period, "cycles": self.n, "seconds": self.seconds,
            "min_us": self.min / 1000.0, "max_us": self.max / 1000.0,
            "mean_us": self.sum / self.n / 1000.0,
            "p99_us": self.abs_percentile(0.99), "p9999_us": self.abs_percentile(0.9999),
            "max_abs_us": max(abs(self.min), abs(self.max)) / 1000.0,
            "over": {str(t): c for t, c in self.over.items()},
            "over_per_hour": {str(t): c / hours for t, c in self.over.items()},
            "overruns": self.overruns,
        }

    def to_json(self):
        return {"thread": self.thread, "period": self.period, "n": self.n,
                "hist": {str(k): v for k, v in sorted(self.hist.items())},
                "min": self.min, "max": self.max, "sum": self.sum,
                "worst": self.worst, "over": {str(k): v for k, v in self.over.items()},
                "overruns": self.overruns}

    @classmethod
    def from_json(cls, d):
        s = cls(d["thread"], d["period"])
        s.n, s.min, s.max, s.sum, s.overruns = d["n"], d["min"], d["max"], d["sum"], d.get("overruns", 0)
        s.hist = {int(k): v for k, v in d["hist"].items()}
        s.worst = [tuple(x) for x in d.get("worst", [])]
        s.over = {int(k): v for k, v in d["over"].items()}
        return s


def claim_text(stats, threshold_us):
    """What a run says about cycles over threshold_us, honestly bounded."""
    k = stats.over.get(threshold_us, 0)
    secs = stats.seconds
    if secs <= 0:
        return "not measured"
    ub = rate_bound(k, secs)
    if k == 0:
        return ("none over %d us in %s: fewer than %s per hour (95 %%)"
                % (threshold_us, duration_text(secs), number_text(ub)))
    return ("%d over %d us in %s: %s per hour, at most %s (95 %%)"
            % (k, threshold_us, duration_text(secs), number_text(k * 3600.0 / secs), number_text(ub)))


def duration_text(s):
    if s >= 3600:
        return "%.1f h" % (s / 3600)
    if s >= 120:
        return "%.0f min" % (s / 60)
    return "%.0f s" % s


def number_text(x):
    if x >= 100:
        return "%.0f" % x
    if x >= 10:
        return "%.0f" % x
    return "%.1f" % x


# comparing two runs: the metrics, and how much they may differ by chance
COMPARED = ("p99_us", "p9999_us", "max_abs_us")


def noise(a, b):
    """{metric: |a - b|} of two summaries of the same conditions (the two
    baselines): how much a metric moves without a change."""
    return {m: abs((a.get(m) or 0) - (b.get(m) or 0)) for m in COMPARED}


def compare(before, after, noise_=None, floor_us=1.0):
    """{metric: (before, after, verdict)}: "better" or "worse" only when the
    difference is more than the noise (and floor_us, the histogram's
    resolution); else "no change"."""
    out = {}
    for m in COMPARED:
        a, b = before.get(m), after.get(m)
        if a is None or b is None:
            continue
        margin = max(floor_us, (noise_ or {}).get(m, 0.0))
        d = b - a
        out[m] = (a, b, "better" if d < -margin else "worse" if d > margin else "no change")
    return out


def rate_differs(k1, s1, k2, s2):
    """True when two counts of events in two run lengths differ by more than
    chance (each one's 95 % interval leaves out the other's rate)."""
    if s1 <= 0 or s2 <= 0:
        return False
    r1, r2 = k1 / s1, k2 / s2
    up1, up2 = poisson_upper(k1) / s1, poisson_upper(k2) / s2
    return r2 > up1 or r1 > up2


# --------------------------------------------------------------------------
# the budget

IO_FUNCTIONS = re.compile(r"^(ecat\.|lcec\.|hm2_|hostmot2|parport\.|hal_parport|ppmc\.)")


class Budget:
    def __init__(self, thread, period, late_us, thread_us, io_us, io_funcs):
        self.thread, self.period = thread, period
        self.late_us, self.thread_us, self.io_us, self.io_funcs = late_us, thread_us, io_us, io_funcs

    @property
    def period_us(self):
        return self.period / 1000.0

    @property
    def left_us(self):
        return self.period_us - self.late_us - (self.thread_us or 0)

    @property
    def share_left(self):
        return self.left_us / self.period_us

    def verdict(self):
        s = self.share_left
        if s < 0:
            return "too little: cycles run into the next one"
        if s < 0.2:
            return "tight"
        if s < 0.5:
            return "enough"
        return "plenty"

    def text(self):
        lines = ["%s, period %s: %.0f us left (%.0f %%), %s"
                 % (self.thread, period_text(self.period), self.left_us, 100 * self.share_left,
                    self.verdict())]
        lines.append("  the start moved by at most %.1f us from one cycle to the next (timedelta; the "
                     "absolute wake-up latency is not measured yet)" % self.late_us)
        if self.thread_us is None:
            lines.append("  the thread's run time: not measured")
        else:
            lines.append("  the thread's longest run %.1f us%s" % (
                self.thread_us, "" if not self.io_funcs else
                "; of it I/O at most %.1f us (%s)" % (self.io_us, ", ".join(self.io_funcs))))
        return "\n".join(lines)

    def to_json(self):
        return {"thread": self.thread, "period": self.period, "late_us": self.late_us,
                "thread_us": self.thread_us, "io_us": self.io_us, "io_funcs": self.io_funcs,
                "left_us": self.left_us, "verdict": self.verdict()}


def budget(stats, times):
    """The budget of a thread from its cycle statistics and the HAL times
    (thread_times()): {name: {"tmax_us", "funcs": {f: tmax_us}}}."""
    s = stats.summary()
    if not stats.n:
        return None
    t = times.get(stats.thread) or {}
    io = {f: v for f, v in (t.get("funcs") or {}).items() if IO_FUNCTIONS.match(f) and v is not None}
    return Budget(stats.thread, stats.period, s["max_abs_us"], t.get("tmax_us"),
                  sum(io.values()) if io else None, sorted(io))


# --------------------------------------------------------------------------
# HAL: times, and what the check adds

def _halcmd():
    return os.environ.get("ECAT_HALCMD") or lcnc_env.program("halcmd")


def halcmd(*args, check=True, run=None):
    argv = [_halcmd()] + [str(a) for a in args]
    if run is not None:
        return run(argv)
    p = subprocess.run(argv, capture_output=True, text=True, env=lcnc_env.env())
    if check and p.returncode != 0:
        raise RtError((p.stderr or p.stdout).strip() or "halcmd %s failed" % args[0])
    return p.stdout


def parse_threads(text):
    """halcmd show thread -> {thread: {"period": ns, "funcs": [names]}}."""
    out, cur = {}, None
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 3 and f[0].isdigit() and f[1] in ("YES", "NO"):
            cur = f[2]
            out[cur] = {"period": int(f[0]), "funcs": []}
        elif cur and len(f) == 2 and f[0].isdigit():
            out[cur]["funcs"].append(f[1])
    return out


def parse_values(text):
    """halcmd -s show pin / show param -> {name: (type, value)}."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 5:
            out[f[4]] = (f[1], f[3])
        elif len(f) == 4 and f[1] in ("s32", "u32", "float", "real", "sint", "uint"):
            out[f[3]] = (f[1], f[2])
    return out


def time_us(type_, value):
    """A HAL time/tmax as us: 2.10 gives seconds as real, 2.9 ns as s32."""
    try:
        v = float(value)
    except ValueError:
        return None
    return v * 1e6 if type_ in ("real", "float") else v / 1000.0


def thread_times(threads_text, values):
    """{thread: {"period", "tmax_us", "funcs": {f: tmax_us}}} from halcmd's
    `show thread` and the pins/params (time and tmax)."""
    out = {}
    for th, d in parse_threads(threads_text).items():
        def tm(name):
            v = values.get(name + ".tmax")
            return time_us(*v) if v else None
        out[th] = {"period": d["period"], "tmax_us": tm(th),
                   "funcs": {f: tm(f) for f in d["funcs"] if not f.startswith(PREFIX)}}
    return out


def read_times(run=None):
    th = halcmd("show", "thread", run=run)
    vals = parse_values(halcmd("-s", "show", "pin", run=run) or "")
    vals.update(parse_values(halcmd("-s", "show", "param", run=run) or ""))
    return thread_times(th or "", vals)


def td_name(thread):
    return "%s-td-%s" % (PREFIX, thread)


def attach_commands(threads):
    """halcmd lines adding a timedelta first in each thread and a sampler
    channel per thread recording it; sampler channel i = threads[i]."""
    names = [td_name(t) for t in threads]
    cmds = ["loadrt timedelta names=%s" % ",".join(names),
            "loadrt sampler depth=%s cfg=%s" % (",".join([str(SAMPLER_DEPTH)] * len(threads)),
                                                ",".join(["s"] * len(threads)))]
    for i, (t, n) in enumerate(zip(threads, names)):
        cmds += ["addf %s %s 1" % (n, t),
                 "addf sampler.%d %s" % (i, t),
                 "net %s-%d %s.out sampler.%d.pin.0" % (PREFIX, i, n, i)]
    return cmds


def detach_commands(threads):
    """The reverse of attach_commands: functions out of the threads first
    (2.10 refuses to unload a component whose function is in a thread)."""
    cmds = []
    for i, t in enumerate(threads):
        cmds += ["delf sampler.%d %s" % (i, t), "delf %s %s" % (td_name(t), t),
                 "delsig %s-%d" % (PREFIX, i)]
    return cmds + ["unloadrt sampler", "unloadrt timedelta"]


def threads_hal(threads):
    """The HAL file of a threads-only run: [(name, period)] fastest first."""
    lines = ["loadrt threads " + " ".join("name%d=%s period%d=%d" % (i + 1, n, i + 1, p)
                                         for i, (n, p) in enumerate(threads))]
    lines += attach_commands([n for n, _ in threads])
    lines.append("start")
    return "\n".join(lines) + "\n"


def comps(run=None):
    """Names of the loaded HAL components."""
    out = []
    for line in (halcmd("-s", "show", "comp", check=False, run=run) or "").splitlines():
        f = line.split()
        if len(f) >= 3:
            out.append(f[2])
    return out


def hal_running(run=None):
    try:
        return any(not c.startswith("halcmd") for c in comps(run))
    except OSError:
        return False


# --------------------------------------------------------------------------
# a measurement

class Recorder:
    """halsampler per thread, its output read as it comes into CycleStats."""

    def __init__(self, threads):
        self.threads = threads                  # [(name, period)], channel = index
        self.stats = [CycleStats(n, p) for n, p in threads]
        self.procs = []
        self.readers = []
        self.errors = []

    def start(self):
        hs = os.environ.get("ECAT_HALSAMPLER") or lcnc_env.program("halsampler")
        for i, st in enumerate(self.stats):
            p = subprocess.Popen([hs, "-c", str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=lcnc_env.env())
            self.procs.append(p)
            t = threading.Thread(target=self._read, args=(p, st), daemon=True)
            t.start()
            self.readers.append(t)
        for st in self.stats:
            st.started = time.time()

    @staticmethod
    def _read(p, st):
        buf = []
        for line in p.stdout:
            buf.append(line)
            if len(buf) >= 256:
                st.feed("".join(buf))
                buf = []
        if buf:
            st.feed("".join(buf))

    def stop(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        for t in self.readers:
            t.join(5)
        self.errors = []
        for p, st in zip(self.procs, self.stats):
            err = p.stderr.read().strip() if p.stderr else ""
            if err and not st.n:
                self.errors.append("%s: halsampler: %s" % (st.thread, err))
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()
        self.procs = []
        for st in self.stats:
            st.ended = time.time()


def start_load(seconds):
    """stress-ng for the run (ends by itself a minute after it), or None
    when it is not installed."""
    if shutil.which("stress-ng") is None:
        return None
    return subprocess.Popen(
        ["stress-ng", "--cpu", "0", "--vm", "2", "--vm-bytes", "20%", "--cache", "2", "--io", "1",
         "--timeout", "%ds" % (seconds + 60)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_load(p):
    if p:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()


class Run:
    """One measurement: its way, load, length, the inventory it ran under,
    the cycle statistics per thread, the HAL times and the budgets."""

    def __init__(self, way, load, seconds, inventory=None):
        self.way, self.load, self.seconds = way, load, seconds
        self.inventory = inventory
        self.stats = []
        self.times = {}
        self.times_before = {}
        self.started = None
        self.notes = []
        self.host = socket.gethostname()

    def budgets(self):
        return [b for b in (budget(s, self.times) for s in self.stats) if b]

    def text(self):
        out = ["%s, %s, %s%s" % (
            {"threads": "Threads only (halrun, no I/O)", "attach": "Beside the running HAL"}[self.way],
            "with load (stress-ng)" if self.load == "stress" else "at rest" if self.load == "rest"
            else "as the computer is used", duration_text(self.stats[0].seconds) if self.stats else "-",
            "" if not self.started else ", " + time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started)))]
        for s in self.stats:
            m = s.summary()
            if not m["cycles"]:
                out.append("%s: no cycles recorded" % s.thread)
                continue
            out.append("%s (%s): %d cycles, error %.1f .. %+.1f us, 99 %% within %d us, "
                       "99.99 %% within %d us%s" % (
                           s.thread, period_text(s.period), m["cycles"], m["min_us"], m["max_us"],
                           m["p99_us"], m["p9999_us"],
                           ", %d samples lost (sampler full)" % m["overruns"] if m["overruns"] else ""))
            for t in (20, 50, 100):
                if t * 1000 < s.period:
                    out.append("  " + claim_text(s, t))
        for b in self.budgets():
            out.append(b.text())
        for n in self.notes:
            out.append("Note: " + n)
        return "\n".join(out)

    def to_json(self):
        return {"version": 1, "host": self.host, "way": self.way, "load": self.load,
                "seconds": self.seconds, "started": self.started, "notes": self.notes,
                "inventory": self.inventory.to_json() if self.inventory else None,
                "stats": [s.to_json() for s in self.stats], "times": self.times,
                "times_before": self.times_before,
                "summary": [s.summary() for s in self.stats],
                "budgets": [b.to_json() for b in self.budgets()]}

    @classmethod
    def from_json(cls, d):
        r = cls(d["way"], d["load"], d["seconds"],
                Inventory.from_json(d["inventory"]) if d.get("inventory") else None)
        r.host, r.started, r.notes = d.get("host"), d.get("started"), d.get("notes", [])
        r.stats = [CycleStats.from_json(s) for s in d["stats"]]
        r.times, r.times_before = d.get("times", {}), d.get("times_before", {})
        return r

    def save(self, path=None):
        if path is None:
            os.makedirs(STATE_DIR, exist_ok=True)
            path = os.path.join(STATE_DIR, "%s-%s-%s.json" % (
                self.host, time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started or time.time())),
                self.way))
        with open(path, "w") as f:
            json.dump(self.to_json(), f)
        return path

    @classmethod
    def load_file(cls, path):
        with open(path) as f:
            return cls.from_json(json.load(f))


def measure_threads(threads, seconds, load="rest", inventory=None, progress=print):
    """Threads only: halrun with the threads, nothing else."""
    if hal_running():
        raise RtError("HAL runs already (LinuxCNC or halrun): measure beside it (attach), or stop it")
    run = Run("threads", load, seconds, inventory)
    d = __import__("tempfile").mkdtemp(prefix="ecatsetup-rt-")
    hal = os.path.join(d, "rt.hal")
    with open(hal, "w") as f:
        f.write(threads_hal(threads))
    log = open(os.path.join(d, "halrun.log"), "w")
    halrun = os.environ.get("ECAT_HALRUN") or lcnc_env.program("halrun")
    proc = subprocess.Popen([halrun, "-I", "-f", hal], cwd=d, stdin=subprocess.PIPE, stdout=log,
                            stderr=subprocess.STDOUT, text=True, env=lcnc_env.env())
    rec = Recorder(threads)
    try:
        end = time.monotonic() + 20
        while time.monotonic() < end:
            if proc.poll() is not None:
                raise RtError("halrun stopped: see %s" % log.name)
            th = parse_threads(halcmd("show", "thread", check=False) or "")
            if all(any(f.startswith(PREFIX) for f in th.get(n, {}).get("funcs", [])) for n, _ in threads):
                break
            time.sleep(0.2)
        else:
            raise RtError("the threads did not start: see %s" % log.name)
        _measure(run, rec, threads, seconds, load, progress)
    finally:
        rec.stop()
        try:
            proc.stdin.write("stop\nexit\n")
            proc.stdin.flush()
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(5)
        log.close()
    return run


def _measure(run, rec, threads, seconds, load, progress):
    run.inventory = run.inventory or inventory()
    # where the threads run, now that they run
    rts = rt_threads(Roots())
    if rts:
        run.notes.append("real-time threads on CPU %s" % list_text(c for _, _, c, _ in rts))
    run.times_before = read_times()
    if run.way == "threads":
        # the check's own HAL: the longest runs from now on, not from the start
        for th, d in run.times_before.items():
            for name in [th] + list(d["funcs"]):
                halcmd("setp", name + ".tmax", 0, check=False)
    stress = start_load(seconds) if load == "stress" else None
    if load == "stress" and stress is None:
        run.notes.append("stress-ng is not installed: measured without load (sudo apt install stress-ng)")
        run.load = "rest"
    run.started = time.time()
    rec.start()
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            time.sleep(min(1.0, seconds - (time.monotonic() - t0)))
            done = time.monotonic() - t0
            s = rec.stats[-1].summary()
            progress("%3.0f %%  %s: %d cycles, worst %.1f us" % (
                100 * done / seconds, rec.stats[-1].thread, s.get("cycles", 0), s.get("max_abs_us", 0) or 0))
    finally:
        stop_load(stress)
        run.times = read_times()
        rec.stop()
    run.stats = rec.stats
    run.notes += rec.errors


def measure_attach(seconds, load="as-used", inventory=None, progress=print):
    """Beside a running HAL: timedelta and sampler into its threads, taken
    out again at the end."""
    if not hal_running():
        raise RtError("no HAL runs: start LinuxCNC first, or measure the threads only")
    loaded = comps()
    for c in ("sampler", "timedelta"):
        if c in loaded:
            raise RtError("the running HAL has %s loaded already: the check needs its own "
                          "(HAL loads a component once). Measure the threads only instead." % c)
    th = parse_threads(halcmd("show", "thread"))
    threads = sorted(((n, d["period"]) for n, d in th.items()), key=lambda x: x[1])
    if not threads:
        raise RtError("the running HAL has no threads")
    run = Run("attach", load, seconds, inventory)
    names = [n for n, _ in threads]
    added = []
    rec = Recorder(threads)
    try:
        for c in attach_commands(names):
            halcmd(*c.split())
            added.append(c)
        _measure(run, rec, threads, seconds, load, progress)
        grew = [t for t, d in run.times.items()
                if (d.get("tmax_us") or 0) > ((run.times_before.get(t) or {}).get("tmax_us") or 0)]
        run.notes.append("the longest runs are counted since LinuxCNC started; %s" % (
            "they grew during the run in " + ", ".join(grew) if grew else "none grew during the run"))
    finally:
        rec.stop()
        if added:
            for c in detach_commands(names):
                halcmd(*c.split(), check=False)
    return run
