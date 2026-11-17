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

import collections
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

from . import lcnc_env

# names of what the check adds to a HAL; the prefix tells them from the user's
PREFIX = "ecatsetup-rt"
PROBE = "ecat_rtprobe"                  # the HAL module (component/ecat_rtprobe.c)
PROBE_FILE = "/dev/shm/ecat-rtprobe"

# spike thresholds, us: counted over the run (cycle-to-cycle error, either way)
THRESHOLDS_US = (10, 20, 50, 100, 200, 500)

def state_dir():
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
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


def hal_loads(ini):
    """The loadrt lines of the configuration's HAL files, as token lists."""
    from . import integrate
    try:
        files = integrate.user_hal_files(ini)
    except OSError:
        files = []
    d = os.path.dirname(os.path.abspath(ini))
    p = os.path.join(d, "ethercat-load.hal")
    if os.path.exists(p):
        files.append(p)
    out = []
    for path in files:
        try:
            text = integrate.read_hal(path, ini)
        except Exception:      # a .tcl file that cannot be expanded: not this check's business
            continue
        for _, _, tok in integrate.statements(text):
            if tok[0] == "loadrt" and len(tok) > 1:
                out.append(tok)
    return out, bool(files)


def io_kinds(ini):
    """What the configuration's I/O is, from the modules its HAL files load."""
    loads, any_files = hal_loads(ini)
    mods = {t[1] for t in loads}
    kinds = [label for mod, label in IO_MODULES if label and mod in mods]
    return kinds or (["none found (a simulated configuration?)"] if any_files else [])


def _ip(text):
    try:
        parts = [int(x) for x in text.split(".")]
        if len(parts) == 4 and all(0 <= x < 256 for x in parts):
            return parts[0] << 24 | parts[1] << 16 | parts[2] << 8 | parts[3]
    except ValueError:
        pass
    return None


def routes(roots):
    """/proc/net/route -> [(iface, net, mask)] as ints, and the default route's iface."""
    out, default = [], None
    for line in (roots.p("net", "route") or "").splitlines()[1:]:
        f = line.split()
        if len(f) < 8:
            continue
        try:
            dst = int.from_bytes(bytes.fromhex(f[1]), "little")
            mask = int.from_bytes(bytes.fromhex(f[7]), "little")
        except ValueError:
            continue
        if dst == 0 and mask == 0:
            default = default or f[0]
        else:
            out.append((f[0], dst, mask))
    return out, default


