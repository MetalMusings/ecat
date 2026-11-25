"""What a problem seen in the event log means, and which of its usual causes
this computer has, has not, or has not been checked for.

The symptoms are data (ecatsetup/explain/*.yaml; a user's own in
~/.config/ecatsetup/explain/ replace a shipped one of the same id), with the
real-time check's safe expressions (rtrules.compile_expr). Each symptom has
a `when` over what the event log shows, a meaning in plain words and its
causes in order; each cause is judged against this computer:

- likely: the computer has it (a kernel master loaded, EEE on the card);
- possible: a known risk is there (a Realtek card), not proof;
- ruled out: checked, and it is not that;
- not checked: nothing here tells; `check` says where to find out.

The context is built from the event log (events.Log), the masters on the
computer (report.master_facts), the computer as the real-time check reads it
(rtcheck.inventory, through rtrules.evidence) and, when the slaves were
examined, their port error counters (diagnose.Report). Every part is
optional: what is missing leaves its causes "not checked".

Each symptom carries its own tests (present/absent contexts, and an
expected state per cause), run by tests/test_explain.py.
"""

import glob
import os
import re

import yaml

from . import events, rtrules

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "explain")
USER = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                    "ecatsetup", "explain")

STATES = ("likely", "possible", "ruled out", "not checked")
MARKS = {"likely": "[x]", "possible": "[?]", "ruled out": "[ok]", "not checked": "[ ]"}


class ExplainError(Exception):
    pass


class Cause:
    FIELDS = ("id", "title", "likely", "possible", "ruled-out", "evidence", "check", "fix", "look",
              "tests")

    def __init__(self, d, where):
        unknown = set(d) - set(self.FIELDS)
        if unknown:
            raise ExplainError("%s: cause %s: unknown keys %s" % (where, d.get("id"),
                                                                   ", ".join(sorted(unknown))))
        for k in ("id", "title"):
            if not d.get(k):
                raise ExplainError("%s: a cause without %s" % (where, k))
        self.d = d
        self.id = d["id"]
        self.exprs = {k: rtrules.compile_expr(d[k]) for k in ("likely", "possible", "ruled-out")
                      if d.get(k) is not None}

    def judge(self, ctx):
        """(state, evidence text) of this cause in ctx."""
        for key, state in (("likely", "likely"), ("possible", "possible"), ("ruled-out", "ruled out")):
            x = self.exprs.get(key)
            if x is not None and rtrules.evaluate(x, ctx):
                # the evidence says what was found; a ruled-out cause needs none
                return state, rtrules.fill(self.d.get("evidence"), ctx) if state != "ruled out" else None
        return "not checked", None


class Symptom:
    FIELDS = ("id", "title", "when", "evidence", "meaning", "causes", "tests")

    def __init__(self, d, path):
        unknown = set(d) - set(self.FIELDS)
        if unknown:
            raise ExplainError("%s: symptom %s: unknown keys %s" % (path, d.get("id"),
                                                                     ", ".join(sorted(unknown))))
        for k in ("id", "title", "when", "meaning", "causes"):
            if not d.get(k):
                raise ExplainError("%s: symptom %s has no %s" % (path, d.get("id"), k))
        self.d, self.path, self.id = d, path, d["id"]
        self.when = rtrules.compile_expr(d["when"])
        where = "%s: %s" % (path, self.id)
        self.causes = [Cause(c, where) for c in d["causes"]]
        ids = [c.id for c in self.causes]
        if len(set(ids)) != len(ids):
            raise ExplainError("%s: two causes with one id" % where)

    def __getitem__(self, k):
        return self.d.get(k)


def load(dirs=None):
    """The symptoms in file order; a user's file replaces a shipped symptom
    of the same id."""
    if dirs is None:
        env = os.environ.get("ECATSETUP_EXPLAIN")
        dirs = env.split(os.pathsep) if env else [HERE, USER]
    out = {}
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            with open(path) as f:
                try:
                    data = yaml.safe_load(f) or []
                except yaml.YAMLError as e:
                    raise ExplainError("%s: %s" % (path, e))
            for x in data:
                s = Symptom(x, path)
                out[s.id] = s
    return list(out.values())


# --------------------------------------------------------------------------
# the context

_SKIPPED = re.compile(r"\bSKIPPED(?: (\d+) times?)?")
_UNMATCHED = re.compile(r"(?:(\d+) datagrams? )?UNMATCHED")
_TIMED_OUT = re.compile(r"(?:(\d+) datagrams? )?TIMED OUT")


def _count(rx, texts):
    n = 0
    for t in texts:
        m = rx.search(t.text)
        if m:
            n += int(m.group(1)) if m.group(1) else 1
    return n


