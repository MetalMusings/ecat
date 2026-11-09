# Writing the configuration into LinuxCNC

EtherCAT Setup writes an `ethercat.yaml` into a LinuxCNC configuration,
together with the HAL lines that load the `ecat` component and connect
the joints to their drives. In the window this is the *HAL & LinuxCNC*
page, *Write to <folder>*; on the command line:

```
ecatsetup --apply ethercat.yaml --ini <machine>.ini
```

`--apply` shows every change as a diff and asks before writing (`--yes`
writes without asking). Nothing is written while the configuration has
errors.

## What is written

In the folder of the INI file:

| File | |
|---|---|
| `ethercat.yaml` | The configuration (the name of the file being edited). |
| `ethercat-load.hal` | New. Loads the component: `loadrt ecat config=[ETHERCAT]CONFIG`. |
| `ethercat.hal` | New. Adds `ecat.read-all` first and `ecat.write-all` last to the servo thread, and connects the joints to the drives. |
| `<machine>.ini` | `[ETHERCAT]CONFIG = ethercat.yaml`; `HALFILE = ethercat-load.hal` before the other HAL files, `HALFILE = ethercat.hal` after them. |
| your HAL files | Only lines that would clash are commented out (below). |

Every file that existed is kept as `.bak` first. `ethercat-load.hal` and
`ethercat.hal` are rewritten each time; change the joints in the program,
not in these files.

There are two files because the component must be loaded before your
HAL files, so that the slaves' pins exist for them, but its functions can
only be added once the servo thread exists, which `motmod` creates in
your first HAL file.

## Joints

A joint is connected to a CiA 402 drive in CSP. `ethercat.hal` then
holds, for joint 0 and a drive `x`:

```
setp x.pos-scale 100.0
net x-pos-cmd joint.0.motor-pos-cmd => x.pos-cmd
net x-pos-fb x.pos-fb => joint.0.motor-pos-fb
net x-enable joint.0.amp-enable-out => x.enable
net x-amp-fault x.drv-fault => joint.0.amp-fault-in
```

and `index-enable` both ways if the drive homes by itself (`modes:
[hm]`). The scale is counts per machine unit. The choice is kept in
`ethercat.yaml` (see [config-format.md](config-format.md#linuxcnc)).

When your HAL files already connect a joint to a drive, the program
proposes the same connection with the same `pos-scale` (*Take over from
the HAL files*; `--apply` takes them over by itself).

Joints the program does not connect stay as your HAL files have them.
CSV drives, gantries with their own components, home and limit switches,
and I/O such as an E-stop are connected in your HAL files for now.

## Names

A slave's pins are `<name>.<pin>`, with the name from `ethercat.yaml`
(letters, digits, `-`, `_`, `.`, unique). A configuration imported from
lcec keeps lcec's names, such as `lcec.0.X`, so that its HAL files work
unchanged; any other name works too, such as `x-drive` (`x-drive.pos-cmd`).

Rename a slave on the Bus page (*Name (pin prefix)*). A joint connected
to it follows, and `ethercat.hal` gets the new name at the next write.
Your own HAL files are not changed: lines there that use the old name,
such as an I/O slave's pins, must be changed by hand.

## Your HAL files

So that the two do not clash, these lines in the INI file's HAL files are
commented out with `#ecatsetup: ` in front:

- loading `lcec_conf`, `lcec`, `lcec_uspace` or `ecat`, and adding their
  functions;
- `net` lines with a pin that `ethercat.hal` connects;
- `setp` of a connected drive's `pos-scale`.

The signal names of those lines are kept in `ethercat.hal`. Other lines
using the signals, such as `net x-enable => pyvcp.x-led` in a
POSTGUI file, keep working. Other pins of a commented `net` line are put
on the same signal in `ethercat.hal`.

Each write starts from your files as you wrote them: lines with
`#ecatsetup: ` are taken as uncommented. A joint taken back out of the
program therefore gets its lines back, and writing twice changes nothing.

Not written:

- when a joint reaches its drive through other components (a filter
  between `joint.N.motor-pos-cmd` and the drive's `pos-cmd`): connect it
  by hand;
- when a HAL file outside the configuration folder (from LinuxCNC's
  `hallib`) would have to change: copy it into the folder.

## Tried

On a copy of the plasma cutter's configuration (three ECT60 in CSP, one
I/O board, QtPlasmaC), 2026-11-09:

- the joints were taken over from its HAL file with their scales;
- 27 lines in `QtPlasma.hal` were commented out (the `ecat` lines, the
  joints' connections and scales);
- the signals `x-enable`, `y-pos-fb` and `z-pos-cmd` stayed connected to
  `custom_postgui.hal`, `ddt` and `sampler`;
- the HAL files loaded in `halrun` (without `iocontrol` and `halui`,
  which need LinuxCNC) with `ecat.read-all` first and `ecat.write-all`
  last in the servo thread;
- the bus reached OP and stopped cleanly.

Then in LinuxCNC (QtPlasmaC, `xdp-native`), with the user at the
machine: the bus in OP, the thread in the same order, the POSTGUI file's
`delay-x-ferror` still on `x-enable`; machine on, homing, jogging all
three axes and moving as with the original configuration. After LinuxCNC
exited, the drives' `0x603F` was 0.
