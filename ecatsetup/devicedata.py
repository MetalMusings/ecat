"""Shared device data (PROTOTYPE, docs/development.md "Shared device data").

Quirk files written from the window, fact by fact, each with its source; shared
through a data repository on GitHub (a Discussion per file, checked and taken in
by a GitHub Action, comments and reports there); fetched only when the user asks
and chosen one fact at a time. No Qt here: the window (gui.DeviceDataDialog) and
the repository's Action (devices-repo/tools) both use it.

A *fact* is one thing a quirk file says, addressed by a path: ("commands",
"0x2010"), ("settings", "0x2009:00"), ("homing", "input-bits", "IN5"). Facts
are the unit of layering (shipped, then the user's own), of sources, of
comparing a shared file with what is in use, and of choosing.
"""

import copy
import datetime
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request

import yaml

from . import quirks

FORMAT = 1
# the prototype's repository; the real one will be MetalMusings/ecat-devices
REPO = os.environ.get("ECATSETUP_DEVICES_REPO") or "MetalMusings/ecat-devices-test"
CATEGORY = "general"
LICENCE = "CC0 1.0 (https://creativecommons.org/publicdomain/zero/1.0/)"

# what a shared file may hold: the device's facts, never the machine's choices
SHARE_KEYS = (quirks.KEYS | {"format", "sources"}) - {"pins"}
SOURCE_KINDS = ("manual", "tried", "shared", "other")
RESULTS = ("worked", "partly", "did not work")
MAX_FILE = 32 * 1024
MAX_TEXT = 2000

# HAL in a shared file: plain logic only (decided 2026-11-26)
HAL_COMPONENTS = {"and2", "or2", "xor2", "not", "edge", "oneshot", "debounce", "timedelay",
                  "mux2", "scale", "comp", "lowpass", "flipflop", "toggle", "sum2", "limit1"}
_INSTANCE = re.compile(r"\{name\}-[A-Za-z0-9_-]+")
_OWN_PIN = re.compile(r"(\{name\}-[A-Za-z0-9_-]+)\.[A-Za-z0-9_.-]+")
_SLAVE = re.compile(r"\{(pin|signal):0x[0-9A-Fa-f]{4}(:[0-9A-Fa-f]{1,2})?\}")
_VALUE = re.compile(r"(TRUE|FALSE|true|false|[-+]?(0x[0-9A-Fa-f]+|[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?))")


class Error(ValueError):
    pass


# -- facts

def _obj_key(k):
    """A quirk file's object key as text in one form: 0x2009:00 / 0x2010."""
    text = "0x%X" % k if isinstance(k, int) else str(k)
    index, _, sub = text.partition(":")
    try:
        return "0x%04X:%02X" % (int(index, 16), int(sub, 16)) if sub else "0x%04X" % int(index, 16)
    except ValueError:
        return text


def facts(raw):
    """The fact paths a quirk file's content holds, in file order."""
    out = []
    raw = raw or {}
    for k in raw.get("commands") or {}:
        out.append(("commands", _obj_key(k)))
    for k in raw.get("limits") or {}:
        out.append(("limits", str(k)))
    for k in raw.get("pins") or {}:
        out.append(("pins", _obj_key(k)))
    for st in raw.get("settings") or []:
        if isinstance(st, dict) and "object" in st:
            out.append(("settings", _obj_key(st["object"])))
    for k in ("hal", "lcec"):
        if raw.get(k) is not None:
            out.append((k,))
    h = raw.get("homing") or {}
    for k in ("methods", "enable-again", "inputs"):
        if h.get(k) is not None:
            out.append(("homing", k))
    for k in ("describe", "extras"):
        for n in h.get(k) or {}:
            out.append(("homing", k, str(n)))
    for n in h.get("input-bits") or {}:
        out.append(("homing", "input-bits", str(n)))
    for k in raw.get("tuning") or {}:
        out.append(("tuning", _obj_key(k)))
    for k in (raw.get("measure") or {}).get("ignore") or {}:
        out.append(("measure", "ignore", _obj_key(k)))
    return out


def key(path):
    """A fact's key in `sources:`: its path as one string."""
    return " ".join(path)


def _find(d, k):
    """The key in mapping d that means k (object keys in any form)."""
    if k in d:
        return k
    return next((x for x in d if _obj_key(x) == k or str(x) == k), None)


def get(raw, path):
    """A fact's value, or None."""
    if path[0] == "settings":
        return next((copy.deepcopy(st) for st in (raw or {}).get("settings") or []
                     if isinstance(st, dict) and _obj_key(st.get("object")) == path[1]), None)
    d = raw or {}
    for p in path[:-1]:
        d = d.get(p) if isinstance(d, dict) else None
        if d is None:
            return None
    if not isinstance(d, dict):
        return None
    k = _find(d, path[-1])
    return copy.deepcopy(d[k]) if k is not None else None


