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
