"""ecat setup prototype: propose a configuration for the slaves on the bus,
from their own data and ESI files, and compare it with an lcec XML file.

    python3 -m ecatsetup [--esi DIR] [--ini FILE] [--compare XML] [-p POS]
    python3 -m ecatsetup ... --write ethercat.yaml [--interface IF] [--transport T]
    python3 -m ecatsetup --check ethercat.yaml
    python3 -m ecatsetup --import ethercat-conf.xml --write ethercat.yaml
    python3 -m ecatsetup --gui [ethercat.yaml] [--ini FILE] [--esi DIR]

Only reads from the bus; nothing is written to the slaves.
"""

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict

import yaml

from . import config, diagnose, esi, events, importxml, integrate, lcecxml, masterstart, netif, propose, tool, update
from . import session
from .session import compare_bus, ini_value, read_slave, servo_period  # noqa: F401


def entry_line(e, coe):
    name = (coe.name(e.index, e.subindex) if coe else "") or e.name
    what = {"profile": "(cia402 profile)", "encoder": "(encoder function)",
            "gap": "(gap)"}.get(e.role, "%-28s %s" % (e.pin, e.hal_type))
    return "      0x%04X:%02X %3d  %-30s %-6s %s" % (e.index, e.subindex, e.bitlen, name[:30],
                                                    e.data_type, what)


def report(info, coe, p, period, hand, out):
    w = out.append
    w("Slave %d  %s" % (p.position, p.name))
    w("  vendor 0x%08X  product 0x%08X  revision 0x%08X  serial %s  state %s"
      % (p.vendor_id, p.product_code, p.revision,
         "0 (cannot be told apart by serial number)" if not p.serial else "0x%08X" % p.serial,
         info["state"]))
    if p.profile:
        w("  Profile: %s (from %s)" % (propose.PROFILE_NAMES[p.profile], p.profile_source))
    else:
        w("  Profile: none recognised; configured from its process data only")
    if p.drive_modes:
        w("  Drive modes (0x6502): %s; using %s" % (" ".join(p.drive_modes), p.mode.upper()))
    w("  ESI: %s" % (p.esi_path or "no matching file"))
    w("  Process data (%s):" % p.pdo_source)
    if p.pdo_sets:
        w("    predefined in the ESI: %s" % "; ".join(p.pdo_sets))
    for pd in p.pdos:
        w("    SM%d %s 0x%04X \"%s\"" % (pd.sm, "out" if pd.direction == "rx" else "in",
                                        pd.index, pd.name))
        for e in pd.entries:
            w(entry_line(e, coe))
    if p.dc_assign is not None:
        cycle = "%d x servo period" % p.dc_sync0_factor
        if period:
            cycle += " = %d ns" % (p.dc_sync0_factor * period)
        w("  Distributed clocks: assignActivate 0x%04X, SYNC0 %s, shift %d ns (from %s)"
          % (p.dc_assign, cycle, p.dc_shift, p.dc_source))
    else:
        w("  Distributed clocks: not used")
    if p.settings:
        names = ", ".join("0x%04X %s" % (e.index, coe.name(e.index, e.subindex))
                          for e in p.settings[:4])
        w("  Settings: %d writable vendor objects, e.g. %s" % (len(p.settings), names))
    for n in p.notes:
        w("  Note: %s" % n)
    if hand is not None:
        compare(p, coe, hand, out)
    w("")


