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
    servo-thread-follows-ref-clock: true
    sync0-shift-ns: auto
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
| `linuxcnc` | How the configuration is connected in LinuxCNC; optional, below. |

The cycle time is not in the file: it is the period of the servo thread
(`[EMCMOT]SERVO_PERIOD` in the INI file) that the component's functions
run in.

## master

| Key | Meaning |
|---|---|
| `interface` | Network interface of the bus, such as `enp2s0`. |
| `transport` | `raw` (default, any network card), `xdp-native` (driver with XDP support, such as `igc` or `igb`) or `xdp-skb`. |
| `start-check` | `true` (default): the component stops at load when the bus differs from the file (below). `false`: it only warns, and starts with the slaves that match. |
| `dc.ref-clock-sync-cycles` | How often the DC clocks are synchronised, in cycles. |
| `dc.servo-thread-follows-ref-clock` | `true`: LinuxCNC's servo thread is steered to follow the DC reference clock (the first DC slave); `false`: the slaves' clocks follow the PC's clock. Called `sync-to-ref-clock` before; that name is an error now. |
| `dc.sync0-shift-ns` | Time from the nominal start of the servo period to SYNC0, for all DC slaves, in ns; or `auto` (below). *Global shift time* in EtherCAT Setup, `globalShiftTime` in lcec. |

A configuration without an `interface` gets the one the master read the
bus through (found from the master's MAC address), when it is proposed
from the bus, updated from it or imported from an lcec XML file without
one. Its `transport` then becomes `xdp-native` if the interface's driver
can do XDP and nothing else was chosen (the kernel tells, Linux 6.3 and
later); otherwise it stays `raw`. The *Master* page says whether the
interface can do `xdp-native`.

`sync0-shift-ns: auto` (needs `servo-thread-follows-ref-clock: true`; EtherCAT Setup
proposes it) lets the component choose the shift so that the turn-around,
from an output written in one servo cycle to the slave's answer read
back, is the same in every cycle: two cycles when SYNC0 can fall after
every frame, otherwise three. A turn-around that varies from cycle to
cycle is what upsets drives and loops; a fixed shift too close to the
time the frame is sent causes it. The component measures when the frame
is sent (`ecat.0.send-offset`) for 2 s once all slaves are in OP, places
SYNC0 in the middle of the window, and keeps watching: it chooses again
when a frame comes so late or early that less than half the margins
are left. A new shift moves the servo thread against SYNC0, `pll-step`
per cycle. The pins `ecat.0.shift-time` (the shift in use) and
`ecat.0.turnaround-cycles` (0 while measuring, then 2 or 3) show the
result; the parameters `ecat.0.auto-shift-margin` (time between a frame
and SYNC0) and `ecat.0.auto-shift-input-margin` (time from SYNC0 until
the inputs are ready) are 50000 ns by default. This is the same code as
lcec's `globalShiftTime="auto"` (see linuxcnc-ethercat's
`documentation/distributed-clocks.md`). The cia402 profile counts on two cycles
(see docs/cia402.md); three is reported as an error.

Before SYNC0 starts in a DC slave, the master waits up to 5 s for the
slave's clock to be within 10 us of the reference clock, and starts SYNC0
anyway if it is not. The pins `<slave>.slave-dc-sync-failed`,
`ecat.0.dc-sync-failed` (any slave) and `ecat.dc-sync-failed` show that
this happened, for example to keep the machine from being switched on.
They need the userspace master of linuxcnc-ethercat-uspace 0.5 or later;
with older master headers they stay false.

### The start check

When the component is loaded (`loadrt ecat`), before it configures the
slaves, it:

- refuses to start when the kernel EtherCAT master is loaded and its
  configuration (`/etc/ethercat.conf`, `MASTER0_DEVICE`) gives it the same
  network card, by name, MAC address or `ff:ff:ff:ff:ff:ff`; or when that
  configuration cannot be read. Two masters on one card do not work:
  `sudo systemctl stop ethercat` (and `disable` to keep it stopped);
