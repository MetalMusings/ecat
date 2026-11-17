#!/usr/bin/python3
"""The real-time check's poller (docs/development.md "Real-time check").

Reads, every --interval seconds on a CPU that is not a real-time one, the
counters that can explain a late cycle on the real-time CPUs (--cpus) and
writes each one's change with a CLOCK_MONOTONIC time stamp, the clock
LinuxCNC's threads run on:

    # rtpoll 1 <interval ns> <cpus>
    K <index> <key> <label>          a new key (may come at any time)
    <t ns> <index>:<delta> ...       counters that grew since the last line
    <t ns> <index>=<value> ...       values that changed (frequency)
    E <text>                         something could not be read

Counters: interrupts and softirqs on those CPUs, their idle state entries,
thermal throttling, ACPI GPEs and SCIs, and as root the SMI count (MSR
0x34, Intel). Values: the CPUs' current frequency.

It imports nothing from ecatsetup, so that it runs as root with
`sudo python3 -I .../rtpoll.py`: python3 -I ignores the user's environment.
Ends after --seconds, or on SIGTERM / a closed output.
"""

import argparse
import os
import signal
import struct
import sys
import time


def read(path):
    try:
        with open(path, errors="replace") as f:
            return f.read()
    except OSError:
        return None


def cpu_list(text):
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return sorted(set(out))


