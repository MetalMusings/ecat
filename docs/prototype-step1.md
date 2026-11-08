# Step 1: can slaves be configured from their own data?

Status: done, 2026-11-08. Step 1 of the plan in the
[design draft](design/ethercat-setup.md).

## The question

Can a slave's process data, distributed clocks and HAL pins be proposed
from what can be read (the slave's SII, its CoE dictionary, a few CoE
objects, and an ESI file if one matches), without a hand-written mapping?

## How it was tried

The prototype, `ecatsetup` (Python, no GUI), reads the bus through the
userspace master's `ethercat` tool. It only reads: the slave list, SII
contents, the CoE dictionary and single SDO uploads (0x1000 device type,
0x6502 supported drive modes). It proposes a configuration per slave and
compares it with an lcec XML file:

```
python3 -m ecatsetup --esi <ESI folder> --ini <machine>.ini --compare ethercat-conf.xml
```

It was run against a plasma cutter's bus while LinuxCNC was running on it
(the tool reached the master inside LinuxCNC), and compared with the
machine's hand-written `ethercat-conf.xml`, which is known to work:

| Pos | Slave | Profile found |
|---|---|---|
| 0–2 | ECT60V202 closed-loop stepper drives | CiA 402 (0x1000 = 0x00040192) |
| 3 | MetalMusings EaserCAT 7000, THC and I/O | Modular device, ETG.5001 (0x1000 = 5001) |

There was no matching ESI file for any of them: none for the ECT60, and
the EaserCAT 7000's firmware folder holds the ESI file of the EaserCAT
6000 (another product code). The proposals below come from the slaves'
own data alone.

## Results

**Process data: derived.**

- ECT60: the SII's default assignment is 0x1600 (control word, mode,
  target position) and 0x1A00 (status word, mode display, actual
  position, digital inputs). That is what CSP needs; the drive reports
  CSP in 0x6502. The hand-written file maps the same plus velocity and
  torque, and leaves out the digital inputs.
- EaserCAT 7000: the proposal has exactly the same entries as the
  hand-written file.

**Distributed clocks: derived.** The ECT60's SII describes a DC mode
"DC" with AssignActivate 0x0300 and SYNC0 = 1 × cycle, which is what the
hand-written file has. The EaserCAT 7000 has no DC mode, and the
hand-written file uses none.

**Profile: derived.** CoE 0x1000 identified both kinds of slave. For the
drives, the CiA 402 objects (control word, status word, mode) are left to
the drive profile, and the rest become pins with fixed names (`pos-cmd`,
`pos-fb`).

**Pins: derived, but naming needs the user.** Every entry gets a pin with
a type from the data type (`float` for REAL, `s32`, `u32`) and a name from
the entry, prefixed by the PDO's name when names repeat
(`out-unit1.calculated-voltage`). The hand-written names differ
(`calculatedVoltage-1`), as names will; aliases chosen in the program are
needed.

## What could not be derived

These are choices, not data. The program must ask for them:

- **Bit fields.** The hand-written file splits `Input12` (32 bit) into 12
  bit pins, `Output4` (8 bit) into 4, and uses bit 0 of three 8-bit flags.
  Nothing in the SII or CoE dictionary says that these are bit fields or
  how many bits are used. An ESI file may say so; otherwise the user
  chooses "split into bits" in the program.
- **Settings.** The hand-written file writes 4–5 vendor objects per drive
  (peak current, idle current, motor direction, closed loop, position
  loop gain) and 10 on the EaserCAT (ADC type, I2C address, filter, ohmic
  sensing times). They are machine-specific. The CoE dictionary gives them
  names, so the program can show "0x2000 Peak Current = 4000" instead of
  `<sdoDataRaw data="a0 0f"/>`, and offer the writable vendor objects (69
  on the ECT60) as a settings list.
- **Cyclic mode** (CSP or CSV) for drives that support several.

## Found on the way

- **The SII can be wrong.** The EaserCAT 7000's SII gives subindex 0 for
  every entry of its record objects (0x6001, 0x7001, ...); its CoE
  dictionary has the right ones (1–6). The prototype takes them from the
  CoE dictionary, matched by order, and says so. Sources must be
  cross-checked, not trusted one by one.
- **Data types differ between sources.** The ECT60's SII says USINT for
  0x6060 (modes of operation), its CoE dictionary SINT, as CiA 402 has
  it. The master's own `ethercat xml` output gives UINT32 for 0x607A,
  which is a DINT.
- **"#" is not a comment in XML.** The hand-written file has lines like
  `#<pdoEntry idx="60FF" .../>`, meant to be commented out. The XML
  parser still reads the element, and the running master maps 0x60FF,
  0x606C and 0x6077. Harmless here, but an example of why hand-written XML
  is hard to get right.
- **No serial numbers.** All four slaves report serial number 0, so they
  can only be told apart by position and identity. Two swapped drives of
  the same type would not be noticed by the configuration.
- **ESI files are hard to match** even for one's own device (the 7000
  folder holds the 6000's file). Matching must be by vendor, product code
  and revision, never by file name, and the program must work without
  one.

## Conclusion

For these slaves the core idea holds: process data, distributed clocks,
profile and pins can be proposed from the slaves' own data, without an
ESI file. What remains are real choices (settings, bit fields, cyclic
mode, pin names), which is what the program's screens are for.

The bus had only two kinds of slave, both with a CoE dictionary and a
correct default mapping. Still to try: slaves without CoE, slaves whose
default mapping does not fit, modular devices with slots, and ESI files
from vendors such as Beckhoff.

## Next

Step 2 of the plan: settle the data model and file (`ethercat.yaml`) from
these proposals, and let the component read it. The proposal objects in
`ecatsetup/propose.py` are a first draft of what the file holds per
slave.