def io_nics(ini, roots):
    """The network cards the I/O uses: ecat's master interface, or the one
    whose subnet holds hm2_eth's board_ip (192.168.1.121 by default)."""
    loads, _ = hal_loads(ini)
    d = os.path.dirname(os.path.abspath(ini))
    out = []
    rts, _ = routes(roots)
    for tok in loads:
        args = dict(x.split("=", 1) for x in tok[2:] if "=" in x)
        if tok[1] == "ecat":
            path = os.path.join(d, args.get("config", "ethercat.yaml"))
            try:
                import yaml
                with open(path) as f:
                    iface = ((yaml.safe_load(f) or {}).get("master") or {}).get("interface")
                if iface:
                    out.append(iface)
            except (OSError, ValueError, AttributeError, ImportError):
                pass
        elif tok[1] == "hm2_eth":
            for ip in args.get("board_ip", "192.168.1.121").strip('"').split(","):
                n = _ip(ip.strip())
                for iface, net, mask in rts:
                    if n is not None and mask and n & mask == net:
                        out.append(iface)
    return sorted(set(out))


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

    vendor = ""
    for line in (r.p("cpuinfo") or "").splitlines():
        if line.startswith("vendor_id"):
            v = line.split(":", 1)[1].strip()
            vendor = {"GenuineIntel": "intel", "AuthenticAMD": "amd"}.get(v, v.lower())
            break
    inv.data["cpu_vendor"] = vendor
    try:
        inv.data["uptime"] = float((r.p("uptime") or "0").split()[0])
    except ValueError:
        inv.data["uptime"] = 0.0
    gpes = gpe_counts(r)
    inv.add("gpes", "ACPI GPEs that have fired", ", ".join(
        "%s %d" % kv for kv in sorted(gpes.items(), key=lambda x: -x[1])[:6]) or "none",
        "/sys/firmware/acpi/interrupts/gpe*", data=gpes)

    def num(text):
        try:
            return int(text)
        except (TypeError, ValueError):
            return None
    sysctl = {"vm.stat_interval": num(r.p("sys", "vm", "stat_interval")),
              "kernel.timer_migration": num(r.p("sys", "kernel", "timer_migration"))}
    thp = re.search(r"\[(\w+)\]", r.s("kernel", "mm", "transparent_hugepage", "enabled") or "")
    sysctl["thp"] = thp.group(1) if thp else None
    inv.add("sysctl", "vm.stat_interval, timer_migration, THP", "%s, %s, %s" % (
        sysctl["vm.stat_interval"], sysctl["kernel.timer_migration"], sysctl["thp"] or "not built in"),
        "/proc/sys/vm/stat_interval, /proc/sys/kernel/timer_migration, "
        "/sys/kernel/mm/transparent_hugepage/enabled", data=sysctl)
    inv.data["modules"] = sorted(line.split()[0] for line in (r.p("modules") or "").splitlines() if line)
    ps = r.s("module", "snd_hda_intel", "parameters", "power_save")
    if ps is not None:
        inv.add("snd_power_save", "Sound card power saving (snd_hda_intel)", ps + " s" if ps != "0" else "off",
                "/sys/module/snd_hda_intel/parameters/power_save", data=num(ps))
    nvme = [n for n in r.ls(S, "class", "nvme")]
    if nvme:
        apst = r.s("module", "nvme_core", "parameters", "default_ps_max_latency_us")
        inv.add("nvme", "NVMe disks", "%s (APST max latency %s us)" % (", ".join(nvme), apst),
                "/sys/class/nvme, /sys/module/nvme_core/parameters", data={"disks": nvme, "apst": num(apst)})
    _, default = routes(r)
    inv.add("default_route", "Default route (the LAN)", default or "none", "/proc/net/route", data=default)

    if ini:
        th = ini_threads(ini)
        inv.add("threads", "LinuxCNC threads (INI)",
                ", ".join("%s %s" % (n, period_text(p)) for n, p in th) or "no SERVO_PERIOD",
                "[EMCMOT] of %s" % ini, data=th)
        io = io_kinds(ini)
        inv.add("io", "I/O", ", ".join(io) or "?", "loadrt lines of the INI's HAL files", data=io)
        nics_io = io_nics(ini, r)
        inv.add("io_nics", "The I/O's network card", ", ".join(nics_io) or "none or not known",
                "ethercat.yaml master.interface, hm2_eth board_ip against /proc/net/route", data=nics_io)
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
    cycles and the counts over thresholds.

    The deltas also give each cycle's time: their sum since the first sample,
    on CLOCK_MONOTONIC (rtapi_get_time in uspace), the clock of the poller's
    time stamps. And they give how LATE each cycle started: the thread wakes
    on a grid (the previous wake-up time plus the period), so the sum of
    (delta - period) is the lateness plus a constant; against its smallest
    value in the LATE_WINDOW before (the least late cycle), it is the
    lateness. A PLL that moves the period (ecat's servo-thread-follows-ref-
    clock) shifts the grid by a few ns per cycle: over 50 ms that is below
    a microsecond. A delay longer than the window is under-counted."""

    WORST = 20
    LATE_WINDOW_NS = 50000000
    SPIKE_NS = 10000             # cycles kept for the per-spike analysis: late or moved > 10 us
    MAX_EVENTS = 20000

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
        # time line and lateness
        self.t = None           # ns of the latest sample since the first one (None: lost)
        self.offset = 0
        self.window = collections.deque()
        self.win_n = max(8, self.LATE_WINDOW_NS // period)
        self.late_hist = {}     # lateness in whole us -> count
        self.late_max = 0
        self.late_n = 0
        self.events = []        # [(t ns since the first sample or None, error ns, late ns)]
        self.events_dropped = 0
        self.anchor = None      # (monotonic ns of the first sample, uncertainty ns)

    def add(self, delta, at=None):
        """One cycle: delta = ns since the previous run (0 = first, skipped);
        at: its time (ns since the first cycle) when known, else the sum of
        the deltas."""
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
        # time and lateness
        if at is not None:
            self.t = at
        elif idx == 0:
            self.t = 0
        elif self.t is not None:
            self.t += delta
        self.offset += e
        w = self.window
        while w and w[-1][1] >= self.offset:
            w.pop()
        w.append((idx, self.offset))
        while w[0][0] <= idx - self.win_n:
            w.popleft()
        late = self.offset - w[0][1]
        lb = late // 1000
        self.late_hist[lb] = self.late_hist.get(lb, 0) + 1
        self.late_n += 1
        if late > self.late_max:
            self.late_max = late
        if late > self.SPIKE_NS or a > self.SPIKE_NS:
            if len(self.events) < self.MAX_EVENTS:
                self.events.append((self.t, e, late))
            else:
                self.events_dropped += 1

    def lost(self):
        """Samples were lost (sampler full): no time line from here on, and
        the lateness starts again."""
        self.overruns += 1
        self.t = None
        self.window.clear()

    def feed(self, text):
        """halsampler's lines (a value, or "<n> <value>" with -t)."""
        for line in text.splitlines():
            f = line.split()
            if not f:
                continue
            if f[0].startswith("overrun"):
                self.lost()
                continue
            try:
                self.add(int(f[-1]))
            except ValueError:
                pass

    @property
    def seconds(self):
        return self.n * self.period / 1e9

    @staticmethod
    def _percentile(hist, n, q):
        if not n:
            return None
        need = q * n
        s = 0
        for k in sorted(hist):
            s += hist[k]
            if s >= need:
                return k + 1
        return max(hist) + 1

    def abs_percentile(self, q):
        """The |error| (us) that q of the cycles stay within (from the
        histogram: an upper bound good to 1 us)."""
        if not self.n:
            return None
        ab = {}
        for b, c in self.hist.items():
            k = b if b >= 0 else -b - 1      # |e| < k + 1 us
            ab[k] = ab.get(k, 0) + c
        return self._percentile(ab, self.n, q)

    def late_percentile(self, q):
        return self._percentile(self.late_hist, self.late_n, q)

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
            "late_p99_us": self.late_percentile(0.99), "late_p9999_us": self.late_percentile(0.9999),
            "late_max_us": self.late_max / 1000.0,
            "over": {str(t): c for t, c in self.over.items()},
            "over_per_hour": {str(t): c / hours for t, c in self.over.items()},
            "overruns": self.overruns,
        }

    def to_json(self, events=2000):
        ev = sorted(self.events, key=lambda x: -max(x[2], abs(x[1])))[:events]
        return {"thread": self.thread, "period": self.period, "n": self.n,
                "hist": {str(k): v for k, v in sorted(self.hist.items())},
                "late_hist": {str(k): v for k, v in sorted(self.late_hist.items())},
                "late_max": self.late_max, "late_n": self.late_n,
                "min": self.min, "max": self.max, "sum": self.sum,
                "worst": self.worst, "over": {str(k): v for k, v in self.over.items()},
                "overruns": self.overruns, "anchor": self.anchor,
                "events": sorted(ev, key=lambda x: (x[0] is None, x[0] or 0)),
                "events_dropped": self.events_dropped + max(0, len(self.events) - events)}

    @classmethod
    def from_json(cls, d):
        s = cls(d["thread"], d["period"])
        s.n, s.min, s.max, s.sum, s.overruns = d["n"], d["min"], d["max"], d["sum"], d.get("overruns", 0)
        s.hist = {int(k): v for k, v in d["hist"].items()}
        s.late_hist = {int(k): v for k, v in d.get("late_hist", {}).items()}
        s.late_max, s.late_n = d.get("late_max", 0), d.get("late_n", 0)
        s.worst = [tuple(x) for x in d.get("worst", [])]
        s.over = {int(k): v for k, v in d["over"].items()}
        s.anchor = tuple(d["anchor"]) if d.get("anchor") else None
        s.events = [tuple(x) for x in d.get("events", [])]
        s.events_dropped = d.get("events_dropped", 0)
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
COMPARED = ("late_p9999_us", "late_max_us", "p99_us", "p9999_us", "max_abs_us")


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
    def __init__(self, thread, period, late_us, thread_us, io_us, io_funcs, moved_us=None):
        self.thread, self.period = thread, period
        self.late_us, self.thread_us, self.io_us, self.io_funcs = late_us, thread_us, io_us, io_funcs
        self.moved_us = moved_us

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
        lines.append("  started at most %.1f us late (against the least late cycle of the 50 ms "
                     "before it)" % self.late_us)
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
                "moved_us": self.moved_us, "left_us": self.left_us, "verdict": self.verdict()}


