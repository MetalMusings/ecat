"""The state behind EtherCAT Setup's window, without Qt: a configuration
being edited, what was read from the bus, and the actions on them (read
the bus, update the file from it, propose a slave again, check, save).

The command line uses the same functions.
"""

import copy
import os
import shutil

import yaml

from . import coe as coe_mod, config, dcinfo, esi, guidance, importxml, integrate, lcecxml, measured, netif, pdoreq, propose, \
    quirks, sii as sii_mod, tool, update


def ini_value(ini_path, section, key):
    """The first value of a key, read as LinuxCNC reads its INI files
    (#INCLUDEs expanded, the first section of a name only); None if not
    there."""
    return integrate.ini_get(integrate.read_ini(ini_path), section, key)


def servo_period(ini_path):
    """[EMCMOT]SERVO_PERIOD in ns, from a LinuxCNC INI file."""
    try:
        return int(float(ini_value(ini_path, "EMCMOT", "SERVO_PERIOD")))
    except (OSError, TypeError, ValueError):
        return None


def read_modules(pos, coe, index):
    """A modular device's module list (ETG.5001): 0xF050 the modules it
    detects, 0xF030 those it is configured for. [ident] or None."""
    if index not in coe.objects:
        return None
    n = tool.upload(pos, index, 0, "uint8")
    if n is None:
        return None
    out = []
    for sub in range(1, min(int(n), 255) + 1):
        v = tool.upload(pos, index, sub, "uint32")
        if v is None:
            return None
        out.append(int(v))
    return out


def sdo_info(sii):
    """Whether the slave lists its object dictionary (SII CoE details bit 1, ETG.2010);
    True when the SII leaves the CoE details empty."""
    return not sii.coe_details or bool(sii.coe_details & 0x02)


def read_slave(pos, esi_devices, mode, pdo_set=None, progress=None, extra_modes=(), way=None,
               profile=None, assignment=None):
    """Read one slave and propose its configuration: (info, coe, proposal)."""
    info = tool.slave_info(pos)
    sii = sii_mod.parse(tool.sii(pos))
    # a slave whose SII says it has no SDO information (such as StepperOnline's A6) gives
    # the master no dictionary to list: waiting for one to settle would only run out the
    # timeout (20 s)
    coe = coe_mod.Dictionary(tool.sdos_settled(pos, progress=progress)) \
        if sii.has_coe and sdo_info(sii) else coe_mod.Dictionary()
    dev, exact = esi.find(esi_devices, sii.vendor_id, sii.product_code, sii.revision)
    # what the slave has: its own dictionary, or, when it gives none (no SDO
    # information, such as StepperOnline's A6), the ESI file's
    known = coe
    if sii.has_coe and not coe.objects and dev:
        known = esi.dictionary(dev)
    # 0x1000 is mandatory for every CoE slave
    device_type = tool.upload(pos, 0x1000, 0, "uint32") if sii.has_coe else None
    modes = None
    if device_type is not None and device_type & 0xFFFF == 402 and 0x6502 in known.objects:
        modes = tool.upload(pos, 0x6502, 0, "uint32")
    # a modular device's modules, before the proposal: with Slots in its ESI file the
    # modules' PDOs come from there (slots.py)
    detected = configured = None
    if sii.has_coe:
        listed = coe if coe.objects else known
        if dev is not None and dev.slots is not None and 0xF050 not in listed.objects:
            listed = coe_mod.Dictionary()
            listed.objects = {0xF050: "Detected module ident list", 0xF030: "Configured module ident list"}
        detected = read_modules(pos, listed, 0xF050)
        configured = read_modules(pos, listed, 0xF030)
    p = propose.propose(info, sii, coe, device_type, modes, dev, exact, mode, pdo_set, extra_modes,
                        way, profile, modules=detected, assignment=assignment)
    # what was read, so that the PDOs can be proposed again without the bus (assign_pdos)
    p.reads = {"sii": sii, "device_type": device_type, "drive_modes": modes, "dev": dev,
               "exact": exact, "detected": detected, "configured": configured}
    p.dc_info.sync = dcinfo.read_sync(pos, known)
    if p.profile == 402:
        # each axis's maximum profile velocity, for checking the INI file's joints
        for n in range(getattr(p, "axes", 1)):
            index = 0x607F + n * 0x800
            if index in known.objects:
                v = tool.upload(pos, index, 0, "uint32")
                if v:
                    p.max_velocity[n] = int(v)
    if detected is not None:
        propose.add_modules(p, detected, configured)
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