def from_log(log, names=None):
    """What the event log shows, as names the symptoms use. Events the
    log explains as the slaves still coming up (events.annotate) are not
    counted."""
    if log is None:
        return {"log": False}
    evs = log.events
    sev = events.annotate(evs, names)
    running = [e for e in evs if sev[e.index][0] != "info"]
    texts = log.texts or []
    late = sum(1 + e.b for e in evs if e.type == events.LATE)
    blocks = [e for e in evs if e.type == events.DRIVE_BLOCK]
    ctx = {
        "log": True,
        "period_us": log.period_ns / 1000.0 if log.period_ns else None,
        "wc_zero": sum(1 for e in running if e.type == events.WC and e.a == 0),
        "wc_incomplete": sum(1 for e in running if e.type == events.WC and e.a == 1),
        "link_down": sum(1 for e in evs if e.type == events.LINK and not e.a),
        "offline": sum(1 for e in running if e.type == events.SLAVE_ONLINE and not e.a),
        "late": late,
        "turnaround_3": sum(1 for e in evs if e.type == events.SHIFT and e.b > 2),
        "drives_shut": sum(1 for e in blocks if e.a == 2),
        "skipped": _count(_SKIPPED, texts),
        "unmatched": _count(_UNMATCHED, texts),
        "timed_out": _count(_TIMED_OUT, texts),
        "messages": texts is not None and log.texts is not None,
    }
    ctx["lost"] = ctx["skipped"] + ctx["unmatched"] + ctx["timed_out"]
    # long-term monitoring: frames really lost (the master's tx - rx), the port
    # errors counted during the run, and how many of the faults came right
    # after a late servo cycle
    totals = events.counter_totals(log)
    ctx["frame_stats"] = events.frame_stats(log)
    ctx["frames_lost"] = totals.get((-1, events.MON_FRAMES_LOST << 8), 0)
    run_errs = {}
    for (slave, code), n in totals.items():
        kind, port = code >> 8, code & 0xFF
        if slave >= 0 and kind in (1, 2, 3) and n:
            run_errs.setdefault((slave, port), {})[("rx", "phy", "lost")[kind - 1]] = n
    ctx["port_errors_run"] = [{"slave": s, "port": p, "counts": c} for (s, p), c in sorted(run_errs.items())]
    ctx["port_errors_run_text"] = "; ".join("slave %d port %d: %s" % (
        e["slave"], e["port"], ", ".join("%s +%d" % kv for kv in sorted(e["counts"].items())))
        for e in ctx["port_errors_run"])
    lates = [e.time for e in evs if e.type == events.LATE]
    faults = [e.time for e in running if e.type == events.WC and e.a in (0, 1)] + \
        [e.time for e in evs if e.type == events.MON_ONSET and e.slave < 0]
    ctx["faults"] = len(faults)
    ctx["faults_after_late"] = sum(1 for t in faults if any(0 <= t - x <= 2.0 for x in lates))
    return ctx


def from_masters(f):
    """report.master_facts as names."""
    if f is None:
        return {"masters_read": False, "kernel_master": None, "hal_running": None}
    bits = []
    if f["device"]:
        bits.append("/dev/EtherCAT0 exists")
    if f["modules"]:
        bits.append("modules " + ", ".join(f["modules"]))
    if f["active"] == "active":
        bits.append("ethercat.service is active")
    return {"masters_read": True, "kernel_master": f["kernel_master"],
            "kernel_master_text": "; ".join(bits) or "not loaded",
            "kernel_service_enabled": f["enabled"] == "enabled",
            "free_masters": len(f["free"]), "hal_running": f["rtapi"]}


def from_computer(inv, interface=None):
    """The real-time check's evidence, and the EtherCAT card in it (`nic`):
    the configuration's master.interface, else the I/O card the INI names."""
    if inv is None:
        return {"computer_read": False}
    ctx = dict(rtrules.evidence(inv))
    ctx["computer_read"] = True
    want = interface or next(iter(ctx.get("io_nics") or []), None)
    nic = next((n for n in ctx.get("nics") or [] if n["name"] == want), None)
    ctx["nic"] = nic
    ctx["nic_name"] = want
    ctx["nic_irq_on_rt"] = any(x["on_rt"] for x in ctx.get("irqs") or [] if x.get("nic") == want) \
        if nic else None
    drops = {k: v for k, v in ((nic or {}).get("rx_drops") or {}).items() if v}
    ctx["nic_dropped"] = sum(drops.values())
    ctx["nic_dropped_text"] = ", ".join("%s %d" % kv for kv in sorted(drops.items()))
    return ctx