def budget(stats, times):
    """The budget of a thread from its cycle statistics and the HAL times
    (thread_times()): {name: {"tmax_us", "funcs": {f: tmax_us}}}."""
    s = stats.summary()
    if not stats.n:
        return None
    t = times.get(stats.thread) or {}
    io = {f: v for f, v in (t.get("funcs") or {}).items() if IO_FUNCTIONS.match(f) and v is not None}
    return Budget(stats.thread, stats.period, s["late_max_us"], t.get("tmax_us"),
                  sum(io.values()) if io else None, sorted(io), s["max_abs_us"])


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


def probe_name(thread):
    return "%s-%s" % (PREFIX, thread)


def probe_file():
    return os.environ.get("ECAT_RTPROBE_FILE") or PROBE_FILE


def attach_commands(threads):
    """halcmd lines putting an ecat_rtprobe function first in each thread:
    each run's time goes into probe_file() (ProbeReader). Nothing else in
    HAL is used, so a configuration's own sampler or timedelta is no
    obstacle."""
    names = [probe_name(t) for t in threads]
    load = "loadrt %s names=%s" % (PROBE, ",".join(names))
    if os.environ.get("ECAT_RTPROBE_FILE"):
        load += " file=%s" % probe_file()
    return [load] + ["addf %s %s 1" % (n, t) for t, n in zip(threads, names)]


def probe_error(text):
    """What to say when loading ecat_rtprobe failed."""
    if re.search(r"not found|No such file|cannot open shared object|dlopen", text):
        return ("%s is not installed for this LinuxCNC (it comes with ecat 0.23 or later, beside "
                "ecat.so): %s" % (PROBE, text))
    return "%s did not load: %s" % (PROBE, text)


def detach_commands(threads):
    """The reverse of attach_commands: functions out of the threads first
    (2.10 refuses to unload a component whose function is in a thread)."""
    return ["delf %s %s" % (probe_name(t), t) for t in threads] + ["unloadrt %s" % PROBE]


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
# the poller's data, and spikes against it

