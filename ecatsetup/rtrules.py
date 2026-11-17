"""The real-time check's rule catalogue (docs/development.md "Real-time
check", "The rule catalogue"): what the check knows, as data.

Rules are YAML files in ecatsetup/rtrules/ and the user's
~/.config/ecatsetup/rtrules/ ($ECATSETUP_RTRULES replaces both, for tests).
A rule:

    id: nic-irq-on-rt-cpu
    title: ...
    each: irqs                  # optional: the rule is tried on every item
    fallback: true              # optional (with each): only for items no other
                                # rule of the same `each` matched
    when: "item.on_rt and item.io_nic"   # an expression over the evidence
    evidence: "IRQ {item.irq} ({item.name}) {item.rate_rt:.0f}/s on CPU {rt_cpus_text}"
    keys: ["irq:{item.irq}"]    # counters it explains (for "unexplained")
    gain: high | medium | low
    cause: ...
    action: {kind: runtime | permanent | bios | advice | measure, text, commands: [...],
             risk, cost}
    effect: ...                 # what should change
    check: ...                  # which measure must show it
    undo: {text, commands: [...]}
    source: [{text, url}]
    seen-here: "where we measured it" or null
    tests: {present: {...}, absent: {...}}   # evidence that makes it fire, and not

Texts and commands are templates: {name}, {item.field}, {x:.1f}. The
expressions are a small, safe subset of Python (no names but the evidence's,
no attributes but its keys, only the functions in FUNCTIONS), evaluated
here; a name that is not there is None, and a comparison with None is false.
"""

import ast
import glob
import operator
import os
import re

import yaml

from . import rtcheck

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rtrules")
USER = os.path.expanduser("~/.config/ecatsetup/rtrules")

KINDS = ("measure", "runtime", "permanent", "bios", "advice")
GAINS = ("high", "medium", "low")


class RuleError(Exception):
    pass


# --------------------------------------------------------------------------
# expressions

def _regex(text, pattern):
    return bool(text is not None and re.search(pattern, str(text)))


FUNCTIONS = {
    "len": lambda x: len(x) if x is not None else 0,
    "any": lambda x: any(x or []),
    "all": lambda x: all(x or []),
    "min": lambda *a: min(*a) if len(a) > 1 else min(a[0]),
    "max": lambda *a: max(*a) if len(a) > 1 else max(a[0]),
    "re": _regex,
    "set": lambda x: set(x or []),
}

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.BitAnd: operator.and_, ast.BitOr: operator.or_, ast.Mod: operator.mod}
_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
        ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.In: lambda a, b: b is not None and a in b, ast.NotIn: lambda a, b: b is None or a not in b,
        ast.Is: operator.is_, ast.IsNot: operator.is_not}


def compile_expr(text):
    try:
        tree = ast.parse(str(text), mode="eval")
    except SyntaxError as e:
        raise RuleError("%r: %s" % (text, e))
    for node in ast.walk(tree):
        ok = isinstance(node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub,
                               ast.BinOp, ast.Compare, ast.Name, ast.Load, ast.Attribute, ast.Subscript,
                               ast.Constant, ast.Call, ast.List, ast.Tuple, ast.IfExp)) or \
            type(node) in _BIN or type(node) in _CMP
        if not ok:
            raise RuleError("%r: %s is not allowed" % (text, type(node).__name__))
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise RuleError("%r: %s is not allowed" % (text, node.attr))
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS):
            raise RuleError("%r: only the functions %s" % (text, ", ".join(sorted(FUNCTIONS))))
    return tree.body


