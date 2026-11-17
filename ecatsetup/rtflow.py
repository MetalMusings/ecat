"""The real-time check's guided work (docs/development.md "Real-time check",
"The guided workflow" and "Changes, and taking them back"), without Qt: the
window's Timing page and `ecatsetup --rt-check --improve` both use it.

The steps:

1. inventory;
2. baseline: two runs made the same way, so that the noise is known;
3. analysis: the findings of the latest run that stands for the computer as
   it is now (the reference);
4. one change (Improve mode, run-time changes only: kernel parameters and
   the BIOS are advice here);
5. measure again the same way; compare with the reference, the noise taken
   into account; keep the change or undo it;
6. back to 3, until the budget is ample and nothing of high gain is left, or
   nothing is left that the check can do.

Every change is an entry in ~/.local/state/ecatsetup/rt-changes.yaml: what,
the commands, the undo commands, when, the runs before and after and the
verdict. Taking back is by hand (the user's choice): the undo script
(rt-undo.sh beside it) has every undo command of what is still in force,
newest first, to be read and run by the user; the window also offers Undo
per change. The flow's own state (which runs are the baseline and the
reference, the change being tried) is in rt-flow.json, so that it survives
the window being closed.

Commands run as `sudo -n sh -c COMMAND`: the window never asks for a
password. When sudo needs one, the caller shows the commands for a terminal,
and the user says when they have run them (`record_by_hand`).
"""

import json
import os
import shlex
import subprocess
import time

import yaml

from . import rtcheck, rtrules

def state_dir():
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "ecatsetup")


def changes_path():
    return os.path.join(state_dir(), "rt-changes.yaml")


def flow_path():
    return os.path.join(state_dir(), "rt-flow.json")


def undo_script_path():
    return os.path.join(state_dir(), "rt-undo.sh")


class SudoError(Exception):
    def __init__(self, text, needs_password=False):
        super().__init__(text)
        self.needs_password = needs_password


def sudo(command, runner=subprocess.run):
    """Run one shell command as root with sudo -n. Returns its output;
    raises SudoError (needs_password when sudo would have asked)."""
    try:
        p = runner(["sudo", "-n", "sh", "-c", command], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise SudoError("could not run sudo: %s" % e)
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        if "password is required" in err or "a terminal is required" in err:
            raise SudoError("sudo needs a password here", needs_password=True)
        raise SudoError(err or "failed (exit %d)" % p.returncode)
    return p.stdout


def can_sudo(runner=subprocess.run):
    try:
        sudo("true", runner)
        return True
    except SudoError:
        return False


# --------------------------------------------------------------------------
# the change log

class Change:
    FIELDS = ("id", "rule", "title", "item", "commands", "undo_commands", "kernel_param", "keep", "state",
              "tried", "ended", "run_before", "run_after", "verdict", "by_hand", "error", "check")

    def __init__(self, **kw):
        for f in self.FIELDS:
            setattr(self, f, kw.get(f))
        self.commands = self.commands or []
        self.undo_commands = self.undo_commands or []

    def to_dict(self):
        return {f: getattr(self, f) for f in self.FIELDS if getattr(self, f) is not None}

    @property
    def in_force(self):
        return self.state in ("tried", "kept")

    def text(self):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.tried)) if self.tried else "?"
        out = "%s  [%s, %s%s]" % (self.title, self.state, when, ", by hand" if self.by_hand else "")
        if self.verdict:
            out += "\n  measured: %s" % self.verdict
        return out


def item_key(finding):
    """What a finding is about, so that the same change is not offered twice."""
    it = finding.item or {}
    return str(it.get("irq") or it.get("name") or "")


class ChangeLog:
    def __init__(self, path=None):
        self.path = path or changes_path()
        self.changes = []
        try:
            with open(self.path) as f:
                data = yaml.safe_load(f) or {}
            self.changes = [Change(**c) for c in data.get("changes") or []]
        except (OSError, yaml.YAMLError):
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write("# The real-time check's changes to this computer (EtherCAT Setup).\n"
                    "# Undo by hand: the undo commands, newest first, or %s\n"
                    % undo_script_path())
            yaml.safe_dump({"changes": [c.to_dict() for c in self.changes]}, f, sort_keys=False,
                           allow_unicode=True, width=100)
        write_undo_script(self)

    def find(self, cid):
        return next((c for c in self.changes if c.id == cid), None)

    def active_for(self, rule, item):
        return next((c for c in self.changes if c.in_force and c.rule == rule and (c.item or "") == item), None)

    def add(self, finding, run_before=None, by_hand=False):
        c = Change(id="c%d-%d" % (int(time.time()), len(self.changes) + 1), rule=finding.id, title=finding.title,
                   item=item_key(finding), commands=list(finding.commands),
                   undo_commands=list(finding.undo_commands), kernel_param=finding.kernel_param,
                   keep=getattr(finding, "keep", None), state="tried", tried=time.time(), run_before=run_before, by_hand=by_hand or None,
                   check=finding.check)
        self.changes.append(c)
        return c