class PollData:
    """The poller's file: per key the times its counter grew (and by how
    much), the times of all polls, and what it could not read."""

    def __init__(self):
        self.interval = 10000000
        self.cpus = []
        self.labels = {}        # key -> label
        self.times = []         # every poll's time, ns
        self.changes = {}       # key -> [(t, delta)]
        self.values = {}        # key -> [(t, value)]
        self.errors = []

    @classmethod
    def parse(cls, text):
        p = cls()
        idx = {}
        for line in text.splitlines():
            if line.startswith("# rtpoll"):
                f = line.split()
                p.interval = int(f[3])
                p.cpus = cpu_list(f[4]) if len(f) > 4 else []
            elif line.startswith("K "):
                f = line.split(None, 3)
                idx[f[1]] = f[2]
                p.labels[f[2]] = f[3] if len(f) > 3 else f[2]
            elif line.startswith("E "):
                p.errors.append(line[2:])
            elif line[:1].isdigit():
                f = line.split()
                t = int(f[0])
                p.times.append(t)
                for x in f[1:]:
                    if ":" in x:
                        i, d = x.split(":", 1)
                        p.changes.setdefault(idx.get(i, i), []).append((t, int(d)))
                    elif "=" in x:
                        i, v = x.split("=", 1)
                        p.values.setdefault(idx.get(i, i), []).append((t, int(v)))
        return p

    @property
    def seconds(self):
        return (self.times[-1] - self.times[0]) / 1e9 if len(self.times) > 1 else 0.0

    def totals(self):
        """{key: (label, total growth, per second)} of the counters that grew."""
        s = self.seconds
        return {k: (self.labels.get(k, k), sum(d for _, d in v), sum(d for _, d in v) / s if s else 0.0)
                for k, v in self.changes.items()}

    def summary(self):
        tot = self.totals()
        return {"interval": self.interval, "cpus": self.cpus, "seconds": self.seconds, "errors": self.errors,
                "polls": len(self.times),
                "counters": {k: {"label": l, "total": t, "per_s": r} for k, (l, t, r) in tot.items()},
                "values": {k: {"label": self.labels.get(k, k), "changes": len(v) - 1,
                               "min": min(x for _, x in v), "max": max(x for _, x in v)}
                           for k, v in self.values.items() if v}}


def spikes(stats, threshold_ns, merge_ns=2000000):
    """The cycles over threshold_ns (lateness, else the cycle-to-cycle move)
    with a time, bursts within merge_ns taken as one: [(t, late, err)]."""
    out = []
    last = None
    for t, e, late in stats.events:
        if t is None or max(late, abs(e)) <= threshold_ns:
            continue
        if out and t - last <= merge_ns:
            last = t
            if max(late, abs(e)) > max(out[-1][1], abs(out[-1][2])):
                out[-1] = (out[-1][0], late, e)
            continue
        last = t
        out.append((t, late, e))
    return out


def spike_threshold(stats):
    """Cycles worth explaining: later than 10 us and than 99.9 % of them."""
    p = stats.late_percentile(0.999) or 0
    return max(CycleStats.SPIKE_NS, p * 1000)


