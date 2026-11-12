# The ecat component

`ecat` is a HAL component that runs the userspace IgH EtherCAT master
inside LinuxCNC. It reads the `ethercat.yaml` that
[EtherCAT Setup](ecatsetup.md) writes (or that you write), and gives each
slave HAL pins. It grew out of lcec_uspace's code (moved to `component/`
with its history) and takes `ethercat.yaml` instead of lcec's XML file.

- [Using the component](#using-the-component): loading, pins, stopping
  the bus, building;
- [The configuration file](#the-configuration-file);
- what each kind of slave gives you: [cia402 drives](#the-cia402-profile),
  [encoder channels](#encoder-channels),
  [analog terminals](#analog-inputs-and-outputs),
  [stepper terminals](#stepper-and-dc-motor-terminals) and
  [modular devices](#modular-devices).

## Using the component

EtherCAT Setup writes these lines itself
([Writing the configuration into LinuxCNC](ecatsetup.md#writing-the-configuration-into-linuxcnc)).
By hand, in a HAL file, instead of `lcec_conf` and `lcec`:

```
loadrt ecat config=ethercat.yaml
addf ecat.read-all  servo-thread
addf ecat.write-all servo-thread
```

- Slave pins are `<slave name>.<pin>` as named in the file; the master's
  are `ecat.0.*` (`ecat.0.all-op`, ...) and `ecat.*`.
- The bus stops in order by itself when LinuxCNC (or `halcmd stop`)
  stops the threads. Otherwise the slaves would lose their process data
  in OP and stop with an error (sync manager watchdog 0x001B,
  synchronization error 0x001A), and CiA 402 drives latch a
  communication fault (0x7500 on the ECT60, after one or two missed
  cycles). A stop guard, an RTAPI task with the servo period beside the
  servo thread, sends the process data unchanged when `ecat.write-all`
  is more than a quarter period late; after 10 such cycles in a row it
  sets every output to zero and takes the slaves to PREOP, and unloading
  waits for it. `ecat.0.guard-state` shows it (0 the servo thread runs, 1
  taking the slaves down, 2 done) and `ecat.0.guard-cycles` counts the
  cycles it sent; while the machine runs that stays 0. After `halcmd stop` and `start`
  the slaves are asked back to OP; DC slaves (such as the ECT60) then stop
  in SAFEOP with a synchronization error, as they did before the guard:
  restart instead.
- `ecat.take-down` does the same on request while the thread runs, and
  `ecat-take-down` sets it and waits for PREOP; it is no longer needed in
  `[HAL]SHUTDOWN`.
- The cycle time is the servo thread's period, taken at the first call
  of `ecat.read-all`; distributed clocks are configured then, before the
  master is activated.
- Profiles `cia402`, `mdp` and `generic` are supported. A `cia402` drive
  gets the CiA 402 state machine and scaled position and velocity pins in
  the component, with the pins of the cia402 HAL component
  ([cia402](#the-cia402-profile)).
- Incremental encoder channels laid out as in MDP 511 (EL5101, EL5151,
  EL5152, EL7041, ...) get position, velocity, index and latch pins, like
  LinuxCNC's encoder component ([encoder channels](#encoder-channels)).

A configuration imported from lcec can keep lcec's pin names by naming
the slaves `lcec.0.<name>`, so that existing HAL files work with only the
lines above changed.

Building the component from a checkout needs the userspace master
package (or a master built from source, with `USPACE_ETHERCAT=<its
prefix>`), `linuxcnc-uspace-dev` and `libyaml-dev`; see
[Developing ecat](development.md#build-and-test).

## The configuration file

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

### Example

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
      - {object: '0x6001:02', bits: 32, type: REAL, pin: unit1.voltage, hal-type: real}
  settings:
  - {object: '0x2000:02', name: 'Settings_Unit1: I2C_address', type: USINT, value: 72}
```

### Top level

| Key | Meaning |
|---|---|
| `ecat` | Format version, `1`. |
| `master` | The master, below. |
| `slaves` | The slaves, in any order. |
| `linuxcnc` | How the configuration is connected in LinuxCNC; optional, below. |

The cycle time is not in the file: it is the period of the servo thread
(`[EMCMOT]SERVO_PERIOD` in the INI file) that the component's functions
run in.

### master

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
(see [the cia402 profile](#the-cia402-profile)); three is reported as an error.

Before SYNC0 starts in a DC slave, the master waits up to 5 s for the
slave's clock to be within 10 us of the reference clock, and starts SYNC0
anyway if it is not. The pins `<slave>.slave-dc-sync-failed`,
`ecat.0.dc-sync-failed` (any slave) and `ecat.dc-sync-failed` show that
this happened, for example to keep the machine from being switched on.
They need the userspace master of linuxcnc-ethercat-uspace 0.5 or later;
with older master headers they stay false.

#### The start check

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
file each stopped the load with their own line.

### slaves

| Key | Meaning |
|---|---|
| `position` | Position on the bus, from 0. |
| `name` | Prefix of the slave's HAL pins: `<name>.<pin>`. Letters, digits, `-`, `_`, `.`; unique. |
| `device` | Device name, for people. |
| `identity` | `vendor`, `product`, and optionally `revision`. At start the component compares them with the bus. |
| `profile` | `cia402` (CiA 402 drive), `mdp` (modular device, ETG.5001) or `generic`. |
| `cia402.mode` | For `cia402`: the mode when enabled, `csp` or `csv` (cyclic, for a joint), `pv` (profile velocity, for a spindle or conveyor) or `pp` (profile position, for a tool changer or indexer; needs the settings `0x607D:01`, `0x607D:02` and `0x607F:00`). |
| `modules` | For a modular device: the module idents it detected (`0xF050`) when it was read, to notice a change ([modular devices](#modular-devices)). The component does not read it. |
| `cia402.axes` | For `cia402`: a drive with several axes in one slave (such as a two-axis ECT60x2): a list of 1 to 4 names, the axes' pin prefixes. Axis n uses the CiA 402 objects at index + n × 0x800 (`0x6040`, `0x6840`, `0x7040`, `0x7840`); see [cia402](#several-axes). Without it, the drive has one axis named after the slave. |
| `cia402.zero-at-start` | For `cia402`: `true` makes positions count from where the drive stands when the bus first reaches OP, for drives that keep their position from an earlier run (see [cia402](#zero-at-start)). Default `false`. |
| `cia402.modes` | For `cia402`: other modes the drive may change to: `hm` for homing by the drive's own method, and `csp`, `csv`, `pv`, `pp` to choose with the `opmode-cmd` pin. It needs `0x6060` and `0x6061`, and each mode's objects, in the process data. |
| `homing` | For `cia402` with `hm`: the homing method chosen in the window, `{method: 17, fast: 3000, slow: 1000, accel: 5000, offset: 0}` (a list of them, `null` for an axis without, for a drive with several axes). The master ignores it. The window writes the same choice to `settings` (0x6098, 0x6099, 0x609A, 0x607C and what the drive needs besides, from its quirk file), which is what the drive gets at start; the drive test writes it too, so a test homes as LinuxCNC will ([quirk files](ecatsetup.md#homing)). |
| `encoders` | Incremental encoder channels laid out as in MDP 511: a list of `name` (pin prefix) and `index` (the channel's status index, such as `'0x6010'`). See [encoder channels](#encoder-channels). |
| `configure-pdos` | `true`: the master writes the PDO assignment and mapping below to the slave; `false`: the slave's own mapping is used and the entries below are only located in it. |
| `process-data` | `outputs` (master to slave) and `inputs` (slave to master): lists of PDOs. |
| `dc` | Distributed clocks; leave out for none. |
| `settings` | SDOs written to the slave at start, in this order. |

#### PDOs and entries

A PDO has `pdo` (its index), `sm` (its sync manager, 0 to 255) and
`entries`. A sync manager holds either outputs or inputs, not both. An
entry has:

| Key | Meaning |
|---|---|
| `object` | `"0xINDEX:SUB"`, or `"0x0000:00"` for a gap. |
| `bits` | Bit length. |
| `type` | Data type: `BOOL`, `SINT`, `INT`, `DINT`, `LINT`, `USINT`, `UINT`, `UDINT`, `ULINT`, `REAL`, `LREAL`. |
| `use` | `profile`: handled by the profile or an encoder channel, no pin (see [cia402](#the-cia402-profile), [encoder channels](#encoder-channels)). `gap`: padding. Left out: an ordinary entry. |
| `pin`, `hal-type` | One pin for the whole entry; `hal-type` is `bool`, `sint`, `uint` or `real` (LinuxCNC 2.10's pin types; a file written for 2.9 spells them `bit`, `s32`, `u32`, `float`, which are read as the same types). Integer pins have 64 bits: a value written to an entry is held within the entry's range (an 8-bit entry takes -128..127 or 0..255 whatever the pin holds), see `raw` for bit patterns. A `real` pin of a `REAL`/`LREAL` entry carries the value as it is; of an integer entry it is scaled. A `LINT` entry takes a `sint` pin and a `ULINT` entry a `uint` pin, with all 64 bits exact (`real` still works, exact up to 2^53). |
| `extend` | `true` on an input entry of 8 to 32 bits with `hal-type: sint`: the pin keeps counting past the wrap of the entry, in 64 bits (an encoder counter, a latched position). The first value is taken as signed, so a counter that counts down past 0 is negative. The counter must not move by half its range or more between two cycles. EtherCAT Setup proposes it for counters of 16 and 32 bits. |
| `raw` | `true` on an entry of up to 32 bits with `hal-type: uint`: the value is a bit pattern (digital outputs), and a value wider than the entry is cut to its width instead of held at the top, which would switch every bit on. |
| `scale`, `offset` | For a `float` pin of an integer entry: pin = value × scale + offset. |
| `split` | One `bool` pin per bit, from bit 0; `null` for a bit without a pin. Instead of `pin`. |
| `invert` | For a `bool` pin: `true` makes the pin the opposite of the bit (read and write), as for a normally closed switch, so that no `not` is needed in HAL. For a `split`: a list of the names of its pins to invert. |

An entry with neither `pin` nor `split` is mapped but has no pin.

#### dc

| Key | Meaning |
|---|---|
| `assign-activate` | The slave's AssignActivate word, from its ESI file or SII, such as `"0x0300"`. |
| `sync0-cycles` | SYNC0 period in servo periods. |
| `sync0-shift-ns` | The slave's own SYNC0 shift. |

#### settings

Each setting is an SDO download: `object` and either `type` and `value`
(written as that type, little endian), or `data` (raw bytes in hex, such
as `"a0 0f"`). `name` is for people.

### linuxcnc

Written by EtherCAT Setup when joints are connected to drives, or
slaves' pins to LinuxCNC's ([writing into LinuxCNC](ecatsetup.md#writing-the-configuration-into-linuxcnc)); the component
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
| `check-hal-at-start` | `true`: `ethercat-load.hal` runs the HAL check (`ecatsetup --check-hal`) when LinuxCNC starts ([writing into LinuxCNC](ecatsetup.md#checking-the-hal-files)). |
| `nets` | A list of `signal` (a HAL signal name: no blanks or `#`, at most 47 characters, each once) and `pins` (at least two; at least one of them a slave's pin; each pin in one net only, and not a pin a joint's connection takes). |

### Importing lcec XML files

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
[quirk files](ecatsetup.md#quirk-files)): an Rtelligent ECT60's `sdoConfig` of `0x2010`
(Zero Position) becomes `cia402: {zero-at-start: true}` when the slave
gets the cia402 profile (read from the bus); as a `generic` slave the
setting is kept, with a note.

lcec's `initCmds`, `idnConfig` and `watchdog` are not imported yet.
`modParam`s become settings where a [quirk file](ecatsetup.md#quirk-files) translates
them (lcec's stepper drivers, [stepper terminals](#from-lcec-stepper)); others
get a note. ecat has no product drivers, so a `modParam` will become settings
rows or process data choices (see the design draft, [Decision: profiles
with quirks, no product
drivers](development.md#decision-profiles-with-quirks-no-product-drivers)).

### In the component

The `ecat` component reads the file with `loadrt ecat config=<file>`. It
supports the profiles `cia402` ([cia402](#the-cia402-profile)), `mdp` and
`generic`, and encoder channels on any of them ([encoder channels](#encoder-channels)). 64-bit integer entries are `sint`/`uint` pins (above).

### Open

- Options for profiles beyond `cia402.mode` and `cia402.modes`.

One master and one bus per configuration, by decision (see the design
draft, *Decided questions*). Scaling for `cia402` (counts per unit) is
the HAL parameter `pos-scale`, set from `linuxcnc.joints` by the
`ethercat.hal` EtherCAT Setup writes; drives that are not joints set it
in HAL.

## The cia402 profile

Status: first version, 2026-11-08. It has run a plasma cutter's LinuxCNC
configuration with three CiA 402 closed-loop stepper drives (ECT60) in
CSP: enabling without a jerk, jogging, E-stop at rest and during motion,
homing, and a program with lines and arcs at 1000 and 3000 mm/min. The
following errors were the same as with lcec and the cia402 component
(about 0.3 mm at 1000 mm/min and 0.8 mm at 3000 mm/min, from the drives'
own position loop).

A slave with `profile: cia402` in `ethercat.yaml` is a CiA 402 (ETG.6010)
drive. The `ecat` component then runs the drive's state machine in every
cycle and gives it scaled position and velocity pins, like the separate
[cia402 HAL component](https://github.com/dbraun1981/hal-cia402) does
together with lcec, but inside `ecat` and without the HAL nets between
them.

### In the configuration file

```yaml
  profile: cia402
  cia402:
    mode: csp
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
      - {object: '0x6077:00', bits: 32, type: DINT, pin: torque-fb, hal-type: sint}
```

`mode` is `csp` (cyclic synchronous position) or `csv` (cyclic
synchronous velocity) for a joint, or for a drive that is not a joint,
`pv` (profile velocity, such as a spindle) or `pp` (profile position,
such as a tool changer or an indexer), below. `modes` lists other
modes the drive may change to: `hm` lets it home by its own method, and
`csp`, `csv`, `pv` and `pp` can be chosen with `opmode-cmd`, below. The
profile handles these objects, which must be `use: profile` entries when
they are in the process data:

| Object | | Needed |
|---|---|---|
| `0x6040` control word | output | always |
| `0x6041` status word | input | always |
| `0x6060` modes of operation | output | no; written every cycle if mapped |
| `0x6061` modes of operation display | input | no; checked before enabling if mapped |
| `0x607A` target position | output | `csp`, `pp` |
| `0x6064` position actual value | input | `csp`, `pp` |
| `0x60FF` target velocity | output | `csv`, `pv` |
| `0x6081` profile velocity | output | no; `pp` gets a `pp-velocity` pin if mapped |
| `0x606C` velocity actual value | input | `csv`, `pv` |
| `0x603F` error code | input | no |

Other entries of the drive, such as torque or digital inputs, get ordinary
pins.

`zero-at-start: true` makes positions count from where the drive stands
when the bus first reaches OP; see [Zero at start](#zero-at-start).

### Pins

Pins are `<slave name>.<pin>`.

| Pin | Type | Dir | |
|---|---|---|---|
| `enable` | bool | in | Enable the drive. Connect `joint.N.amp-enable-out`. |
| `pos-cmd` | real | in | Position command (`csp`), machine units. |
| `velocity-cmd` | real | in | Velocity command (`csv`, `pv`). |
| `fault-reset` | bool | in | A rising edge resets a drive fault. |
| `watchdog` | uint | in | Must change within `watchdog-ms`, if that is set. |
| `pos-fb` | real | out | Position feedback, machine units. |
| `velocity-fb` | real | out | Velocity feedback. |
| `drv-fault` | bool | out | The drive was shut down while enabled; connect `joint.N.amp-fault-in`. |
| `fault-reason` | sint | out | Why, below. |
| `cia-state` | sint | out | The drive's state, below. |
| `statusword`, `controlword` | uint | out | As read and written. |
| `opmode`, `opmode-display` | sint | out | As written and read. |
| `error-code` | uint | out | `0x603F`, if mapped. |
| `drv-actual-position`, `drv-actual-velocity` | sint | out | Unscaled feedback. |
| `drv-target-position`, `drv-target-velocity` | sint | out | Unscaled commands as written. |
| `pos-zero` | sint | out | With `zero-at-start`: the drive's position taken as 0, in counts. |
| `stat-switchon-ready`, `stat-switched-on`, `stat-op-enabled`, `stat-fault`, `stat-voltage-enabled`, `stat-quick-stop`, `stat-switchon-disabled`, `stat-warning`, `stat-remote`, `stat-target-reached` | bool | out | Status word bits. |
| `opmode-no-mode`, `opmode-homing`, `opmode-cyclic-position`, `opmode-cyclic-velocity` | bool | out | From `opmode-display`. |

Parameters:

| Parameter | Default | |
|---|---|---|
| `pos-scale` | 1.0 | Counts per machine unit. |
| `velo-scale` | 1.0 | Velocity counts per machine unit. |
| `auto-fault-reset` | true | Reset a drive fault on a rising edge of `enable`. |
| `enable-timeout-ms` | 2000 | Time allowed to reach "Operation enabled"; 0 for no limit. |
| `watchdog-ms` | 0 | Watchdog time for the `watchdog` pin; 0 for off. |
| `mode` | | The mode from the file, 8 (CSP), 9 (CSV), 3 (PV) or 1 (PP); read only. |

`cia-state`: 0 not ready to switch on, 1 switch on disabled, 2 ready to
switch on, 3 switched on, 4 operation enabled, 5 quick stop active,
6 fault reaction active, 7 fault, -1 unknown (no valid process data).

`fault-reason`: 0 none, 1 drive fault, 2 no valid process data (working
counter or slave not in OP), 3 "Operation enabled" not reached in time,
4 the drive left "Operation enabled" by itself, 5 watchdog, 6 the drive
does not report the configured mode, does not change mode within 1 s,
or does not stop for a mode change within `stop-timeout-ms`, 7 `pos-cmd`
(CSP) or `velocity-cmd` (CSV, PV) is not a finite number (NaN or
infinite), and in PP `pp-target` or `pp-velocity`, or `opmode-cmd` asks
for a mode that is not in the file,
8 the drive reports a homing error, 9 homing not done within
`home-timeout-ms`, 10 a PP move not done within `pp-timeout-ms`, 11 the
master's DC sync check for the drive timed out (see Behaviour).

### Behaviour

- Without `enable` the drive is held in "Ready to switch on" (control word
  `0x06`): power stage off.
- With `enable` the profile steps it through "Switched on" to "Operation
  enabled". It enables operation only if the drive reports the configured
  mode, when `0x6061` is mapped.
- Until the drive is in "Operation enabled", the target position is its
  actual position and the target velocity is 0, so it does not move when
  it is enabled. After that it follows `pos-cmd` or `velocity-cmd`.
- If, while `enable` is true, the drive faults, leaves "Operation enabled"
  by itself, does not get there within `enable-timeout-ms`, the process
  data stops being valid, `watchdog` stops changing, or the command is NaN
  or infinite (it is never turned into a target), the profile shuts
  the drive down and sets `drv-fault` and `fault-reason`. It stays shut down
  until `enable` goes false, also if the cause goes away.
- The same holds when the master's DC sync check for the drive timed out
  (`<slave>.slave-dc-sync-failed`, `fault-reason` 11): the master then
  starts SYNC0 on a clock that is not synchronized with the other slaves
  and the servo thread, and the drive is not run at all until a new
  configuration passes the check (needs linuxcnc-ethercat-uspace 0.5 or
  later).
- A drive fault is reset by a rising edge of `fault-reset`, or of `enable`
  with `auto-fault-reset`; bit 7 of the control word is held for up to
  100 ms.

### A joint in CSV

In CSP the drive closes the position loop itself, at its own rate. In
CSV it only follows a velocity, so LinuxCNC closes the position loop,
with a `pid` in the servo thread between motion and the drive, as for an
analog servo amplifier in velocity mode. CSP is usually the better
choice: in CSV the bus delay (two or three servo cycles from command to
feedback) is inside LinuxCNC's loop and limits the gain.

```
loadrt pid names=x-pid
addf ecat.read-all       servo-thread
addf motion-command-handler servo-thread
addf motion-controller   servo-thread
addf x-pid.do-pid-calcs  servo-thread
addf ecat.write-all      servo-thread

setp x-drive.pos-scale  [JOINT_0]SCALE
setp x-drive.velo-scale [JOINT_0]SCALE
setp x-pid.Pgain     [JOINT_0]P
setp x-pid.Igain     [JOINT_0]I
setp x-pid.Dgain     [JOINT_0]D
setp x-pid.FF1       [JOINT_0]FF1
setp x-pid.maxoutput [JOINT_0]MAX_OUTPUT

net x-enable  joint.0.amp-enable-out => x-drive.enable x-pid.enable
net x-pos-cmd joint.0.motor-pos-cmd  => x-pid.command
net x-vel-cmd joint.0.vel-cmd        => x-pid.command-deriv
net x-pos-fb  x-drive.pos-fb         => x-pid.feedback joint.0.motor-pos-fb
net x-vel-fb  x-drive.velocity-fb    => x-pid.feedback-deriv
net x-vel     x-pid.output           => x-drive.velocity-cmd
net x-fault   x-drive.drv-fault      => joint.0.amp-fault-in
```

- `velo-scale` is the drive's velocity unit per machine unit per second:
  `SCALE` when the drive counts velocity in position counts per second;
  check the drive's manual (some use rpm or counts per millisecond).
- `pid.output` is then a velocity in machine units per second. With
  `FF1 = 1.0` the feedforward gives the velocity of the path, and the
  P term only corrects the error. `P` is in 1/s (velocity per unit of
  following error): start small, such as 20, with `I = D = 0`, and raise
  it while watching the following error in halscope.
- `MAX_OUTPUT` a bit above `MAX_VELOCITY` keeps a fault from asking for
  full speed.
- The order of the functions matters: the pid must run after
  `motion-controller` and before `ecat.write-all`, or its output reaches
  the drive a cycle late.

### Changing modes

With more modes than `mode` in `modes` (for example
`cia402: {mode: pv, modes: [pp]}`, a spindle that also positions), the
drive gets the pin `opmode-cmd` (sint, in): 0 for `mode`, or the number of
one of the modes: 1 PP, 3 PV, 8 CSP, 9 CSV. `opmode` shows the mode
written, `opmode-display` the drive's. A number that is not a mode of
the file shuts the drive down (`fault-reason` 7); homing (6) goes by its
own pins.

A drive that is not enabled changes at once, and is enabled only once it
reports the new mode. An enabled drive changes in steps:

1. It is stopped: in PV and PP halted (control word bit 8) until it
   reports zero speed or target reached, at least two cycles; in CSP
   and CSV the target is held at the actual position, or velocity 0, for
   two cycles. If it does not stop within `stop-timeout-ms`, it is shut
   down (`fault-reason` 6).
2. The new mode is written to `0x6060`, and `0x6061` must follow within
   1 s (`fault-reason` 6).
3. The new mode starts from the actual values: CSP follows `pos-cmd` only
   once it is within 10 counts of the actual position, CSV `velocity-cmd`
   only once it is 0, PV ramps to `velocity-cmd`, and PP moves only on a
   new `pp-start`.

Asked back to the old mode before step 2, the drive goes on in it. The
mode objects `0x6060` and `0x6061` must be in the process data, and each
allowed mode's objects; PP's limits must be in `settings`.

A joint should stay in CSP or CSV: LinuxCNC must be the only one that
plans its motion. The other modes are for drives that are not joints.

### Profile velocity (PV)

With `mode: pv` the drive ramps to `velocity-cmd` with its own
acceleration and deceleration (`0x6083`, `0x6084`; set them in
`settings`). It is for a drive that LinuxCNC does not move as a joint: a
spindle, a conveyor, a pump. The drive follows `velocity-cmd` as soon as
it is in "Operation enabled", and stands still at 0.

Extra pins and parameter:

| Pin | Type | Dir | |
|---|---|---|---|
| `at-speed` | bool | out | The drive runs at `velocity-cmd` (status word bit 10). Connect `spindle.N.at-speed`. |
| `zero-speed` | bool | out | The drive reports zero speed (status word bit 12). |

| Parameter | Default | |
|---|---|---|
| `stop-timeout-ms` | 10000 | Time allowed to stop before the drive is switched off; 0 to switch off at once. |

- When `enable` goes false, or the profile shuts the drive down (a NaN
  command, the watchdog, ...), a running drive is first halted (control
  word bit 8) with its own deceleration, and switched off when it
  reports zero speed or after `stop-timeout-ms`. It does not coast.
  Enabled again while it slows down, it runs up again. When the drive
  faults, or the bus stops, it cannot be halted; it does its own fault
  reaction.
- The status word answers a new control word or target two servo cycles
  later, when SYNC0 falls after the frame has reached the drive: the frame
  sent at the end of cycle n is used at SYNC0 in cycle n, and its answer
  comes with the frame of cycle n + 1, read at the start of cycle n + 2.
  `master.dc.sync0-shift-ns: auto` places SYNC0 so, and
  `ecat.0.turnaround-cycles` shows 2. So `at-speed` waits two cycles after
  a new target, so that it never reports the old target as reached, and a
  drive is halted for at least two cycles before "zero speed" is believed.
  A turn-around of three cycles (SYNC0 before the frame, such as
  `sync0-shift-ns: 0`) is a configuration error: these status bits can
  then be taken one cycle too early.

  To be confirmed in use: two cycles is measured only for the mode
  display (`0x6061`) of ECT60 drives. A drive with a slower internal cycle
  may update "target reached" or "zero speed" later. If use shows that,
  more cycles must be allowed, for example as a parameter per drive.
  Please report drives where `at-speed` or a stop comes too early.

For a spindle:

```
net spindle-vel  spindle.0.speed-out-rps => spindle-drive.velocity-cmd
net spindle-on   spindle.0.on            => spindle-drive.enable
net spindle-at   spindle-drive.at-speed  => spindle.0.at-speed
```

with `velo-scale` the drive's velocity units per revolution per second.

### Profile position (PP)

With `mode: pp` the drive moves to a position on its own, with its own
trajectory: for positioning that LinuxCNC does not do as a joint, such
as a tool changer, an indexer, a stop or a loader. The drive must know
its limits, so the file must set them in `settings`; EtherCAT Setup
proposes the drive's present values:

```yaml
  cia402: {mode: pp}
  settings:
  - {object: '0x607D:01', type: DINT, value: -100000}   # software limit, min
  - {object: '0x607D:02', type: DINT, value: 100000}    # software limit, max
  - {object: '0x607F:00', type: UDINT, value: 50000}    # max profile velocity
```

Acceleration and deceleration are `0x6083` and `0x6084`, also settings.

Extra pins and parameters:

| Pin | Type | Dir | |
|---|---|---|---|
| `pp-target` | real | in | Target position, machine units (`pos-scale`). |
| `pp-velocity` | real | in | Profile velocity (`velo-scale`), if `0x6081` is in the process data; otherwise the drive's setting. |
| `pp-start` | bool | in | A rising edge starts a move to `pp-target`. |
| `pp-relative` | bool | in | The move is relative to the last target. |
| `pp-halt` | bool | in | Stop (control word bit 8); released, the move goes on. |
| `pp-done` | bool | out | At the target of the last move. |

| Parameter | Default | |
|---|---|---|
| `pp-timeout-ms` | 60000 | Time allowed for a move, not counting time halted; 0 for no limit. |
| `pp-immediate` | 0 | Also set control word bit 5, "change set immediately", with every set-point. Off by default: with it the ECT60 acknowledged a set-point and did not move (tried 2026-11-10). |
| `stop-timeout-ms` | 10000 | As in PV. |

- The drive moves only on a rising edge of `pp-start`: enabling it, or
  changing `pp-target`, does not move it.
- The profile does the set-point handshake: it sets control word bit 4
  (new set-point, with bit 6 for relative, and bit 5 "change
  immediately" if `pp-immediate`), waits for the drive's acknowledge (status word bit 12), and
  clears bit 4. A start during a move replaces it; a start while the
  drive still acknowledges the last one is given as soon as it can be.
- A drive that acknowledges a set-point but does not take it (target
  reached stays set; the ECT60 does this now and then) gets it again: if
  target reached has not cleared 6 cycles after the acknowledge, the
  profile gives the same set-point once more, at most twice. A move too
  short for the drive to show counts as done after that.
- `pp-done` is set when the drive reports target reached after it has
  acknowledged the set-point, so it never answers an earlier move, and
  not while halted (target reached then means standing still) or for
  two cycles after the halt is released.
- When `enable` goes false or the drive is shut down during a move, it is
  halted first, as in PV.

#### From G-code

An indexer or a tool carousel can be moved from G-code with motion's
I/O pins: `M68` sets an analog output (the target), `M64`/`M65` a digital
output (the start), and `M66` waits for a digital input (the result).
Motion has four of each by default (`num_dio` and `num_aio` of
`motmod`).

```
net indexer-on     motion.motion-enabled => indexer.enable
net indexer-target motion.analog-out-00  => indexer.pp-target
net indexer-start  motion.digital-out-00 => indexer.pp-start
net indexer-done   indexer.pp-done       => motion.digital-in-00
```

A subroutine, `o<index> call [90]`, in `nc_files/subroutines/index.ngc`
(or a directory in `[RS274NGC]SUBROUTINE_PATH`):

```
o<index> sub
  M68 E0 Q#1            (target)
  M64 P0                (pp-start: rising edge)
  M66 P0 L1 Q10         (wait for pp-done to rise, at most 10 s)
  M65 P0
  o1 if [#5399 EQ -1]
    (abort, indexer did not reach its target)
  o1 endif
o<index> endsub
M2
```

- `M66 ... L1` waits for a rising edge of `pp-done`, not for it to be
  high: it is still high from the last move until the drive takes the
  new set-point, a few cycles after the start.
- `M64`, `M65` and `M68` act at once, not in step with the motion of
  the joints; the program waits at `M66`, with the joints standing.
- For a tool changer, the same lines go into an `M6` remap.
- `drv-fault` can be connected to the E-stop chain or to a
  `motion.digital-in` that the subroutine checks; a drive shut down
  during a move does not raise `pp-done`, so `M66` times out.

### Several axes

A drive with two or more axes in one slave (ETG.6010 "multi-axis": a
two-axis ECT60x2, ECR60x2 or EL72xx with channel offset) keeps each
axis's objects 0x800 apart: the second axis's control word is `0x6840`,
its target position `0x687A`, and so on. Name the axes in the file:

```yaml
  name: xy
  profile: cia402
  cia402: {mode: csp, axes: [xy-1, xy-2]}
  process-data:
    outputs:
    - pdo: '0x1600'
      sm: 2
      entries:
      - {object: '0x6040:00', bits: 16, type: UINT, use: profile}
      - {object: '0x607A:00', bits: 32, type: DINT, use: profile}
      - {object: '0x6840:00', bits: 16, type: UINT, use: profile}
      - {object: '0x687A:00', bits: 32, type: DINT, use: profile}
    ...
```

Each axis then has all of this section's pins and parameters under its own
name (`xy-1.enable`, `xy-2.pos-cmd`, ...) and its own state: one axis can
be enabled, faulted or homing while the other is not. The slave's name
is left for its other pins. `mode`, `modes` and `zero-at-start` apply to
every axis; every axis needs the objects of its modes, and in PP its own
limits (`0x687D:01` ...) as settings.

EtherCAT Setup proposes the axes when the drive's process data has a
status word at `0x6841` (and so on), names them after the slave (`-1`,
`-2`) and renames them with it. Joints connect to an axis like to a
drive (`linuxcnc.joints: [{joint: 0, slave: xy-1, ...}]`), and the test
mode moves one axis at a time (choose it in the *Move* card). Its limits
are written to that axis's objects.

Built and unit-tested, not tried on a drive with several axes.

### Zero at start

Some drives keep their position counter from an earlier run as long as
they have power; the Rtelligent ECT60 does. A machine configuration that
needs the position to start at 0 when LinuxCNC starts can say so:

```yaml
  cia402:
    mode: csp
    zero-at-start: true
```

When the bus first reaches OP after `ecat` is loaded, the drive's
position is taken as 0: it is kept in the pin `pos-zero` (counts), taken
off `pos-fb`, and added to the target written to the drive (`pos-cmd` in
CSP, an absolute `pp-target` in PP). `pos-fb` is 0 until then. Nothing is
written to the drive, so it works on any drive, and the drive's own
counts stay visible in `drv-actual-position` and `drv-target-position`.

- It is taken once per load of `ecat`. A bus that drops and comes back
  keeps it; a drive that lost power counts from its own 0 again, and the
  machine must be homed, as without the option.
- Homing by the drive (`modes: [hm]`) sets `pos-zero` back to 0: the
  drive's position is then the homed one.
- The 32-bit counter wraps as the drive's does.

This replaces writing a "zero position" command as a setting, such as
the ECT60's `0x2010` (Zero Position), which is a command rather than a
setting and did not take effect as a start-up SDO on the ECT60.
In EtherCAT Setup it is *Start position* on the slave's page.

### Homing by the drive

With `modes: [hm]` (and `0x6060` and `0x6061` as `use: profile`
entries), the drive can home by its own method (CiA 402 mode 6), like
the cia402 component's homing. It works with any `mode`: a joint in CSP
homes through motion (`index-enable`), and a drive that is not a joint,
such as an indexer in PP or a spindle in PV, homes on the `home` pin. The method, speeds, acceleration and home
offset are the drive's objects `0x6098`, `0x6099`, `0x609A` and `0x607C`;
set them in `settings`.

Extra pins and parameter:

| Pin | Type | Dir | |
|---|---|---|---|
| `index-enable` | bool | io | Connect `joint.N.index-enable`. A rising edge starts homing; it is cleared when the drive has homed. |
| `home` | bool | in | A rising edge starts homing: for a drive that is not a joint (from an M-code, ladder or a tool changer), or for tests without motion. |
| `stat-homing` | bool | out | Homing is in progress. |
| `stat-homed` | bool | out | The drive has homed; until homing starts again. |
| `home-error` | bool | out | The last homing failed or timed out. |

| Parameter | Default | |
|---|---|---|
| `home-timeout-ms` | 60000 | Time allowed for homing; 0 for no limit. |

Homing is started only while the drive is enabled, in "Operation
enabled", and only by a new rising edge. The profile then:

1. in PV and PP, halts the drive (control word bit 8) and waits until it
   stands still, as for a [mode change](#changing-modes): zero speed in
   PV, target reached in PP, at the earliest after two cycles. A drive
   that does not stop within `stop-timeout-ms` is shut down
   (`fault-reason` 6). In CSP and CSV the target is held at once;
2. writes mode 6 and waits for the drive to report it (within 1 s);
3. sets control word bit 4 and waits for "homing attained" and "target
   reached" (status word bits 12 and 10). A drive that still shows these
   from an earlier homing gets 20 ms to clear them;
4. clears bit 4, writes the mode in use, holds the target at the
   actual position, and waits for the drive to report the mode;
5. clears `index-enable` and sets `stat-homed`.

After homing the drive goes on in its mode from the new position: CSP
follows `pos-cmd` again only once it is within 10 counts of the drive's
position, so a command from before homing does not make it jump back
(through motion, `pos-cmd` matches at once); PV ramps to `velocity-cmd`
again; PP waits for a new `pp-start`. Homing is not a value of
`opmode-cmd`: it is started by its edge and ends in the mode it began
in.

Until then, while homing was started by `index-enable`, `pos-fb` follows
`pos-cmd`, so that motion sees no following error while it moves the
joint slowly in its index search. When `index-enable` is cleared, `pos-fb`
becomes the drive's position after homing, as an encoder's count resets
at its index, and motion takes it as the home position.

In the INI file, home the joint on an index pulse: `HOME_USE_INDEX =
YES`, `HOME_SEARCH_VEL = 0` to start the index search where the joint
is, and a small `HOME_LATCH_VEL`. As with an encoder's index, motion
gives the joint the position `HOME_OFFSET` where the drive has homed,
whatever position the drive itself reports there: it sets the motor
offset to the drive's position minus `HOME_OFFSET`. So `HOME_OFFSET`
places the machine coordinates. The drive's home offset (`0x607C`) does
not change them; whether the drive applies it at all depends on the
drive and the method (an ECT60 with method 35 does not), and it matters
only for the drive's own counts, such as its software limits (`0x607D`).
With `HOME` equal to `HOME_OFFSET` there is no final move.

```
[JOINT_0]
HOME_SEARCH_VEL = 0
HOME_LATCH_VEL = 1
HOME_USE_INDEX = YES
HOME_OFFSET = 10
HOME = 10
```

```
net x-index-enable  joint.0.index-enable <=> x-drive.index-enable
```

In EtherCAT Setup the homing card on the slave's page (*Homing by the
drive*) lists the methods the drive supports, writes the choice and the
objects it needs to `settings`, and shows the INI lines above for the
joint that uses the drive, with the `index-enable` net, ready to copy.
It does not change the INI file.

A homing error (status word bit 13) or a timeout shuts the drive down,
like the faults above, and sets `home-error`. If motion abandons homing
(clears `index-enable`), or `enable` goes false, the profile stops the
drive's homing and returns to the configured mode without a fault.

Tried on a plasma cutter's ECT60 drive (2026-11-09) with method 35
(homing on the current position): homing took 0.25 s; with
`HOME_OFFSET = HOME = 10` the joint read 10 where the drive homed, also
with `0x607C` set. The drive's position jumped by up to 150 mm when it
homed, and the largest following error, measured every servo cycle
through homing, was 0.02 mm. A method that finds the same place every
time is still to be found for that machine: its home switches are not
wired to the drives, and the ECT60's torque homing against a hard stop
(methods 36 and 37, set up in `0x2030`) did not work there. Method 36
moves towards the negative end (CCW). With the homing velocity
`0x6099:01` at 2000 pulses/s the axis moved at about a third of that,
and at 500 or 1000 pulses/s not at all, also with 80 % of the current in
`0x2030:01`; each time the drive stopped with error 0x8611 (closed-loop
tracking error over the limit in `0x2022`). The profile shut the drive
down on the drive fault, as for any other.

What Rtelligent's manual says (ECT Series User Manual, section 2.4.6;
[PDF](https://rtelligentglobal.com/uploads/Rtelligent-ECT-Series-User-Manual1.pdf)):
the ECT60 supports the homing methods 17 to 30, 35, 36 and 37 (`0x6098`,
INT8, default 17). Methods 36 and 37 are torque homing, without external
limit signals: the drive decides from its own torque output that a
mechanical stop is reached. The manual describes both as running CCW at
high speed and ending at the stop (37 is probably CW; not tried). The
parameters are `0x2030:01` the maximum torque output and `0x2030:02` the
torque that says the motor is at the stop, both in percent of the current
set in `0x2000`; `0x2030:03` the motor speed at the stop (RPM);
`0x2030:04` how long that holds (units of 50 us); `0x2030:05` and `:06` the
speed loop's P and I gain during torque homing. `0x204C` ("HM method
select", 0 or 1) switches between two descriptions of the methods; 36 and
37 exist only with 0 (the default). Homing starts with a rising edge on
control word bit 4 in mode 6, and a rising edge on bit 8 stops it, as the
profile does it. The manual does not say how the torque, speed and time
conditions combine.

**Torque homing works on the ECT60 when the speed loop has an integral
gain** (found 2026-11-10, with the drive test, on X; the drive left
`0x2030:06` at 0 and torque homing never ended). With `0x2030:06` = 0 the
speed loop is a P regulator: it pushes the axis against the stop with only
about 750 mA, never reaching the detect torque (`0x2030:02`, 50 % of the
4000 mA in `0x2000` = 2000 mA; `actual-torque` reads in mA), and ends in
a following-error fault 0x8611 when `0x2022` is too small. With the I gain
at 100 the current rises to the detect level in about 1.6 s, the drive
sets "homing attained" (status word bit 12), and the position becomes 0
at the stop. Two runs in a row, from 10 mm and 30 mm away, both ended
this way (the axis presses about 0.3 mm further while the current rises,
so the stop's stiffness sets the repeatability). The settings that
worked, in `settings` of the slave:

```yaml
  - {object: '0x6098:00', type: SINT, value: 36}      # torque homing, towards CCW (X-)
  - {object: '0x6099:01', type: UDINT, value: 3000}   # pulses/s
  - {object: '0x6099:02', type: UDINT, value: 1000}
  - {object: '0x2030:01', type: UINT, value: 70}      # max torque output, % of 0x2000
  - {object: '0x2030:02', type: UINT, value: 50}      # torque that says "at the stop"
  - {object: '0x2030:03', type: UINT, value: 20}      # speed at the stop, RPM
  - {object: '0x2030:04', type: UINT, value: 2000}    # hold time, 50 us units
  - {object: '0x2030:05', type: UINT, value: 1000}    # speed loop P (the drive's own)
  - {object: '0x2030:06', type: UINT, value: 100}     # speed loop I (the drive's own: 0)
  - {object: '0x2022:00', type: UDINT, value: 100000} # following-error window, see below
```

`0x2022` has to be larger than what the position reference runs away
while the axis is stopped and the current rises: about 4000 counts or
more at these speeds, so the drive's 4000 is too tight. With 20000 and no
I gain the drive still faulted (0x8611) after 4.5 s at the stop; with
100000 and the I gain both runs ended before any fault. Not tried: `0x2030:03` at 200 with the I gain, method
37 (no stop to try it against in X+), other axes. Run the homing from
LinuxCNC only after trying it with the drive test, away from the stop
at first, with the emergency stop within reach.

Earlier on 2026-11-10 with the drive test and `0x6099:01` at 3000
pulses/s, 70 % in `0x2030:01`, detect limits of 50 % and 60 % and a
following-error window of 200 mm: X moves towards X− at about half the
commanded speed, stops softly at the end stop with 70 to 80 % torque,
and the drive never sets "homing attained" (status word bit 12), so the
homing does not end. Method 36 is not usable on the ECT60 as it is.

PP and PV were tried on the same machine on 2026-11-10: PV runs as
expected on X, Y and Z; PP works with the set-point given again as above
([test mode](ecatsetup.md#pp-pv-and-homing-at-the-machine)). The ECT60s
were delivered with `0x6098` = 17 (negative limit switch): a drive that
is homed without a switch drives into its end stop. Use method 35, or set
the method in `settings`.

### From lcec and the cia402 component

In the HAL file:

- remove `loadrt cia402 ...` and its `addf` lines;
- remove the nets between lcec's pins and `cia402.*` (status word, control
  word, modes, target and actual position and velocity);
- rename the other `cia402.<axis>.` pins and parameters to the slave's
  pins: `net x-pos-cmd => x-drive.pos-cmd`,
  `setp x-drive.pos-scale 100`; `csp-mode` is `cia402.mode` in the file;
- connect `drv-fault` to `joint.N.amp-fault-in`, so that LinuxCNC stops
  when a drive is shut down.

In `ethercat.yaml`, give the drive `profile: cia402` and `cia402.mode`,
and turn the entries above into `use: profile`. EtherCAT Setup proposes
drives this way.

## Encoder channels

Status: first version, 2026-11-08. It has run an EL5101 with a 3000-line
(12000 counts per turn) encoder, 32-bit counter: counting both ways,
velocity, and the index found twice 12000 counts apart.

An incremental encoder channel whose objects are laid out as in ETG.5001
MDP 511 gets position, velocity, index and latch pins from the `ecat`
component, like LinuxCNC's `encoder` component. This is not tied to one
terminal: Beckhoff uses the layout in the EL5101, EL5151, EL5152, EL7041,
EL7047, EL7342, EP5101 and others (about 40 families in Beckhoff's ESI
files), and a slave can have several channels, also next to a stepper or
servo channel.

A channel has its inputs at a status index S (`0x6000 + 0x10 × n`) and
its outputs at S + `0x1000`. The channel handles these entries, which are
`use: profile` in the file:

| Object | | |
|---|---|---|
| S:01 | input, 1 bit | latch C valid |
| S:02 | input, 1 bit | latch extern valid |
| S:03 | input, 1 bit | set counter done |
| S:11 | input, 16 or 32 bits | counter value; needed |
| S:12 | input, as S:11 | latch value |
| S+0x1000:01 | output, 1 bit | enable latch C |
| S+0x1000:02 | output, 1 bit | enable latch extern on positive edge |
| S+0x1000:03 | output, 1 bit | set counter |
| S+0x1000:04 | output, 1 bit | enable latch extern on negative edge |
| S+0x1000:11 | output, as S:11 | set counter value |

The channel's other entries, such as the states of inputs A, B and C,
open circuit or frequency, keep ordinary pins.

### In the configuration file (encoder)

```yaml
- position: 3
  name: io-3
  profile: mdp
  encoders:
  - name: enc
    index: '0x6010'
  process-data:
    outputs:
    - pdo: '0x1603'
      sm: 2
      entries:
      - {object: '0x7010:01', bits: 1, type: BOOL, use: profile}
      ...
      - {object: '0x7010:11', bits: 32, type: UDINT, use: profile}
    inputs:
    - pdo: '0x1A04'
      sm: 3
      entries:
      - {object: '0x6010:01', bits: 1, type: BOOL, use: profile}
      ...
      - {object: '0x6010:09', bits: 1, type: BOOL, pin: status-of-input-a, hal-type: bool}
      ...
      - {object: '0x6010:11', bits: 32, type: UDINT, use: profile}
      - {object: '0x6010:12', bits: 32, type: UDINT, use: profile}
```

EtherCAT Setup finds the channels, a "Counter value" at S:11 with
"Latch C valid" at S:01, and proposes them this way, named `enc`, or
`enc-1`, `enc-2`, ... when a slave has several. The EL5101's default PDO
assignment has a 16-bit counter; `--pdo-set "3=Standard 32 Bit (MDP 511)"`
gives the 32-bit one. Both work, since the count is extended (below).

### Pins (encoder)

Pins are `<slave name>.<channel name>.<pin>`.

| Pin | Type | Dir | |
|---|---|---|---|
| `pos` | real | out | Position: counts / `pos-scale`, from the index or reset. |
| `velocity` | real | out | Velocity in units per second, over `velocity-window-ms`. |
| `counts` | sint | out | Counts from the index or reset. |
| `raw-count` | sint | out | The terminal's counter as read. |
| `index-enable` | bool | I/O | Set it to find the index: at the index pulse, `pos` becomes 0 and the pin is cleared. Connect it to `joint.N.index-enable` or `spindle.N.index-enable`. Only there when latch C is mapped. |
| `reset` | bool | in | While true, `pos` is 0 at the current count. |
| `latch-enable-pos`, `latch-enable-neg` | bool | I/O | Arm the external latch input for a rising or falling edge; cleared when it latches. Only there when the external latch is mapped. |
| `latch-pos` | real | out | The position at the latched edge. |
| `ok` | bool | out | The process data is valid (working counter, slave in OP). |

Parameters:

| Parameter | Default | |
|---|---|---|
| `pos-scale` | 1.0 | Counts per unit. |
| `velocity-window-ms` | 10 | Time over which `velocity` is measured, at most 256 cycles. Its resolution is one count per window: 100 counts/s at 10 ms. |

### Behaviour (encoder)

- The counter, 16 or 32 bits, is extended to 64 bits, so `pos` does not
  wrap. The counter must move less than half its range in one cycle
  (32767 counts for 16 bits).
- `pos` is 0 when the component starts.
- The terminal's counter is never set; `index-enable` and `reset` move a
  reference instead. So `pos` jumps only at the index or a reset, never
  through a counter write that lands a cycle late.
- An index or latch is taken only after its "valid" bit was seen false
  since the pin was set, so an old latch is not taken for a new one.
- When the process data is not valid, `ok` is false, `pos` holds and
  `velocity` is 0. When it is valid again, counting goes on from there;
  if the terminal lost power meanwhile, the position is off and the axis
  must be homed again.

### From lcec

lcec's EL5101 driver had pins such as `enc-pos`, `enc-index-c-enable` and
`enc-pos-scale` (a pin there, a parameter here):

| lcec | ecat |
|---|---|
| `lcec.0.X.enc-pos` | `X.enc.pos` |
| `lcec.0.X.enc-count` | `X.enc.counts` |
| `lcec.0.X.enc-raw-count` | `X.enc.raw-count` |
| `lcec.0.X.enc-index-c-enable` | `X.enc.index-enable` |
| `lcec.0.X.enc-reset` | `X.enc.reset` |
| `lcec.0.X.enc-pos-scale` | `X.enc.pos-scale` (parameter: `setp`) |
| `lcec.0.X.enc-index-ext-pos-enable` | `X.enc.latch-enable-pos` (sets `latch-pos`, does not reset `pos`) |

lcec's EL5101 driver mapped the old (non-MDP) objects at `0x6000`/`0x7000`;
those are not handled here.

## Analog inputs and outputs

Analog terminals that follow ETG.5001 (Beckhoff EL3xxx inputs, EL4xxx
outputs, and others laid out the same way) need no profile and no C code.
EtherCAT Setup proposes them as `generic` slaves whose values are float
pins in the terminal's unit:

```yaml
  - pdo: '0x1A00'
    sm: 3
    entries:
    - {object: '0x6000:01', bits: 1, type: BOOL, pin: underrange, hal-type: bool}
    - {object: '0x6000:07', bits: 1, type: BOOL, pin: error, hal-type: bool}
    - {object: '0x6000:11', bits: 16, type: INT, pin: value, hal-type: real, scale: 0.00030518509475997192}
```

### The range

The value of a channel (`0x6nn0:11` for inputs, `0x7nn0:11` for outputs,
16 bits) has its full scale `0x7FFF` at the top of the measuring range.
The range is taken from the device's name, as the slave or its ESI file
gives it: `+/-10V`, `±75 mV`, `0-10V`, `4..20 mA`. Then:

| Range | Raw 0 | Raw 0x7FFF | `scale` (input) | `offset` (input) |
|---|---|---|---|---|
| ±10 V | 0 V | 10 V | 10/32767 | 0 |
| 0..10 V | 0 V | 10 V | 10/32767 | 0 |
| 4..20 mA | 4 mA | 20 mA | 16/32767 | 4 |

An input pin is `raw * scale + offset`. An output is written as
`(pin + offset) * scale`, so an output's `scale` is the inverse
(32767/10 for 0..10 V) and its `offset` the negative low end (-4 for
4..20 mA). The program writes both.

A terminal whose name has no range (temperature terminals, for example,
which report 0.1 °C per count) keeps a `sint` pin in raw counts, with a
note. Change `hal-type` to `real` and set `scale` by hand, or leave it.

### Status and settings

The status bits (`underrange`, `overrange`, `limit-1`, `limit-2`,
`error`, ...) are bit pins under their own names. Connect `error` to
whatever must stop when the signal is lost, such as the THC's enable.

The channel settings `0x8nn0` (filter, user scaling, presentation) are
offered as settings rows with their dictionary names. A terminal with
user scaling enabled (`0x8nn0:01`) reports scaled raw values; the pin's
`scale` must then be set to match.

### Not yet

- Tried with unit tests only, not on a terminal.
- Ranges come from the name. Where the name has none, a
  [quirk file](ecatsetup.md#quirk-files) can give the value's pin its `scale` and
  `offset` under `pins`.

## Stepper and DC motor terminals

Stepper terminals such as the Beckhoff EL7031, EL7041, EL7041-0052 and
EP7041, and DC motor terminals such as the EL7342, are handled as
ordinary `generic` slaves: no profile and no code of their own. EtherCAT
Setup proposes a pin for each object, named after it (`enable`, `reset`,
`reduce-torque`, `velocity`, `ready-to-enable`, `error`, ...).

What is special about a product goes into a [quirk file](ecatsetup.md#quirk-files).
The EL7041 has one, `beckhoff-el7041.yaml`, which gives:

| Object | Pin | |
|---|---|---|
| `0x7010:01` Enable | `enable` (bit, in) | The output stage is on while true. |
| `0x7010:02` Reset | `reset` (bit, in) | A rising edge clears an error. |
| `0x7010:03` Reduce torque | `reduce-torque` (bit, in) | The reduced current (`0x8010:02`) while true. |
| `0x7010:21` Velocity | `velocity` (float, in) | Velocity interface: -1..1 of the speed range. |
| `0x6010:04` Error | `error` (bit, out) | |

and HAL lines that reset an error when `enable` comes on again (below).
For another terminal, copy that file, change `match` and the objects, and
put it in `~/.config/ecatsetup/devices/`. Without a quirk file, the
velocity is a `sint` pin in raw values (±32767).

`nn` is the channel: the EL7041's motor is `0x7010`/`0x6010`, its
encoder `0x7000`/`0x6000`. The encoder channel is handled by the
[encoder function](#encoder-channels) when its counter and latch C are mapped.

With the quirk file, the velocity pin has `scale: 32767`: 1.0 is the top of the speed range
set in `0x8nn2:05` (0: 1000, 1: 2000, 2: 4000, 3: 8000, 4: 16000, 5: 32000
full steps per second). The terminal holds the written value within
±32767.

### Settings (stepper)

The channel settings `0x8nn0` (motor: maximum and reduced current in mA,
nominal voltage, coil resistance, full steps, encoder increments, ...)
and `0x8nn2` (features: operation mode, speed range, feedback,
microsteps, ...) are offered as settings with their dictionary names.
Take the motor's values from its data sheet and the units from the
terminal's documentation.

The operation mode `0x8nn2:01` chooses the interface: the default
(automatic) follows the process data mapped; 3 is the position interface
(EL7031, EL7041-0052 with `0x7nn0:11`).

### A joint with the velocity interface

The terminal runs at a velocity; a PID closes the position loop on the
encoder (or on the terminal's internal microstep counter when
`0x8nn2:08`, feedback, is off):

```
loadrt pid names=pid.x
addf pid.x.do-pid-calcs servo-thread

setp x-stm.enc.pos-scale 800          # counts per mm
setp pid.x.Pgain 50
setp pid.x.FF1 0.01                   # 1 / (mm/s at velocity 1.0)
setp pid.x.maxoutput 1.0

net x-pos-cmd  joint.0.motor-pos-cmd => pid.x.command
net x-pos-fb   x-stm.enc.pos         => pid.x.feedback joint.0.motor-pos-fb
net x-vel      pid.x.output          => x-stm.velocity
net x-enable   joint.0.amp-enable-out => pid.x.enable x-stm.enable
net x-fault    x-stm.error           => joint.0.amp-fault-in
```

`FF1` is the inverse of the velocity at 1.0: with a 4000 full steps/s
range, a 200-step motor and a 5 mm lead, 1.0 is 100 mm/s, so `FF1` is
0.01.

### A joint with the position interface

The position pin is a raw `sint` in microsteps. In the file, make it a
float in machine units:

```yaml
    - {object: '0x7010:11', bits: 32, type: DINT, pin: position, hal-type: real, scale: 6400.0}
```

(`scale` is microsteps per mm), and connect it:

```
net x-pos-cmd joint.0.motor-pos-cmd => x-stm.position joint.0.motor-pos-fb
net x-enable  joint.0.amp-enable-out => x-stm.enable
```

The terminal reports no position back in this interface, so the
command is the feedback, as with lcec's el70x1 driver.

### From lcec (stepper)

Importing an lcec XML file translates the stepper drivers' `modParam`s
into settings:

| lcec type | modParams |
|---|---|
| `EL7041`, `EL7041-1000`, `EP7041` | `maxCurrent`, `reducedCurrent` (A, written as mA), `nominalVoltage`, `coilResistance`, `motorEMF` (×1000, as lcec wrote them), `motorFullsteps`, `encoderIncrements`, `startVelocity`, `driveOnDelay`, `driveOffDelay`, `maxSpeed` (1000..32000 → 0..5), `encoder` (0x8012:08), `microsteps` (1..64 → 0..6) |
| `EL7031`, `EL7041-0052` | `maxCurrent`, `redCurrent`, `nomVoltage`, `coilRes`, `motorEMF` (as given), and `0x8012:01 = 3` (position interface), which the driver always set |

Others, such as `kp` and `ki`, get a note. lcec's EL7041 driver had no
`break` after `motorEMF` and the cases after it, so a `motorEMF` there was
also written into `motorFullsteps`, `encoderIncrements` and the following
objects; the import writes each value only into its own object. The pins
differ from lcec's (`enable` instead of `srv-enable`, and so on): the
HAL files must be changed.

### Resetting an error

lcec's EL7041 driver pulsed `reset` when `enable` came on again after an
error. The quirk file does the same in HAL: when the configuration is
written into LinuxCNC, `ethercat.hal` gets, for a terminal `x-stm`,

```
loadrt and2 names=x-stm-auto-reset
loadrt edge names=x-stm-enable-edge
addf x-stm-enable-edge servo-thread
addf x-stm-auto-reset servo-thread
...
setp x-stm-enable-edge.in-edge FALSE
setp x-stm-enable-edge.out-width-ns 10000000
net x-enable => x-stm-enable-edge.in
net x-stm-enable-rise x-stm-enable-edge.out => x-stm-auto-reset.in0
net x-stm-error x-stm.error
net x-stm-error => x-stm-auto-reset.in1
net x-stm-reset x-stm-auto-reset.out => x-stm.reset
```

The lines are left out, with a note, when nothing drives `enable` yet,
when your HAL files connect `reset` themselves, or when they load `edge`
or `and2` (HAL loads a component once).

### Not yet (stepper)

- Tried with unit tests only, not on a terminal.
- Writing a joint to a stepper terminal is not in *HAL & LinuxCNC*;
  connect it in your HAL files as above.

## Modular devices

Most EtherCAT I/O is modular in a way that needs nothing special: an
EK1100 coupler with terminals is one slave per terminal on the bus, and
each terminal is configured on its own.

The harder case is one slave with modules in slots behind it, such as a
coupler from another vendor with I/O modules, or a box with plug-in
cards (ETG.5001 "modular device"). Its process data depends on which
modules are in it. Support is **best effort** for now; reports from
users with such devices will decide what comes next.

What EtherCAT Setup does:

- When the slave has them, it reads the modules the slave detects
  (`0xF050`, Detected Module Ident List) and those it is configured for
  (`0xF030`, Configured Module Ident List).
- The process data is taken **as the slave maps it now**, from its SII
  and CoE: a coupler that maps its modules by itself works.
- The detected modules are recorded in the file:

  ```yaml
  - position: 2
    name: io
    modules: ['0x00000101', '0x00000202']
  ```

  The `ecat` component does not read them; EtherCAT Setup uses them to
  notice a change.
- A note says when `0xF030` differs from `0xF050`: some devices refuse
  OP until the configured list matches the modules in them. Write
  `0xF030` with the vendor's tool, or as settings (one row per
  subindex), and read the bus again.
- When the modules on the bus differ from the file (a module added,
  removed or swapped), *Update from bus*, `--update` and the check say
  so: propose that slave again, since its process data depends on the
  modules.

Not yet:

- Building the process data per slot from an ESI file's modules and
  slots, and writing `0xF030` from it.
- The `ecat` component's start check compares slaves, not modules.
