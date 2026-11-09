"""The state behind EtherCAT Setup's window, without Qt: a configuration
being edited, what was read from the bus, and the actions on them (read
the bus, update the file from it, propose a slave again, check, save).

The command line uses the same functions.
"""

import copy
import os
import shutil

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


def read_slave(pos, esi_devices, mode, pdo_set=None, progress=None):
    """Read one slave and propose its configuration: (info, coe, proposal)."""
    info = tool.slave_info(pos)
    sii = sii_mod.parse(tool.sii(pos))
    coe = coe_mod.Dictionary(tool.sdos_settled(pos, progress=progress)) if sii.has_coe \
        else coe_mod.Dictionary()
    device_type = tool.upload(pos, 0x1000, 0, "uint32") if coe else None
    modes = None
    if device_type is not None and device_type & 0xFFFF == 402 and 0x6502 in coe.objects:
        modes = tool.upload(pos, 0x6502, 0, "uint32")
    dev, exact = esi.find(esi_devices, sii.vendor_id, sii.product_code, sii.revision)
    p = propose.propose(info, sii, coe, device_type, modes, dev, exact, mode, pdo_set)
    if p.profile == 402 and mode == "pp":
        # proposed from the drive's own values; the user must check them
        for index, sub, dtype in propose.PP_LIMITS:
            v = tool.upload(pos, index, sub, "int32" if dtype == "DINT" else "uint32")
            if v is not None:
                p.pp_limits[(index, sub)] = v
        p.notes.append("PP: software limits 0x607D and max profile velocity 0x607F are the drive's "
                       "present values (%s); check them" % (", ".join(
                           "0x%04X:%02X=%d" % (i, s_, v) for (i, s_), v in sorted(p.pp_limits.items()))
                           or "none could be read"))
    return info, coe, p


def compare_bus(cfg, positions, slave_info=None):
    """The bus against a configuration: (problems, notes), as readable
    strings. A problem is a slave that is missing or of another kind; a
    note is a different revision or a slave not in the file."""
    slave_info = slave_info or tool.slave_info
    problems, notes = [], []
    positions = set(positions)
    for s in cfg.get("slaves") or []:
        pos, ident = s["position"], s.get("identity") or {}
        if pos not in positions:
            problems.append("slave %d (%s) is missing" % (pos, s.get("name")))
            continue
        info = slave_info(pos)
        want = (int(str(ident.get("vendor")), 16), int(str(ident.get("product")), 16))
        if (info["vendor_id"], info["product_code"]) != want:
            problems.append("slave %d is 0x%08X/0x%08X, the file expects %s/%s (%s)"
                            % (pos, info["vendor_id"], info["product_code"], ident.get("vendor"),
                               ident.get("product"), s.get("device", "")))
        elif "revision" in ident and info["revision"] != int(str(ident["revision"]), 16):
            notes.append("slave %d has revision 0x%08X, the file expects %s"
                         % (pos, info["revision"], ident["revision"]))
    for pos in sorted(positions - {s["position"] for s in cfg.get("slaves") or []}):
        notes.append("slave %d is not in the file" % pos)
    return problems, notes


class BusSlave:
    """What was read from one slave on the bus."""

    def __init__(self, info, coe, proposal):
        self.info, self.coe, self.proposal = info, coe, proposal

    @property
    def position(self):
        return self.info["position"]