def correlate(stats, poll, threshold_ns=None):
    """Which counters grew with the spikes: for each, in how many spikes'
    time windows it grew, and how many to expect by chance from how often it
    grows in any window. The window is the anchor's uncertainty plus one
    poll interval each way."""
    if stats.anchor is None or not poll.times:
        return None
    t0, unc = stats.anchor
    thr = spike_threshold(stats) if threshold_ns is None else threshold_ns
    sp = spikes(stats, thr)
    out = {"threshold_us": thr / 1000.0, "spikes": len(sp), "window_ms": 0.0, "counters": []}
    if not sp:
        return out
    iv = poll.interval
    # a poll at s counts what grew since the poll before: a spike at t shows
    # in the first poll after it
    lo_w, hi_w = unc, unc + iv
    out["window_ms"] = (lo_w + hi_w) / 1e6
    times = poll.times
    n_polls = len(times)
    import bisect
    span_polls = max(1, (lo_w + hi_w) // max(1, iv))
    for key, ch in list(poll.changes.items()) + [(k, [(t, 1) for t, _ in v[1:]]) for k, v in poll.values.items()]:
        ts = [t for t, _ in ch]
        p = min(1.0, len(ts) / n_polls)
        expected = 1 - (1 - p) ** span_polls
        hit = 0
        for t, late, e in sp:
            at = t0 + t
            i = bisect.bisect_left(ts, at - lo_w)
            if i < len(ts) and ts[i] <= at + hi_w:
                hit += 1
        if hit:
            out["counters"].append({"key": key, "label": poll.labels.get(key, key), "with": hit,
                                    "of": len(sp), "expected": expected,
                                    "lift": (hit / len(sp)) / expected if expected > 0 else float("inf")})
    out["counters"].sort(key=lambda c: (-c["with"], -min(c["lift"], 1e9)))
    return out


def strong(c, min_spikes=3):
    """A correlation worth naming: in at least half the spikes (and
    min_spikes of them), at least twice as often as by chance."""
    return c["with"] >= min_spikes and c["with"] * 2 >= c["of"] and c["lift"] >= 2


def correlation_text(corr, top=5):
    if corr is None:
        return ["  spikes not compared with the counters (no poller data)"]
    if not corr["spikes"]:
        return ["  no spikes over %.0f us to explain" % corr["threshold_us"]]
    out = ["  %d spike(s) over %.0f us compared with the counters (window %.0f ms):" % (
        corr["spikes"], corr["threshold_us"], corr["window_ms"])]
    shown = 0
    for c in corr["counters"]:
        if shown >= top:
            break
        out.append("    %d of %d with %s (by chance about %.0f %%)%s" % (
            c["with"], c["of"], c["label"], 100 * c["expected"], "  <- suspect" if strong(c) else ""))
        shown += 1
    if not corr["counters"]:
        out.append("    no counter grew with them")
    return out


# --------------------------------------------------------------------------
# root: the SMI count and rtla timerlat

SUDO = ["sudo", "-n"]


def can_sudo():
    try:
        return subprocess.run(SUDO + ["true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def parse_rtla(text):
    """rtla timerlat top -q: per CPU the IRQ and thread latency maxima (us),
    whether it stopped at its threshold, and its analysis of that spike."""
    out = {"cpus": {}, "hit": "hit stop tracing" in (text or ""), "analysis": ""}
    for line in (text or "").splitlines():
        m = re.match(r"\s*(\d+)\s+#(\d+)\s*\|\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*\|"
                     r"\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)", line)
        if m:
            out["cpus"][m.group(1)] = {"count": int(m.group(2)), "irq_max_us": int(m.group(6)),
                                       "thread_max_us": int(m.group(10))}
    if out["hit"]:
        i = text.index("hit stop tracing")
        out["analysis"] = text[i:].split("\n", 1)[1].strip() if "\n" in text[i:] else ""
        # the spike it stopped at is not in the table's maximum
        m = re.search(r"CPU (\d+) hit stop tracing", text)
        lat = re.search(r"Thread latency:\s*([\d.]+) us", text)
        if m and lat:
            c = out["cpus"].setdefault(m.group(1), {"count": 0, "irq_max_us": 0, "thread_max_us": 0})
            c["thread_max_us"] = max(c["thread_max_us"], int(float(lat.group(1)) + 0.999))
    return out


def rtla_text(r):
    if not r:
        return []
    if r.get("error"):
        return ["rtla timerlat: %s" % r["error"]]
    out = []
    for c, v in sorted(r["cpus"].items()):
        out.append("rtla timerlat on CPU %s: wake-up latency at most %d us (thread), %d us (IRQ), "
                   "%d wake-ups" % (c, v["thread_max_us"], v["irq_max_us"], v["count"]))
    if r["hit"]:
        out.append("rtla stopped at the first wake-up over %s us; its analysis:" % r.get("threshold_us"))
        out += ["  " + x for x in r["analysis"].splitlines()]
    return out


def gpe_counts(roots=None):
    """{gpeXX: count} of the ACPI GPEs that have fired, from /sys/firmware."""
    r = roots or Roots()
    out = {}
    for name in r.ls(r.sys, "firmware", "acpi", "interrupts"):
        if re.fullmatch(r"gpe[0-9A-Fa-f]{2}", name):
            v = r.s("firmware", "acpi", "interrupts", name)
            try:
                n = int((v or "0").split()[0])
            except ValueError:
                continue
            if n:
                out[name] = n
    return out


# --------------------------------------------------------------------------
# a measurement

class ProbeReader:
    """ecat_rtprobe's file (component/ecat_rtprobe_core.h has the layout):
    the run times of each thread, read as they come."""

    HEAD = struct.Struct("<8sIIIIq")
    INST = 128

    def __init__(self, path):
        import mmap
        with open(path, "rb") as f:
            self.map = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        magic, version, n, slots, size, self.created = self.HEAD.unpack_from(self.map, 0)
        if magic != b"ECATRTP\0" or version != 1 or size != self.INST or slots & (slots - 1):
            self.map.close()
            raise RtError("%s is not an ecat_rtprobe file of version 1" % path)
        self.n, self.slots = n, slots
        self.names = [self.map[64 + i * self.INST:64 + i * self.INST + 64].split(b"\0")[0].decode()
                      for i in range(n)]
        self.rings = 64 + n * self.INST

    def count(self, i):
        return struct.unpack_from("<Q", self.map, 64 + i * self.INST + 72)[0]

    def period(self, i):
        return struct.unpack_from("<q", self.map, 64 + i * self.INST + 64)[0]

    def times(self, i, start, end):
        """The times of runs start .. end-1 (all still in the ring)."""
        out = []
        base = self.rings + i * self.slots * 8
        k = start
        while k < end:
            slot = k & (self.slots - 1)
            m = min(end - k, self.slots - slot)
            out += struct.unpack_from("<%dq" % m, self.map, base + slot * 8)
            k += m
        return out

    def close(self):
        self.map.close()


class Recorder:
    """Reads ecat_rtprobe's file every READ_EVERY s into CycleStats, one per
    thread: the time between runs and each run's own time (the anchor is
    exact: the probe's clock is the poller's, CLOCK_MONOTONIC)."""

    READ_EVERY = 0.05

    def __init__(self, threads):
        self.threads = threads                  # [(name, period)]
        self.stats = [CycleStats(n, p) for n, p in threads]
        self.errors = []
        self.reader = None
        self.thread = None
        self.stopping = threading.Event()

    def start(self):
        try:
            self.reader = ProbeReader(probe_file())
        except (OSError, ValueError) as e:
            raise RtError("cannot read the probe's file %s: %s" % (probe_file(), e))
        index = {n: i for i, n in enumerate(self.reader.names)}
        self.index = [index.get(probe_name(n)) for n, _ in self.threads]
        for st, i in zip(self.stats, self.index):
            if i is None:
                self.errors.append("%s: the probe has no function for this thread" % st.thread)
        # runs before now are not part of the measurement
        self.next = [self.reader.count(i) if i is not None else 0 for i in self.index]
        self.last = [None] * len(self.stats)
        for st in self.stats:
            st.started = time.time()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stopping.wait(self.READ_EVERY):
            self.read()
        self.read()

    def read(self):
        r = self.reader
        for k, (st, i) in enumerate(zip(self.stats, self.index)):
            if i is None:
                continue
            end = r.count(i)
            start = self.next[k]
            if end - start > r.slots - 64:
                # the ring went round before it was read: the runs between are gone
                st.lost()
                self.last[k] = None
                start = end - (r.slots - 64)
            if end <= start:
                continue
            times = r.times(i, start, end)
            # the count may have moved on while the slots were read: a slot
            # written again since then is not taken
            if r.count(i) - start > r.slots:
                st.lost()
                self.last[k] = None
                self.next[k] = r.count(i)
                continue
            self.next[k] = end
            prev = self.last[k]
            for t in times:
                if prev is not None:
                    if st.anchor is None:
                        st.anchor = (t, 0)
                    # the time is the probe's own, also after runs were lost
                    st.add(t - prev, at=t - st.anchor[0])
                prev = t
            self.last[k] = prev

    def stop(self):
        if self.thread is not None:
            self.stopping.set()
            self.thread.join(5)
            self.thread = None
        if self.reader is not None:
            self.reader.close()
            self.reader = None
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


POLLER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rtpoll.py")


class Run:
    """One measurement: its way, load, length, the inventory it ran under,
    the cycle statistics per thread, the HAL times and the budgets, the
    poller's counters and the spikes against them, and as root rtla's
    measurement and the SMI count."""

    def __init__(self, way, load, seconds, inventory=None, root=False):
        self.way, self.load, self.seconds = way, load, seconds
        self.inventory = inventory
        self.root = root
        self.stats = []
        self.times = {}
        self.times_before = {}
        self.started = None
        self.notes = []
        self.host = socket.gethostname()
        self.rt_cpus = []
        self.poll = None            # PollData.summary()
        self.correlations = {}      # thread -> correlate()
        self.rtla = None
        self.smi_measured = False
        self.gpe_rates = {}         # gpeXX -> per second during the run
        self.poll_path = None

    def budgets(self):
        return [b for b in (budget(s, self.times) for s in self.stats) if b]

    def counter_rate(self, key):
        c = ((self.poll or {}).get("counters") or {}).get(key)
        return c["per_s"] if c else 0.0

    def text(self):
        out = ["%s, %s, %s%s%s" % (
            {"threads": "Threads only (halrun, no I/O)", "attach": "Beside the running HAL"}[self.way],
            "with load (stress-ng)" if self.load == "stress" else "at rest" if self.load == "rest"
            else "as the computer is used", duration_text(self.stats[0].seconds) if self.stats else "-",
            "" if not self.started else ", " + time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started)),
            ", as root" if self.root else "")]
        for s in self.stats:
            m = s.summary()
            if not m["cycles"]:
                out.append("%s: no cycles recorded" % s.thread)
                continue
            out.append("%s (%s): %d cycles; late at most %.1f us (99.99 %% within %d us); moved %.1f .. "
                       "%+.1f us from cycle to cycle%s" % (
                           s.thread, period_text(s.period), m["cycles"], m["late_max_us"],
                           m["late_p9999_us"], m["min_us"], m["max_us"],
                           ", %d samples lost (sampler full)" % m["overruns"] if m["overruns"] else ""))
            for t in (20, 50, 100):
                if t * 1000 < s.period:
                    out.append("  " + claim_text(s, t))
            if self.poll is not None or s.thread in self.correlations:
                out += correlation_text(self.correlations.get(s.thread))
        for b in self.budgets():
            out.append(b.text())
        out += rtla_text(self.rtla)
        if self.poll is not None:
            busy = sorted(((v["per_s"], v["label"]) for k, v in self.poll["counters"].items()
                           if k.startswith(("irq:", "softirq:")) and v["per_s"] >= 1), reverse=True)
            if busy:
                out.append("On the real-time CPU%s %s: %s" % (
                    "s" if len(self.rt_cpus) > 1 else "", list_text(self.rt_cpus),
                    ", ".join("%s %s/s" % (l, number_text(r)) for r, l in busy[:8])))
            if self.smi_measured:
                out.append("SMIs during the run: %d" % ((self.poll["counters"].get("smi") or {}).get("total", 0)))
            elif not self.root:
                out.append("SMIs and rtla: not measured without sudo (--rt-root)")
        for g, r in sorted(self.gpe_rates.items(), key=lambda x: -x[1]):
            out.append("ACPI %s fired %s times a second" % (g, number_text(r)))
        for n in self.notes:
            out.append("Note: " + n)
        return "\n".join(out)

    def to_json(self):
        return {"version": 2, "host": self.host, "way": self.way, "load": self.load,
                "seconds": self.seconds, "started": self.started, "notes": self.notes, "root": self.root,
                "inventory": self.inventory.to_json() if self.inventory else None,
                "stats": [s.to_json() for s in self.stats], "times": self.times,
                "times_before": self.times_before, "rt_cpus": self.rt_cpus, "poll": self.poll,
                "correlations": self.correlations, "rtla": self.rtla, "smi_measured": self.smi_measured,
                "gpe_rates": self.gpe_rates,
                "summary": [s.summary() for s in self.stats],
                "budgets": [b.to_json() for b in self.budgets()]}

    @classmethod
    def from_json(cls, d):
        r = cls(d["way"], d["load"], d["seconds"],
                Inventory.from_json(d["inventory"]) if d.get("inventory") else None, d.get("root", False))
        r.host, r.started, r.notes = d.get("host"), d.get("started"), d.get("notes", [])
        r.stats = [CycleStats.from_json(s) for s in d["stats"]]
        r.times, r.times_before = d.get("times", {}), d.get("times_before", {})
        r.rt_cpus, r.poll, r.correlations = d.get("rt_cpus", []), d.get("poll"), d.get("correlations", {})
        r.rtla, r.smi_measured, r.gpe_rates = d.get("rtla"), d.get("smi_measured", False), d.get("gpe_rates", {})
        return r

    def save(self, path=None):
        if path is None:
            os.makedirs(state_dir(), exist_ok=True)
            base = os.path.join(state_dir(), "%s-%s-%s" % (
                self.host, time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started or time.time())),
                self.way))
            path, n = base + ".json", 1
            while os.path.exists(path):
                n += 1
                path = "%s-%d.json" % (base, n)
        with open(path, "w") as f:
            json.dump(self.to_json(), f)
        if self.poll_path and os.path.exists(self.poll_path):
            shutil.copyfile(self.poll_path, os.path.splitext(path)[0] + ".poll")
        return path

    @classmethod
    def load_file(cls, path):
        with open(path) as f:
            return cls.from_json(json.load(f))


