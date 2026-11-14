"""A capture: everything about the slaves on a bus in one file, to send
to a developer who does not have the devices (docs/development.md
"Devices we do not have").

record() runs with a master up (free running or in LinuxCNC) and keeps
every call of the ethercat tool with its output: the slave list, each
slave's "slaves -v", SII, CoE dictionary, every entry readable in its
present state, PDO assignment, ESC registers 0x0000-0x0FFF, what
proposing a configuration and the diagnosis read, plus the report and
the matching ESI devices. A few seconds of the frames on the wire are
added when tcpdump can be run as root.

Replay serves the recorded calls instead of the tool, so the proposal,
the diagnosis, the check and the window run against a capture without
the bus (`ecatsetup --from-capture FILE ...`), and tests get fake slaves
of kinds we do not have.

The file is a gzipped tar: capture.json (what was recorded), tool.json
(the calls), report.md, esi/*.xml, frames.pcap. Bus data only: nothing
of the user's files except the configuration and INI lines the report
holds.
"""

import base64
import io
import json
import os
import shutil
import subprocess
import tarfile
import time
import xml.etree.ElementTree as ET

from . import coe as coe_mod, esi, netif, report, session, sii as sii_mod, tool

FORMAT = 1
STATES = {"PREOP": 0, "SAFEOP": 1, "OP": 2}
REG_END = 0x1000     # ESC registers; process RAM above is not read (mailbox side effects)
REG_CHUNK = 0x400


class CaptureError(Exception):
    pass


def default_name():
    return time.strftime("ecat-capture-%Y%m%d-%H%M%S.tar.gz")


class Recorder:
    """Wraps tool.run and keeps every call: args, output or error."""

    def __init__(self, run):
        self.real = run
        self.calls = []

    def run(self, *args, binary=False):
        args = [str(a) for a in args]
        try:
            out = self.real(*args, binary=binary)
        except tool.ToolError as e:
            self.calls.append({"args": args, "error": str(e)})
            raise
        if binary:
            self.calls.append({"args": args, "b64": base64.b64encode(out).decode()})
        else:
            self.calls.append({"args": args, "out": out})
        return out


def _key(args):
    return " ".join(str(a) for a in args)


def _loose_key(args):
    """The key without a type option and "--": upload -p 0 -t uint16 0x6041 0
    matches upload -p 0 0x6041 0."""
    args = [str(a) for a in args]
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a == "-t":
            skip = True
        elif a != "--":
            out.append(a)
    return " ".join(out)


def readable_entries(dictionary, state):
    """Entries a slave in this AL state answers an upload for."""
    i = STATES.get(state)
    if i is None:
        return []
    return [e for _k, e in sorted(dictionary.entries.items())
            if len(e.access) > 2 * i and e.access[2 * i] == "r"]


def esi_extract(devices):
    """Minimal ESI files holding only the given devices' descriptions:
    {file name: xml bytes}. A device whose file cannot be read is left out."""
    out = {}
    by_file = {}
    for d in devices:
        by_file.setdefault(d.path, []).append(d)
    for path, devs in by_file.items():
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        keep = {(d.product_code, d.revision) for d in devs}
        new = ET.Element("EtherCATInfo")
        vendor = root.find("Vendor")
        if vendor is not None:
            new.append(vendor)
        descs = ET.SubElement(new, "Descriptions")
        groups = root.find("Descriptions/Groups")
        if groups is not None:
            descs.append(groups)
        devs_el = ET.SubElement(descs, "Devices")
        for dev in root.findall("Descriptions/Devices/Device"):
            t = dev.find("Type")
            if t is None:
                continue
            try:
                ident = (esi.number(t.get("ProductCode")), esi.number(t.get("RevisionNo") or "0"))
            except (TypeError, ValueError):
                continue
            if ident in keep:
                devs_el.append(dev)
        name = os.path.basename(path)
        while name in out:
            name = "_" + name
        out[name] = ET.tostring(new, encoding="utf-8", xml_declaration=True)
    return out