- waits for the link (up to 10 s) and the master's scan of the bus;
- compares each slave in the file with the slave at its position:
  vendor and product must be the same. A different or missing slave stops
  the load with one line per slave, for example

  ```
  ecat: slave 2 is EL2008 (vendor 0x00000002, product 0x07D83052); the configuration expects EL1008 there (vendor 0x00000002, product 0x03F03052) as io
  ecat: the bus does not match the configuration, so ecat does not start: run EtherCAT Setup (ecatsetup --gui) to update it, or set master: start-check: false to start anyway
  ```

  A different revision is only a warning, and slaves after the last one
  in the file are only mentioned: they stay in PREOP, unused.

With `master: start-check: false`, a difference or a missing link is a
warning and the component starts as before: the master configures the
slaves that match and leaves the others alone. The check of the kernel
master is always made.

Tried on the plasma cutter (2026-11-10, halrun, no motion): with the
kernel master running, ecat refused before it took the card; with it
stopped, the matching file loaded after waiting 3.5 s for the link and
the scan, a wrong product code on slave 3 and an extra slave 4 in the
file each stopped the load with their own line. The comparison needs
linuxcnc-ethercat-uspace 1.1 (`ecrt_master()` and
`ecrt_master_get_slave()` exported); with 1.0 only the kernel master is
checked.

## slaves

