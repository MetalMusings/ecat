# The configuration file (reference)

EtherCAT Setup writes `ethercat.yaml`, and the `ecat` HAL component reads
it. **You do not need to edit it**: everything here is set in the window
([EtherCAT Setup](ecatsetup.md)), and the program rewrites the file
without the comments added by hand. This document is for those who write
tools around ecat, read a file someone posted when asking for help, or
configure a machine by hand anyway: the file's format, loading the
component without EtherCAT Setup, the full command line, and the format of
quirk files.

What each kind of slave does in LinuxCNC (pins, parameters, behaviour)
is in [EtherCAT Setup: What the slaves give in
LinuxCNC](ecatsetup.md#what-the-slaves-give-in-linuxcnc).

- [Loading the component by hand](#loading-the-component-by-hand)
- [The configuration file](#the-configuration-file)
- [The profiles in the file](#the-profiles-in-the-file)
- [Command line](#command-line)
- [Quirk files](#quirk-files)

## Loading the component by hand

EtherCAT Setup writes these lines itself
([Writing the configuration into LinuxCNC](ecatsetup.md#writing-the-configuration-into-linuxcnc)).
By hand, in a HAL file, instead of `lcec_conf` and `lcec`:

```
loadrt ecat config=ethercat.yaml
addf ecat.read-all  servo-thread
addf ecat.write-all servo-thread
```


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
| `irq-cpu` | The CPU the network card's interrupts are pinned to, when the master starts (default: not pinned, they stay where the system put them). Give the CPU of the servo thread (with `isolcpus=3`: 3), so that the frames' arrival is handled there and not behind the other work of the CPUs the graphics, the mouse and the GUI use. Needs the privilege `rtapi_app` has (run in place: setuid). Without it, a lost cycle now and then (working counter 0 for one cycle, which shuts the drives down) was seen at 250 and 200 us, never in `halrun` without a GUI; see [development.md](development.md#the-servo-period-2026-11-16). |
| `cpu` | The CPU for the master's own threads (default: not bound). |
| `start-check` | `true` (default): the component stops at load when the bus differs from the file (below). `false`: it only warns, and starts with the slaves that match. |
| `dc.ref-clock-sync-cycles` | How often the DC clocks are synchronised, in cycles. With `servo-thread-follows-ref-clock: false` keep it small (1 to 100): at 1000 the slaves' clock wandered by microseconds within two minutes, at 1 or 100 it stayed within about one (measured 2026-11-14, docs/development.md "DC clock variants"). |
| `dc.servo-thread-follows-ref-clock` | `true`: LinuxCNC's servo thread is steered to follow the DC reference clock (the first slave with `dc:`, see [dc](#dc)); `false`: the slaves' clocks follow the PC's clock (measured 2026-11-13 with ECT60 drives, 1 ms: the slaves' clock drifted ~20 us/s against the thread although the reference clock was synchronised every 1000 cycles, so SYNC0 wandered through the whole period and the turn-around alternated between 2 and 3 cycles; under load (stress-ng, 10 min) the master's DC sync check also failed; use `true` when drives count on 2; `scripts/dc-jitter-measure` repeats the measurement). Called `sync-to-ref-clock` before; that name is an error now. |
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
the inputs are ready) are 50000 ns by default, and a sixth of the servo
period when that is less (20833 ns at 125 us: with 50 us each, a 125 us period had 25 us
left for the spread of the frames' send time, and one late frame put SYNC0 in the
three-cycle position). This is the same code as
lcec's `globalShiftTime="auto"` (see linuxcnc-ethercat's
`documentation/distributed-clocks.md`). The cia402 profile counts on two cycles
(see [the cia402 profile](ecatsetup.md#cia-402-drives)); three is reported as an error.

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
| `cia402.mode` | For `cia402`: the mode when enabled, `csp` or `csv` (cyclic, for a joint), `pv` (profile velocity, for a spindle or conveyor) or `pp` (profile position, for a tool changer or indexer; needs the settings `0x607D:01` and `0x607D:02`, and `0x607F:00` where the drive has it). |
| `modules` | For a modular device: the module idents it detected (`0xF050`) when it was read, to notice a change ([modular devices](ecatsetup.md#modular-devices)). The component does not read it. |
| `cia402.axes` | For `cia402`: a drive with several axes in one slave (such as a two-axis ECT60x2): a list of 1 to 4 names, the axes' pin prefixes. Axis n uses the CiA 402 objects at index + n × 0x800 (`0x6040`, `0x6840`, `0x7040`, `0x7840`); see [cia402](ecatsetup.md#several-axes). Without it, the drive has one axis named after the slave. |
| `cia402.home-enable-again` | For `cia402` with `hm`: `true` takes the drive down to "Switched on" and enables it again after homing, before it follows its command, for a drive that ignores its target after homing until enabled again (the ECT60 after torque homing). Written by the homing card. Default `false`. |
| `cia402.home-speed` | For `cia402` with `hm`: the fastest the drive moves while homing, counts/s (1 for a method that does not move). A larger step of the drive's position in one cycle is it setting its position, not travel (`home-travel`, `home-max-travel`). Written by the homing card; without it, a step of more than half of `home-max-travel` counts as such. |
| `cia402.zero-at-start` | For `cia402`: `true` makes positions count from where the drive stands when the bus first reaches OP, for drives that keep their position from an earlier run (see [cia402](ecatsetup.md#zero-at-start)). Default `false`. |
| `cia402.feedforward` | For `cia402` in CSP: `velocity` (the share of the command's velocity fed forward, 1.0 for all), `torque` (% of rated torque per machine unit/s² of acceleration), `torque-max` (the torque offset's limit, %, default 30); written to `0x60B1` and `0x60B2`, which must be `use: profile` in the outputs (see [cia402](ecatsetup.md#feedforward-in-csp)). |
| `cia402.modes` | For `cia402`: other modes the drive may change to: `hm` for homing by the drive's own method, and `csp`, `csv`, `pv`, `pp` to choose with the `opmode-cmd` pin. It needs `0x6060` and `0x6061`, and each mode's objects, in the process data. |
| `homing` | For `cia402` with `hm`: the homing method chosen in the window, `{method: 17, fast: 3000, slow: 1000, accel: 5000, offset: 0}` (a list of them, `null` for an axis without, for a drive with several axes). The master ignores it. The window writes the same choice to `settings` (0x6098, 0x6099, 0x609A, 0x607C and what the drive needs besides, from its quirk file), which is what the drive gets at start; the drive test writes it too, so a test homes as LinuxCNC will ([quirk files](#homing-quirk-files)). |
| `encoders` | Incremental encoder channels laid out as in MDP 511: a list of `name` (pin prefix) and `index` (the channel's status index, such as `'0x6010'`). See [encoder channels](ecatsetup.md#encoder-channels). |
| `unused` | `true`: the slave is on the bus but LinuxCNC does not use it: the component compares its `identity` with the bus at start (so a swapped slave is noticed) and does nothing else with it: no pins, no configuration, no state change. Everything else the entry holds is kept, unread, for a later `false`. Not with `eoe` (a slave with a network address is used). Default `false`. A slave that is not in the file at all is treated the same way, but an `unused` entry keeps the place (and the identity check), and the window and `--check` still know it. |
| `configure-pdos` | `true`: the master writes the PDO assignment and mapping below to the slave; `false`: the slave's own mapping is used and the entries below are only located in it. `false` is not advised: many slaves keep a mapping only until the power goes off, and the window asks before it sets it. With `false` the start check compares the PDOs below with the slave's own, as the master read them, sync manager by sync manager, and stops with a message naming both when they differ (`ecatsetup --check` and the window's check compare the same with a master running) (needs linuxcnc-ethercat-uspace 1.6 or later, which has `ecrt_master_get_pdo` & co.; with 1.5 it is not compared, a warning says so). |
| `process-data` | `outputs` (master to slave) and `inputs` (slave to master): lists of PDOs. |
| `dc` | Distributed clocks; leave out for none. |
| `eoe` | An IP address and more for a slave that speaks Ethernet over EtherCAT; leave out for none (see [eoe](#eoe)). |
| `init-cmds` | What the device's ESI file writes at start (its InitCmds; also a modular device's `0xF030` and a drive's mode), written **before** `settings`, in PREOP whenever the master configures the slave. Each row: `at` (`IP` or `PS`; others are not supported yet), `object`, and `value` with `type`, or `data` (hex bytes), or `from: profile` (a cia402 drive's `0x6060:00`: its mode) or `from: cycle-time` (the servo period in ns, UDINT: `0x1C32:02`, `0x1C33:02`); `complete: true` with `data` for complete access from subindex 0 or 1 (from 1 needs the master package 1.6 or later); `disabled: true` to leave a row out and keep it, so that EtherCAT Setup's *Update* does not bring it back. EtherCAT Setup fills it from the ESI file; keep your own SDOs in `settings`. |
| `settings` | SDOs written to the slave at start, in this order, after `init-cmds`. |
| `sdo-timeout-ms` | How long the slave may take to answer an SDO (its settings at start, and the master's other SDO transfers to it), 1 to 600000 ms; the master's own is 1000 ms. EtherCAT Setup takes the ESI file's `Mailbox/Timeout/ResponseTimeout` where it is longer (2000 ms in most). Needs a master with `ecrt_slave_config_sdo_timeout()` (the master package 1.6 or later); with an older one a warning says it is not used. |
| `state-timeouts-ms` | Longer state change timeouts than the master's (ETG.2000's 3000, 10000, 5000 and 200 ms), for a slave that needs them: `preop` (INIT to PREOP), `safeop-op` (PREOP to SAFEOP and SAFEOP to OP), `back-to-init` (to INIT, and down to PREOP), `back-to-safeop` (OP to SAFEOP), each 1 to 600000 ms; leave a key out for the master's. EtherCAT Setup takes those of the ESI file (`Info/StateMachine/Timeout`) that are longer. |

#### PDOs and entries

A PDO has `pdo` (its index), `sm` (its sync manager, 0 to 255) and
`entries`. A sync manager holds either outputs or inputs, not both. An
entry has:

| Key | Meaning |
|---|---|
| `object` | `"0xINDEX:SUB"`, or `"0x0000:00"` for a gap. |
| `bits` | Bit length. |
| `type` | Data type: `BOOL`, `SINT`, `INT`, `DINT`, `LINT`, `USINT`, `UINT`, `UDINT`, `ULINT`, `REAL`, `LREAL`. |
| `use` | `profile`: handled by the profile or an encoder channel, no pin (see [cia402](ecatsetup.md#cia-402-drives), [encoder channels](ecatsetup.md#encoder-channels)). `gap`: padding. Left out: an ordinary entry. |
| `pin`, `hal-type` | One pin for the whole entry; `hal-type` is `bool`, `sint`, `uint` or `real` (LinuxCNC 2.10's pin types; a file written for 2.9 spells them `bit`, `s32`, `u32`, `float`, which are read as the same types). Integer pins have 64 bits: a value written to an entry is held within the entry's range (an 8-bit entry takes -128..127 or 0..255 whatever the pin holds), see `raw` for bit patterns. A `real` pin of a `REAL`/`LREAL` entry carries the value as it is; of an integer entry it is scaled. A `LINT` entry takes a `sint` pin and a `ULINT` entry a `uint` pin, with all 64 bits exact (`real` still works, exact up to 2^53). On LinuxCNC 2.9 integer pins have 32 bits (a value is held within the entry's range as above, but a pin cannot hold more than 32 bits), and a `LINT`/`ULINT` entry takes `real` only. |
| `extend` | `true` on an input entry of 8 to 32 bits with `hal-type: sint`: the pin keeps counting past the wrap of the entry, in 64 bits (an encoder counter, a latched position). The first value is taken as signed, so a counter that counts down past 0 is negative. The counter must not move by half its range or more between two cycles. EtherCAT Setup proposes it for counters of 16 and 32 bits. On LinuxCNC 2.9 the pin has 32 bits and wraps there: `extend` is for entries of 8 to 16 bits only. |
| `raw` | `true` on an entry of up to 32 bits with `hal-type: uint`: the value is a bit pattern (digital outputs), and a value wider than the entry is cut to its width instead of held at the top, which would switch every bit on. |
| `scale`, `offset` | For a `float` pin of an integer entry: pin = value × scale + offset. |
| `split` | One `bool` pin per bit, from bit 0; `null` for a bit without a pin. Instead of `pin`. |
| `invert` | For a `bool` pin: `true` makes the pin the opposite of the bit (read and write), as for a normally closed switch, so that no `not` is needed in HAL. For a `split`: a list of the names of its pins to invert. |

An entry with neither `pin` nor `split` is mapped but has no pin.

#### dc

| Key | Meaning |
|---|---|
| `assign-activate` | The slave's AssignActivate word, from its ESI file or SII, such as `"0x0300"`. EtherCAT Setup offers the slave's own DC modes and warns about a value the slave does not describe, or a slave whose chip cannot take SYNC0 ([Distributed clocks](ecatsetup.md#distributed-clocks)). |
| `sync0-cycles` | SYNC0 period in servo periods. |
| `sync0-shift-ns` | The slave's own SYNC0 shift. |
| `sync1-cycles` | For a DC mode that uses SYNC1 (`assign-activate` with bit `0x0400`, such as `0x0700`): SYNC1 every n SYNC0 cycles (0 to 65535). EtherCAT Setup takes it from the ESI file's mode (`CycleTimeSync1`; a negative factor there counts bus cycles). Without it SYNC1 has no cycle of its own, and a slave may refuse SAFEOP (AL `0x0030`): EtherCAT Setup warns. |
| `sync1-shift-ns` | SYNC1 after SYNC0, in ns (0 or more, less than the SYNC0 cycle); a value other than 0 needs `sync1-cycles`. The register the master writes is (n - 1) x the SYNC0 cycle + this shift (ETG.2000). |

The **DC reference clock** is the used slave with `dc:` (an
`assign-activate` other than `0x0000`) that comes first on the bus: ETG.1020
(Protocol Enhancements, 21.1) has "the first device that should be
synchronized is used as the reference clock". The component tells the
master so (`ecrt_master_select_reference_clock`). Without that the master
takes the first DC-capable slave on the bus, also one the file leaves
`unused` or does not have, and requests OP of it, which an unconfigured
drive refuses. So a coupler without `dc:` (an EK1100) is not the reference
clock; slaves before the reference clock are not synchronized, which is
right for slaves without `dc:`. Without any slave with `dc:` the master's
own choice stands. `ecatsetup --check` names the reference clock and warns
when it has no DC system time (a chip with delay measurement only). The
choice needs the master package 1.5 or later (uspace-v1.5, fork
52eabcf4; before it the selection counted only from the next bus scan, so
the master's own choice stayed).

#### eoe

Slaves with an embedded web server (a drive, an IO-Link master, a
gateway) speak Ethernet over EtherCAT. This block gives such a slave its
address: the master sends it to the slave when it configures it, before
the slave goes to SAFE-OP.

```yaml
- position: 2
  name: x
  eoe: {ip: 192.168.10.21/24, host-ip: 192.168.10.1, hostname: drive-x}
```

| Key | Meaning |
|---|---|
| `ip` | The slave's IPv4 address, required, with the prefix length (`192.168.10.21/24`) or without. |
| `mask` | The subnet mask (`255.255.255.0`), instead of the prefix length. Not both. |
| `gateway`, `dns` | IPv4 addresses. |
| `hostname` | At most 31 characters. |
| `mac` | The slave's MAC address (`02:00:00:00:10:21`); the slave's own if left out. |
| `host-ip` | The PC's own address in the slave's subnet (`192.168.10.1`, no prefix length), see below. Needs the subnet (`ip` with a prefix length, or `mask`). |

The PC reaches the slave through a network device of its own per slave,
`eoe0s<position>` (`eoe0a<alias>` for a slave with an alias). The master
makes it when it scans the bus, and makes it anew at every rescan and when
LinuxCNC takes the bus over, so an address given to it with `ip addr add`
does not last. With `host-ip`, the master gives the device that address and
the slave's subnet mask and brings it up, each time: `ping 192.168.10.21`
and the slave's web page then work while LinuxCNC (or the I/O test) runs,
with nothing to set by hand. Without `host-ip` the device is left down and
is yours to configure.

- Give every EoE slave a subnet of its own (`192.168.10.x`, `192.168.11.x`):
  each has its own device on the PC, and two devices in one subnet reach
  only one of the slaves.
- Do not use a subnet the PC already is in (its ordinary network): the PC
  would have two ways into it.
- Nothing is routed or bridged: only the PC itself reaches the slave. For
  other computers to reach it, set up forwarding or a bridge yourself, and
  mind that a bridge puts the machine's bus on your network.
- The address is there while `ecat` is loaded. The idle master (after
  `ethercat-uspace start`, with LinuxCNC not running) knows no
  `ethercat.yaml` and sets none: `sudo ip addr add 192.168.10.1/24 dev
  eoe0s2; sudo ip link set eoe0s2 up` then.

In the window the block is edited on the slave page's *Network (EoE)* card
([ecatsetup.md](ecatsetup.md)), which proposes a free subnet.

`ecatsetup --check` and the window refuse an address that is not an IPv4
address, a mask that is not a subnet mask, a MAC address or hostname that
does not fit, a `host-ip` outside the slave's subnet or equal to the slave's
address, and warn about the same address on two slaves, a gateway outside
the slave's subnet, two slaves with `host-ip` in one subnet and (in the
window) a `host-ip` subnet the PC already has on another network device. With the bus read, a slave whose SII does not
list the EoE mailbox protocol is warned about, and a slave that has it gets
a note in its proposal. A slave that does not take the parameters only gives
an error line in the master's log: the bus starts anyway.

The frames to and from the slave are carried by the master's own EoE, which
needs the master package 1.4 or later: see [Not yet](ecatsetup.md#network-over-eoe-status).

#### settings

Each setting is an SDO download: `object` and either `type` and `value`
(written as that type, little endian), or `data` (raw bytes in hex, such
as `"a0 0f"`, at most 1024 bytes, also in `init-cmds`). `name` is for
people.

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
  params:
  - {pin: Encoder.enc.pos-scale, value: 4096.0}
  old-pins:
  - {old: lcec.0.E7000.in-1, new: E7000.in-1}
  - {old: lcec.0.E7000.rawData-1}
```

| Key | Meaning |
|---|---|
| `joints` | A list of `joint` (the joint's number), `slave` (the name of a `cia402` slave in mode `csp`) and `scale` (its `pos-scale`, counts per machine unit, not 0). Each joint and each slave once. |
| `check-hal-at-start` | `true`: `ethercat-load.hal` runs the HAL check (`ecatsetup --check-hal`) when LinuxCNC starts ([writing into LinuxCNC](ecatsetup.md#checking-the-hal-files)). |
| `params` | A list of `pin` (a parameter of a slave, of one of its axes or encoder channels, such as `Encoder.enc.pos-scale`) and `value` (a number; `true` or `false` for a bool parameter; a whole number, not negative for a `uint`), each parameter once. Written as `setp` lines in `ethercat.hal`. Not the master's parameters, and not the `pos-scale` of a drive a joint sets. |
| `old-pins` | For a configuration moved from lcec ([from an lcec configuration](ecatsetup.md#from-an-lcec-configuration)): a list of `old` (a pin the HAL files use, of lcec or of the cia402 component) and `new` (the pin or parameter of this configuration that takes its place; left out: the pin is taken out of its lines), each old pin once. |
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
To move a whole LinuxCNC configuration from lcec, its HAL files too, read
the bus instead and see [from an lcec configuration](ecatsetup.md#from-an-lcec-configuration).

Some lcec settings are replaced by what ecat does instead (in
[quirk files](ecatsetup.md#quirk-files)): an Rtelligent ECT60's `sdoConfig` of `0x2010`
(Zero Position) becomes `cia402: {zero-at-start: true}` when the slave
gets the cia402 profile (read from the bus); as a `generic` slave the
setting is kept, with a note.

lcec's `initCmds`, `idnConfig` and `watchdog` are not imported yet.
`modParam`s become settings where a [quirk file](ecatsetup.md#quirk-files) translates
them (lcec's stepper drivers, [stepper terminals](ecatsetup.md#from-lcec-stepper)); others
get a note. ecat has no product drivers, so a `modParam` will become settings
rows or process data choices (see the design draft, [Decision: profiles
with quirks, no product
drivers](development.md#decision-profiles-with-quirks-no-product-drivers)).

### In the component

The `ecat` component reads the file with `loadrt ecat config=<file>`. It
supports the profiles `cia402` ([cia402](ecatsetup.md#cia-402-drives)), `mdp` and
`generic`, and encoder channels on any of them ([encoder channels](ecatsetup.md#encoder-channels)). 64-bit integer entries are `sint`/`uint` pins (above).

### Open

- Options for profiles beyond `cia402.mode` and `cia402.modes`.

One master and one bus per configuration, by decision (see the design
draft, *Decided questions*). Scaling for `cia402` (counts per unit) is
the HAL parameter `pos-scale`, set from `linuxcnc.joints` by the
`ethercat.hal` EtherCAT Setup writes; drives that are not joints set it
in HAL.

## The profiles in the file

### cia402

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
| `0x6060` modes of operation | output | with `modes` (changing modes); else written every cycle if mapped, or set once as the setting `0x6060:00` (below) |
| `0x6061` modes of operation display | input | with `modes`; else checked before enabling if mapped |
| `0x607A` target position | output | `csp`, `pp` |
| `0x6064` position actual value | input | `csp`, `pp` |
| `0x60FF` target velocity | output | `csv`, `pv` |
| `0x6081` profile velocity | output | no; `pp` gets a `pp-velocity` pin if mapped |
| `0x606C` velocity actual value | input | `csv`, `pv` |
| `0x60B1` velocity offset | output | no; feedforward in `csp`, 0 without it |
| `0x60B2` torque offset | output | no; feedforward in `csp`, 0 without it |
| `0x603F` error code | input | no |

The drive's mode must be set or seen. With one mode, either `0x6060` is
in the process data (written every cycle), or the mode is written once at
start: an `init-cmds` row `{at: PS, object: '0x6060:00', from: profile}`
(the value is the mode's number: 1 `pp`, 3 `pv`, 8 `csp`, 9 `csv`, as
StepperOnline's and Rtelligent's ESI files ask with an InitCmd) or a
setting `0x6060:00` of type `SINT`; or at least `0x6061` is mapped, so
that the profile enables the drive only when it shows the mode. A value
written at start must be the mode of `cia402.mode`. Without any of them the
component does not load: "the drive's mode is neither set nor seen ...".
EtherCAT Setup proposes the row where `0x6060` is not mapped (StepperOnline
A6's default PDOs `0x1701`/`0x1B01`, for example), also for a drive that
reports one mode only in `0x6502`: such a drive may still start in mode 0
(no mode). Changing modes while running (`modes`) needs
both `0x6060` and `0x6061` in the process data.

Other entries of the drive, such as torque or digital inputs, get ordinary
pins.

`zero-at-start: true` makes positions count from where the drive stands
when the bus first reaches OP; see [Zero at start](ecatsetup.md#zero-at-start).

#### Changing modes

`cia402: {mode: pv, modes: [pp]}` is a spindle that also positions: it gets the pin `opmode-cmd`. The mode objects `0x6060` and `0x6061` must be in the process data, and each allowed mode's objects; PP's limits must be in `settings`.

#### Profile position (PP)


```yaml
  cia402: {mode: pp}
  settings:
  - {object: '0x607D:01', type: DINT, value: -100000}   # software limit, min
  - {object: '0x607D:02', type: DINT, value: 100000}    # software limit, max
  - {object: '0x607F:00', type: UDINT, value: 50000}    # max profile velocity, if the drive has it
```

#### Several axes


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

Joints connect to an axis like to a drive (`linuxcnc.joints: [{joint: 0, slave: xy-1, ...}]`).

#### Zero at start


```yaml
  cia402:
    mode: csp
    zero-at-start: true
```

#### Feedforward


```yaml
  cia402:
    mode: csp
    feedforward:
      velocity: 1.0       # the share of the velocity fed forward
      torque: 0.012       # % of rated torque per mm/s² of acceleration
      torque-max: 30      # the torque offset's limit, %; default 30
  process-data:
    outputs:
    - pdo: '0x1600'
      entries:
      - {object: '0x6040:00', bits: 16, type: UINT, use: profile}
      - {object: '0x607A:00', bits: 32, type: DINT, use: profile}
      - {object: '0x60B1:00', bits: 32, type: DINT, use: profile}
      - {object: '0x60B2:00', bits: 16, type: INT, use: profile}
```

#### Homing

With `modes: [hm]` (and `0x6060` and `0x6061` as `use: profile` entries) the drive homes by its own method; the method, speeds, acceleration and home offset are its objects `0x6098`, `0x6099`, `0x609A` and `0x607C`, in `settings`. `homing:` keeps the window's choice (see [slaves](#slaves)), with `enable-again` the box *Enable the drive again after homing*; from it the homing card writes `cia402.home-enable-again` and `cia402.home-speed` (the profile reads those, not `homing:`).

#### From lcec and the cia402 component

`csp-mode` is `cia402.mode` in the file; give the drive `profile: cia402` and turn the entries the profile owns into `use: profile`. EtherCAT Setup proposes drives this way.

### Encoder channels

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

### Serial channels

```yaml
- position: 4
  name: rs485
  profile: generic
  serial:
  - name: vfd
    output-pdo: '0x1602'
    input-pdo: '0x1A02'
    port: /dev/ttyECAT0
    baud: 19200
    frame: 8E1
  process-data:
    outputs:
    - pdo: '0x1602'
      sm: 2
      entries:
      - {object: '0x3003:01', bits: 16, type: UINT, use: profile}
      - {object: '0x3003:02', bits: 8, type: USINT, use: profile}
      # ... to 0x3003:17, 22 data bytes
    inputs:
    - pdo: '0x1A02'
      sm: 3
      entries:
      - {object: '0x3103:01', bits: 16, type: UINT, use: profile}
      - {object: '0x3103:02', bits: 8, type: USINT, use: profile}
      # ... to 0x3103:17
  settings:
  - {object: '0x8000:06', value: 1, type: BOOL}   # half duplex (RS485)
```

| Key | | |
|---|---|---|
| `name` | needed | the pin prefix: `<slave>.<name>.` |
| `output-pdo`, `input-pdo` | needed | the channel's PDOs (0x16xx/0x17xx and 0x1Axx/0x1Bxx), both in the process data, of the same size: 3 to 24 bytes |
| `port` | needed | the serial port's path, such as `/dev/ttyECAT0` |
| `baud` | 9600 | at start: 300, 600, 1200 (EL6002, EL6022), 2400, 4800, 9600, 12000, 14400 (EL6001, EL6021 from firmware 07), 19200, 38400, 57600 or 115200 |
| `frame` | 8N1 | data bits, parity, stop bits at start: 7E1, 7O1, 8N1, 8E1, 8O1, 7N2, 7E2, 7O2, 8N2, 8E2, 8O2 |
| `send-continuous` | false | "Enable send FIFO data continuous" (0x8000:04): the terminal sends what the program wrote as a whole (up to 128 bytes), without a pause; for Modbus RTU above 38400 baud (below) |
| `settings-index` | 0x8000 + 0x10 × channel | the channel's CoE settings object |

The baud rate, data frame and send continuous are written at start (as
settings: so they are not also in `settings:`); the terminal's other settings, such as half
duplex for RS485 or "Enable send FIFO data continuous", are ordinary
settings. A slave can have up to 8 channels (an EL6022 has two).

### Analog terminals

```yaml
  - pdo: '0x1A00'
    sm: 3
    entries:
    - {object: '0x6000:01', bits: 1, type: BOOL, pin: underrange, hal-type: bool}
    - {object: '0x6000:07', bits: 1, type: BOOL, pin: error, hal-type: bool}
    - {object: '0x6000:11', bits: 16, type: INT, pin: value, hal-type: real, scale: 0.00030518509475997192}
```

`scale` and `offset` of a float pin come from the range (EtherCAT Setup writes both; [the range](ecatsetup.md#the-range)).

### Stepper terminals: the position interface

The position pin is a raw `sint` in microsteps. In the file, make it a
float in machine units:

```yaml
    - {object: '0x7010:11', bits: 32, type: DINT, pin: position, hal-type: real, scale: 6400.0}
```

### Modular devices

The detected modules are recorded in the file:

```yaml
- position: 2
  name: io
  modules: ['0x00000101', '0x00000202']
```

The `ecat` component does not read them; EtherCAT Setup uses them to notice a change.

## Command line

Everything the window does with a file is done by the same functions from
the command line, which suits scripts. Installed from the package the
command is `ecatsetup`; from a checkout, `python3 -m ecatsetup`. Reading
the bus needs a running master.

```
ecatsetup --esi <ESI folder> --ini <machine>.ini --compare ethercat-conf.xml
ecatsetup --ini <machine>.ini --write ethercat.yaml [--interface enp2s0]
ecatsetup --esi <ESI folder> --pdo-set "3=Standard 32 Bit (MDP 511)" --write ethercat.yaml
ecatsetup --import ethercat-conf.xml --ini <machine>.ini --write ethercat.yaml
ecatsetup --ini <machine>.ini --update ethercat.yaml
ecatsetup --check ethercat.yaml
ecatsetup --diagnose
ecatsetup --check-hal <machine>.ini
ecatsetup --events [prev]
ecatsetup --report --ini <machine>.ini
ecatsetup --capture --esi <ESI folder>
ecatsetup --response ~/.local/state/ecatsetup/responses/<file>.samples [--ini <machine>.ini]
ecatsetup --from-capture ecat-capture-<time>.tar.gz --diagnose
ecatsetup --rt-check <machine>.ini [--rt-seconds 900] [--rt-load stress] [--rt-twice] [--rt-root]
ecatsetup --rt-check <machine>.ini --improve [--rt-report report.md]
ecatsetup --rt-todo [todo.md]
ecatsetup --from-run ~/.local/state/ecatsetup/rt-runs/<file>.json
ecatsetup --apply ethercat.yaml --ini <machine>.ini
ecatsetup --gui ethercat.yaml --ini <machine>.ini --esi <ESI folder>
```

- Without options, the bus is read and the proposals are reported;
  `--compare` compares them with an lcec XML file. With ESI files, a
  slave gets the ESI's predefined PDO assignment that the ESI marks as
  default (TwinCAT's "Predefined PDO Assignment"), or the one named with
  `--pdo-set`. Unsigned 32-bit counters, latches and positions get `sint`
  pins, since they count down past 0. Input counters of 16 and 32 bits get
  `extend: true`, so that the pin goes on counting past the wrap of the entry
  (64 bits); a note says so. The digital outputs (`0x60FE`) get `raw: true`.
- `--write` writes the proposals as an ecat configuration file;
  `--import` converts an lcec XML file; `--check` checks a file and
  compares the bus with it.
- `--update` brings a file up to date with the bus and keeps what is in
  it: slaves still on the bus keep their entries (names, pins, settings,
  mapping), slaves moved by others put in or taken out before them keep
  theirs at the new position, new slaves get proposals, missing ones are
  left out, and each change is reported. The old file is kept as `.bak`.
- `--diagnose` reports, per slave, why it is not in OP (the AL status
  code in plain words, with what usually lies behind it), its links and
  the error counters that grow while it watches (`--seconds`, pointing at
  a cable or connector), the slave's own diagnosis history and error
  register, and for a CiA 402 drive its state, error code and what is
  missing to reach "Operation enabled".
- `--events` shows the event log, and `--report` writes one file to
  attach when asking for help ([Event log and report](ecatsetup.md#event-log-and-report)).
  `--capture` records the bus for a developer who does not have the
  devices, and `--from-capture` reads the bus from such a file
  ([A capture of the bus](ecatsetup.md#a-capture-of-the-bus)).
- `--response` analyses a recording of the Response card again
  ([Measuring a drive's response](ecatsetup.md#measuring-a-drives-response)).
- `--rt-check` is the first part of the real-time check: it changes
  nothing and needs no sudo. It lists what the computer is (kernel and
  PREEMPT_RT, isolated CPUs, frequency governor and turbo, idle states,
  where every interrupt goes, network cards with their coalescing, EEE and
  ASPM, graphics driver, BIOS, virtual machine or not, the INI's threads and
  I/O, the CPU LinuxCNC's threads run on), each fact with where it was
  read. Then it measures how evenly the real-time threads start, every
  cycle: with LinuxCNC running, beside it (a function of ecat's own small
  HAL module `ecat_rtprobe` put first into each thread and taken out again at
  the end; it only notes the time of each run, talks to nothing, and works
  beside a configuration's own `sampler` or `timedelta`); otherwise in `halrun` with the INI's threads alone
  (`--rt-way threads|attach|inventory` chooses). It prints how late the
  threads started (the largest, and what 99.99 % of the cycles stay within),
  the cycles over 20, 50 and 100 us with what the length of the run can say
  ("none in 15 min: fewer than 12 per hour"), and the period's budget: what
  is left of the period after the lateness and the thread's longest run, and
  how much of that run is the I/O driver's. Beside it, counters of the
  real-time CPU are read every 10 ms (interrupts, softirqs, idle states,
  frequency, heat, ACPI events), and each late cycle is compared with them:
  "11 of 12 spikes came with the SMI count growing (by chance 3 %)". With
  `--rt-root` (sudo asks once) it also counts SMIs and, with the threads
  only, runs `rtla timerlat` (package `rtla`), whose own analysis of the
  first wake-up over 50 us is shown. `rtla` is never run beside LinuxCNC:
  switching the kernel's tracing on and off at its start and end delays the
  real-time threads, once enough for LinuxCNC's "Unexpected realtime delay". Then the **findings**: what the rule catalogue makes of the
  inventory and the run, most to gain first, each with what was seen, why it
  matters, what to do (the commands, or the kernel parameter), its risk and
  cost, how to check it worked, how to undo it, where the knowledge comes
  from, and whether we have seen it on our own computers. Nothing is changed:
  the commands are for you to run (as root), one at a time, measuring again
  after each. A late cycle that no rule explains is reported as such, with
  the counter that came with it. `--rt-load stress` runs `stress-ng`
  meanwhile; `--rt-twice` measures twice and says how much the numbers
  move by chance, so that a later change is judged against that. Every run
  is saved in `~/.local/state/ecatsetup/rt-runs/`; `--from-run` shows one
  again. `--improve` goes on as the *Timing* tab does
  ([The computer's timing](ecatsetup.md#the-computers-timing)): two runs, then the
  run-time changes of the findings one at a time with sudo, each measured
  again and kept or undone; `--rt-report` writes the report at the end.
  `--rt-todo [FILE]` writes the lasting changes for you to make (kernel
  parameters, sysctl, a start-up service, the BIOS), from what was kept and
  found so far ([The computer's timing](ecatsetup.md#the-computers-timing), step 7).
  The rules are data, in `ecatsetup/rtrules/*.yaml`;
  your own go in `~/.config/ecatsetup/rtrules/` (a rule with the same `id`
  replaces the shipped one; the format is at the top of `rtrules.py`).
- Other options: `--mode` chooses the mode proposed for CiA 402 drives,
  `-p` limits the proposal to some slaves, `--json` prints it as JSON,
  `--transport` and `--interface` set the master's for `--write`.
- `--apply` writes into a LinuxCNC configuration, and `--check-hal` checks
  its HAL files against the file ([Writing the configuration into
  LinuxCNC](ecatsetup.md#writing-the-configuration-into-linuxcnc)); `--net PIN=PIN`
  connects I/O pins, `--take-over-io` reads the connections your HAL
  files already make, `--yes` does not ask, and `--overwrite-edited`
  replaces files edited by hand. `ecatsetup --help` lists them all.

## Quirk files

The format of the files described in [Quirk files](ecatsetup.md#quirk-files). **Development stage**: the format may still change, and is not taught to users yet.


```yaml
name: Beckhoff EL7041
match: {vendor: '0x00000002', product: '0x1B813052'}

pins:                      # when the slave is proposed
  '0x7010:21': {pin: velocity, hal-type: real, scale: 32767}
  '0x6000:00': {split: [in-1, in-2, in-3, null, in-5]}   # one pin per bit

settings:                  # added to the slave when it is proposed
- {object: '0x8010:01', name: Maximal current, type: UINT, value: 1500}

commands:                  # objects that are commands, not settings
  '0x2010': Zero Position, a command
  '0x1010:01':             # the longer form tells "Run command" what to write
    note: Store parameters
    value: 1702257011      # "save" as ASCII
    type: uint32
    confirm: Keeps every setting through a power cycle

limits:                    # where the drive test finds a limit
  following-error: {object: '0x2022:00', type: uint32}
  travel: why the drive's own object is not used

tuning:                    # settings the Response card's search shows first
  '0x2009:00': {kind: command-filter, type: UINT, unit: us, min: 0, max: 25600, factory: 6400,
                note: what it does, tried: what it gave on a machine}
  '0x2023:04': {kind: feedforward, type: UINT, when: {object: '0x2011:00', values: [1]}}

measure:
  ignore: {'0x6077': why the response measurement does not record it}

hal:                       # HAL lines for each matching slave
  load: {edge: ['{name}-enable-edge']}
  addf: ['{name}-enable-edge']
  lines:
  - net {signal:0x7010:01} => {name}-enable-edge.in

lcec:                      # importing lcec XML files
  types: [EL7041, EP7041]
  modparams:
    maxCurrent: {object: '0x8010:01', type: UINT, convert: milli}
    maxSpeed: {object: '0x8012:05', type: UINT, values: {1000: 0, 2000: 1}}
  settings:                # always added for these lcec types
  - {object: '0x8012:01', type: USINT, value: 3, note: why}
  sdo:                     # sdoConfig entries replaced
    '0x2010:00': {replace: cia402.zero-at-start, note: why}
```

Every key but `name` is optional. A file with only `lcec` (no `match`)
serves the import alone.

| Key | |
|---|---|
| `match` | `vendor` and `product` of the device. |
| `pins` | Per object: `pin` (its name), and optionally `hal-type`, `scale`, `offset`, as in [the configuration file](#pdos-and-entries); or `split`, a list of bit pin names from bit 0 (`null` for a bit without a pin) for an object that holds several bits. Used when EtherCAT Setup proposes the slave; the file then holds them like any other pin. *Save as quirk file* keeps bit pins too. |
| `settings` | Settings (SDOs written at every start) the slave gets when it is proposed: `object`, `type`, `value`, and optionally `name`, as in [the configuration file](#pdos-and-entries). |
| `commands` | Per object index: why it is a command. The test mode does not write it. |
| `tuning` | Per object: `kind` (`command-filter`, `position-loop`, `feedforward`, `torque`, `gain-high`, `other`) and `type`, and optionally `unit`, `min`, `max`, `factory`, `note`, `tried` (what it gave on a machine), and `when` (`object` and `values`: it is used only in that mode of the drive). Shown by *Look for settings in the drive* ([Measuring a drive's response](ecatsetup.md#measuring-a-drives-response)). |
| `measure` | `ignore`: per object index, why the response measurement does not record it (a drive's object that does not mean what the standard says). |
| `limits` | For the drive test: `following-error`, `max-torque`, `max-velocity` as `object` and `type` (the ethercat tool's: `uint32` ...), or `travel` and the others as a text saying why the drive's own object is not used. |
| `hal` | `load`: per HAL component, its instances (one `loadrt ... names=` for all slaves); `addf`: functions, added to the servo thread between `ecat.read-all` and `ecat.write-all`; `lines`: HAL lines. |
| `lcec.types` | lcec type names (`type=` in the XML) that `modparams` and `settings` apply to. |
| `lcec.modparams` | Per lcec `modParam`: the `object` and `type` of the setting, and `convert` (`milli`: ×1000, `flag`: 0/1, else a number as it is) or `values` (a table). |
| `lcec.settings` | Settings always added for those types (what the lcec driver always wrote). |
| `lcec.sdo` | An lcec `sdoConfig` that ecat does differently: `replace` with `cia402.zero-at-start` (when the slave gets the cia402 profile). |

### HAL lines

In `hal`, these are filled in for each slave:

| | |
|---|---|
| `{name}` | The slave's name (its pin prefix). |
| `{pin:0x7010:02}` | The pin of that object. It must be free: if your HAL files connect it, the lines are left out. |
| `{signal:0x7010:01}` | The signal that object's pin is on, in your HAL files or `ethercat.hal`. For an output of the slave (an input pin in HAL, such as `enable`), something must drive it, or the lines are left out. For an input of the slave, a signal is made if there is none. |

The lines are written into `ethercat.hal` under a comment naming the
quirk file. They are left out, with a note in the plan, when an object
has no pin in the configuration, when a placeholder cannot be filled, or
when your HAL files load the same component (HAL loads a component only
once: add the instance names there and connect them by hand).

### Homing (quirk files)

```yaml
homing:
  methods: [17, 18, 35, 36]          # the CiA 402 homing methods (0x6098) the drive supports
  describe:                          # the drive's own text for a method (default: the standard's)
    36: {text: "Torque homing ...", needs: [mechanical stop], moves: true}
  extras:                            # settings the drive needs besides 0x6098, 0x6099, 0x609A, 0x607C
    36:
      - {object: '0x2030:01', type: UINT, value: 70, name: Max torque output}
```

Without `methods`, EtherCAT Setup reads the drive's 0x60E3 (supported homing
methods); if the drive has neither, every method of the standard is offered.
The ECT60 has no 0x60E3 (read from a drive, 2026-11-10), so its file lists
the methods from the manual. The extras are for the axis of the slave's
first axis; for later axes their indexes move by 0x800 as the other objects do.