def _get(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    if isinstance(obj, (list, tuple)) and isinstance(key, int) and -len(obj) <= key < len(obj):
        return obj[key]
    return None


def evaluate(node, ctx):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in ("True", "False", "None"):
            return {"True": True, "False": False, "None": None}[node.id]
        return ctx.get(node.id)
    if isinstance(node, ast.Attribute):
        return _get(evaluate(node.value, ctx), node.attr)
    if isinstance(node, ast.Subscript):
        return _get(evaluate(node.value, ctx), evaluate(node.slice, ctx))
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            v = True
            for x in node.values:
                v = evaluate(x, ctx)
                if not v:
                    return v
            return v
        v = False
        for x in node.values:
            v = evaluate(x, ctx)
            if v:
                return v
        return v
    if isinstance(node, ast.UnaryOp):
        v = evaluate(node.operand, ctx)
        if isinstance(node.op, ast.Not):
            return not v
        return -v if v is not None else None
    if isinstance(node, ast.BinOp):
        a, b = evaluate(node.left, ctx), evaluate(node.right, ctx)
        try:
            return _BIN[type(node.op)](a, b)
        except (TypeError, ZeroDivisionError):
            return None
    if isinstance(node, ast.Compare):
        left = evaluate(node.left, ctx)
        for op, right_node in zip(node.ops, node.comparators):
            right = evaluate(right_node, ctx)
            try:
                if not _CMP[type(op)](left, right):
                    return False
            except TypeError:
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        args = [evaluate(a, ctx) for a in node.args]
        try:
            return FUNCTIONS[node.func.id](*args)
        except (TypeError, ValueError):
            return None
    if isinstance(node, (ast.List, ast.Tuple)):
        return [evaluate(x, ctx) for x in node.elts]
    if isinstance(node, ast.IfExp):
        return evaluate(node.body, ctx) if evaluate(node.test, ctx) else evaluate(node.orelse, ctx)
    raise RuleError("cannot evaluate %s" % type(node).__name__)


_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)(:[^{}]*)?\}")


KEEP_FORMS = ("sysctl", "modprobe", "once", "file", "boot")


def fill_keep(keep, ctx):
    """An action's keep: (how a run-time change is made to last; the rule's
    kernel-param is the other way) with its templates filled."""
    if not keep:
        return None
    out = {}
    for k, v in keep.items():
        if isinstance(v, str):
            out[k] = fill(v, ctx)
        elif isinstance(v, list):
            out[k] = [fill(x, ctx) for x in v]
        elif isinstance(v, dict):
            out[k] = {kk: (fill(vv, ctx) if isinstance(vv, str) else [fill(x, ctx) for x in vv]
                           if isinstance(vv, list) else vv) for kk, vv in v.items()}
        else:
            out[k] = v
    return out


def fill(template, ctx):
    """A template with {name}, {item.field} and {x:.1f} filled from ctx;
    an unknown name stays as "?"."""
    if template is None:
        return None

    def sub(m):
        v = ctx
        for part in m.group(1).split("."):
            if part.startswith("_"):
                return "?"
            v = _get(v, part)
        if v is None:
            return "?"
        if isinstance(v, (list, tuple)):
            v = rtcheck.list_text(v) if all(isinstance(x, int) for x in v) else ", ".join(str(x) for x in v)
        try:
            return format(v, m.group(2)[1:]) if m.group(2) else str(v)
        except (ValueError, TypeError):
            return str(v)
    return _FIELD.sub(sub, str(template))


# --------------------------------------------------------------------------
# the catalogue