def compare(p, coe, h, out):
    w = out.append
    w("  Compared with the XML file's slave %d \"%s\" (%s):" % (h.idx, h.name, h.type))
    if (h.vendor_id, h.product_code) != (p.vendor_id, p.product_code):
        w("    identity differs: XML 0x%08X/0x%08X" % (h.vendor_id, h.product_code))

    def key_set(entries):
        return {(sm, e.index, e.subindex): e for sm, e in entries}
    mine = key_set([(pd.sm, e) for pd in p.pdos for e in pd.entries if e.role != "gap"])
    theirs = key_set([(sm, e) for (sm, _pdo), es in h.pdos.items() for e in es])
    if not h.pdos:
        w("    PDOs: the XML file keeps the slave's own mapping")
    elif set(mine) == set(theirs):
        w("    PDOs: the same entries")
    else:
        for k in sorted(set(mine) - set(theirs)):
            w("    PDOs: only in the proposal: SM%d 0x%04X:%02X %s" % (k[0], k[1], k[2], mine[k].name))
        for k in sorted(set(theirs) - set(mine)):
            w("    PDOs: only in the XML file:  SM%d 0x%04X:%02X %s"
              % (k[0], k[1], k[2], coe.name(k[1], k[2]) if coe else ""))
    for k in sorted(set(mine) & set(theirs)):
        e, x = mine[k], theirs[k]
        if x.bits:
            pins = [b.pin for b in x.bits if b.pin]
            used = "%d bit pins (%s ... %s)" % (len(pins), pins[0], pins[-1]) if len(pins) > 1 \
                else "one bit pin (%s), the other bits unused" % pins[0]
            w("    Pins: the XML file splits 0x%04X:%02X into %s; proposal: one %s pin \"%s\""
              % (k[1], k[2], used, e.hal_type or "-", e.pin or e.role))
        elif e.role in ("profile", "encoder"):
            pass
        elif x.pin and x.pin != e.pin:
            w("    Pins: 0x%04X:%02X  XML \"%s\" (%s), proposal \"%s\" (%s)"
              % (k[1], k[2], x.pin, x.hal_type, e.pin, e.hal_type))
    if h.assign_activate is None and p.dc_assign is None:
        w("    DC: neither uses it")
    elif h.assign_activate == p.dc_assign:
        w("    DC: the same (assignActivate 0x%04X, sync0Cycle %s)" % (p.dc_assign, h.sync0_cycle))
    else:
        w("    DC: XML %s, proposal %s" % (
            "none" if h.assign_activate is None else "0x%04X" % h.assign_activate,
            "none" if p.dc_assign is None else "0x%04X" % p.dc_assign))
    for index, sub, data in h.sdos:
        entry = coe.entries.get((index, sub)) if coe else None
        w("    Setting in the XML file: 0x%04X:%02X %-32s = %s"
          % (index, sub, (coe.name(index, sub) if coe else "")[:32], propose.decode(data, entry)))


