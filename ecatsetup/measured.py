"""What was measured about a device by running it: results of trying a
SYNC0 cycle (the servo period) on it, kept per device (vendor, product).

Two files with the same format: the results that come with EtherCAT Setup
(ecatsetup/measured/sync0.yaml) and the user's own
(~/.config/ecatsetup/measured/sync0.yaml, or $ECATSETUP_MEASURED); they
are added together, the user's winning for the same period. Written from
the Test page ("Remember this period"). The component never reads them.

Only the periods that were tried count: a result "ok at 125 us" and
"refused at 100 us" say nothing about 110 us.
"""

import datetime
import os

import yaml

from . import config, quirks

SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "measured", "sync0.yaml")


def user_file():
    d = os.environ.get("ECATSETUP_MEASURED") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "ecatsetup", "measured")
    return os.path.join(d, "sync0.yaml")


def _read(path):
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or []
    except (OSError, yaml.YAMLError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


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


def describe(res):
    """The results as one line: "250 us ok, 100 us refused (AL 0x0036); 2026-11-16"."""
    if not res:
        return ""
    parts = ["%g us %s%s" % (r["period-us"], r.get("result", "?"),
                             " (AL %s)" % r["al-status"] if r.get("al-status") else "")
             for r in sorted(res, key=lambda r: -r["period-us"])]
    dates = sorted({str(r["date"]) for r in res if r.get("date")})
    return ", ".join(parts) + ("; measured %s" % ", ".join(dates) if dates else "")


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
    if al_status is not None:
        new["al-status"] = "0x%04X" % al_status
    if note:
        new["note"] = note
    entry["results"] = [r for r in entry.get("results") or []
                        if not isinstance(r, dict) or r.get("period-us") != new["period-us"]] + [new]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("# Measured by EtherCAT Setup (Test page, Remember this period). See\n"
                "# ecatsetup/measured/sync0.yaml for what the lines mean.\n")
        yaml.safe_dump(entries, f, sort_keys=False, default_flow_style=None, allow_unicode=True)
    return path