class Rule:
    FIELDS = ("id", "title", "each", "fallback", "when", "evidence", "keys", "gain", "cause", "action",
              "effect", "check", "undo", "source", "seen-here", "tests")

    def __init__(self, d, path):
        unknown = set(d) - set(self.FIELDS)
        if unknown:
            raise RuleError("%s: rule %s: unknown keys %s" % (path, d.get("id"), ", ".join(sorted(unknown))))
        for k in ("id", "title", "when", "cause", "action", "gain", "source"):
            if not d.get(k):
                raise RuleError("%s: rule %s has no %s" % (path, d.get("id"), k))
        self.d = d
        self.path = path
        self.id = d["id"]
        self.each = d.get("each")
        self.fallback = bool(d.get("fallback"))
        self.when = compile_expr(d["when"])
        a = d["action"]
        if a.get("kind") not in KINDS:
            raise RuleError("%s: rule %s: action kind must be one of %s" % (path, self.id, ", ".join(KINDS)))
        if d["gain"] not in GAINS:
            raise RuleError("%s: rule %s: gain must be one of %s" % (path, self.id, ", ".join(GAINS)))
        if a["kind"] == "runtime" and a.get("commands") and not (d.get("undo") or {}).get("commands"):
            raise RuleError("%s: rule %s: a run-time change needs undo commands" % (path, self.id))
        if a["kind"] == "permanent" and not (d.get("undo") or {}).get("text"):
            raise RuleError("%s: rule %s: a permanent change needs undo text" % (path, self.id))
        keep = a.get("keep")
        if keep is not None:
            forms = [k for k in keep if k in KEEP_FORMS]
            unknown = set(keep) - set(KEEP_FORMS) - {"note", "once-undo"}
            if a["kind"] != "runtime" or len(forms) != 1 or unknown:
                raise RuleError("%s: rule %s: keep is for a run-time change, with one of %s (and a note)"
                                % (path, self.id, ", ".join(KEEP_FORMS)))
            if "once" in keep and not keep.get("once-undo"):
                raise RuleError("%s: rule %s: keep once needs once-undo" % (path, self.id))
            if "file" in keep and not (isinstance(keep["file"], dict) and keep["file"].get("path")
                                       and keep["file"].get("text")):
                raise RuleError("%s: rule %s: keep file needs a path and a text" % (path, self.id))

    def __getitem__(self, k):
        return self.d.get(k)

    def __repr__(self):
        return "Rule(%s)" % self.id


def load(dirs=None):
    """The rules, by id; a user's file replaces a shipped rule of the same id."""
    if dirs is None:
        env = os.environ.get("ECATSETUP_RTRULES")
        dirs = env.split(os.pathsep) if env else [HERE, USER]
    rules = {}
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            with open(path) as f:
                try:
                    data = yaml.safe_load(f) or []
                except yaml.YAMLError as e:
                    raise RuleError("%s: %s" % (path, e))
            for x in data:
                r = Rule(x, path)
                rules[r.id] = r
    return rules


# --------------------------------------------------------------------------
# evidence: what the inventory and a run show, as names the rules use