| Key | Meaning |
|---|---|
| `position` | Position on the bus, from 0. |
| `name` | Prefix of the slave's HAL pins: `<name>.<pin>`. Letters, digits, `-`, `_`, `.`; unique. |
| `device` | Device name, for people. |
| `identity` | `vendor`, `product`, and optionally `revision`. At start the component compares them with the bus. |
| `profile` | `cia402` (CiA 402 drive), `mdp` (modular device, ETG.5001) or `generic`. |
| `cia402.mode` | For `cia402`: the mode when enabled, `csp` or `csv` (cyclic, for a joint), `pv` (profile velocity, for a spindle or conveyor) or `pp` (profile position, for a tool changer or indexer; needs the settings `0x607D:01`, `0x607D:02` and `0x607F:00`). |
| `modules` | For a modular device: the module idents it detected (`0xF050`) when it was read, to notice a change ([modules.md](modules.md)). The component does not read it. |
| `cia402.axes` | For `cia402`: a drive with several axes in one slave (such as a two-axis ECT60x2): a list of 1 to 4 names, the axes' pin prefixes. Axis n uses the CiA 402 objects at index + n × 0x800 (`0x6040`, `0x6840`, `0x7040`, `0x7840`); see [cia402.md](cia402.md#several-axes). Without it, the drive has one axis named after the slave. |
| `cia402.zero-at-start` | For `cia402`: `true` makes positions count from where the drive stands when the bus first reaches OP, for drives that keep their position from an earlier run (see [cia402.md](cia402.md#zero-at-start)). Default `false`. |
| `cia402.modes` | For `cia402`: other modes the drive may change to: `hm` for homing by the drive's own method, and `csp`, `csv`, `pv`, `pp` to choose with the `opmode-cmd` pin. It needs `0x6060` and `0x6061`, and each mode's objects, in the process data. |
| `homing` | For `cia402` with `hm`: the homing method chosen in the window, `{method: 17, fast: 3000, slow: 1000, accel: 5000, offset: 0}` (a list of them, `null` for an axis without, for a drive with several axes). The master ignores it. The window writes the same choice to `settings` (0x6098, 0x6099, 0x609A, 0x607C and what the drive needs besides, from its quirk file), which is what the drive gets at start; the drive test writes it too, so a test homes as LinuxCNC will ([quirks.md](quirks.md#homing)). |
| `encoders` | Incremental encoder channels laid out as in MDP 511: a list of `name` (pin prefix) and `index` (the channel's status index, such as `'0x6010'`). See [encoder.md](encoder.md). |
| `configure-pdos` | `true`: the master writes the PDO assignment and mapping below to the slave; `false`: the slave's own mapping is used and the entries below are only located in it. |
| `process-data` | `outputs` (master to slave) and `inputs` (slave to master): lists of PDOs. |
| `dc` | Distributed clocks; leave out for none. |
| `settings` | SDOs written to the slave at start, in this order. |

### PDOs and entries

A PDO has `pdo` (its index), `sm` (its sync manager, 0 to 255) and
`entries`. A sync manager holds either outputs or inputs, not both. An
entry has:

| Key | Meaning |
|---|---|
| `object` | `"0xINDEX:SUB"`, or `"0x0000:00"` for a gap. |
| `bits` | Bit length. |
| `type` | Data type: `BOOL`, `SINT`, `INT`, `DINT`, `LINT`, `USINT`, `UINT`, `UDINT`, `ULINT`, `REAL`, `LREAL`. |
| `use` | `profile`: handled by the profile or an encoder channel, no pin (see [cia402.md](cia402.md), [encoder.md](encoder.md)). `gap`: padding. Left out: an ordinary entry. |
| `pin`, `hal-type` | One pin for the whole entry; `hal-type` is `bit`, `s32`, `u32` or `float`. A `float` pin of a `REAL`/`LREAL` entry carries the value as it is; of an integer entry it is scaled. A `LINT`/`ULINT` entry must be `float` (LinuxCNC 2.9 has no 64-bit pins): the whole 64 bits are read and written, exact up to 2^53, and written values are held within the type's range. |
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

## linuxcnc

Written by EtherCAT Setup when joints are connected to drives, or
slaves' pins to LinuxCNC's ([linuxcnc.md](linuxcnc.md)); the component
does not read it.

```yaml
linuxcnc:
  joints:
  - {joint: 0, slave: lcec.0.X, scale: 100.0}
  - {joint: 1, slave: lcec.0.Y, scale: 66.6666666666666}
  nets:
  - signal: home-x
    pins: [lcec.0.E7000.in-1, joint.0.home-sw-in]
```

| Key | Meaning |
|---|---|
| `joints` | A list of `joint` (the joint's number), `slave` (the name of a `cia402` slave in mode `csp`) and `scale` (its `pos-scale`, counts per machine unit, not 0). Each joint and each slave once. |
| `check-hal-at-start` | `true`: `ethercat-load.hal` runs the HAL check (`ecatsetup --check-hal`) when LinuxCNC starts ([linuxcnc.md](linuxcnc.md#checking-the-hal-files)). |
| `nets` | A list of `signal` (a HAL signal name: no blanks or `#`, at most 47 characters, each once) and `pins` (at least two; at least one of them a slave's pin; each pin in one net only, and not a pin a joint's connection takes). |

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

Some lcec settings are replaced by what ecat does instead (in
[quirk files](quirks.md)): an Rtelligent ECT60's `sdoConfig` of `0x2010`
(Zero Position) becomes `cia402: {zero-at-start: true}` when the slave
gets the cia402 profile (read from the bus); as a `generic` slave the
setting is kept, with a note.

lcec's `initCmds`, `idnConfig` and `watchdog` are not imported yet.
`modParam`s become settings where a [quirk file](quirks.md) translates
them (lcec's stepper drivers, [stepper.md](stepper.md#from-lcec)); others
get a note. ecat has no product drivers, so a `modParam` will become settings
rows or process data choices (see the design draft, [Decision: profiles
with quirks, no product
drivers](design/ethercat-setup.md#decision-profiles-with-quirks-no-product-drivers)).

## In the component

The `ecat` component reads the file with `loadrt ecat config=<file>`. It
supports the profiles `cia402` ([cia402.md](cia402.md)), `mdp` and
`generic`, and encoder channels on any of them ([encoder.md](encoder.md)). 64-bit integer entries are `float` pins (above).

## Open

- Options for profiles beyond `cia402.mode` and `cia402.modes`.

One master and one bus per configuration, by decision (see the design
draft, *Decided questions*). Scaling for `cia402` (counts per unit) is
the HAL parameter `pos-scale`, set from `linuxcnc.joints` by the
`ethercat.hal` EtherCAT Setup writes; drives that are not joints set it
in HAL.