def main(argv=None):
    prog = os.path.basename(sys.argv[0])
    prog = "python3 -m ecatsetup" if prog == "__main__.py" else prog
    ap = argparse.ArgumentParser(prog=prog, description=__doc__.split("\n")[0])
    ap.add_argument("--esi", action="append", default=[], help="ESI file or folder (repeatable)")
    ap.add_argument("--ini", help="LinuxCNC INI file, for the servo period")
    ap.add_argument("--compare", help="lcec XML file (ethercat-conf.xml) to compare with")
    ap.add_argument("--mode", default="csp", choices=sorted(propose.CIA402_MODES),
                    help="mode for CiA 402 drives: csp or csv for joints, pv (profile "
                         "velocity) for a spindle or conveyor, pp (profile position) for "
                         "positioning that is not a joint")
    ap.add_argument("--pdo-set", action="append", default=[], metavar="POS=NAME",
                    help="use the ESI's predefined PDO assignment NAME for slave POS")
    ap.add_argument("-p", "--position", type=int, action="append", help="only these slaves")
    ap.add_argument("--json", action="store_true", help="the proposals as JSON")
    ap.add_argument("--write", metavar="FILE", help="write the proposals as an ecat configuration")
    ap.add_argument("--interface", help="network interface, for --write (default: the one the "
                    "master reads the bus through)")
    ap.add_argument("--transport", choices=sorted(config.TRANSPORTS),
                    help="master transport, for --write (default: xdp-native if the interface "
                    "can do it, else raw)")
    ap.add_argument("--update", metavar="FILE",
                    help="update an ecat configuration from the bus, keeping its choices; "
                         "written back with a .bak copy, or to --write")
    ap.add_argument("--events", nargs="?", const="now", metavar="prev|FILE",
                    help="show the ecat component's event log (the flight recorder): of the "
                         "running or last run, 'prev' for the run before, or a saved FILE")
    ap.add_argument("--report", nargs="?", const="", metavar="FILE",
                    help="write a report to attach when asking for help: versions, the "
                         "configuration (--check FILE or --ini), the INI lines, a diagnosis of "
                         "the bus and the event logs; FILE defaults to ethercat-report-<time>.md")
    ap.add_argument("--capture", nargs="?", const="", metavar="FILE",
                    help="write a capture of the bus to send to a developer: every slave's "
                         "identity, SII, dictionary and values, registers, the report and a "
                         "few seconds of frames; FILE defaults to ecat-capture-<time>.tar.gz")
    ap.add_argument("--no-values", action="store_true",
                    help="for --capture, leave out the object values (faster)")
    ap.add_argument("--frames-seconds", type=float, default=3.0, metavar="S",
                    help="for --capture, seconds of frames on the wire (0: none; needs "
                         "tcpdump and root)")
    ap.add_argument("--capture-info", metavar="FILE", help="show what a capture holds")
    ap.add_argument("--capture-c", metavar="FILE",
                    help="print a C header with fake slaves from a capture, for the "
                         "component's tests (component/tests)")
    ap.add_argument("--c-name", default="capture", metavar="NAME",
                    help="for --capture-c, the prefix of the C names")
    ap.add_argument("--c-cycles", type=int, default=1000, metavar="N",
                    help="for --capture-c, at most N cycles of process data per slave")
    ap.add_argument("--from-capture", metavar="FILE",
                    help="read the bus from a capture instead of the master (with the "
                         "proposal, --diagnose, --check, --gui); its ESI files are added")
    ap.add_argument("--check-hal", nargs="?", const="", metavar="INI",
                    help="check the HAL files of a LinuxCNC configuration against its "
                         "ethercat.yaml: pins that do not exist, pins on two signals, two "
                         "writers; INI defaults to --ini or $INI_FILE_NAME (as LinuxCNC sets it)")
    ap.add_argument("--diagnose", action="store_true",
                    help="diagnose the slaves: state, cables, the slave's messages, CiA 402")
    ap.add_argument("--seconds", type=float, default=2.0,
                    help="for --diagnose, how long to watch the error counters")
    ap.add_argument("--check", metavar="FILE", help="check an ecat configuration, and the bus against it")
    ap.add_argument("--import", dest="import_xml", metavar="XML",
                    help="import an lcec XML file; with --write, write it as an ecat configuration")
    ap.add_argument("--apply", metavar="FILE",
                    help="write an ecat configuration into the LinuxCNC configuration of --ini: "
                         "the file, ethercat-load.hal, ethercat.hal and the INI changes, after "
                         "showing them; joints connected to drives in your HAL files are taken over")
    ap.add_argument("--yes", action="store_true", help="for --apply, write without asking")
    ap.add_argument("--overwrite-edited", action="store_true",
                    help="for --apply, replace ethercat.hal or ethercat-load.hal even though "
                         "they were changed by hand")
    ap.add_argument("--net", action="append", default=[], metavar="PIN=PIN[,PIN...]",
                    help="for --apply, connect a slave's pin to LinuxCNC pins (repeatable); "
                         "PIN= with nothing after it removes the connection")
    ap.add_argument("--take-over-io", action="store_true",
                    help="for --apply, also take over the slaves' I/O pins as your HAL files "
                         "(not the POSTGUI ones) connect them")
    ap.add_argument("--gui", nargs="?", const="", metavar="FILE",
                    help="open the EtherCAT Setup window, with this configuration file if given")
    args = ap.parse_args(argv)

    if args.capture_c:
        from . import capture, capturec
        try:
            sys.stdout.write(capturec.header(args.capture_c, args.c_name, args.c_cycles))
        except capture.CaptureError as e:
            sys.exit(str(e))
        sys.exit(0)
    if args.capture_info:
        sys.exit(capture_info(args.capture_info))
    if args.from_capture:
        use_capture(args)
    if args.capture is not None:
        sys.exit(write_capture(args))

    if args.gui is not None:
        try:
            from . import gui
        except ImportError as e:
            sys.exit("The window needs PyQt5 (apt install python3-pyqt5): %s" % e)
        sys.exit(gui.main(args.gui or None, args.esi, args.ini, args.mode))

    if args.apply:
        sys.exit(apply_file(args))
    if args.check_hal is not None:
        sys.exit(check_hal(args))
    if args.report is not None:
        sys.exit(write_report(args))
    if args.check:
        sys.exit(check_file(args.check, args.esi))
    if args.events:
        sys.exit(show_events(args))
    if args.diagnose:
        sys.exit(diagnose_bus(args))
    if args.import_xml:
        sys.exit(import_file(args))

    esi_devices = esi.load_dirs(args.esi)
    period = servo_period(args.ini) if args.ini else None
    pdo_sets = {}
    for a in args.pdo_set:
        pos, _, name = a.partition("=")
        if not pos.isdigit() or not name:
            sys.exit("--pdo-set takes POS=NAME, such as 3=\"Standard 32 Bit (MDP 511)\"")
        pdo_sets[int(pos)] = name
    hand = {s.idx: s for s in lcecxml.load(args.compare).slaves} if args.compare else {}

    try:
        try:
            positions = args.position or tool.slave_positions()
        except tool.ToolError:
            if not masterstart.offer_terminal():
                raise
            positions = args.position or tool.slave_positions()
    except tool.ToolError as e:
        sys.exit("Cannot reach a master: %s\nStart one with: sudo ethercat-uspace start" % e)

    out, proposals = [], []
    if args.esi:
        out.append("ESI: %d device descriptions read" % len(esi_devices))
    if period:
        out.append("Servo period: %d ns (from %s)" % (period, args.ini))
    out.append("")
    for pos in positions:
        info, coe, p = read_slave(pos, esi_devices, args.mode, pdo_sets.get(pos))
        proposals.append(p)
        report(info, coe, p, period, hand.get(pos) if args.compare else None, out)

    if args.json:
        print(json.dumps([asdict(p) for p in proposals], indent=2, default=str))
    else:
        print("\n".join(out))
    if args.update:
        sys.exit(update_file(args, proposals, period))
    if args.write:
        try:
            iface = args.interface or netif.master_interface()
            transport = args.transport or netif.default_transport(iface)
            config.write(config.from_proposals(proposals, iface, transport, period), args.write)
        except ValueError as e:
            sys.exit(str(e))
        print("Wrote %s" % args.write)