def evidence(inv, run=None):
    """The context the rules are evaluated in."""
    d = inv.data
    cmd = rtcheck.cmdline_params(d.get("cmdline") if isinstance(d.get("cmdline"), str) else "")
    cpus = d.get("cpus") or []
    if run is not None and run.rt_cpus:
        rt_cpus, guessed = list(run.rt_cpus), False
    elif d.get("rt_cpu"):
        rt_cpus, guessed = list(d["rt_cpu"]), False
    else:
        # where LinuxCNC (uspace) would put them: the last isolated CPU, else the last CPU
        rt_cpus, guessed = ((d.get("isolated") or [])[-1:] or cpus[-1:]), True
    nonrt = [c for c in cpus if c not in rt_cpus]
    smt = d.get("smt") or {}
    siblings = sorted({s for c in rt_cpus for s in smt.get(str(c), [c]) if s not in rt_cpus})
    io = d.get("io") or []
    io_nics = d.get("io_nics") or []
    mesa = any("hm2_eth" in x for x in io)
    ctx = {
        "rt": d.get("preempt_rt"), "vm": d.get("vm"), "battery": d.get("battery"),
        "smt": any(len(v) > 1 for v in smt.values()), "cpus": cpus, "ncpus": len(cpus),
        "isolated": d.get("isolated") or [], "nohz_full": d.get("nohz_full") or [],
        "rcu_nocbs": d.get("rcu_nocbs") or [], "isolcpus_flags": d.get("isolcpus_flags") or [],
        "irqaffinity": d.get("irqaffinity") or [],
        "rt_cpus": rt_cpus, "rt_cpus_guessed": guessed, "rt_cpus_text": rtcheck.list_text(rt_cpus),
        "nonrt": nonrt, "nonrt_text": rtcheck.list_text(nonrt), "rt_siblings": siblings,
        "rt_siblings_text": rtcheck.list_text(siblings),
        "rt_isolated": bool(rt_cpus) and set(rt_cpus) <= set(d.get("isolated") or []),
        "irqbalance": d.get("irqbalance"), "networkmanager": d.get("networkmanager"),
        "cpu_vendor": d.get("cpu_vendor") or "", "turbo": d.get("turbo"),
        "cmd": cmd, "cmdline": d.get("cmdline") if isinstance(d.get("cmdline"), str) else "",
        "graphics": d.get("graphics") or [], "modules": d.get("modules") or [],
        "aspm_policy": (inv.fact("aspm_policy").value if inv.fact("aspm_policy") else None),
        "sysctl": d.get("sysctl") or {}, "snd_power_save": d.get("snd_power_save"),
        "nvme": d.get("nvme"), "default_route": d.get("default_route"),
        "io": io, "io_nics": io_nics, "mesa_eth": mesa,
        "ethercat": any("EtherCAT" in x for x in io), "parport": any("parport" in x for x in io),
        "pstate": inv.fact("pstate").value if inv.fact("pstate") else None,
        "last_cpu": cpus[-1] if cpus else 0,
    }
    sc = d.get("sysctl") or {}
    ctx.update(stat_interval=sc.get("vm.stat_interval"), timer_migration=sc.get("kernel.timer_migration"),
               thp=sc.get("thp"))
    if inv.fact("turbo") and inv.fact("turbo").source.endswith("no_turbo"):
        ctx.update(turbo_off_cmd="echo 1 > /sys/devices/system/cpu/intel_pstate/no_turbo",
                   turbo_on_cmd="echo 0 > /sys/devices/system/cpu/intel_pstate/no_turbo")
    else:
        ctx.update(turbo_off_cmd="echo 0 > /sys/devices/system/cpu/cpufreq/boost",
                   turbo_on_cmd="echo 1 > /sys/devices/system/cpu/cpufreq/boost")
    govs = d.get("governor") or {}
    ctx["governor_rt"] = govs.get(str(rt_cpus[0])) if rt_cpus else None
    ctx["governors"] = govs
    ctx["governor_restore"] = "; ".join(
        "echo %s > /sys/devices/system/cpu/cpu%s/cpufreq/scaling_governor" % (g, c)
        for c, g in sorted(govs.items(), key=lambda x: int(x[0])))

    # idle states on the real-time CPUs
    idle = (d.get("cpuidle") or {})
    ctx["idle_driver"] = idle.get("driver")
    deep = []
    for c in rt_cpus:
        for st in (idle.get("states") or {}).get(str(c), []):
            if not st["disabled"] and st["latency_us"] > 2:
                deep.append({"cpu": c, "state": st["state"], "name": st["name"], "latency_us": st["latency_us"],
                             "path": "/sys/devices/system/cpu/cpu%d/cpuidle/%s/disable" % (c, st["state"])})
    ctx["deep_states"] = deep
    ctx["deep_states_text"] = ", ".join("%s (%d us)" % (x["name"], x["latency_us"]) for x in deep)
    ctx["deepest_idle_us"] = max([x["latency_us"] for x in deep] or [0])
    ctx["disable_deep_cmds"] = "; ".join("echo 1 > %s" % x["path"] for x in deep)
    ctx["enable_deep_cmds"] = "; ".join("echo 0 > %s" % x["path"] for x in deep)

    # the measurement
    measured = run is not None and bool(run.stats)
    ctx.update(measured=measured, root=bool(run and run.root), load=run.load if run else None,
               seconds=(run.stats[0].seconds if measured else 0), way=run.way if run else None)
    period = None
    threads = d.get("threads") or []
    for name, p in threads:
        if name == "servo-thread":
            period = p
    servo = base = None
    if measured:
        by = sorted(run.stats, key=lambda s: s.period)
        servo = next((s for s in by if s.thread == "servo-thread"), by[-1])
        base = next((s for s in by if s is not servo), None)
        period = servo.period
    ctx["period_us"] = period / 1000.0 if period else None
    budgets = {b.thread: b for b in (run.budgets() if measured else [])}
    if servo is not None:
        m = servo.summary()
        b = budgets.get(servo.thread)
        ctx.update(late_max_us=m.get("late_max_us"), late_p9999_us=m.get("late_p9999_us"),
                   moved_max_us=m.get("max_abs_us"), left_share=b.share_left if b else None,
                   left_us=b.left_us if b else None, thread_us=b.thread_us if b else None,
                   late_share=(m.get("late_max_us") or 0) / (servo.period / 1000.0))
        funcs = []
        for f, us in ((run.times.get(servo.thread) or {}).get("funcs") or {}).items():
            if us is not None:
                funcs.append({"name": f, "tmax_us": us, "share": us / (servo.period / 1000.0)})
        ctx["funcs"] = sorted(funcs, key=lambda x: -x["tmax_us"])
    else:
        ctx.update(late_max_us=None, late_p9999_us=None, moved_max_us=None, left_share=None, left_us=None,
                   thread_us=None, late_share=None, funcs=[])
    if base is not None:
        m = base.summary()
        bb = budgets.get(base.thread)
        ctx.update(base_period_us=base.period / 1000.0, base_late_max_us=m.get("late_max_us"),
                   base_left_share=bb.share_left if bb else None)
    else:
        bp = [p for n, p in threads if n == "base-thread"]
        ctx.update(base_period_us=bp[0] / 1000.0 if bp else None, base_late_max_us=None, base_left_share=None)

    # counters during the run (per second on the real-time CPUs), and the spikes against them
    counters = ((run.poll or {}).get("counters") or {}) if run else {}
    ctx["counters"] = {k: v["per_s"] for k, v in counters.items()}
    corr = {}
    spikes = 0
    for th, c in ((run.correlations or {}).items() if run else []):
        if not c:
            continue
        if servo is not None and th == servo.thread:
            spikes = c["spikes"]
        for x in c["counters"]:
            if rtcheck.strong(x) and x["key"] not in corr:
                corr[x["key"]] = x
    ctx["spikes"] = spikes
    ctx["suspects"] = sorted(corr)
    ctx["suspect_items"] = [corr[k] for k in sorted(corr)]
    ctx["smi_measured"] = bool(run and run.smi_measured)
    ctx["smi_count"] = (counters.get("smi") or {}).get("total", 0)
    ctx["smi_suspect"] = "smi" in corr
    ctx["gpe_rate"] = (counters.get("gpe:all") or {}).get("per_s", 0.0)
    ctx["gpe_suspect"] = "gpe:all" in corr or "gpe:sci" in corr
    ctx["throttle_run"] = sum(v["total"] for k, v in counters.items() if k.startswith("throttle:"))
    ctx["freq_suspect"] = any(k.startswith("freq:") for k in corr)
    ctx["idle_suspect"] = any(k.startswith("idle:") for k in corr)
    ctx["loc_rate"] = (counters.get("irq:LOC") or {}).get("per_s", 0.0)
    ctx["rcu_rate"] = (counters.get("softirq:RCU") or {}).get("per_s", 0.0)
    rtla = (run.rtla if run else None) or {}
    ctx["rtla_thread_max_us"] = max([v["thread_max_us"] for v in (rtla.get("cpus") or {}).values()] or [None]) \
        if rtla.get("cpus") else None
    ctx["rtla_analysis"] = rtla.get("analysis") or ""

    # GPEs: per GPE, its rate during the run, else since boot
    gpes = []
    since = d.get("gpes") or {}
    up = d.get("uptime") or 0
    for g, n in since.items():
        rate = (run.gpe_rates or {}).get(g) if run and run.gpe_rates else None
        gpes.append({"name": g, "number": g[3:], "count": n, "rate": rate if rate is not None else
                     (n / up if up else 0.0), "during_run": rate is not None})
    ctx["gpes"] = sorted(gpes, key=lambda x: -x["rate"])
    ctx["top_gpe"] = ctx["gpes"][0] if ctx["gpes"] else None

    # interrupts
    irqs = []
    rows = (d.get("interrupts") or {}).get("rows") or []
    cols = (d.get("interrupts") or {}).get("cpus") or []
    for row in rows:
        if not row["irq"].isdigit():
            continue
        aff = row.get("affinity") or []
        on_rt = bool(set(aff) & set(rt_cpus))
        key = "irq:" + row["irq"]
        if measured and counters:
            rate = (counters.get(key) or {}).get("per_s", 0.0)
        else:
            n = sum(c for i, c in enumerate(row["counts"]) if i < len(cols) and cols[i] in rt_cpus)
            rate = n / up if up else 0.0
        name = row["name"]
        nic = None
        for x in d.get("nics") or []:
            if re.search(r"(^|[^\w])%s($|[^\w])" % re.escape(x["name"]), name) or \
                    (x.get("pci") and x["pci"] in row.get("chip", "")):
                nic = x["name"]
        irqs.append({
            "irq": row["irq"], "name": name, "chip": row.get("chip", ""), "affinity": aff,
            "affinity_text": rtcheck.list_text(aff), "on_rt": on_rt, "rate_rt": rate, "key": key,
            "nic": nic, "io_nic": nic in io_nics if nic else False,
            # MSI-X queue vectors spread one per CPU are managed by the kernel:
            # their affinity cannot be written (isolcpus=managed_irq keeps them off)
            "managed": bool(re.search(r"-(TxRx|rx|tx|txrx|fp|comp)-?\d+$|^nvme\d+q\d+", name)) and len(aff) == 1,
            "suspect": key in corr,
        })
    ctx["irqs"] = irqs

    # network cards
    nics = []
    for x in d.get("nics") or []:
        rx = x.get("rx_usecs")
        nics.append({"name": x["name"], "driver": x.get("driver"), "io": x["name"] in io_nics,
                     "rx_usecs": int(rx) if rx and str(rx).isdigit() else None,
                     "eee_on": bool(x.get("eee")) and str(x.get("eee")).startswith("enabled"),
                     "aspm_on": any((x.get("aspm") or {}).values()),
                     "default_route": x["name"] == d.get("default_route"),
                     "up": x.get("operstate") == "up"})
    ctx["nics"] = nics
    return ctx