def try_change(log, finding, run_before=None, runner=subprocess.run):
    """Make a run-time change: its commands with sudo, one after the other.
    If one fails, the ones before it are undone (the finding's undo commands
    as far as they go: they are in the same order) and SudoError is raised;
    nothing is logged then."""
    if finding.kind != "runtime" or not finding.commands:
        raise SudoError("this finding has no run-time change to make")
    if log.active_for(finding.id, item_key(finding)):
        raise SudoError("this change is in force already")
    done = 0
    try:
        for cmd in finding.commands:
            sudo(cmd, runner)
            done += 1
    except SudoError:
        for cmd in finding.undo_commands[:done]:
            try:
                sudo(cmd, runner)
            except SudoError:
                pass
        raise
    c = log.add(finding, run_before)
    log.save()
    return c


def record_by_hand(log, finding, run_before=None):
    """The user ran the commands in a terminal (sudo needed a password)."""
    c = log.add(finding, run_before, by_hand=True)
    log.save()
    return c


def undo_change(log, change, runner=subprocess.run, by_hand=False):
    if not change.in_force:
        raise SudoError("this change is not in force")
    if not by_hand:
        for cmd in change.undo_commands:
            sudo(cmd, runner)
    change.state = "undone"
    change.ended = time.time()
    log.save()


def keep_change(log, change, verdict=None):
    change.state = "kept"
    if verdict:
        change.verdict = verdict
    log.save()


def write_undo_script(log, path=None):
    path = path or undo_script_path()
    live = [c for c in log.changes if c.in_force]
    lines = ["#!/bin/sh",
             "# Takes back the real-time check's changes that are still in force, newest first.",
             "# Written by EtherCAT Setup %s. Read it before you run it: sudo sh %s"
             % (time.strftime("%Y-%m-%d %H:%M"), path),
             "# Run-time changes are gone after a restart anyway; this is for the running system.",
             "set -x"]
    for c in reversed(live):
        lines.append("")
        lines.append("# %s (%s)" % (c.title, time.strftime("%Y-%m-%d %H:%M", time.localtime(c.tried or 0))))
        lines += c.undo_commands or ["# (nothing to run: %s)" % (c.kernel_param or "no undo command")]
    if not live:
        lines.append("# nothing is in force")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------
# comparing runs

def same_way(a, b):
    return a.way == b.way and a.load == b.load and abs(a.seconds - b.seconds) <= 0.25 * max(a.seconds, b.seconds)


def compare_runs(before, after, noise_=None):
    """[(thread, {metric: (before, after, verdict)}, verdict)] and one word for
    all: better, worse, mixed or "no change"."""
    out = []
    words = set()
    for a in before.stats:
        b = next((x for x in after.stats if x.thread == a.thread), None)
        if b is None or not a.n or not b.n:
            continue
        c = rtcheck.compare(a.summary(), b.summary(), (noise_ or {}).get(a.thread))
        late = [c[m][2] for m in ("late_p9999_us", "late_max_us") if m in c]
        v = "better" if "better" in late and "worse" not in late else \
            "worse" if "worse" in late and "better" not in late else \
            "mixed" if late and "better" in late else "no change"
        words.add(v)
        out.append((a.thread, c, v))
    if not words:
        return out, "not comparable"
    if words == {"no change"}:
        return out, "no change"
    if "worse" in words and "better" not in words:
        return out, "worse"
    if "better" in words and "worse" not in words:
        return out, "better"
    return out, "mixed"


def comparison_text(rows, word):
    lines = ["Compared with the run before: %s" % word]
    names = {"late_p9999_us": "late, 99.99 %", "late_max_us": "late, largest", "p9999_us": "moved, 99.99 %",
             "max_abs_us": "moved, largest", "p99_us": "moved, 99 %"}
    for thread, c, v in rows:
        lines.append("  %s: %s" % (thread, v))
        for m in ("late_p9999_us", "late_max_us"):
            if m in c:
                a, b, vv = c[m]
                lines.append("    %-16s %7.1f -> %7.1f us  %s" % (names[m], a, b, vv))
    return "\n".join(lines)


def noise_of(a, b):
    """{thread: noise} of two runs made the same way."""
    out = {}
    for x in a.stats:
        y = next((s for s in b.stats if s.thread == x.thread), None)
        if y is not None and x.n and y.n:
            out[x.thread] = rtcheck.noise(x.summary(), y.summary())
    return out