def measure_threads(threads, seconds, load="rest", inventory=None, progress=print, root=False, cancel=None):
    """Threads only: halrun with the threads, nothing else."""
    if hal_running():
        raise RtError("HAL runs already (LinuxCNC or halrun): measure beside it (attach), or stop it")
    run = Run("threads", load, seconds, inventory, root)
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
                log.flush()
                with open(log.name) as f:
                    text = f.read()
                if PROBE in text:
                    raise RtError(probe_error(text.strip().splitlines()[-1] if text.strip() else ""))
                raise RtError("halrun stopped: see %s" % log.name)
            th = parse_threads(halcmd("show", "thread", check=False) or "")
            if all(any(f.startswith(PREFIX) for f in th.get(n, {}).get("funcs", [])) for n, _ in threads):
                break
            time.sleep(0.2)
        else:
            raise RtError("the threads did not start: see %s" % log.name)
        _measure(run, rec, threads, seconds, load, progress, d, cancel)
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


def start_poller(run, cpus, seconds, folder):
    """The poller beside the run, as root when the run is (the SMI count)."""
    run.poll_path = os.path.join(folder, "poll.txt")
    argv = ["python3", "-I", POLLER, "--cpus", list_text(cpus), "--seconds", str(seconds + 30),
            "--out", run.poll_path]
    if run.root:
        if not os.path.exists("/dev/cpu/0/msr"):
            if subprocess.run(SUDO + ["modprobe", "msr"], capture_output=True).returncode == 0:
                run.notes.append("loaded the msr module to read the SMI count (until the next start)")
        argv = SUDO + argv + ["--smi"]
    if os.environ.get("ECAT_RTPOLL"):
        argv = [os.environ["ECAT_RTPOLL"]] + argv[argv.index("--cpus"):]
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def stop_poller(run, p):
    if p is None:
        return
    if p.poll() is None:
        p.terminate()
    try:
        _, err = p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        _, err = p.communicate()
    if err and err.strip():
        run.notes.append("poller: " + err.strip().splitlines()[-1])
    try:
        with open(run.poll_path, errors="replace") as f:
            data = PollData.parse(f.read())
    except OSError:
        run.notes.append("the poller wrote nothing")
        return
    run.poll = data.summary()
    run.smi_measured = "smi" in data.labels
    for st in run.stats:
        run.correlations[st.thread] = correlate(st, data)
    for e in data.errors:
        run.notes.append("poller: " + e)