def put(raw, path, value):
    """Set a fact in a quirk file's content (in place); value None removes it."""
    if path[0] == "settings":
        lst = [st for st in raw.get("settings") or []
               if not (isinstance(st, dict) and _obj_key(st.get("object")) == path[1])]
        if value is not None:
            lst.append(dict(value, object=path[1]))
        if lst:
            raw["settings"] = lst
        else:
            raw.pop("settings", None)
        return
    d = raw
    for p in path[:-1]:
        if not isinstance(d.get(p), dict):
            d[p] = {}
        d = d[p]
    old = _find(d, path[-1])
    if old is not None:
        del d[old]
    if value is not None:
        d[path[-1]] = copy.deepcopy(value)


def merge(base, top):
    """Layer one quirk file's content over another's, fact by fact: a fact of
    `top` replaces the same fact of `base`; the rest of `base` stays."""
    out = copy.deepcopy(base or {})
    top = top or {}
    for path in facts(top):
        put(out, path, get(top, path))
    for k in ("name", "match", "format"):
        if top.get(k) is not None:
            out[k] = copy.deepcopy(top[k])
    if top.get("sources"):
        out["sources"] = dict(out.get("sources") or {}, **copy.deepcopy(top["sources"]))
    return out


def label(path):
    """A fact in words, for the window."""
    head = path[0]
    if head == "commands":
        return "%s is a command, not a setting" % path[1]
    if head == "settings":
        return "Setting %s" % path[1]
    if head == "limits":
        return "Drive test limit: %s" % path[1]
    if head == "pins":
        return "Pin for %s" % path[1]
    if head == "tuning":
        return "Tuning: %s" % path[1]
    if head == "measure":
        return "Not recorded when measuring: %s" % path[2]
    if head == "hal":
        return "HAL lines"
    if head == "lcec":
        return "Translation of lcec's settings"
    if head == "homing":
        return {"methods": "Homing methods", "enable-again": "Enable again after homing methods",
                "inputs": "How the inputs get their function and polarity"}.get(path[1]) or (
            "Input %s is 0x60FD bit" % path[2] if path[1] == "input-bits" else
            "Homing method %s: %s" % (path[2], "settings it needs" if path[1] == "extras"
                                      else "description"))
    return key(path)


def text(value):
    """A fact's value as short text."""
    if value is None:
        return "(nothing)"
    if isinstance(value, (str, int, float)):
        return str(value)
    return yaml.safe_dump(value, default_flow_style=True, sort_keys=False, width=1000).strip()


def source_text(src):
    if not src:
        return "no source given"
    kind = src.get("kind", "other")
    bits = {"manual": "the manual", "tried": "tried on a machine", "shared": "shared",
            "other": ""}.get(kind, kind)
    more = [str(src[k]) for k in ("ref", "date", "revision", "firmware", "by") if src.get(k)]
    return ", ".join([bits] + more if bits else more) or "no source given"


# -- the user's own file

def _norm_hex(x):
    return "0x%08X" % (x if isinstance(x, int) else int(str(x), 16))


def own_path(ident):
    """The user's own quirk file for (vendor, product): the one that matches,
    else <vendor>-<product>.yaml in the user's folder."""
    for q in quirks.load():
        if q.get("path", "").startswith(quirks.SHIPPED):
            continue
        if ident in q.get("matches", [q.get("match")]):
            return q["path"]
    return os.path.join(quirks.user_dir(), "%08x-%08x.yaml" % ident)


def read_raw(path):
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return None
    return data if isinstance(data, dict) else None


def own_raw(ident, name=None):
    """The user's own quirk file's content for a device (a new one if none)."""
    raw = read_raw(own_path(ident))
    if raw is None:
        raw = {"format": FORMAT, "name": name or "device",
               "match": {"vendor": _norm_hex(ident[0]), "product": _norm_hex(ident[1])}}
    return raw