# --------------------------------------------------------------------------
# the flow

STEPS = ("Inventory", "Baseline", "Findings", "One change", "Measure again", "Done")


class Flow:
    """Where the guided work is. Runs are kept as saved files (Run.save)."""

    def __init__(self, path=None, log=None):
        self.path = path or flow_path()
        self.log = log or ChangeLog()
        self.inventory = None
        self.ini = None
        self.baseline = []          # run files
        self.reference = None       # the run file that stands for the computer as it is now
        self.latest = None          # the latest run file
        self.pending = None         # the change being tried (id), waiting for its measurement
        self.history = []           # [{"time", "text"}]: what happened, for the report
        self._runs = {}
        self.load()

    # -- state

    def load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.ini = d.get("ini")
        self.baseline = [p for p in d.get("baseline", []) if os.path.exists(p)]
        self.reference = d.get("reference") if d.get("reference") and os.path.exists(d["reference"]) else None
        self.latest = d.get("latest") if d.get("latest") and os.path.exists(d["latest"]) else None
        self.pending = d.get("pending") if self.log.find(d.get("pending") or "") else None
        self.history = d.get("history", [])

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"ini": self.ini, "baseline": self.baseline, "reference": self.reference,
                       "latest": self.latest, "pending": self.pending, "history": self.history}, f, indent=1)

    def note(self, text):
        self.history.append({"time": time.time(), "text": text})

    def reset(self):
        """Start again from the inventory (the change log stays)."""
        self.baseline, self.reference, self.latest, self.pending = [], None, None, None
        self.note("started again")
        self.save()

    def run(self, path):
        if path not in self._runs:
            self._runs[path] = rtcheck.Run.load_file(path)
        return self._runs[path]

    @property
    def noise(self):
        if len(self.baseline) >= 2:
            return noise_of(self.run(self.baseline[0]), self.run(self.baseline[1]))
        return None

    # -- what happened

    def take_inventory(self, ini=None, roots=None):
        self.ini = ini
        self.roots = roots
        self.inventory = rtcheck.inventory(roots, ini=ini)
        return self.inventory

    def refresh_inventory(self):
        if self.inventory is not None:
            self.inventory = rtcheck.inventory(getattr(self, "roots", None), ini=self.ini)

    def add_run(self, run, path=None):
        """A measurement finished: save it and move the flow on. Returns the
        comparison (rows, word) with the reference when there is one."""
        path = path or run.save()
        self._runs[path] = run
        self.latest = path
        result = None
        if self.pending:
            ref = self.run(self.reference) if self.reference else None
            if ref is not None and same_way(ref, run):
                result = compare_runs(ref, run, self.noise)
                ch = self.log.find(self.pending)
                ch.run_after = path
                ch.verdict = result[1]
                self.log.save()
                self.note("measured after %s: %s" % (ch.title, result[1]))
            else:
                self.note("measured after the change, but not the same way as before: not compared")
        elif len(self.baseline) < 2:
            if self.baseline and not same_way(self.run(self.baseline[0]), run):
                self.baseline = []
            self.baseline.append(path)
            self.reference = path
            self.note("baseline run %d" % len(self.baseline))
            if len(self.baseline) == 2:
                result = compare_runs(self.run(self.baseline[0]), run)
        else:
            ref = self.run(self.reference) if self.reference else None
            if ref is not None and same_way(ref, run):
                result = compare_runs(ref, run, self.noise)
            self.reference = path
            self.note("measured again")
        self.save()
        return result

    def tried(self, change):
        self.pending = change.id
        self.note("tried: %s" % change.title)
        self.refresh_inventory()
        self.save()

    def keep(self):
        ch = self.log.find(self.pending)
        keep_change(self.log, ch)
        if ch.run_after:
            self.reference = ch.run_after
        self.pending = None
        self.note("kept: %s (%s)" % (ch.title, ch.verdict or "not measured"))
        self.save()

    def undo(self, runner=subprocess.run, by_hand=False):
        ch = self.log.find(self.pending)
        undo_change(self.log, ch, runner, by_hand=by_hand)
        self.pending = None
        self.note("undone: %s (%s)" % (ch.title, ch.verdict or "not measured"))
        self.refresh_inventory()
        self.save()

    # -- what to do next

    def findings(self):
        """The findings of the reference run (or of the inventory alone)."""
        run = self.run(self.reference) if self.reference else None
        # the computer as it is now (after a change), else as the run saw it
        inv = self.inventory or (run.inventory if run is not None else None)
        if inv is None:
            return [], {}
        ctx = rtrules.evidence(inv, run)
        return rtrules.analyse(ctx), ctx

    def tryable(self, f):
        """True when the Improve mode can make this finding's change now."""
        return (getattr(f, "kind", None) == "runtime" and bool(getattr(f, "commands", None))
                and self.pending is None and not self.log.active_for(f.id, item_key(f)))

    def step(self):
        """(index into STEPS, the text saying what to do now)."""
        if self.inventory is None:
            return 0, "Read what the computer is."
        if len(self.baseline) < 2 and not self.pending:
            return 1, ("Measure %s, the way the machine is used: the first run is the baseline, "
                       "the second shows how much the numbers move by chance." %
                       ("once more, the same way" if self.baseline else "twice"))
        if self.pending:
            ch = self.log.find(self.pending)
            if ch.run_after:
                return 4, "Measured after \"%s\": %s. Keep it or undo it." % (ch.title, ch.verdict)
            return 4, "Measure again the same way, to see what \"%s\" did." % ch.title
        found, ctx = self.findings()
        b = [x for x in (self.run(self.reference).budgets() if self.reference else [])]
        ample = bool(b) and all(x.share_left >= 0.5 for x in b)
        high = [f for f in found if f.gain == "high" and f.kind in ("runtime", "permanent")]
        tryable = [f for f in found if self.tryable(f)]
        if ample and not high:
            return 5, ("%s left of the period, and nothing of high gain left to try: no more changes "
                       "are needed (the rest is optional). Measure beside LinuxCNC while the machine "
                       "is used to confirm." %
                       ", ".join("%.0f %% (%s)" % (100 * x.share_left, x.thread) for x in b))
        if not tryable:
            return 5, ("Nothing is left that the check can change at run time; the findings below "
                       "say what is left (kernel parameters, BIOS, advice) and why.")
        return 2, ("Choose one change from the findings (most to gain first), try it, then measure "
                   "again the same way.")