def _mods(mods):
    return ", ".join("0x%08X" % int(str(m), 16) for m in mods) if mods else "none"


def modules_match(in_file, on_bus):
    """Whether a slave's modules on the bus are those in the file. A file
    without modules (not a modular device, or written before they were
    recorded) matches anything."""
    if in_file is None or on_bus is None:
        return True
    try:
        return [int(str(m), 16) for m in in_file] == [int(m) for m in on_bus]
    except ValueError:
        return False


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


def same_kind(s, info):
    """Whether the slave on the bus (info) is the kind the file's slave s is
    for (vendor and product; True if the file does not say)."""
    ident = (s or {}).get("identity") or {}
    try:
        want = (int(str(ident["vendor"]), 16), int(str(ident["product"]), 16))
    except (KeyError, ValueError):
        return True
    return (info.get("vendor_id"), info.get("product_code")) == want


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
        # True: period is the INI file's SERVO_PERIOD; False: typed in (or
        # None), for trying slaves without a machine configuration
        self.period_from_ini = self.period is not None
        self.mode = mode
        self.path = None        # the file the configuration came from, or is saved to
        self.cfg = None         # the configuration (the file's YAML structure)
        self.saved = None       # a copy of cfg as last loaded or saved
        self.bus = {}           # position -> BusSlave
        self.bus_error = None   # why the bus could not be read, or None
        self.bus_interface = None  # the interface the master read the bus through, if known

    # the configuration

    @property
    def dirty(self):
        return self.cfg is not None and self.cfg != self.saved

    def new(self, interface=None, transport="raw"):
        """A configuration proposed from the bus as read. Returns the notes."""
        self.cfg = config.from_proposals([b.proposal for _, b in sorted(self.bus.items())],
                                         interface, transport, self.period)
        self.saved = None   # the file name, if one was given, is kept
        return netif.fill_master(self.cfg, self.bus_interface)

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
        filled = netif.fill_master(self.cfg, self.bus_interface, keep_transport=bool(xml_cfg.transport))
        if filled:
            notes = [n for n in notes if not n.startswith("master: no interface")]
            if self.cfg["master"]["transport"] != "raw":
                notes = [n for n in notes if not n.startswith("master: no transport")]
        self.path, self.saved = None, None
        return notes + filled

    def fresh(self, interface=None, transport="raw"):
        """A configuration proposed from the bus as it is."""
        return config.from_proposals([b.proposal for _, b in sorted(self.bus.items())],
                                     interface, transport, self.period)

    def drive_max_counts(self):
        """{axis name: maximum profile velocity in counts/s} of the drives
        read from the bus, by the configuration's names."""
        out = {}
        for s in (self.cfg or {}).get("slaves") or []:
            b = self.bus.get(s.get("position"))
            mv = getattr(b.proposal, "max_velocity", None) if b is not None else None
            if mv:
                for n, axis in enumerate(config.axes(s)):
                    if n in mv:
                        out[axis] = mv[n]
        return out

    def bus_changes(self):
        """How the bus differs from the configuration (update.Changes), or
        None without both."""
        if self.cfg is None or not self.bus:
            return None
        return update.compare(self.cfg, self.fresh())

    def update_from_bus(self, interface=None, transport="raw"):
        """Bring the configuration up to date with the bus, keeping its
        choices (see update.py). Returns the notes."""
        fresh = config.from_proposals([b.proposal for _, b in sorted(self.bus.items())],
                                      interface, transport, self.period)
        if self.cfg is None:
            self.cfg = fresh
            return ["proposed from the bus"] + netif.fill_master(self.cfg, self.bus_interface)
        self.cfg, notes = update.merge(self.cfg, fresh)
        # what the device needs written at start (ESI InitCmds, the drive's mode): only
        # recommended for an existing file, never added on its own
        for s in self.cfg.get("slaves") or []:
            timeouts = self.missing_state_timeouts(s.get("position"))
            if timeouts:
                notes.append("slave %s (%s): recommended timeouts, from its ESI file: %s; add "
                             "them with \"Add what the device needs\" on its page"
                             % (s["position"], s.get("name"), ", ".join("%s %d ms" % kv for kv in sorted(timeouts.items()))))
            missing = self.missing_init_cmds(s.get("position"))
            if missing:
                notes.append("slave %s (%s): its ESI file writes at start what the file does not have yet: %s; "
                             "add them with \"Add what the device needs\" on its page"
                             % (s["position"], s.get("name"), ", ".join(_row_text(r) for r in missing)))
        return notes + netif.fill_master(self.cfg, self.bus_interface)

    def missing_init_cmds(self, position):
        """The init-cmds rows the proposal of the slave read from the bus has (its ESI file's
        InitCmds, 0xF030; a drive's mode where 0x6060 is not in the file's process data and
        no setting gives it) that the slave's entry lacks (a row turned off counts as there).
        Empty without the bus."""
        s, b = self.slave(position), self.bus.get(position)
        if s is None or b is None or not same_kind(s, b.info):
            return []

        def key(r):
            return (str(r.get("object")).upper(), bool(r.get("complete")), r.get("at"))
        have = {key(r) for r in s.get("init-cmds") or [] if isinstance(r, dict)}
        mapped = set()
        for d in (s.get("process-data") or {}).values():
            for pdo in d or []:
                for e in pdo.get("entries") or []:
                    try:
                        mapped.add(config.parse_obj(e.get("object")))
                    except ValueError:
                        pass
        rest = [dict(r) for r in getattr(b.proposal, "init_cmds", None) or []
                if r.get("from") != "profile" and key(r) not in have]
        out = []
        # a drive's mode at start: decided by the file as it is now (its modes and PDOs may
        # have changed since the slave was read), not by the proposal
        c = s.get("cia402") or {}
        if s.get("profile") == "cia402" and not c.get("modes"):
            for a in range(len(c.get("axes") or [1])):
                index = 0x6060 + a * config.AXIS_OFFSET
                row = {"at": "PS", "object": "0x%04X:00" % index, "from": "profile"}
                if (index, 0) in mapped or config.mode_setting(s, a) is not None or any(
                        str(r.get("object")).upper() == row["object"].upper()
                        for r in s.get("init-cmds") or [] if isinstance(r, dict)):
                    continue        # set every cycle, already at start, or turned off
                out.append(row)
        return out + rest

    def missing_state_timeouts(self, position):
        """The state change timeouts the proposal from the bus has (the ESI file's, where
        longer than the master's) that the slave's entry lacks: {key: ms}; "sdo" for the
        SDO response timeout (sdo-timeout-ms)."""
        s, b = self.slave(position), self.bus.get(position)
        if s is None or b is None or not same_kind(s, b.info):
            return {}
        have = s.get("state-timeouts-ms") if isinstance(s.get("state-timeouts-ms"), dict) else {}
        out = {k: v for k, v in (getattr(b.proposal, "state_timeouts", None) or {}).items() if k not in have}
        if getattr(b.proposal, "sdo_timeout", None) and "sdo-timeout-ms" not in s:
            out["sdo"] = b.proposal.sdo_timeout       # sdo-timeout-ms
        return out

    def init_cmd_view(self, position):
        """Every InitCmd of the device and every init-cmds row of the file, for the window's
        card, each with what happens to it (shown, never hidden, the user's wish 2026-11-22):
        [{"row", "state", "text", "file_index"}]. States: "in-file" (written at start),
        "off" (disabled: true), "needed" (the file lacks it and the drive needs it),
        "offered" (the ESI file writes it, the file does not have it), "not-needed" (the
        profile sets it another way) and "skipped" (ecat does not write it)."""
        from . import initcmds
        s = self.slave(position)
        if s is None:
            return []
        dev = self.esi_device(position)
        found = initcmds.from_esi(dev, self.dictionary(position), s.get("profile")) if dev else None
        mapped = set()
        for d in (s.get("process-data") or {}).values():
            for pdo in d or []:
                for e in pdo.get("entries") or []:
                    try:
                        mapped.add(config.parse_obj(e.get("object")))
                    except ValueError:
                        pass
        c = s.get("cia402") or {}

        def mode_reason(obj_text):
            """Why the profile's mode row is not needed now, or None."""
            try:
                o = config.parse_obj(obj_text)
            except ValueError:
                return None
            if s.get("profile") != "cia402" or not 0x6060 <= o[0] <= 0x6060 + 3 * config.AXIS_OFFSET:
                return None
            if o in mapped:
                return "%s is in the PDOs: the profile sets the mode every cycle" % obj_text[:6]
            if c.get("modes"):
                return "the drive switches modes: 0x6060 must be in the PDOs"
            if config.mode_setting(dict(s, **{"init-cmds": []}), (o[0] - 0x6060) // config.AXIS_OFFSET) is not None:
                return "a setting writes 0x6060"
            return None

        out = []
        listed = set()
        for i, r in enumerate(s.get("init-cmds") or []):
            if not isinstance(r, dict):
                continue
            listed.add(str(r.get("object")).upper())
            if r.get("disabled"):
                out.append({"row": r, "state": "off", "file_index": i,
                            "text": "turned off: kept in the file, so that Update does not bring it back"})
                continue
            why = mode_reason(str(r.get("object"))) if r.get("from") == "profile" else None
            out.append({"row": r, "state": "in-file", "file_index": i,
                        "text": "written at start" + ("; not needed now: " + why if why else "")})
        missing = self.missing_init_cmds(position)
        for r in missing:
            listed.add(str(r.get("object")).upper())
            out.append({"row": r, "state": "needed" if r.get("from") == "profile" else "offered",
                        "file_index": None,
                        "text": "needed: the drive's mode is neither set nor seen" if r.get("from") == "profile"
                        else "the device's ESI file writes this; not in the file yet"})
        for r in (found.cmds if found else []):
            if str(r.get("object")).upper() in listed:
                continue
            listed.add(str(r.get("object")).upper())
            why = mode_reason(str(r.get("object"))) if r.get("from") == "profile" else None
            if why:
                out.append({"row": r, "state": "not-needed", "file_index": None, "text": "not needed: " + why})
            elif self.bus.get(position) is None:
                out.append({"row": r, "state": "offered", "file_index": None,
                            "text": "the device's ESI file writes this; not in the file yet"})
        for text, why in (found.skipped if found else []):
            out.append({"row": {"object": text}, "state": "skipped", "file_index": None,
                        "text": "not written by ecat: " + why})
        return out

    def add_device_needs(self, position):
        """Add what missing_init_cmds and missing_state_timeouts name; returns the rows
        added (and the timeouts, as {"object": "state-timeouts-ms.<key>", "value": ms})."""
        s = self.slave(position)
        added = self.missing_init_cmds(position)
        timeouts = self.missing_state_timeouts(position)
        if s is not None and added:
            s.setdefault("init-cmds", []).extend(dict(r) for r in added)
        if s is not None and "sdo" in timeouts:
            s["sdo-timeout-ms"] = timeouts.pop("sdo")
            added.append({"object": "sdo-timeout-ms", "value": s["sdo-timeout-ms"]})
        if s is not None and timeouts:
            if not isinstance(s.get("state-timeouts-ms"), dict):
                s["state-timeouts-ms"] = {}
            s["state-timeouts-ms"].update(timeouts)
        return added + [{"object": "state-timeouts-ms.%s" % k, "value": v} for k, v in sorted(timeouts.items())]

    def check(self):
        """Errors in the configuration, and the bus compared with it:
        (errors, bus problems, bus notes)."""
        if self.cfg is None:
            return [], [], []
        errors = config.check(self.cfg)
        if not self.bus:
            return errors, [], []
        problems, notes = compare_bus(self.cfg, self.bus, lambda pos: self.bus[pos].info)
        # a modular device's modules, as read with the bus
        for s in self.cfg.get("slaves") or []:
            b = self.bus.get(s.get("position"))
            have = getattr(b.proposal, "modules", None) if b is not None else None
            if b is not None and not modules_match(s.get("modules"), have):
                problems.append("slave %d (%s): its modules differ from the file (%s on the bus, "
                                "%s in the file): propose it again, since its process data "
                                "depends on them" % (s["position"], s.get("name"),
                                                     _mods(have), _mods(s.get("modules"))))
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
            bus[pos] = BusSlave(*read_slave(pos, self.esi_devices, mode, progress=progress,
                                            extra_modes=self._slave_modes(pos, mode)))
        self.bus, self.bus_error = bus, None
        self.bus_interface = netif.master_interface()
        return bus

    def _slave_mode(self, pos):
        s = self.slave(pos)
        return ((s or {}).get("cia402") or {}).get("mode") or self.mode

    def _slave_modes(self, pos, mode):
        """The other modes the slave's entry lists (cia402.modes), without `mode`."""
        c = (self.slave(pos) or {}).get("cia402") or {}
        return [m for m in (c.get("modes") or []) if m != mode]

    def propose_again(self, position, mode=None, pdo_set=None):
        """Read one slave again and replace its entry with a new proposal in
        the given mode or PDO assignment. Its name and other modes are kept.
        Returns the proposal's notes."""
        old = self.slave(position)
        mode = mode or self._slave_mode(position)
        info, coe, p = read_slave(position, self.esi_devices, mode, pdo_set,
                                  extra_modes=self._slave_modes(position, mode))
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
                netif.fill_master(self.cfg, self.bus_interface)
            self.cfg["slaves"].append(new)
            self.cfg["slaves"].sort(key=lambda s: s["position"])
        return list(p.notes)

    # what the PDO part of a slave's entry is made of: replaced together by a way or a profile
    PDO_KEYS = ("process-data", "encoders", "serial", "configure-pdos")

    def assign_pdos(self, position, indexes):
        """Read the slave and take the PDOs the user ticked (`indexes`, the device's PDO
        numbers) as its process data: an open PDO the file already has keeps its entries,
        a new one is filled with what the profile needs; the rest of its entry stays.
        Returns the proposal's notes."""
        old = self.slave(position)
        if old is None:
            raise ValueError("slave %d is not in the configuration" % position)
        mode = self._slave_mode(position)
        b = self.bus.get(position)
        reads = getattr(b.proposal, "reads", None) if b is not None else None
        if reads is not None:
            # proposed again from what was read: no bus traffic, at once
            info, coe = b.info, b.coe
            p = propose.propose(info, reads["sii"], coe, reads["device_type"], reads["drive_modes"],
                                reads["dev"], reads["exact"], mode, None,
                                self._slave_modes(position, mode), None, old.get("profile"),
                                modules=reads["detected"], assignment=list(indexes))
            if reads["detected"] is not None:
                propose.add_modules(p, reads["detected"], reads["configured"])
            was = b.proposal
            p.reads, p.dc_info.sync = reads, was.dc_info.sync
            p.max_velocity, p.pp_limits = was.max_velocity, was.pp_limits
        else:
            info, coe, p = read_slave(position, self.esi_devices, mode,
                                      extra_modes=self._slave_modes(position, mode),
                                      profile=old.get("profile"), assignment=list(indexes))
        self.bus[position] = BusSlave(info, coe, p)
        new = config.slave_from_proposal(p, name=old.get("name"))
        dev = self.esi_device(position)
        fixed = {d.index for d in dev.pdos if d.fixed} if dev else set()
        had = {(key, str(pdo.get("pdo")).upper()): pdo
               for key, pdos in (old.get("process-data") or {}).items() for pdo in pdos or []}
        for key, pdos in (new.get("process-data") or {}).items():
            for pdo in pdos or []:
                was = had.get((key, str(pdo.get("pdo")).upper()))
                try:
                    index = int(str(pdo.get("pdo")), 16)
                except ValueError:
                    continue
                if was is not None and index not in fixed:
                    pdo["entries"] = copy.deepcopy(was.get("entries") or [])
        _take_pdos(old, new)
        return list(p.notes)

    def apply_pdos(self, position, way=None, pdo_set=None):
        """Read the slave and take only its PDOs (process data, encoder and serial
        channels) from a proposal with the way the user chose ("recommended", a way's
        title, or a predefined set): the rest of its entry stays, and pins of objects
        that stay keep the names they had. Returns the proposal's notes."""
        old = self.slave(position)
        if old is None:
            raise ValueError("slave %d is not in the configuration" % position)
        mode = self._slave_mode(position)
        info, coe, p = read_slave(position, self.esi_devices, mode, pdo_set,
                                  extra_modes=self._slave_modes(position, mode), way=way,
                                  profile=old.get("profile"))
        self.bus[position] = BusSlave(info, coe, p)
        new = config.slave_from_proposal(p, name=old.get("name"))
        _take_pdos(old, new)
        return list(p.notes)

    def change_profile(self, position, profile):
        """The user's profile for a slave ("cia402", "mdp", "generic"): its PDOs proposed
        again for it, the profile's own block set (cia402: the mode, csp unless it had one);
        name, settings, DC and the rest stay. Returns the proposal's notes."""
        old = self.slave(position)
        if old is None:
            raise ValueError("slave %d is not in the configuration" % position)
        mode = ((old.get("cia402") or {}).get("mode")) or "csp"
        info, coe, p = read_slave(position, self.esi_devices, mode, None, profile=profile,
                                  extra_modes=self._slave_modes(position, mode) if profile == "cia402" else ())
        self.bus[position] = BusSlave(info, coe, p)
        new = config.slave_from_proposal(p, name=old.get("name"))
        _take_pdos(old, new)
        old["profile"] = new["profile"]
        if new["profile"] == "cia402":
            kept = old.get("cia402") or {}
            old["cia402"] = dict(new["cia402"], **{k: v for k, v in kept.items() if k != "mode"})
            old["cia402"]["mode"] = mode
        else:
            old.pop("cia402", None)
        return list(p.notes)

    def dictionary(self, position):
        """The object dictionary to guide a slave's settings: the slave's own,
        with the ESI file's ranges and defaults added; the ESI's alone when
        the slave was not read (coe.Dictionary, empty if there is neither)."""
        b = self.bus.get(position)
        mine = b.coe if b is not None and b.coe else coe_mod.Dictionary()
        dev = self.esi_device(position)
        return mine.enriched(esi.dictionary(dev)) if dev else mine

    def esi_device(self, position):
        """The ESI file's device for a slave (the bus's identity, else the
        configuration's), or None."""
        b = self.bus.get(position)
        s = self.slave(position)
        ident = quirks.identity(s) if s else None
        revision = None
        if b is not None and b.info.get("vendor_id") is not None:
            ident = (b.info["vendor_id"], b.info["product_code"])
            revision = b.info.get("revision")
        elif s:
            try:
                revision = int(str((s.get("identity") or {}).get("revision")), 16)
            except ValueError:
                pass
        if not ident:
            return None
        dev, _exact = esi.find(self.esi_devices, ident[0], ident[1], revision or 0)
        return dev

    def setting_problems(self):
        """[(level, text)] about the settings of all slaves, from the
        dictionaries: "error" or "warning"."""
        out = []
        for s in (self.cfg or {}).get("slaves") or []:
            for row, found in guidance.check_slave(s, self.dictionary(s["position"])).items():
                st = s["settings"][row]
                for level, text in found:
                    out.append((level, "slave %s (%s): setting %s: %s"
                                % (s["position"], s.get("name"), st.get("object"), text)))
            for text in pp_problems(s, self.dictionary(s["position"])):
                out.append(("warning", "slave %s (%s): %s" % (s["position"], s.get("name"), text)))
        return out

    def pdo_problems(self, position=None):
        """[(level, text)] about the slaves' PDOs against their ESI files (pdoreq.check_slave):
        "error", "warning" or "note". Nothing for a slave without a matching ESI file."""
        out = []
        for s in (self.cfg or {}).get("slaves") or []:
            if position is not None and s.get("position") != position:
                continue
            dev = self.esi_device(s.get("position"))
            try:
                exact = dev is not None and dev.revision == int(str((s.get("identity") or {}).get("revision")), 16)
            except ValueError:
                exact = False
            for level, text in pdoreq.check_slave(s, dev, exact):
                out.append((level, "slave %s (%s): %s" % (s.get("position"), s.get("name"), text)))
        return out

    def dc_info(self, position):
        """What the slave at a position can do with DC (dcinfo.DcInfo), or
        None if the bus was not read or another kind of slave is there."""
        b = self.bus.get(position)
        if b is None or not same_kind(self.slave(position), b.info):
            return None
        return getattr(b.proposal, "dc_info", None)

    def dc_problems(self):
        """[(level, text)] about the slaves' DC settings against what each
        slave read from the bus can do: "warning" or "note"."""
        out = [("warning", t) for t in config.dc_warnings(self.cfg)]
        out += [("note", t) for t in config.serial_notes(self.cfg)]
        with_host = any(isinstance(s.get("eoe"), dict) and "host-ip" in s["eoe"] for s in (self.cfg or {}).get("slaves") or [])
        out += [("warning", t) for t in config.eoe_warnings(self.cfg, config.host_networks() if with_host else ())]
        for s in (self.cfg or {}).get("slaves") or []:
            b = self.bus.get(s.get("position"))
            if s.get("eoe") and b is not None and same_kind(s, b.info) and not getattr(b.proposal, "has_eoe", True):
                out.append(("warning", "slave %s (%s): has an eoe: block, but its SII does not list the EoE "
                            "mailbox protocol: the master cannot set its IP address" % (s["position"], s.get("name"))))
        ref = config.ref_clock(self.cfg)
        for s in (self.cfg or {}).get("slaves") or []:
            for level, text in (dcinfo.problems(s, self.dc_info(s.get("position")), self.period, ref_clock=ref is s)
                                + (measured.problems(s, self.period) if s.get("dc") else [])):
                out.append((level, "slave %s (%s): %s" % (s["position"], s.get("name"), text)))
        return out

    def object_name(self, position, index, subindex):
        b = self.bus.get(position)
        return b.coe.name(index, subindex) if b and b.coe else ""


def _row_text(r):
    """"0x8000:06 = 1", "0x1C32:02 = the cycle time", "0x6060:00 = the mode" of an init-cmds row."""
    what = {"profile": "the mode", "cycle-time": "the cycle time"}.get(r.get("from"))
    return "%s = %s%s" % (r.get("object"), what or r.get("value", r.get("data")),
                          " (complete access)" if r.get("complete") else "")


def _take_pdos(old, new):
    """Put new's PDO part into old; a pin of an object that stays keeps its name (and
    what the user set on it: scale, offset, invert, bit pins)."""
    kept = {}
    for d in ((old.get("process-data") or {}).values()):
        for pdo in d or []:
            for e in pdo.get("entries") or []:
                if e.get("pin") or e.get("split"):
                    kept[e.get("object")] = e
    for d in ((new.get("process-data") or {}).values()):
        for pdo in d or []:
            for e in pdo.get("entries") or []:
                was = kept.get(e.get("object"))
                if was is not None and e.get("use") is None and (e.get("pin") or e.get("split")):
                    for key in ("pin", "split", "hal-type", "scale", "offset", "invert", "extend", "raw"):
                        e.pop(key, None)
                        if key in was:
                            e[key] = copy.deepcopy(was[key])
    for key in Session.PDO_KEYS:
        if key in new:
            old[key] = new[key]
        else:
            old.pop(key, None)


def pp_problems(slave, dictionary):
    """A cia402 drive with PP whose dictionary has the maximum profile
    velocity (0x607F) that its settings do not set: texts. Not an error,
    since a drive may not have the object (config.check needs 0x607D only)."""
    c = slave.get("cia402") or {}
    if slave.get("profile") != "cia402" or "pp" not in [c.get("mode")] + list(c.get("modes") or []):
        return []
    have = set()
    for st in slave.get("settings") or []:
        try:
            i, sub = str(st.get("object")).split(":")
            have.add((int(i, 16), int(sub, 16)))
        except ValueError:
            pass
    out = []
    index, sub, _ = propose.PP_MAX_VELOCITY
    for a in range(len(c.get("axes") or []) or 1):
        o = index + a * propose.AXIS_OFFSET
        if dictionary and o in dictionary.objects and (o, sub) not in have:
            out.append("cia402 mode pp: the drive has 0x%04X (max profile velocity) and the settings "
                       "do not set it; the drive plans PP moves with its present value" % o)
    return out


def load_yaml_text(text):
    """A configuration from YAML text, for editing the file as text."""
    cfg = yaml.safe_load(text)
    if not isinstance(cfg, dict):
        raise ValueError("not a YAML mapping")
    return cfg
