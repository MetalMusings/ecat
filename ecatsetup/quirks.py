"""Quirks: what EtherCAT Setup knows about particular devices beyond
their ESI file, SII and dictionary, per (vendor, product).

Quirks act only through what EtherCAT Setup writes into ethercat.yaml or
does in its test mode; the component has no quirk table, so that the
file stays complete and predictable (docs/design/ethercat-setup.md,
"Decision: profiles with quirks, no product drivers"). A new entry is
data, not code: propose one with the device's vendor and product codes,
and a source (manual, or a test on a machine) for each fact.

Keys of an entry:

- "name": the device, for messages;
- "commands": {index: why} objects that are commands, not settings: the
  test mode does not write them, and a configuration should not set them
  at every start;
- "limits": where the drive test finds a limit, if not in the CiA 402
  object: {limit: (index, subindex, ethercat tool type)}, or a text
  saying why the drive's object is not used;
- "import": {(index, subindex): (what, note)} lcec sdoConfig entries that
  the import replaces: what is "cia402.zero-at-start" or None (dropped).
"""

ECT60 = (0x00000A88, 0x0A880002)

QUIRKS = {
    ECT60: {
        "name": "Rtelligent ECT60",
        "commands": {
            0x2010: "Zero Position on the ECT60: a command, not a setting",
        },
        "limits": {
            # "Position trace error Limit", in pulses (drive error 0x8611
            # when exceeded)
            "following-error": (0x2022, 0, "uint32"),
            # tried on the plasma cutter, 2026-11-09: in CSP it moved 4.6 mm
            # past a 0x607D limit of 1 mm, without an error
            "travel": "the ECT60 ignores its software position limits (0x607D) in CSP",
        },
        "import": {
            # written at every start to make the drive report 0; did not
            # work as a start-up SDO (the plasma cutter, 2026-11-10)
            (0x2010, 0): ("cia402.zero-at-start",
                          "0x2010 (Zero Position) replaced by cia402.zero-at-start: the "
                          "drive's position at start counts as 0, without writing to it"),
        },
    },
}


def identity(slave):
    """(vendor, product) of a configuration's slave, or None."""
    ident = (slave or {}).get("identity") or {}
    try:
        return int(str(ident.get("vendor")), 16), int(str(ident.get("product")), 16)
    except ValueError:
        return None


def of(key):
    """The quirks of a (vendor, product), or of a configuration's slave."""
    if isinstance(key, dict):
        key = identity(key)
    return QUIRKS.get(key, {})


def command(slave, index):
    """Why an object of this slave is a command, not a setting; or None."""
    return of(slave).get("commands", {}).get(index)


# -- lcec modParams, translated into settings by the XML import

def _milli(text):
    return int(round(float(text) * 1000))


def _enum(table):
    def conv(text):
        v = int(float(text))
        if v not in table:
            raise ValueError("%s is not one of %s" % (text, ", ".join(str(k) for k in table)))
        return table[v]
    return conv


def _flag(text):
    return 1 if text.strip().lower() in ("1", "true", "yes", "on") else 0


# What lcec's drivers wrote for each modParam: name -> (index, subindex,
# type, conversion). lcec_el7041.c lacks a break after motorEMF and the
# following cases, so it also wrote that value into the objects after it;
# the import writes only the object each modParam is meant for.
_EL7041 = {
    "maxCurrent": (0x8010, 0x01, "UINT", _milli),       # A -> mA
    "reducedCurrent": (0x8010, 0x02, "UINT", _milli),
    "nominalVoltage": (0x8010, 0x03, "UINT", _milli),   # x1000, as lcec wrote it
    "coilResistance": (0x8010, 0x04, "UINT", _milli),   # x1000, as lcec wrote it
    "motorEMF": (0x8010, 0x05, "UINT", _milli),         # x1000, as lcec wrote it
    "motorFullsteps": (0x8010, 0x06, "UINT", int),
    "encoderIncrements": (0x8010, 0x07, "UINT", int),
    "startVelocity": (0x8010, 0x09, "UINT", int),
    "driveOnDelay": (0x8010, 0x10, "UINT", int),
    "driveOffDelay": (0x8010, 0x11, "UINT", int),
    "maxSpeed": (0x8012, 0x05, "UINT", _enum({1000: 0, 2000: 1, 4000: 2, 8000: 3,
                                              16000: 4, 32000: 5})),
    "encoder": (0x8012, 0x08, "USINT", _flag),
    "microsteps": (0x8012, 0x45, "USINT", _enum({1: 0, 2: 1, 4: 2, 8: 3, 16: 4, 32: 5, 64: 6})),
}
# lcec_el70x1.c (position interface): raw values, and position mode always
_EL70X1 = {
    "maxCurrent": (0x8010, 0x01, "UINT", int),
    "redCurrent": (0x8010, 0x02, "UINT", int),
    "nomVoltage": (0x8010, 0x03, "UINT", int),
    "coilRes": (0x8010, 0x04, "UINT", int),
    "motorEMF": (0x8010, 0x05, "UINT", int),
}
_POSITION_MODE = [(0x8012, 0x01, "USINT", 3,
                   "operation mode 3 (position interface), which lcec's el70x1 driver set")]

LCEC_MODPARAMS = {
    "EL7041": (_EL7041, []), "EL7041_1000": (_EL7041, []), "EL7041-1000": (_EL7041, []),
    "EP7041": (_EL7041, []),
    "EL7031": (_EL70X1, _POSITION_MODE), "EL7041-0052": (_EL70X1, _POSITION_MODE),
}


def lcec_modparams(lcec_type, modparams):
    """lcec modParams of a slave as settings: ([(index, subindex, type,
    value, name)], notes)."""
    table, always = LCEC_MODPARAMS.get(lcec_type, ({}, []))
    out, notes = [], []
    for index, sub, dtype, value, why in always:
        out.append((index, sub, dtype, value, None))
        notes.append("0x%04X:%02X = %d added: %s" % (index, sub, value, why))
    for name, text in modparams:
        if name not in table:
            notes.append("modParam %s=%s not imported%s" % (
                name, text, "" if table else " (no translation for lcec type %s)" % lcec_type))
            continue
        index, sub, dtype, conv = table[name]
        try:
            value = conv(text)
        except (TypeError, ValueError) as e:
            notes.append("modParam %s=%s not imported: %s" % (name, text, e))
            continue
        out = [o for o in out if (o[0], o[1]) != (index, sub)]
        out.append((index, sub, dtype, value, name))
        notes.append("modParam %s=%s imported as setting 0x%04X:%02X = %d"
                     % (name, text, index, sub, value))
    return out, notes