def apply_file(args):
    """Write a configuration into a LinuxCNC configuration. Returns an exit code."""
    if not args.ini:
        print("--apply needs --ini, the machine's INI file")
        return 2
    try:
        cfg = config.load(args.apply)
    except (OSError, ValueError) as e:
        print("Cannot read %s: %s" % (args.apply, e))
        return 1
    for j, (name, scale) in sorted(integrate.propose_joints(args.ini, cfg).items()):
        integrate.set_joint(cfg, j, name, scale)
        print("joint %d -> %s, scale %g (as connected in your HAL files)" % (j, name, scale))
    if args.take_over_io:
        for sig, pins in sorted(integrate.propose_nets(args.ini, cfg).items()):
            own = [x for x in pins if integrate.is_own(cfg, x)]
            integrate.set_net(cfg, own[0], [x for x in pins if x != own[0]], sig)
            print("net %s: %s (as connected in your HAL files)" % (sig, " ".join(pins)))
    for spec in args.net:
        pin, _, rest = spec.partition("=")
        others = [x.strip() for x in rest.split(",") if x.strip()]
        integrate.set_net(cfg, pin.strip(), others)
    errors = config.check(cfg)
    if errors:
        print("The configuration has errors:\n  " + "\n  ".join(errors))
        return 1
    name = os.path.basename(args.apply)
    p = integrate.plan(args.ini, cfg, name)
    dest = os.path.join(os.path.dirname(os.path.abspath(args.ini)), name)
    if not p.changes and not p.errors and os.path.exists(dest) and \
            open(dest).read() == config.dumps(cfg):
        print("Nothing to change: %s is up to date" % os.path.dirname(dest))
        return 0
    integrate.describe(p)
    if p.errors:
        return 1
    if p.edited and not args.overwrite_edited:
        print("\nNothing written: %s changed by hand. Move the change into a HAL file of your "
              "own, or give --overwrite-edited." % ", ".join(p.edited))
        return 1
    if not args.yes:
        try:
            if input("\nWrite %s and these files? [y/N] " % dest).strip().lower() not in ("y", "yes"):
                print("Nothing written.")
                return 1
        except EOFError:
            print("Nothing written.")
            return 1
    if os.path.exists(dest):
        shutil.copyfile(dest, dest + ".bak")
    config.write(cfg, dest)
    for path in [dest] + integrate.apply(p, overwrite_edited=args.overwrite_edited):
        print("Wrote %s" % path)
    return 0