# --------------------------------------------------------------------------
# findings

class Finding:
    def __init__(self, rule, ctx, item=None):
        c = dict(ctx)
        if item is not None:
            c["item"] = item
        self.rule = rule
        self.item = item
        a = rule["action"] or {}
        u = rule["undo"] or {}
        self.id = rule.id
        self.title = fill(rule["title"], c)
        self.evidence = fill(rule["evidence"], c)
        self.cause = fill(rule["cause"], c)
        self.kind = a.get("kind")
        self.gain = rule["gain"]
        self.action = fill(a.get("text"), c)
        self.commands = [fill(x, c) for x in a.get("commands") or []]
        self.kernel_param = fill(a.get("kernel-param"), c)
        self.keep = fill_keep(a.get("keep"), c)
        if self.keep is None and self.kind == "runtime" and self.commands and not self.kernel_param:
            # kept after a restart by running the same commands at each start
            self.keep = {"boot": True}
        self.risk = fill(a.get("risk"), c)
        self.cost = fill(a.get("cost"), c)
        self.effect = fill(rule["effect"], c)
        self.check = fill(rule["check"], c)
        self.undo = fill(u.get("text"), c)
        self.undo_commands = [fill(x, c) for x in u.get("commands") or []]
        self.keys = [fill(x, c) for x in rule["keys"] or []]
        self.source = rule["source"] or []
        self.seen = rule["seen-here"]

    def rank(self):
        k = KINDS.index(self.kind)
        late = 1 if self.kind in ("bios", "advice") else 0
        return (late, GAINS.index(self.gain), k)

    def to_json(self):
        return {x: getattr(self, x) for x in ("id", "title", "evidence", "cause", "kind", "gain", "action",
                                              "commands", "kernel_param", "risk", "cost", "effect", "check", "undo",
                                              "undo_commands", "keys", "source", "seen")}

    def text(self):
        out = ["%s  [%s, gain %s]" % (self.title, self.kind, self.gain)]
        if self.evidence:
            out.append("  Seen: " + self.evidence)
        out.append("  Why: " + " ".join(self.cause.split()))
        if self.action:
            out.append("  What to do: " + " ".join(self.action.split()))
        for c in self.commands:
            out.append("    $ " + c)
        if self.kernel_param:
            out.append("  Kernel parameter%s: %s" % (" (to keep it)" if self.kind == "runtime" else "",
                                                     self.kernel_param))
        if self.risk:
            out.append("  Risk: " + " ".join(self.risk.split()))
        if self.cost:
            out.append("  Cost: " + " ".join(self.cost.split()))
        if self.check:
            out.append("  Check: " + " ".join(self.check.split()))
        if self.undo or self.undo_commands:
            out.append("  Undo: " + " ".join((self.undo or "").split()))
            for c in self.undo_commands:
                out.append("    $ " + c)
        for s in self.source:
            out.append("  Source: %s%s" % (s.get("text", ""), " (%s)" % s["url"] if s.get("url") else ""))
        out.append("  Seen here: %s" % (self.seen or "no, from the documentation"))
        return "\n".join(out)