class Poller:
    def __init__(self, cpus, proc="/proc", sys_="/sys", dev="/dev", smi=False):
        self.cpus = cpus
        self.proc, self.sys, self.dev = proc, sys_, dev
        self.keys = {}          # key -> index
        self.labels = {}
        self.last = {}          # index -> last counter value
        self.values = {}        # index -> last value
        self.out = []           # lines not written yet
        self.smi_fd = None
        self.errors = set()
        if smi:
            try:
                self.smi_fd = os.open(os.path.join(dev, "cpu", "0", "msr"), os.O_RDONLY)
            except OSError as e:
                self.error("SMI count not read: %s (needs root and the msr module)" % e)
        self.idle_paths = []
        for c in cpus:
            base = os.path.join(sys_, "devices/system/cpu/cpu%d/cpuidle" % c)
            try:
                states = sorted(s for s in os.listdir(base) if s.startswith("state"))
            except OSError:
                states = []
            for st in states:
                name = (read(os.path.join(base, st, "name")) or st).strip()
                self.idle_paths.append(("idle:%d:%s" % (c, name), "CPU %d enters idle state %s" % (c, name),
                                        os.path.join(base, st, "usage")))
        self.freq_paths = [("freq:%d" % c, "CPU %d frequency (kHz)" % c,
                            os.path.join(sys_, "devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % c))
                           for c in cpus]
        self.throttle_paths = []
        for c in cpus:
            for kind in ("core", "package"):
                self.throttle_paths.append((
                    "throttle:%d:%s" % (c, kind), "CPU %d %s thermal throttling" % (c, kind),
                    os.path.join(sys_, "devices/system/cpu/cpu%d/thermal_throttle/%s_throttle_count"
                                 % (c, kind))))
        self.acpi_paths = [("gpe:all", "ACPI GPEs (all)", os.path.join(sys_, "firmware/acpi/interrupts/gpe_all")),
                           ("gpe:sci", "ACPI SCIs", os.path.join(sys_, "firmware/acpi/interrupts/sci"))]

    def error(self, text):
        if text not in self.errors:
            self.errors.add(text)
            self.out.append("E %s" % text)

    def index(self, key, label):
        i = self.keys.get(key)
        if i is None:
            i = self.keys[key] = len(self.keys)
            self.labels[key] = label
            self.out.append("K %d %s %s" % (i, key, label))
        return i

    def table(self, text, prefix, what):
        """/proc/interrupts or /proc/softirqs: {key: (label, sum over cpus)}."""
        out = {}
        lines = (text or "").splitlines()
        if not lines:
            return out
        cols = [int(c[3:]) for c in lines[0].split() if c.startswith("CPU")]
        want = [cols.index(c) for c in self.cpus if c in cols]
        for line in lines[1:]:
            head, _, rest = line.partition(":")
            f = rest.split()
            nums = []
            for x in f[:len(cols)]:
                if not x.isdigit():
                    break
                nums.append(int(x))
            if len(nums) < len(cols):
                continue
            name = head.strip()
            tail = f[len(cols):]
            if what == "irq":
                desc = " ".join(tail[2:]) if name.isdigit() and len(tail) > 2 else " ".join(tail)
                label = "IRQ %s (%s)" % (name, desc) if name.isdigit() else "%s: %s" % (name, desc)
            else:
                label = "softirq %s" % name
            out[prefix + name] = (label, sum(nums[i] for i in want))
        return out

    def sample(self):
        counters = {}
        counters.update(self.table(read(os.path.join(self.proc, "interrupts")), "irq:", "irq"))
        counters.update(self.table(read(os.path.join(self.proc, "softirqs")), "softirq:", "softirq"))
        for key, label, path in self.idle_paths + self.throttle_paths + self.acpi_paths:
            v = read(path)
            if v is not None:
                try:
                    counters[key] = (label, int(v.split()[0]))
                except (ValueError, IndexError):
                    pass
        if self.smi_fd is not None:
            try:
                counters["smi"] = ("SMIs (system management interrupts)",
                                   struct.unpack("<Q", os.pread(self.smi_fd, 8, 0x34))[0])
            except OSError as e:
                self.error("SMI count not read: %s" % e)
                self.smi_fd = None
        values = {}
        for key, label, path in self.freq_paths:
            v = read(path)
            if v is not None and v.strip().isdigit():
                values[key] = (label, int(v))
        return counters, values

    def step(self, now_ns):
        counters, values = self.sample()
        parts = []
        for key, (label, v) in counters.items():
            i = self.index(key, label)
            prev = self.last.get(i)
            self.last[i] = v
            if prev is not None and v != prev:
                parts.append("%d:%d" % (i, v - prev))
        for key, (label, v) in values.items():
            i = self.index(key, label)
            if self.values.get(i) != v:
                self.values[i] = v
                parts.append("%d=%d" % (i, v))
        self.out.append("%d %s" % (now_ns, " ".join(parts)) if parts else "%d" % now_ns)

    def flush(self, f):
        if self.out:
            f.write("\n".join(self.out) + "\n")
            f.flush()
            self.out = []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cpus", required=True, help="the real-time CPUs, as 3 or 2-3")
    ap.add_argument("--interval", type=float, default=0.01)
    ap.add_argument("--seconds", type=float, default=3600.0)
    ap.add_argument("--smi", action="store_true", help="read the SMI count (root)")
    ap.add_argument("--affinity", help="CPUs to run on (default: every CPU but --cpus)")
    ap.add_argument("--out", help="file to write (default: standard output)")
    a = ap.parse_args(argv)
    cpus = cpu_list(a.cpus)
    try:
        aff = cpu_list(a.affinity) if a.affinity else sorted(os.sched_getaffinity(0) - set(cpus))
        if aff:
            os.sched_setaffinity(0, aff)
    except OSError:
        pass
    stop = []
    signal.signal(signal.SIGTERM, lambda *x: stop.append(1))
    signal.signal(signal.SIGINT, lambda *x: stop.append(1))
    f = open(a.out, "w") if a.out else sys.stdout
    p = Poller(cpus, smi=a.smi)
    interval = int(a.interval * 1e9)
    f.write("# rtpoll 1 %d %s\n" % (interval, ",".join(str(c) for c in cpus)))
    start = time.monotonic_ns()
    nxt = start
    try:
        while not stop and time.monotonic_ns() - start < a.seconds * 1e9:
            p.step(time.monotonic_ns())
            p.flush(f)
            nxt += interval
            d = nxt - time.monotonic_ns()
            if d > 0:
                time.sleep(d / 1e9)
            else:
                nxt = time.monotonic_ns()
    except BrokenPipeError:
        pass
    finally:
        try:
            p.flush(f)
            if f is not sys.stdout:
                f.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