def diagnose_bus(args):
    """Diagnose the slaves on the bus. Returns an exit code."""
    try:
        try:
            positions = args.position or tool.slave_positions()
        except tool.ToolError:
            if not masterstart.offer_terminal():
                raise
            positions = args.position or tool.slave_positions()
        found = diagnose.diagnose(positions, args.seconds)
    except tool.ToolError as e:
        print("Cannot reach a master: %s\nStart one with: sudo ethercat-uspace start" % e)
        return 2
    print("No problems found" if not found else "%d problem%s found" % (found, "" if found == 1 else "s"))
    try:
        log = events.read()
        print()
        events.report(log, last=20)
    except events.EventsError:
        pass
    return 1 if found else 0


def check_hal(args):
    """The HAL check. Returns 1 if HAL would refuse something, else 0."""
    from . import halcheck
    ini = args.check_hal or args.ini or os.environ.get("INI_FILE_NAME")
    if not ini:
        print("--check-hal needs the INI file (or INI_FILE_NAME, as LinuxCNC sets it)")
        return 2
    e = integrate.read_ini(ini) if os.path.exists(ini) else []
    name = integrate.ini_get(e, "ETHERCAT", "CONFIG")
    if not name:
        print("%s has no [ETHERCAT]CONFIG" % ini)
        return 2
    path = os.path.join(os.path.dirname(os.path.abspath(ini)), name)
    try:
        cfg = config.load(path)
    except (OSError, yaml.YAMLError) as x:
        print("Cannot read %s: %s" % (path, x))
        return 1
    found = halcheck.check(ini, cfg)
    for f in found:
        print("ecatsetup: %s" % f)
    errors = sum(1 for f in found if f.severity == "error")
    if errors:
        print("ecatsetup: the HAL files do not match %s: %d error%s (EtherCAT Setup, HAL & "
              "LinuxCNC page, shows them)" % (name, errors, "" if errors == 1 else "s"))
    elif not found:
        print("ecatsetup: the HAL files match %s" % name)
    return 1 if errors else 0


def write_report(args):
    """Write the report file. Returns an exit code."""
    from . import report
    path = args.report or report.default_name()
    try:
        tool.slave_positions()
    except tool.ToolError:
        # the report reads the bus: offer a master (on a terminal, after a yes) first;
        # declined, the report says that none answered
        masterstart.offer_terminal()
    text = report.build(args.check, args.ini, seconds=args.seconds)
    with open(path, "w") as f:
        f.write(text)
    print("Wrote %s (%d lines). Look it through before posting it." % (path, text.count("\n")))
    return 0


def capture_info(path):
    from . import capture
    try:
        print("\n".join(capture.describe(path)))
    except capture.CaptureError as e:
        print(e)
        return 1
    return 0


def use_capture(args):
    """Serve the bus from a capture file; its ESI files go to a temporary folder."""
    import atexit
    import shutil
    import tempfile
    from . import capture
    try:
        replay = capture.Replay(args.from_capture)
    except capture.CaptureError as e:
        sys.exit(str(e))
    tmp = tempfile.mkdtemp(prefix="ecatsetup-capture-")
    atexit.register(shutil.rmtree, tmp, True)
    if replay.esi_dir(tmp):
        args.esi.append(tmp)
    replay.install()
    print("Reading the bus from the capture %s (%s)" % (
        args.from_capture, replay.manifest.get("time", "?")), file=sys.stderr)


def write_capture(args):
    """Write a capture file. Returns an exit code."""
    from . import capture
    if args.from_capture:
        print("--capture reads the bus, not a capture")
        return 2
    path = args.capture or capture.default_name()
    try:
        tool.slave_positions()
    except tool.ToolError:
        masterstart.offer_terminal()
    try:
        notes = capture.record(path, args.esi, args.check, args.ini, seconds=args.seconds,
                               pcap_seconds=args.frames_seconds, values=not args.no_values)
    except capture.CaptureError as e:
        print("No capture: %s" % e)
        return 1
    except tool.ToolError as e:
        print("No capture: no master could be reached (%s). Start one with "
              "`sudo ethercat-uspace start`, or run LinuxCNC." % str(e).strip())
        return 1
    print("\n".join(capture.describe(path)))
    print("Wrote %s. It holds bus data, the configuration and INI lines the report "
          "holds, and no other files." % path)
    return 0