class Unexplained:
    """A counter that grew with the spikes, which no rule explains."""
    kind, gain, id = "advice", "medium", "unexplained"

    def __init__(self, c, ctx):
        self.c = c
        self.title = "Spikes came with %s, and no rule knows it" % c["label"]
        self.evidence = "%d of %d spikes, by chance about %.0f %%" % (c["with"], c["of"], 100 * c["expected"])
        self.keys = [c["key"]]
        self.commands, self.undo_commands = [], []
        # the same fields as a Finding, for whatever lists findings
        self.item = self.kernel_param = self.keep = self.check = None
        self.action = self.what()

    def what(self):
        if self.c["key"].startswith("irq:"):
            return "move that interrupt off the real-time CPU (the general action), and look up its driver"
        return "find out what drives this counter; tell the developers, with the report"

    def rank(self):
        return (1, 1, KINDS.index("advice"))

    def to_json(self):
        return {"id": self.id, "title": self.title, "evidence": self.evidence, "kind": self.kind,
                "gain": self.gain, "keys": self.keys}

    def text(self):
        return "%s  [advice]\n  Seen: %s\n  What to do: %s" % (self.title, self.evidence, self.what())


def merge_same(found):
    """One finding for the items of a rule that say the same with nothing to
    run per item (five queue interrupts of one network card on the wrong
    CPU): the evidence and keys of all, the first one's item."""
    out = []
    by = {}
    for f in found:
        if f.item is None or f.commands or f.undo_commands:
            out.append(f)
            continue
        k = (f.id, f.title, f.action, f.kernel_param)
        if k in by:
            first = by[k]
            if f.evidence and f.evidence not in (first.evidence or ""):
                first.evidence = "%s; %s" % (first.evidence, f.evidence) if first.evidence else f.evidence
            first.keys += [x for x in f.keys if x not in first.keys]
            first.items.append(f.item)
            continue
        f.items = [f.item]
        by[k] = f
        out.append(f)
    return out


