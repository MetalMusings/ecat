"""ecat setup prototype: propose a configuration for the slaves on the bus,
from their own data and ESI files, and compare it with an lcec XML file.

    python3 -m ecatsetup [--esi DIR] [--ini FILE] [--compare XML] [-p POS]
    python3 -m ecatsetup ... --write ethercat.yaml [--interface IF] [--transport T]
    python3 -m ecatsetup --check ethercat.yaml
    python3 -m ecatsetup --import ethercat-conf.xml --write ethercat.yaml

Only reads from the bus; nothing is written to the slaves.
"""

import argparse
import json
import shutil
import sys
from dataclasses import asdict

import yaml

from . import coe as coe_mod, config, esi, importxml, lcecxml, propose, sii as sii_mod, tool, update


def ini_value(ini_path, section, key):
    """The first value of a key, read as LinuxCNC reads its INI files: text
    before the first section and lines without "=" are ignored."""
    current = None
    with open(ini_path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("[") and "]" in line:
                current = line[1:line.index("]")].strip()
            elif current == section and "=" in line and not line.startswith(("#", ";")):
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.split("#")[0].strip()
    return None


def servo_period(ini_path):
    """[EMCMOT]SERVO_PERIOD in ns, from a LinuxCNC INI file."""
    try:
        return int(float(ini_value(ini_path, "EMCMOT", "SERVO_PERIOD")))
    except (OSError, TypeError, ValueError):
        return None


def read_slave(pos, esi_devices, mode):
    info = tool.slave_info(pos)
    sii = sii_mod.parse(tool.sii(pos))
    coe = coe_mod.Dictionary(tool.sdos(pos)) if sii.has_coe else coe_mod.Dictionary()
    device_type = tool.upload(pos, 0x1000, 0, "uint32") if coe else None
    modes = None
    if device_type is not None and device_type & 0xFFFF == 402 and 0x6502 in coe.objects:
        modes = tool.upload(pos, 0x6502, 0, "uint32")
    dev, exact = esi.find(esi_devices, sii.vendor_id, sii.product_code, sii.revision)
    p = propose.propose(info, sii, coe, device_type, modes, dev, exact, mode)
    return info, coe, p


def entry_line(e, coe):
    name = (coe.name(e.index, e.subindex) if coe else "") or e.name
    what = {"profile": "(drive profile)", "gap": "(gap)"}.get(e.role, "%-28s %s" % (e.pin, e.hal_type))
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
        elif e.role == "profile":
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
    ap = argparse.ArgumentParser(prog="python3 -m ecatsetup", description=__doc__.split("\n")[0])
    ap.add_argument("--esi", action="append", default=[], help="ESI file or folder (repeatable)")
    ap.add_argument("--ini", help="LinuxCNC INI file, for the servo period")
    ap.add_argument("--compare", help="lcec XML file (ethercat-conf.xml) to compare with")
    ap.add_argument("--mode", default="csp", choices=sorted(propose.CIA402_MODES),
                    help="cyclic mode for CiA 402 drives")
    ap.add_argument("-p", "--position", type=int, action="append", help="only these slaves")
    ap.add_argument("--json", action="store_true", help="the proposals as JSON")
    ap.add_argument("--write", metavar="FILE", help="write the proposals as an ecat configuration")
    ap.add_argument("--interface", help="network interface, for --write")
    ap.add_argument("--transport", default="raw", choices=sorted(config.TRANSPORTS),
                    help="master transport, for --write")
    ap.add_argument("--update", metavar="FILE",
                    help="update an ecat configuration from the bus, keeping its choices; "
                         "written back with a .bak copy, or to --write")
    ap.add_argument("--check", metavar="FILE", help="check an ecat configuration, and the bus against it")
    ap.add_argument("--import", dest="import_xml", metavar="XML",
                    help="import an lcec XML file; with --write, write it as an ecat configuration")
    args = ap.parse_args(argv)

    if args.check:
        sys.exit(check_file(args.check))
    if args.import_xml:
        sys.exit(import_file(args))

    esi_devices = esi.load_dirs(args.esi)
    period = servo_period(args.ini) if args.ini else None
    hand = {s.idx: s for s in lcecxml.load(args.compare).slaves} if args.compare else {}

    try:
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
        info, coe, p = read_slave(pos, esi_devices, args.mode)
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
            config.write(config.from_proposals(proposals, args.interface, args.transport, period), args.write)
        except ValueError as e:
            sys.exit(str(e))
        print("Wrote %s" % args.write)


def update_file(args, proposals, period):
    """Update a configuration file from the proposals. Returns an exit code."""
    try:
        old = config.load(args.update)
    except (OSError, yaml.YAMLError) as e:
        print("Cannot read %s: %s" % (args.update, e))
        return 1
    fresh = config.from_proposals(proposals, args.interface, args.transport, period)
    cfg, notes = update.merge(old, fresh)
    print("Update of %s:" % args.update)
    for n in notes:
        print("  %s" % n)
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


def check_file(path):
    """Check a configuration file, then the bus against it. Returns an exit code."""
    try:
        cfg = config.load(path)
    except (OSError, yaml.YAMLError) as e:
        print("Cannot read %s: %s" % (path, e))
        return 1
    errors = config.check(cfg)
    for e in errors:
        print("Error: %s" % e)
    if errors:
        return 1
    print("%s: %d slaves, no errors in the file" % (path, len(cfg["slaves"])))
    try:
        positions = set(tool.slave_positions())
    except tool.ToolError:
        print("No master running: the bus was not checked")
        return 0
    bad = 0
    for s in cfg["slaves"]:
        pos, ident = s["position"], s["identity"]
        if pos not in positions:
            print("Bus: slave %d (%s) is missing" % (pos, s["name"]))
            bad += 1
            continue
        info = tool.slave_info(pos)
        want = (int(ident["vendor"], 16), int(ident["product"], 16))
        if (info["vendor_id"], info["product_code"]) != want:
            print("Bus: slave %d is 0x%08X/0x%08X, the file expects %s/%s (%s)"
                  % (pos, info["vendor_id"], info["product_code"], ident["vendor"],
                     ident["product"], s.get("device", "")))
            bad += 1
        elif "revision" in ident and info["revision"] != int(ident["revision"], 16):
            print("Bus: slave %d has revision 0x%08X, the file expects %s"
                  % (pos, info["revision"], ident["revision"]))
    extra = positions - {s["position"] for s in cfg["slaves"]}
    for pos in sorted(extra):
        print("Bus: slave %d is not in the file" % pos)
    if not bad:
        print("Bus: matches the file")
    return 1 if bad else 0


if __name__ == "__main__":
    main()