# --------------------------------------------------------------------------
# the report

def report(flow):
    """The check's report, as Markdown: what the user saw, did and got,
    including what the check could not explain. Only reads."""
    out = ["# Real-time check report", "",
           "Written %s by EtherCAT Setup on %s." % (time.strftime("%Y-%m-%d %H:%M"), os.uname().nodename), ""]
    inv = flow.inventory or (flow.run(flow.latest).inventory if flow.latest else None)
    if inv is not None:
        out += ["## The computer", "", "```", rtcheck.inventory_text(inv), "```", ""]
    runs = []
    for p in flow.baseline + [flow.reference, flow.latest] + [c.run_after for c in flow.log.changes] + \
            [c.run_before for c in flow.log.changes]:
        if p and p not in runs and os.path.exists(p):
            runs.append(p)
    if runs:
        out += ["## Runs", ""]
        for p in sorted(runs, key=lambda x: flow.run(x).started or 0):
            r = flow.run(p)
            tag = []
            if p in flow.baseline:
                tag.append("baseline")
            if p == flow.reference:
                tag.append("reference")
            out += ["### %s%s" % (os.path.basename(p), " (%s)" % ", ".join(tag) if tag else ""), "",
                    "```", r.text(), "```", ""]
            if r.rtla and r.rtla.get("text"):
                out += ["rtla's own output:", "", "```", r.rtla["text"].strip(), "```", ""]
    n = flow.noise
    if n:
        out += ["## The noise (two baseline runs)", ""]
        for th, v in n.items():
            out.append("- %s: lateness 99.99 %% %.0f us, largest %.1f us" % (th, v["late_p9999_us"], v["late_max_us"]))
        out.append("")
    if flow.log.changes:
        out += ["## Changes", ""]
        for c in flow.log.changes:
            out.append("- " + c.text().replace("\n", "\n  "))
            for cmd in c.commands:
                out.append("  - `%s`" % cmd)
            if c.undo_commands:
                out.append("  - undo: " + "; ".join("`%s`" % x for x in c.undo_commands))
        out.append("")
    found, ctx = flow.findings()
    out += ["## Findings now", ""]
    out += ["```", rtrules.report(found, ctx), "```", ""]
    unexplained = [f for f in found if f.id == "unexplained"]
    if unexplained:
        out += ["## Not explained by any rule", ""]
        out += ["- %s (%s)" % (f.title, f.evidence) for f in unexplained]
        out.append("")
    if flow.history:
        out += ["## What was done, in order", ""]
        out += ["- %s %s" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(h["time"])), h["text"])
                for h in flow.history]
        out.append("")
    step, text = flow.step()
    out += ["## Where the check stands", "", "Step %d of %d, %s: %s" % (step + 1, len(STEPS), STEPS[step], text), ""]
    return "\n".join(out)


def default_report_name():
    return "rt-check-report-%s.md" % time.strftime("%Y%m%d-%H%M")


def shell_line(cmd):
    """How a user types a command as root in a terminal."""
    return "sudo sh -c %s" % shlex.quote(cmd)