def pcap(interface, seconds, runner=subprocess.run):
    """EtherCAT frames on the interface for some seconds, as pcap bytes,
    or (None, why not)."""
    if not interface:
        return None, "the master's network card was not found"
    if not shutil.which("tcpdump"):
        return None, "tcpdump is not installed (apt install tcpdump)"
    cmd = ["timeout", "%g" % seconds, "tcpdump", "-i", interface, "-U", "-c", "50000",
           "-w", "-", "ether", "proto", "0x88a4"]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    try:
        p = runner(cmd, capture_output=True, timeout=seconds + 10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "tcpdump: %s" % e
    # timeout ends tcpdump with 124; the frames are in the output all the same
    if not p.stdout:
        err = p.stderr.decode(errors="replace").strip().splitlines()
        if p.returncode == 1 and cmd[0] == "sudo":
            return None, "tcpdump needs root: run the capture with sudo, or allow sudo"
        return None, "tcpdump gave nothing (%s)" % (err[-1] if err else "exit %d" % p.returncode)
    if len(p.stdout) <= 24:     # the pcap header alone
        return None, ("no EtherCAT frames seen on %s (a master using XDP hides them "
                      "from tcpdump)" % interface)
    return p.stdout, None


def record(path, esi_paths=(), cfg_path=None, ini_path=None, seconds=2.0, pcap_seconds=3.0,
           values=True, progress=print, pcap_fn=None):
    """Write a capture of the bus to path. Returns the notes (what is missing)."""
    rec = Recorder(tool.run)
    notes = []
    manifest = {"format": FORMAT, "time": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                "versions": report.versions(), "slaves": [], "notes": notes}
    tool.run = rec.run
    try:
        positions = tool.slave_positions()
        if not positions:
            raise CaptureError("the master sees no slaves")
        for args in (("master",), ("version",), ("slaves",), ("graph",), ("config",),
                     ("domains",)):
            try:
                tool.run(*args)
            except tool.ToolError:
                pass
        devices = esi.load_dirs(esi_paths) if esi_paths else []
        used = []
        for pos in positions:
            progress("Slave %d: identity, SII, dictionary" % pos)
            info = tool.slave_info(pos)
            entry = {"position": pos, "vendor_id": info.get("vendor_id"),
                     "product_code": info.get("product_code"), "revision": info.get("revision"),
                     "state": info.get("state")}
            sii = sii_mod.parse(tool.sii(pos))
            entry["name"] = getattr(sii, "name", "") or ""
            for args in (("pdos", "-p", pos), ("xml", "-p", pos)):
                try:
                    tool.run(*args)
                except tool.ToolError:
                    pass
            for a in range(0, REG_END, REG_CHUNK):
                try:
                    tool.run("reg_read", "-p", pos, "0x%04x" % a, REG_CHUNK)
                except tool.ToolError:
                    pass
            # what proposing reads, with the ESI given (the window and --propose)
            try:
                _info, coe, _p = session.read_slave(pos, devices, "csp")
            except tool.ToolError as e:
                notes.append("slave %d: proposal could not be read: %s" % (pos, e))
                coe = coe_mod.Dictionary()
            dev, _exact = esi.find(devices, info.get("vendor_id"), info.get("product_code"),
                                   info.get("revision"))
            if dev:
                used.append(dev)
                entry["esi"] = dev.type_name or dev.name
            if values and coe:
                todo = readable_entries(coe, info.get("state"))
                progress("Slave %d: %d object values" % (pos, len(todo)))
                failed = 0
                for e in todo:
                    try:
                        tool.run("upload", "-p", pos, "0x%04x" % e.index, e.subindex)
                    except tool.ToolError:
                        failed += 1
                entry["values"] = len(todo) - failed
                entry["values_refused"] = failed
            manifest["slaves"].append(entry)
        progress("Report: diagnosis over %g s" % seconds)
        report_text = report.build(cfg_path, ini_path, seconds=seconds)
        interface = netif.master_interface()
        manifest["interface"] = interface
    finally:
        tool.run = rec.real

    frames = None
    if pcap_seconds:
        progress("Frames on the wire for %g s" % pcap_seconds)
        frames, why = (pcap_fn or pcap)(interface, pcap_seconds)
        if why:
            notes.append("no frames: " + why)
    if not devices:
        notes.append("no ESI files given (--esi): the capture has the slaves' own data only")
    elif len(used) < len(positions):
        notes.append("no ESI description found for %d slave(s)" % (len(positions) - len(used)))

    files = {"capture.json": json.dumps(manifest, indent=1).encode(),
             "tool.json": json.dumps(rec.calls).encode(),
             "report.md": report_text.encode()}
    for name, data in esi_extract(used).items():
        files["esi/" + name] = data
    if frames:
        files["frames.pcap"] = frames
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mtime = int(time.time())
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
    return notes


def read(path):
    """The files of a capture: {name: bytes}."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            files = {}
            for m in tar.getmembers():
                if m.isfile():
                    files[m.name] = tar.extractfile(m).read()
    except (OSError, tarfile.TarError) as e:
        raise CaptureError("%s: not a capture (%s)" % (path, e))
    if "capture.json" not in files or "tool.json" not in files:
        raise CaptureError("%s: not a capture (capture.json or tool.json missing)" % path)
    manifest = json.loads(files["capture.json"])
    if manifest.get("format", 0) > FORMAT:
        raise CaptureError("%s: written by a newer ecatsetup (format %s)" % (path,
                                                                            manifest["format"]))
    return files


class Replay:
    """The ethercat tool as recorded in a capture. A call recorded more
    than once gives its outputs in turn, the last one again after that.
    Writes (download, states, ...) are refused: a capture cannot answer them."""

    READS = {"master", "version", "slaves", "graph", "config", "domains", "pdos", "xml",
             "reg_read", "sii_read", "sdos", "upload", "diag"}

    def __init__(self, path):
        self.path = path
        self.files = read(path)
        self.manifest = json.loads(self.files["capture.json"])
        self.exact, self.loose = {}, {}
        for c in json.loads(self.files["tool.json"]):
            self.exact.setdefault(_key(c["args"]), []).append(c)
            self.loose.setdefault(_loose_key(c["args"]), []).append(c)
        self.served = {}
        self.missing = []

    def run(self, *args, binary=False):
        args = [str(a) for a in args]
        if args and args[0] not in self.READS:
            raise tool.ToolError("%s: a capture cannot answer this (%s)" % (
                os.path.basename(self.path), " ".join(args)))
        k = _key(args)
        calls = self.exact.get(k)
        if calls is None:
            k = _loose_key(args)
            calls = self.loose.get(k)
        if calls is None:
            self.missing.append(_key(args))
            raise tool.ToolError("%s: not in the capture" % _key(args))
        n = self.served.get(k, 0)
        self.served[k] = n + 1
        c = calls[min(n, len(calls) - 1)]
        if "error" in c:
            raise tool.ToolError(c["error"])
        if "b64" in c:
            data = base64.b64decode(c["b64"])
            return data if binary else data.decode(errors="replace")
        return c["out"].encode() if binary else c["out"]

    def esi_dir(self, where):
        """Write the capture's ESI files into the folder where; returns it, or
        None if the capture has none."""
        names = [n for n in self.files if n.startswith("esi/")]
        if not names:
            return None
        os.makedirs(where, exist_ok=True)
        for n in names:
            with open(os.path.join(where, os.path.basename(n)), "wb") as f:
                f.write(self.files[n])
        return where

    def install(self):
        """Serve tool calls from the capture from now on."""
        tool.run = self.run


def describe(path):
    """A capture's contents as text lines (ecatsetup --capture-info)."""
    files = read(path)
    m = json.loads(files["capture.json"])
    out = ["Capture %s, %s" % (path, m.get("time", "?"))]
    out += ["  " + v for v in m.get("versions", [])]
    if m.get("interface"):
        out.append("  interface %s" % m["interface"])
    for s in m.get("slaves", []):
        ids = "0x%08X:0x%08X rev 0x%08X" % tuple(x or 0 for x in (
            s.get("vendor_id"), s.get("product_code"), s.get("revision")))
        line = "  slave %d: %s %s %s" % (s["position"], s.get("name") or "?", ids, s.get("state"))
        if "values" in s:
            line += ", %d values" % s["values"]
            if s.get("values_refused"):
                line += " (%d refused)" % s["values_refused"]
        if s.get("esi"):
            line += ", ESI %s" % s["esi"]
        out.append(line)
    calls = len(json.loads(files["tool.json"]))
    out.append("  %d tool calls, %s" % (calls, ", ".join(
        "%s %d bytes" % (n, len(d)) for n, d in sorted(files.items())
        if n not in ("capture.json", "tool.json"))))
    for n in m.get("notes", []):
        out.append("  note: " + n)
    return out