RTLA_BESIDE_LINUXCNC = (
    "rtla timerlat was not run beside LinuxCNC: its start and end switch the kernel's tracing on and "
    "off, which delayed the real-time threads (39 and 57 us measured on an N100, and once enough for "
    "LinuxCNC's \"Unexpected realtime delay\"); measure the threads only to have it")


def start_rtla(run, seconds, threshold_us):
    if not run.root:
        return None
    if run.way == "attach":
        # never beside a machine that runs: see RTLA_BESIDE_LINUXCNC
        run.notes.append(RTLA_BESIDE_LINUXCNC)
        return None
    if shutil.which("rtla") is None and not os.path.exists("/usr/bin/rtla"):
        run.rtla = {"error": "not installed (sudo apt install rtla)"}
        return None
    hk = [c for c in (run.inventory.data.get("cpus") or []) if c not in run.rt_cpus]
    argv = SUDO + ["rtla", "timerlat", "top", "-q", "-c", list_text(run.rt_cpus), "-d", "%ds" % int(seconds),
                   "-T", str(int(threshold_us))]
    if hk:
        argv += ["-H", list_text(hk)]
    run.rtla = {"threshold_us": threshold_us, "argv": argv}
    run.notes.append("rtla's own timer thread ran on CPU %s beside the measured threads"
                     % list_text(run.rt_cpus))
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def stop_rtla(run, p):
    if p is None:
        return
    try:
        out, _ = p.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        p.terminate()
        out, _ = p.communicate()
    r = parse_rtla(out)
    r.update(threshold_us=run.rtla["threshold_us"], text=out)
    if not r["cpus"] and not r["hit"]:
        r["error"] = (out.strip().splitlines() or ["no output"])[-1]
    run.rtla = r