def analyse(ctx, rules=None):
    """The findings for this evidence, ranked: [Finding or Unexplained]."""
    rules = load() if rules is None else rules
    found = []
    matched = {}            # each -> set of item indexes a specific rule matched
    order = sorted(rules.values(), key=lambda r: r.fallback)
    for r in order:
        if r.each:
            items = ctx.get(r.each) or []
            for i, item in enumerate(items):
                if r.fallback and i in matched.get(r.each, set()):
                    continue
                c = dict(ctx)
                c["item"] = item
                if evaluate(r.when, c):
                    found.append(Finding(r, ctx, item))
                    if not r.fallback:
                        matched.setdefault(r.each, set()).add(i)
        elif evaluate(r.when, ctx):
            found.append(Finding(r, ctx))
    found = merge_same(found)
    explained = set()
    for f in found:
        for k in f.keys:
            explained.add(k)
    for c in ctx.get("suspect_items") or []:
        if not any(c["key"] == k or (k.endswith(":") and c["key"].startswith(k)) for k in explained):
            found.append(Unexplained(c, ctx))
    found.sort(key=lambda f: f.rank())
    return found


def report(findings, ctx):
    out = []
    if not findings:
        if ctx.get("measured"):
            out.append("No rule applies: nothing the check knows of stands in the way at this period.")
        else:
            out.append("No rule applies to the inventory alone; measure to see more.")
    for i, f in enumerate(findings, 1):
        out.append("%d. %s" % (i, f.text()))
    return "\n".join(out)
