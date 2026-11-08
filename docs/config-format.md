# The ecat configuration file

Status: **draft, format version 1**, 2026-11-08. It will change as the
component and the setup program are built.

`ethercat.yaml` describes the EtherCAT bus of one LinuxCNC machine
configuration. EtherCAT Setup writes it; the `ecat` HAL component reads it:

```
loadrt ecat config=ethercat.yaml
```

It is YAML, so it can be read, compared and posted when asking for help,
but it is not meant to be written by hand: the program rewrites it and
does not keep comments added by hand. EtherCAT identifiers (vendor,
product, object indexes) are written as `"0x..."` strings, as in vendor
documentation.

## Example

From a bus with a CiA 402 drive and an I/O slave (shortened):

```yaml
ecat: 1
master:
  interface: enp2s0
  transport: xdp-native
  dc:
    ref-clock-sync-cycles: 1000
    sync-to-ref-clock: true
    sync0-shift-ns: 300000
slaves:
- position: 0
  name: x-drive
  device: ECT60V202(COE)
  identity: {vendor: '0x00000A88', product: '0x0A880002', revision: '0x00000202'}
  profile: cia402
  cia402:
    mode: csp
  configure-pdos: true
  process-data:
    outputs:
    - pdo: '0x1600'
      sm: 2
      entries:
      - {object: '0x6040:00', bits: 16, type: UINT, use: profile}
      - {object: '0x6060:00', bits: 8, type: SINT, use: profile}
      - {object: '0x607A:00', bits: 32, type: DINT, use: profile}
    inputs:
    - pdo: '0x1A00'
      sm: 3
      entries:
      - {object: '0x6041:00', bits: 16, type: UINT, use: profile}
      - {object: '0x6061:00', bits: 8, type: SINT, use: profile}
      - {object: '0x6064:00', bits: 32, type: DINT, use: profile}
  dc:
    assign-activate: '0x0300'
    sync0-cycles: 1
    sync0-shift-ns: 0
  settings:
  - {object: '0x2000:00', name: Peak Current, type: UINT, value: 4000}
- position: 3
  name: io
  device: MetalMusings EaserCAT 7000
  identity: {vendor: '0xD0BED1D0', product: '0x00007000', revision: '0x00000002'}
  profile: mdp
  configure-pdos: false
  process-data:
    inputs:
    - pdo: '0x1A00'
      sm: 3
      entries:
      - {object: '0x6000:00', bits: 32, type: DINT, split: [in-1, in-2, in-3, in-4]}
    - pdo: '0x1A01'
      sm: 3
      entries:
      - {object: '0x6001:02', bits: 32, type: REAL, pin: unit1.voltage, hal-type: float}
  settings:
  - {object: '0x2000:02', name: 'Settings_Unit1: I2C_address', type: USINT, value: 72}
```

## Top level

| Key | Meaning |
|---|---|
| `ecat` | Format version, `1`. |
| `master` | The master, below. |
| `slaves` | The slaves, in any order. |

The cycle time is not in the file: it is the period of the servo thread
(`[EMCMOT]SERVO_PERIOD` in the INI file) that the component's functions
run in.

## master

| Key | Meaning |
|---|---|
| `interface` | Network interface of the bus, such as `enp2s0`. |
| `transport` | `raw` (default, any network card), `xdp-native` (driver with XDP support, such as `igc`) or `xdp-skb`. |
| `dc.ref-clock-sync-cycles` | How often the DC clocks are synchronised, in cycles. |
| `dc.sync-to-ref-clock` | `true`: the servo thread follows the DC reference clock; `false`: the slaves follow the master. |
| `dc.sync0-shift-ns` | Time from the nominal start of the servo period to SYNC0, for all DC slaves. |

## slaves

| Key | Meaning |
|---|---|
| `position` | Position on the bus, from 0. |
| `name` | Prefix of the slave's HAL pins: `<name>.<pin>`. Letters, digits, `-`, `_`, `.`; unique. |
| `device` | Device name, for people. |
| `identity` | `vendor`, `product`, and optionally `revision`. At start the component compares them with the bus. |
| `profile` | `cia402` (CiA 402 drive), `mdp` (modular device, ETG.5001) or `generic`. |
| `cia402.mode` | For `cia402`: the cyclic mode, `csp` or `csv`. |
| `configure-pdos` | `true`: the master writes the PDO assignment and mapping below to the slave; `false`: the slave's own mapping is used and the entries below are only located in it. |
| `process-data` | `outputs` (master to slave) and `inputs` (slave to master): lists of PDOs. |
| `dc` | Distributed clocks; leave out for none. |
| `settings` | SDOs written to the slave at start, in this order. |

### PDOs and entries

A PDO has `pdo` (its index), `sm` (its sync manager) and `entries`. An
entry has:

| Key | Meaning |
|---|---|
| `object` | `"0xINDEX:SUB"`, or `"0x0000:00"` for a gap. |
| `bits` | Bit length. |
| `type` | Data type: `BOOL`, `SINT`, `INT`, `DINT`, `LINT`, `USINT`, `UINT`, `UDINT`, `ULINT`, `REAL`, `LREAL`. |
| `use` | `profile`: handled by the profile, no pin (for `cia402`, see [cia402.md](cia402.md)). `gap`: padding. Left out: an ordinary entry. |
| `pin`, `hal-type` | One pin for the whole entry; `hal-type` is `bit`, `s32`, `u32`, `s64`, `u64` or `float`. A `float` pin of a `REAL`/`LREAL` entry carries the value as it is; of an integer entry it is scaled. |
| `scale`, `offset` | For a `float` pin of an integer entry: pin = value × scale + offset. |
| `split` | One `bit` pin per bit, from bit 0; `null` for a bit without a pin. Instead of `pin`. |

An entry with neither `pin` nor `split` is mapped but has no pin.

### dc

| Key | Meaning |
|---|---|
| `assign-activate` | The slave's AssignActivate word, from its ESI file or SII, such as `"0x0300"`. |
| `sync0-cycles` | SYNC0 period in servo periods. |
| `sync0-shift-ns` | The slave's own SYNC0 shift. |

### settings

Each setting is an SDO download: `object` and either `type` and `value`
(written as that type, little endian), or `data` (raw bytes in hex, such
as `"a0 0f"`). `name` is for people.

## Importing lcec XML files

```
python3 -m ecatsetup --import ethercat-conf.xml --ini machine.ini --write ethercat.yaml
```

Generic slaves are carried over with their process data, pin names, bit
splits, scaling, distributed clocks and SDOs. Slaves with an lcec product
driver are configured from the slave's own data if a master is running,
and otherwise left out. With a master running, settings get names and
types from the slaves' CoE dictionaries, and mappings are checked against
them. The import prints a note for everything it changes or leaves out.

lcec's `initCmds`, `modParam`, `idnConfig` and `watchdog` are not imported
yet.

## In the component

The `ecat` component reads the file with `loadrt ecat config=<file>`. It
supports the profiles `cia402` ([cia402.md](cia402.md)), `mdp` and
`generic`. `hal-type` `s64` and `u64` are not supported yet.

## Open

- Scaling for `cia402` (counts per unit): a HAL parameter for now; it may
  belong in the file.
- Options for profiles beyond `cia402.mode`.
- Several masters (only one bus is supported).