def show_events(args):
    """Print the component's event log. Returns an exit code."""
    path = {"now": events.PATH, "prev": events.PATH + ".prev"}.get(args.events, args.events)
    try:
        log = events.read(path)
    except events.EventsError as e:
        print(e)
        return 1
    names = {}
    if log.config and os.path.exists(log.config):
        try:
            names = events.names_of(config.load(log.config))
        except (OSError, yaml.YAMLError):
            pass
    events.report(log, names)
    return 0


def update_file(args, proposals, period):
    """Update a configuration file from the proposals. Returns an exit code."""
    try:
        old = config.load(args.update)
    except (OSError, yaml.YAMLError) as e:
        print("Cannot read %s: %s" % (args.update, e))
        return 1
    fresh = config.from_proposals(proposals, args.interface, args.transport or "raw", period)
    changes = update.compare(old, fresh)
    cfg, notes = update.merge(old, fresh)
    notes += netif.fill_master(cfg, args.interface or netif.master_interface(),
                               keep_transport=bool(args.transport))
    print("Update of %s:" % args.update)
    for n in notes:
        print("  %s" % n)
    if args.ini:
        for n in update.consequences(old, changes, args.ini):
            if ":" in n.split(" ", 1)[0]:     # a HAL file's line
                print("  then: %s" % n)
    errors = config.check(cfg)
    for e in errors:
        print("Error: %s" % e)
    if errors:
        return 1
    out = args.write or args.update
    if cfg == old and out == args.update:
        print("  no changes: the file matches the bus")
        return 0
    if out == args.update:
        shutil.copyfile(args.update, args.update + ".bak")
        print("Kept the old file as %s.bak" % args.update)
    config.write(cfg, out)
    print("Wrote %s" % out)
    return 0


def import_file(args):
    """Import an lcec XML file, using the bus if a master runs."""
    xml_cfg = lcecxml.load(args.import_xml)
    period = servo_period(args.ini) if args.ini else None
    bus = {}
    try:
        positions = set(tool.slave_positions())
        esi_devices = esi.load_dirs(args.esi)
        for x in xml_cfg.slaves:
            if x.idx in positions:
                bus[x.idx] = read_slave(x.idx, esi_devices, args.mode)
        print("Read %d slaves from the bus" % len(bus))
    except tool.ToolError:
        print("No master running: imported from the XML file alone")
    cfg, notes = importxml.import_lcec(xml_cfg, bus, period)
    for n in notes:
        print("Note: %s" % n)
    errors = config.check(cfg)
    for e in errors:
        print("Error: %s" % e)
    if args.write and not errors:
        config.write(cfg, args.write)
        print("Wrote %s" % args.write)
    elif not args.write:
        print(config.dumps(cfg))
    return 1 if errors else 0


def check_file(path, esi_paths=()):
    """Check a configuration file, then the bus against it. Returns an exit code."""
    try:
        cfg = config.load(path)
    except (OSError, yaml.YAMLError) as e:
        print("Cannot read %s: %s" % (path, e))
        return 1
    errors = config.check(cfg)
    for e in errors:
        print("Error: %s" % e)
    from . import quirks
    quirks.load()
    for e in quirks.problems:
        print("Warning: quirk file not read: %s" % e)
    if errors:
        return 1
    print("%s: %d slaves, no errors in the file" % (path, len(cfg["slaves"])))
    if esi_paths:
        # settings against the ESI files' object dictionaries
        sess = session.Session(esi_paths)
        sess.cfg = cfg
        for level, text in sess.setting_problems():
            print("%s: %s" % (level.capitalize(), text))
    try:
        positions = set(tool.slave_positions())
    except tool.ToolError:
        print("No master running: the bus was not checked")
        return 0
    problems, notes = compare_bus(cfg, positions)
    for m in problems + notes:
        print("Bus: %s" % m)
    # DC settings against what each slave can do (warnings only)
    from . import dcinfo, esi
    devices = esi.load_dirs(esi_paths) if esi_paths else []
    for s in cfg.get("slaves") or []:
        if s.get("position") not in positions:
            continue
        try:
            d = dcinfo.read(s["position"], devices)
            if not session.same_kind(s, tool.slave_info(s["position"])):
                continue
        except tool.ToolError:
            continue
        for level, text in dcinfo.problems(s, d):
            print("%s: slave %s (%s): %s" % (level.capitalize(), s["position"], s.get("name"), text))
    bad = len(problems)
    if not bad:
        print("Bus: matches the file")
    return 1 if bad else 0


if __name__ == "__main__":
    main()
