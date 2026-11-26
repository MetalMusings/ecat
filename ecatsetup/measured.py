"""What was measured about a device by running it: results of trying a
SYNC0 cycle (the servo period) on it, kept per device (vendor, product).

Two files with the same format: the results that come with EtherCAT Setup
(ecatsetup/measured/sync0.yaml) and the user's own
(~/.config/ecatsetup/measured/sync0.yaml, or $ECATSETUP_MEASURED); they
are added together, the user's winning for the same period. Written from
the Live tab ("Remember this period"). The component never reads them.

Only the periods that were tried count: a result "ok at 125 us" and
"refused at 100 us" say nothing about 110 us.
"""

import copy
import datetime
import glob
import os

import yaml

from . import quirks

SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "measured", "sync0.yaml")


def user_file():
    d = os.environ.get("ECATSETUP_MEASURED") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "ecatsetup", "measured")
    return os.path.join(d, "sync0.yaml")


_cache = {}     # path -> ((mtime, size), entries): the window checks every slave after each change


def _read(path):
    try:
        st = os.stat(path)
    except OSError:
        return []
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit is not None and hit[0] == stamp:
        return copy.deepcopy(hit[1])
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or []
    except (OSError, yaml.YAMLError):
        return []
    out = [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    _cache[path] = (stamp, out)
    return copy.deepcopy(out)


def _key(entry):
    try:
        m = entry["match"]
        return int(str(m["vendor"]), 16), int(str(m["product"]), 16)
    except (KeyError, TypeError, ValueError):
        return None


def results(slave):
    """[{period-us, result, ...}] for a configuration's slave (or a
    (vendor, product)), by period; {} for none. The user's results win for
    the same period."""
    key = slave if isinstance(slave, tuple) else quirks.identity(slave)
    if not key:
        return []
    by_period = {}
    for path in (SHIPPED, user_file()):          # the user's last: it replaces
        for entry in _read(path):
            if _key(entry) != key:
                continue
            for r in entry.get("results") or []:
                try:
                    by_period[float(r["period-us"])] = dict(r, **{"period-us": float(r["period-us"])})
                except (KeyError, TypeError, ValueError):
                    continue
    return [by_period[p] for p in sorted(by_period)]


def _us(ns):
    return ns / 1000.0


def verdict(res, period_ns):
    """(level, text) for a servo period against the results, or None when
    nothing was measured. level: "ok", "unknown" (between what was tried) or
    "bad" (at or below a period that was refused)."""
    if not res or not period_ns:
        return None
    p = _us(period_ns)
    oks = [r["period-us"] for r in res if r.get("result") == "ok"]
    refused = [r for r in res if r.get("result") == "refused"]
    lo_ok = min(oks) if oks else None
    bad = [r for r in refused if p <= r["period-us"] and (lo_ok is None or r["period-us"] < lo_ok)]
    if bad:
        r = max(bad, key=lambda r: r["period-us"])
        code = " (AL %s)" % r["al-status"] if r.get("al-status") else ""
        return "bad", "refused at %g us%s: the servo period %g us is not above it" % (r["period-us"], code, p)
    if lo_ok is not None and p >= lo_ok:
        return "ok", ("tried ok at this period" if p == lo_ok else
                      "tried ok at %g us, the shortest that worked; this period is longer" % lo_ok)
    if lo_ok is not None:
        below = [r["period-us"] for r in refused if r["period-us"] < lo_ok]
        gap = "between %g and %g us" % (max(below), lo_ok) if below else "below %g us" % lo_ok
        return "unknown", "not tried %s (%g us is the shortest that worked)" % (gap, lo_ok)
    return "unknown", "no period tried ok yet"


def revision(slave):
    """The revision of a configuration's slave as an int, or None."""
    try:
        return int(str((slave.get("identity") or {}).get("revision")), 16)
    except (AttributeError, TypeError, ValueError):
        return None


def _rev(r):
    try:
        return int(str(r.get("revision")), 16)
    except (TypeError, ValueError):
        return None


def other_revisions(slave, res):
    """The revisions (ints) some results were measured on that differ from the slave's:
    a new revision may behave differently. Results without a revision say nothing."""
    mine = revision(slave)
    if mine is None:
        return []
    return sorted({_rev(r) for r in res if _rev(r) not in (None, mine)})


def describe(res):
    """The results as one line: "250 us ok, 100 us refused (AL 0x0036); 2026-11-16"."""
    if not res:
        return ""
    parts = ["%g us %s%s" % (r["period-us"], r.get("result", "?"),
                             " (AL %s)" % r["al-status"] if r.get("al-status") else "")
             for r in sorted(res, key=lambda r: -r["period-us"])]
    dates = sorted({str(r["date"]) for r in res if r.get("date")})
    return ", ".join(parts) + ("; measured %s" % ", ".join(dates) if dates else "")


def problems(slave, period_ns):
    """[(level, text)] about a slave's servo period against what was measured
    on this kind of device ("warning" when it was refused at or above the
    period, "note" when the period lies between the ones that were tried)."""
    res = results(slave)
    v = verdict(res, period_ns)
    out = []
    if v is not None and v[0] != "ok":
        out.append(("warning" if v[0] == "bad" else "note", "this device was measured: " + v[1]))
    other = other_revisions(slave, res)
    if other:
        out.append(("note", "measured on revision %s; this slave is 0x%08X, so the result may differ"
                    % (", ".join("0x%08X" % r for r in other), revision(slave))))
    return out


def line(slave, period_ns):
    """The slave page's line about what was measured, or "" when nothing was."""
    res = results(slave)
    if not res:
        return ""
    v = verdict(res, period_ns)
    other = other_revisions(slave, res)
    return "Measured on the bus: %s%s%s" % (
        describe(res), "" if v is None else " (%s)" % v[1],
        "; some on revision %s, this one is 0x%08X: check it again" % (
            ", ".join("0x%08X" % r for r in other), revision(slave)) if other else "")


def record(slave, period_ns, result, al_status=None, note="", today=None):
    """Add a result for a configuration's slave to the user's file (the same
    period replaces its result); returns the path."""
    key = quirks.identity(slave)
    if not key:
        raise ValueError("the slave has no vendor and product")
    if result not in ("ok", "refused"):
        raise ValueError("result is ok or refused")
    path = user_file()
    entries = _read(path)
    entry = next((e for e in entries if _key(e) == key), None)
    if entry is None:
        entry = {"name": str(slave.get("device") or slave.get("name") or "device"),
                 "match": {"vendor": "0x%08X" % key[0], "product": "0x%08X" % key[1]}, "results": []}
        entries.append(entry)
    new = {"period-us": round(_us(period_ns), 3), "result": result,
           "date": (today or datetime.date.today()).isoformat()}
    if new["period-us"] == int(new["period-us"]):
        new["period-us"] = int(new["period-us"])
    if revision(slave) is not None:
        new["revision"] = "0x%08X" % revision(slave)
    if al_status is not None:
        new["al-status"] = "0x%04X" % al_status
    if note:
        new["note"] = note
    entry["results"] = [r for r in entry.get("results") or []
                        if not isinstance(r, dict) or r.get("period-us") != new["period-us"]] + [new]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("# Measured by EtherCAT Setup (Live tab, Remember this period). See\n"
                "# ecatsetup/measured/sync0.yaml for what the lines mean.\n")
        yaml.safe_dump(entries, f, sort_keys=False, default_flow_style=None, allow_unicode=True, width=1000)
    return path


def report(today=None):
    """What device data there is, as text for `ecatsetup --device-data`: quirk files and
    measured results, the user's and those that came with the program, and what looks
    wrong: files that could not be read, results without a revision or older than a
    year, a user quirk file that hides a shipped one, a user result that differs from a
    shipped result for the same period."""
    today = today or datetime.date.today()
    lines, notes = [], []
    quirks.load(reload=True)
    lines.append("Quirk files (pins, settings, limits, HAL lines):")
    shipped = {os.path.basename(p) for p in glob.glob(os.path.join(quirks.SHIPPED, "*.yaml"))}
    for label_, d in (("yours", quirks.user_dir()), ("shipped", quirks.SHIPPED)):
        names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "*.yaml")))
        lines.append("  %s (%s): %s" % (label_, d, ", ".join(names) or "none"))
    def matches(d):
        out = {}
        for path in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            try:
                with open(path) as f:
                    out[quirks.parse(yaml.safe_load(f), path).get("match")] = os.path.basename(path)
            except (OSError, yaml.YAMLError, TypeError, ValueError, AttributeError):
                pass
        return out
    theirs = matches(quirks.SHIPPED)
    for key, name in matches(quirks.user_dir()).items():
        if key in theirs:
            notes.append("your %s replaces the shipped %s for the same device: what only the "
                         "shipped file says is not used" % (name, theirs[key]))
    for pr in quirks.problems:
        notes.append("quirk file not read: " + pr)
    lines.append("Measured SYNC0 results:")
    for label_, path in (("shipped", SHIPPED), ("yours", user_file())):
        entries = _read(path)
        lines.append("  %s (%s): %s" % (label_, path, "" if entries else "none"))
        for e in entries:
            res = [r for r in e.get("results") or [] if isinstance(r, dict)]
            revs = sorted({str(r.get("revision")) for r in res if r.get("revision")})
            lines.append("    %s: %s" % (e.get("name", "?"), describe(
                [dict(r, **{"period-us": float(r.get("period-us", 0))}) for r in res])))
            if res and len(revs) == 0:
                notes.append("%s: no result says which revision it was measured on" % e.get("name"))
            for r in res:
                try:
                    age = (today - datetime.date.fromisoformat(str(r.get("date")))).days
                except ValueError:
                    notes.append("%s %s us: no valid date" % (e.get("name"), r.get("period-us")))
                    continue
                if age > 365:
                    notes.append("%s %s us: measured %d days ago, check it again"
                                 % (e.get("name"), r.get("period-us"), age))
    shipped_by = {}
    for e in _read(SHIPPED):
        for r in e.get("results") or []:
            shipped_by[(_key(e), r.get("period-us"))] = r.get("result")
    for e in _read(user_file()):
        for r in e.get("results") or []:
            old = shipped_by.get((_key(e), r.get("period-us")))
            if old and old != r.get("result"):
                notes.append("%s %s us: you found %s, the shipped result is %s (yours is used)"
                             % (e.get("name"), r.get("period-us"), r.get("result"), old))
    if notes:
        lines.append("To look at:")
        lines += ["  - " + n for n in notes]
    else:
        lines.append("Nothing to look at.")
    return "\n".join(lines)