RTLA_THRESHOLD_US = 50


def _measure(run, rec, threads, seconds, load, progress, folder, cancel=None):
    run.inventory = run.inventory or inventory()
    # where the threads run, now that they run
    rts = rt_threads(Roots())
    run.rt_cpus = sorted({c for _, _, c, _ in rts}) or run.inventory.data.get("isolated") or \
        (run.inventory.data.get("cpus") or [0])[-1:]
    run.times_before = read_times()
    if run.way == "threads":
        # the check's own HAL: the longest runs from now on, not from the start
        for th, d in run.times_before.items():
            for name in [th] + list(d["funcs"]):
                halcmd("setp", name + ".tmax", 0, check=False)
    gpe0 = gpe_counts()
    stress = start_load(seconds) if load == "stress" else None
    if load == "stress" and stress is None:
        run.notes.append("stress-ng is not installed: measured without load (sudo apt install stress-ng)")
        run.load = "rest"
    poller = start_poller(run, run.rt_cpus, seconds, folder)
    rtla = None
    run.started = time.time()
    try:
        time.sleep(0.3)          # the poller's first line
        rec.start()
        rtla = start_rtla(run, seconds, RTLA_THRESHOLD_US)
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            if cancel is not None and cancel.wait(max(0.0, min(1.0, seconds - (time.monotonic() - t0)))):
                run.notes.append("stopped after %.0f s of %.0f" % (time.monotonic() - t0, seconds))
                break
            if cancel is None:
                time.sleep(max(0.0, min(1.0, seconds - (time.monotonic() - t0))))
            done = time.monotonic() - t0
            s = rec.stats[-1].summary()
            progress("%3.0f %%  %s: %d cycles, late at most %.1f us" % (
                100 * min(done, seconds) / seconds, rec.stats[-1].thread, s.get("cycles", 0),
                s.get("late_max_us", 0) or 0))
    finally:
        stop_load(stress)
        run.times = read_times()
        rec.stop()
        run.stats = rec.stats
        run.notes += rec.errors
        stop_rtla(run, rtla)
        stop_poller(run, poller)
        secs = rec.stats[0].seconds if rec.stats else seconds
        gpe1 = gpe_counts()
        run.gpe_rates = {g: (n - gpe0.get(g, 0)) / secs for g, n in gpe1.items()
                         if n > gpe0.get(g, 0) and secs > 0}


def measure_attach(seconds, load="as-used", inventory=None, progress=print, root=False, cancel=None):
    """Beside a running HAL: an ecat_rtprobe function first in each of its
    threads, taken out again at the end."""
    if not hal_running():
        raise RtError("no HAL runs: start LinuxCNC first, or measure the threads only")
    if PROBE in comps():
        raise RtError("%s is loaded already: another check is measuring, or one was cut short. "
                      "Wait for it, or take it out: halcmd delf %s-<thread> <thread> for each "
                      "thread, then halcmd unloadrt %s" % (PROBE, PREFIX, PROBE))
    th = parse_threads(halcmd("show", "thread"))
    threads = sorted(((n, d["period"]) for n, d in th.items()), key=lambda x: x[1])
    if not threads:
        raise RtError("the running HAL has no threads")
    run = Run("attach", load, seconds, inventory, root)
    names = [n for n, _ in threads]
    added = []
    rec = Recorder(threads)
    folder = __import__("tempfile").mkdtemp(prefix="ecatsetup-rt-")
    try:
        for c in attach_commands(names):
            try:
                halcmd(*c.split())
            except RtError as e:
                raise RtError(probe_error(str(e)) if c.startswith("loadrt") else str(e))
            added.append(c)
        _measure(run, rec, threads, seconds, load, progress, folder, cancel)
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