class Session:
    """A configuration being edited, and the bus it is for."""

    def __init__(self, esi_paths=(), ini_path=None, mode="csp"):
        self.esi_paths = list(esi_paths)
        self.esi_devices = esi.load_dirs(self.esi_paths)
        self.ini_path = ini_path
        self.period = servo_period(ini_path) if ini_path else None
        self.mode = mode
        self.path = None        # the file the configuration came from, or is saved to
        self.cfg = None         # the configuration (the file's YAML structure)
        self.saved = None       # a copy of cfg as last loaded or saved
        self.bus = {}           # position -> BusSlave
        self.bus_error = None   # why the bus could not be read, or None

    # the configuration

    @property
    def dirty(self):
        return self.cfg is not None and self.cfg != self.saved

    def new(self, interface=None, transport="raw"):
        """A configuration proposed from the bus as read."""
        self.cfg = config.from_proposals([b.proposal for _, b in sorted(self.bus.items())],
                                         interface, transport, self.period)
        self.saved = None   # the file name, if one was given, is kept
        return []

    def load(self, path):
        cfg = config.load(path)
        if not isinstance(cfg, dict):
            raise ValueError("%s is not an ecat configuration" % path)
        self.cfg, self.path, self.saved = cfg, path, copy.deepcopy(cfg)

    def import_xml(self, path):
        """Import an lcec XML file, with what was read from the bus.
        Returns the notes. The result has no file name yet."""
        xml_cfg = lcecxml.load(path)
        bus = {pos: (b.info, b.coe, b.proposal) for pos, b in self.bus.items()}
        self.cfg, notes = importxml.import_lcec(xml_cfg, bus, self.period)
        self.path, self.saved = None, None
        return notes

    def update_from_bus(self, interface=None, transport="raw"):
        """Bring the configuration up to date with the bus, keeping its
        choices (see update.py). Returns the notes."""
        fresh = config.from_proposals([b.proposal for _, b in sorted(self.bus.items())],
                                      interface, transport, self.period)
        if self.cfg is None:
            self.cfg = fresh
            return ["proposed from the bus"]
        self.cfg, notes = update.merge(self.cfg, fresh)
        return notes

    def check(self):
        """Errors in the configuration, and the bus compared with it:
        (errors, bus problems, bus notes)."""
        if self.cfg is None:
            return [], [], []
        errors = config.check(self.cfg)
        if not self.bus:
            return errors, [], []
        problems, notes = compare_bus(self.cfg, self.bus, lambda pos: self.bus[pos].info)
        return errors, problems, notes

    def save(self, path=None):
        """Write the configuration; an existing file is kept as .bak.
        Raises ValueError if it has errors."""
        path = path or self.path
        if not path:
            raise ValueError("no file name")
        errors = config.check(self.cfg)
        if errors:
            raise ValueError("not saved, the configuration has errors:\n  " + "\n  ".join(errors))
        if os.path.exists(path):
            shutil.copyfile(path, path + ".bak")
        config.write(self.cfg, path)
        self.path, self.saved = path, copy.deepcopy(self.cfg)

    def dumps(self):
        return config.dumps(self.cfg) if self.cfg is not None else ""

    def slave(self, position):
        for s in (self.cfg or {}).get("slaves") or []:
            if s.get("position") == position:
                return s
        return None

    # the bus

    def read_bus(self, progress=lambda text: None, positions=None):
        """Read the slaves on the bus. Raises tool.ToolError without a master."""
        positions = positions if positions is not None else tool.slave_positions()
        bus = {}
        for pos in positions:
            progress("Reading slave %d" % pos)
            mode = self._slave_mode(pos)
            bus[pos] = BusSlave(*read_slave(pos, self.esi_devices, mode, progress=progress))
        self.bus, self.bus_error = bus, None
        return bus

    def _slave_mode(self, pos):
        s = self.slave(pos)
        return ((s or {}).get("cia402") or {}).get("mode") or self.mode

    def propose_again(self, position, mode=None, pdo_set=None):
        """Read one slave again and replace its entry with a new proposal in
        the given mode or PDO assignment. Its name and other modes are kept.
        Returns the proposal's notes."""
        old = self.slave(position)
        info, coe, p = read_slave(position, self.esi_devices, mode or self._slave_mode(position),
                                  pdo_set)
        self.bus[position] = BusSlave(info, coe, p)
        new = config.slave_from_proposal(p, name=(old or {}).get("name"))
        if old is not None:
            modes = (old.get("cia402") or {}).get("modes")
            if modes and "cia402" in new:
                new["cia402"]["modes"] = [m for m in modes if m != new["cia402"]["mode"]]
            slaves = self.cfg["slaves"]
            slaves[slaves.index(old)] = new
        else:
            if self.cfg is None:
                self.cfg = config.from_proposals([], None, "raw", self.period)
            self.cfg["slaves"].append(new)
            self.cfg["slaves"].sort(key=lambda s: s["position"])
        return list(p.notes)

    def object_name(self, position, index, subindex):
        b = self.bus.get(position)
        return b.coe.name(index, subindex) if b and b.coe else ""


def load_yaml_text(text):
    """A configuration from YAML text, for editing the file as text."""
    cfg = yaml.safe_load(text)
    if not isinstance(cfg, dict):
        raise ValueError("not a YAML mapping")
    return cfg