def write_raw(path, raw, why="Written by EtherCAT Setup, fact by fact (Device data...)."):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("# Quirk file for %s. %s\n# See docs/ecatsetup.md \"Quirk files\".\n"
                % (raw.get("name", "a device"), why))
        yaml.safe_dump(raw, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
    os.replace(tmp, path)
    quirks.load(reload=True)
    return path


def today():
    return datetime.date.today().isoformat()


def remember(ident, name, path, value, source):
    """Keep one fact in the user's own quirk file, with its source; returns
    the file. Checked as a quirk file before it is written."""
    raw = own_raw(ident, name)
    raw["format"] = FORMAT
    put(raw, path, value)
    srcs = raw.get("sources") or {}
    if value is None:
        srcs.pop(key(path), None)
    elif source:
        srcs[key(path)] = {k: v for k, v in source.items() if v not in (None, "")}
    if srcs:
        raw["sources"] = srcs
    else:
        raw.pop("sources", None)
    try:
        quirks.parse(copy.deepcopy(raw))
    except (quirks.QuirkError, TypeError, ValueError, AttributeError) as e:
        raise Error("not kept: %s" % e)
    return write_raw(own_path(ident), raw)


def current_raw(ident):
    """What is in use for a device, as quirk file content (shipped + own)."""
    return copy.deepcopy(quirks.of(ident).get("raw") or {})


def sources(ident):
    return current_raw(ident).get("sources") or {}


# -- sharing

def strip(raw):
    """A quirk file's content as it is shared: the machine's choices (pins:
    names, scales, offsets) left out; sources kept."""
    out = {"format": FORMAT}
    for k, v in (raw or {}).items():
        if k in SHARE_KEYS and k != "format":
            out[k] = copy.deepcopy(v)
    srcs = {k: v for k, v in (out.get("sources") or {}).items() if not k.startswith("pins ")}
    if srcs:
        out["sources"] = srcs
    else:
        out.pop("sources", None)
    return out


def check_hal(hal):
    """Problems of a quirk file's hal: block for sharing: only components of
    plain logic, only setp and net, only the slave's own pins and the file's
    own instances (never a machine signal)."""
    out = []
    if not isinstance(hal, dict):
        return ["hal: not a mapping"]
    inst = set()
    for comp, names in (hal.get("load") or {}).items():
        if comp not in HAL_COMPONENTS and not str(comp).startswith("conv_"):
            out.append("hal: the component %s is not among those a shared file may load (%s, conv_*)"
                       % (comp, ", ".join(sorted(HAL_COMPONENTS))))
        for n in names or []:
            if not _INSTANCE.fullmatch(str(n)):
                out.append("hal: the instance %s must be named {name}-something" % n)
            inst.add(str(n))

    def own(tok):
        m = _OWN_PIN.fullmatch(tok)
        return bool(_SLAVE.fullmatch(tok) and tok.startswith("{pin:")) or bool(m and m.group(1) in inst)

    for f in hal.get("addf") or []:
        f = str(f)
        if f.split(".")[0] not in inst:
            out.append("hal: addf %s is not one of the file's own instances" % f)
    for line in hal.get("lines") or []:
        toks = str(line).split("#")[0].split()
        if not toks:
            continue
        cmd = toks[0]
        if cmd == "setp":
            if len(toks) != 3 or not own(toks[1]) or not _VALUE.fullmatch(toks[2]):
                out.append("hal: %r: setp takes one of the file's own pins (or the slave's) and a number"
                           % line)
        elif cmd == "net":
            if len(toks) < 3:
                out.append("hal: %r: net needs a signal and pins" % line)
                continue
            sig = toks[1]
            if not (_INSTANCE.fullmatch(sig) or (_SLAVE.fullmatch(sig) and sig.startswith("{signal:"))):
                out.append("hal: %r: the signal must be {name}-something or {signal:...}, never a "
                           "machine signal" % line)
            for t in toks[2:]:
                if t in ("=>", "<=", "<=>"):
                    continue
                if not own(t):
                    out.append("hal: %r: %s is neither the slave's pin nor one of the file's own" % (line, t))
        else:
            out.append("hal: %r: only setp and net lines may be shared (not %s)" % (line, cmd))
    return out


def _strings(x, where, out):
    if isinstance(x, str) and len(x) > MAX_TEXT:
        out.append("%s: a text longer than %d characters" % (where, MAX_TEXT))
    elif isinstance(x, dict):
        for k, v in x.items():
            _strings(k, where, out)
            _strings(v, "%s.%s" % (where, k), out)
    elif isinstance(x, list):
        for v in x:
            _strings(v, where, out)


def check(raw):
    """Problems that keep a file from being shared ([] if none). The same check
    runs in the window (before Share and on fetched files) and in the data
    repository's Action. It checks the file against itself; values against the
    device (ESI file, dictionary) are checked in the window when used."""
    if not isinstance(raw, dict):
        return ["not a YAML mapping"]
    out = []
    fmt = raw.get("format")
    if not isinstance(fmt, int) or fmt < 1:
        out.append("format: a version number is needed (format: %d)" % FORMAT)
    elif fmt > FORMAT:
        out.append("format %d is newer than this check knows (%d)" % (fmt, FORMAT))
    extra = set(raw) - SHARE_KEYS
    if "pins" in extra:
        out.append("pins: pin names and scales are a machine's choice and are not shared")
    if extra - {"pins"}:
        out.append("unknown keys: %s" % ", ".join(sorted(extra - {"pins"})))
    m = raw.get("match")
    if not isinstance(m, dict) or "vendor" not in m or "product" not in m:
        out.append("match: vendor and product are needed")
    try:
        size = len(yaml.safe_dump(raw))
    except yaml.YAMLError as e:
        return out + ["cannot be written as YAML: %s" % e]
    if size > MAX_FILE:
        out.append("the file is %d bytes, more than %d" % (size, MAX_FILE))
    _strings(raw, "file", out)
    if raw.get("hal") is not None:
        out += check_hal(raw["hal"])
    srcs = raw.get("sources")
    if srcs is not None:
        if not isinstance(srcs, dict):
            out.append("sources: a mapping, fact: {kind, ref, date, ...}")
        else:
            have = {key(p) for p in facts(raw)}
            for k, v in srcs.items():
                if not isinstance(v, dict) or v.get("kind") not in SOURCE_KINDS:
                    out.append("sources.%s: needs kind (%s)" % (k, ", ".join(SOURCE_KINDS)))
                elif k not in have:
                    out.append("sources.%s: no such fact in the file" % k)
    if not out:
        try:
            quirks.parse(copy.deepcopy({k: v for k, v in raw.items() if k not in ("format", "sources")}))
        except (quirks.QuirkError, TypeError, ValueError, AttributeError) as e:
            out.append(str(e))
    return out


def share(raw):
    """(title, body, url): a new GitHub Discussion in the data repository,
    prefilled with the stripped file. The user sends it from the browser."""
    s = strip(raw)
    m = s.get("match") or {}
    title = "%s (vendor %s, product %s)" % (s.get("name", "device"), m.get("vendor"), m.get("product"))
    body = ("Shared from EtherCAT Setup. Contributions are %s.\n\n"
            "Write here what the device is, and how the facts were found.\n\n"
            "```yaml\n%s```\n" % (LICENCE, yaml.safe_dump(s, sort_keys=False, allow_unicode=True)))
    q = urllib.parse.urlencode({"category": CATEGORY, "title": title, "body": body},
                               quote_via=urllib.parse.quote)
    return title, body, "https://github.com/%s/discussions/new?%s" % (REPO, q)


_FENCE = re.compile(r"^```[ \t]*(\S*)[^\n]*\n(.*?)^```", re.M | re.S)


def from_post(body):
    """The quirk file in a Discussion post (its first ```yaml block), parsed;
    raises Error."""
    for m in _FENCE.finditer(body or ""):
        if m.group(1) in ("yaml", "yml", ""):
            try:
                data = yaml.safe_load(m.group(2))
            except yaml.YAMLError as e:
                raise Error("the YAML block cannot be read: %s" % e)
            return data
    raise Error("no ```yaml block with the quirk file in the post")


def repo_path(raw, number):
    """Where the data repository keeps a file sent in as Discussion `number`."""
    m = raw["match"]
    products = m["product"] if isinstance(m["product"], list) else [m["product"]]
    return "devices/%08x-%08x/%d.yaml" % (int(str(m["vendor"]), 16), int(str(products[0]), 16), number)


# -- reports

def report(path, result, used, revision=None, firmware=None, words=""):
    """A report as a Discussion comment: the user's words and a block the index
    reads. The author can edit it later or add another."""
    if result not in RESULTS:
        raise Error("result is one of %s" % ", ".join(RESULTS))
    block = {"ecat-report": FORMAT, "file": path, "result": result, "used": list(used or [])}
    if revision:
        block["revision"] = str(revision)
    if firmware:
        block["firmware"] = str(firmware)
    return "%s\n\n```ecat-report\n%s```\n" % (
        (words or "").strip() or "Report from EtherCAT Setup.",
        yaml.safe_dump(block, sort_keys=False, allow_unicode=True))


def read_report(body):
    """A report from a comment, or None: {result, file, used, revision,
    firmware, words}."""
    for m in _FENCE.finditer(body or ""):
        if m.group(1) != "ecat-report":
            continue
        try:
            d = yaml.safe_load(m.group(2))
        except yaml.YAMLError:
            return None
        if not isinstance(d, dict) or d.get("result") not in RESULTS:
            return None
        words = (body[:m.start()] + body[m.end():]).strip()
        out = {k: d.get(k) for k in ("result", "file", "revision", "firmware")}
        out["used"] = [str(x) for x in d.get("used") or []][:100]
        out["words"] = words[:MAX_TEXT]
        return out
    return None


def summary(reports):
    """'2 worked, 1 did not work' from an index entry's reports."""
    n = {r: 0 for r in RESULTS}
    for r in reports or []:
        if r.get("result") in n:
            n[r["result"]] += 1
    return ", ".join("%d %s" % (c, r) for r, c in n.items() if c) or "no reports yet"


# -- fetching

def cache_dir():
    return os.environ.get("ECATSETUP_SHARED_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "ecatsetup", "shared")


def source_url():
    return os.environ.get("ECATSETUP_DEVICES_URL") or \
        "https://codeload.github.com/%s/tar.gz/refs/heads/main" % REPO


_MEMBER = re.compile(r"(index\.json|devices/[0-9a-f]{8}-[0-9a-f]{8}/[0-9]+\.yaml)")


def fetch(url=None, timeout=30):
    """Fetch the data repository (only when the user asks) into the cache:
    index.json and devices/; nothing else is taken from the archive. Returns
    the number of files."""
    url = url or source_url()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = r.read(20 * 1024 * 1024 + 1)
    except (OSError, ValueError) as e:
        raise Error("could not fetch %s: %s" % (url, e))
    if len(data) > 20 * 1024 * 1024:
        raise Error("the archive is larger than 20 MB")
    dest = cache_dir()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".shared-", dir=os.path.dirname(dest))
    n = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for mem in tar.getmembers():
                rel = mem.name.split("/", 1)[1] if "/" in mem.name else ""
                if not mem.isfile() or not _MEMBER.fullmatch(rel) or mem.size > 256 * 1024:
                    continue
                out = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with tar.extractfile(mem) as src, open(out, "wb") as f:
                    f.write(src.read())
                n += 1
        with open(os.path.join(tmp, "fetched.json"), "w") as f:
            json.dump({"url": url, "time": datetime.datetime.now().isoformat(timespec="seconds")}, f)
    except (tarfile.TarError, OSError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise Error("the archive cannot be read: %s" % e)
    old = dest + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(dest):
        os.replace(dest, old)
    os.replace(tmp, dest)
    shutil.rmtree(old, ignore_errors=True)
    return n


def fetched():
    """When and from where the cache was fetched, or None."""
    try:
        with open(os.path.join(cache_dir(), "fetched.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def index():
    try:
        with open(os.path.join(cache_dir(), "index.json")) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def shared_for(ident):
    """The fetched files for (vendor, product): [{"path", "raw", "problems",
    "discussion", "url", "author", "upvotes", "reports"}], best first. Each
    file is checked again here: the window trusts nothing it fetched."""
    folder = os.path.join(cache_dir(), "devices", "%08x-%08x" % ident)
    entries = {e.get("path"): e for e in index().get("files") or [] if isinstance(e, dict)}
    out = []
    for f in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        rel = "devices/%08x-%08x/%s" % (ident + (f,))
        raw = read_raw(os.path.join(folder, f))
        e = entries.get(rel, {})
        out.append({"path": rel, "raw": raw, "problems": check(raw),
                    "discussion": e.get("discussion"), "url": e.get("url"),
                    "author": e.get("author"), "upvotes": e.get("upvotes", 0),
                    "reports": e.get("reports") or []})
    worked = lambda x: sum(1 for r in x["reports"] if r.get("result") == "worked")
    out.sort(key=lambda x: (bool(x["problems"]), -worked(x), -(x["upvotes"] or 0), x["path"]))
    return out


def compare(ident, shared_raw):
    """A shared file's facts against what is in use, one by one:
    [{"path", "label", "now", "shared", "same", "source"}]. Pins are not
    offered (a machine's choice)."""
    now = current_raw(ident)
    srcs = (shared_raw or {}).get("sources") or {}
    out = []
    for p in facts(shared_raw):
        if p[0] == "pins":
            continue
        a, b = get(now, p), get(shared_raw, p)
        out.append({"path": p, "label": label(p), "now": a, "shared": b, "same": a == b,
                    "source": srcs.get(key(p))})
    return out


def take(ident, name, entry, path):
    """Keep one fact of a fetched file in the user's own quirk file; its source
    says where it came from (and the source the sharer gave)."""
    raw = entry["raw"]
    src = {"kind": "shared", "ref": entry.get("url") or entry.get("path"),
           "by": entry.get("author"), "date": today()}
    given = (raw.get("sources") or {}).get(key(path))
    if given:
        src["given"] = source_text(given)
    return remember(ident, name, path, get(raw, path), src)