def from_reports(reports):
    """Port error counters of examined slaves (diagnose.Report): `port_errors`
    lists the ports whose rx, phy or lost-link counters are not zero."""
    if not reports:
        return {"ports_read": False, "port_errors": [], "port_errors_text": ""}
    errs = []
    for r in reports:
        for p in r.ports:
            bad = {k: p.now[k] for k in ("rx", "phy", "lost") if p.now.get(k)}
            if bad:
                errs.append({"slave": r.position, "port": p.number, "counts": bad})
    return {"ports_read": True, "port_errors": errs,
            "port_errors_text": "; ".join("slave %d port %d: %s" % (
                e["slave"], e["port"], ", ".join("%s %d" % kv for kv in sorted(e["counts"].items())))
                for e in errs)}


def context(log=None, names=None, masters=None, inv=None, interface=None, reports=None):
    ctx = {}
    ctx.update(from_computer(inv, interface))
    ctx.update(from_masters(masters))
    ctx.update(from_reports(reports))
    ctx.update(from_log(log, names))
    # the cable cause's evidence: only the parts that say something
    parts = [("during the run: " + ctx["port_errors_run_text"]) if ctx.get("port_errors_run_text") else "",
             ("error counters: " + ctx["port_errors_text"]) if ctx.get("port_errors_text") else "",
             "link down %d times" % ctx["link_down"] if ctx.get("link_down") else "",
             "slaves offline %d times while running" % ctx["offline"] if ctx.get("offline") else ""]
    ctx["cable_text"] = "; ".join(p for p in parts if p)
    return ctx


# --------------------------------------------------------------------------
# findings

class Finding:
    def __init__(self, symptom, ctx):
        self.symptom = symptom
        self.id = symptom.id
        self.title = rtrules.fill(symptom["title"], ctx)
        self.evidence = " ".join(rtrules.fill(symptom["evidence"], ctx).split()) if symptom["evidence"] else ""
        self.meaning = " ".join(str(symptom["meaning"]).split())
        self.causes = []
        for c in symptom.causes:
            state, ev = c.judge(ctx)
            self.causes.append({
                "id": c.id, "title": rtrules.fill(c.d["title"], ctx), "state": state,
                "evidence": " ".join((ev or "").split()),
                "check": " ".join(str(rtrules.fill(c.d.get("check"), ctx) or "").split()),
                "fix": " ".join(str(rtrules.fill(c.d.get("fix"), ctx) or "").split()),
                "look": c.d.get("look")})
        # the likely ones first, then possible, then not checked, ruled out last
        order = {"likely": 0, "possible": 1, "not checked": 2, "ruled out": 3}
        self.causes.sort(key=lambda c: order[c["state"]])

    def __repr__(self):
        return "Finding(%s)" % self.id


def findings(ctx, symptoms=None):
    """The symptoms whose `when` holds in ctx."""
    out = []
    for s in symptoms if symptoms is not None else load():
        if rtrules.evaluate(s.when, ctx):
            out.append(Finding(s, ctx))
    return out


def text_lines(found):
    """Findings as plain text (report, command line)."""
    out = []
    for f in found:
        out.append(f.title)
        if f.evidence:
            out.append("  Seen: " + f.evidence)
        out.append("  " + f.meaning)
        out.append("  Causes, the likely ones first ([x] likely, [?] possible, [ ] not checked, "
                   "[ok] ruled out):")
        for c in f.causes:
            line = "  %-4s %s" % (MARKS[c["state"]], c["title"])
            if c["evidence"]:
                line += ": " + c["evidence"]
            out.append(line)
            if c["state"] in ("likely", "possible") and c["fix"]:
                out.append("         What to do: " + c["fix"])
            elif c["state"] == "not checked" and c["check"]:
                out.append("         How to check: " + c["check"])
        out.append("")
    return out


def html(found, colours=None):
    """Findings as rich text for the window; colours: {state: "#rrggbb"}."""
    import html as h
    colours = colours or {"likely": "#c0392b", "possible": "#b7791f", "ruled out": "#2f855a",
                          "not checked": "#718096"}
    out = []
    for f in found:
        out.append("<p><b>%s</b>%s<br>%s</p>" % (
            h.escape(f.title), "<br><i>Seen: %s</i>" % h.escape(f.evidence) if f.evidence else "",
            h.escape(f.meaning)))
        out.append("<table cellspacing='0' cellpadding='3'>")
        for c in f.causes:
            more = c["fix"] if c["state"] in ("likely", "possible") else \
                c["check"] if c["state"] == "not checked" else ""
            label = {"likely": "What to do: ", "possible": "What to do: ",
                     "not checked": "How to check: "}.get(c["state"], "")
            out.append("<tr><td valign='top' style='color:%s'><b>%s</b></td><td>%s%s%s</td></tr>" % (
                colours[c["state"]], h.escape(c["state"]), h.escape(c["title"]),
                ": " + h.escape(c["evidence"]) if c["evidence"] else "",
                "<br><span style='color:%s'>%s%s</span>" % (colours["not checked"], label, h.escape(more))
                if more else ""))
        out.append("</table>")
    return "".join(out)
