# Quirk files

A quirk file tells EtherCAT Setup what it cannot read from a device's ESI
file, SII or object dictionary: better pin names, a scale, an object that
is a command and not a setting, where a drive keeps a limit, HAL lines a
device needs, and how lcec's settings for it translate. ecat has no
product drivers; a device that needs something special gets a quirk file
instead, which anyone can write without a new release.

Quirks act only through what EtherCAT Setup writes (`ethercat.yaml`,
`ethercat.hal`) or does in its test mode. The `ecat` component never reads
them, so the written files say everything that happens.

## Where they are

| Folder | |
|---|---|
| `~/.config/ecatsetup/devices/*.yaml` (or `$ECATSETUP_DEVICES`) | Your own. A file here for a vendor and product replaces the one that comes with the program. |
| `ecatsetup/devices/*.yaml` in the program | Those that come with EtherCAT Setup: `rtelligent-ect60.yaml`, `beckhoff-el7041.yaml`, `beckhoff-el70x1-position.yaml`. |

A file that cannot be read is skipped, with a warning in the window and
from `ecatsetup --check`. To share one, post it or propose it for the
program, with where each fact comes from (a manual, a test on a machine).

## Format

```yaml
name: Beckhoff EL7041
match: {vendor: '0x00000002', product: '0x1B813052'}

pins:                      # when the slave is proposed
  '0x7010:21': {pin: velocity, hal-type: float, scale: 32767}

commands:                  # objects that are commands, not settings
  '0x2010': Zero Position, a command

limits:                    # where the drive test finds a limit
  following-error: {object: '0x2022:00', type: uint32}
  travel: why the drive's own object is not used

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
| `pins` | Per object: `pin` (its name), and optionally `hal-type`, `scale`, `offset`, as in [config-format.md](config-format.md). Used when EtherCAT Setup proposes the slave; the file then holds them like any other pin. |
| `commands` | Per object index: why it is a command. The test mode does not write it. |
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

## The files that come with the program

- `rtelligent-ect60.yaml`: `0x2010` is a command (Zero Position); the
  drive test's following error is `0x2022`; the ECT60 ignores `0x607D` in
  CSP; lcec's `0x2010` sdoConfig becomes `cia402.zero-at-start`.
- `beckhoff-el7041.yaml`: pin names and the velocity scale; HAL lines
  that reset an error when `enable` comes on again (as lcec's driver did);
  lcec's modParams. See [stepper.md](stepper.md).
- `beckhoff-el70x1-position.yaml`: lcec's el70x1 modParams and the
  position interface it always set.
