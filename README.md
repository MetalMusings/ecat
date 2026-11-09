# ecat

EtherCAT for LinuxCNC, configured without hand-written XML.

**Status: early development.** The design draft, a prototype of the
setup program's core, the first part of its window and the HAL component exist; they are
built in steps.

`ecat` is planned as:

- **EtherCAT Setup**, an interactive program that shows the slaves on the
  bus, configures them from the slave's own data and its ESI file, helps
  bring up and troubleshoot a slave that does not work, tests it, connects
  it to HAL, and writes the result into the LinuxCNC configuration;
- **`ecat`**, a HAL component that runs the userspace IgH EtherCAT master
  inside LinuxCNC and reads the configuration written by EtherCAT Setup.

Read the design draft:
[docs/design/ethercat-setup.md](docs/design/ethercat-setup.md).

## Install

Packages for Debian 12 and 13 with LinuxCNC 2.9.4 or newer:

1. the userspace EtherCAT master, `linuxcnc-ethercat-uspace` 0.7 or newer,
   from the `uspace-v*` releases of
   [MetalMusings/linuxcnc-ethercat](https://github.com/MetalMusings/linuxcnc-ethercat/releases);
2. `linuxcnc-ecat` from this repository's `ecat-v*`
   [releases](https://github.com/MetalMusings/ecat/releases): the `ecat`
   HAL component and the `ecatsetup` command.

```
sudo apt install ./linuxcnc-ethercat-uspace_*_amd64.deb ./linuxcnc-ecat_*_amd64.deb
```

`scripts/build-deb.sh` builds the `linuxcnc-ecat` package on a machine with
the master package, `linuxcnc-uspace-dev` and `libyaml-dev` installed
(`-w` builds uncommitted changes, to try them out).

## Prototype

`ecatsetup` is the first step: it reads the slaves on the bus (SII, CoE
dictionary, ESI files) and proposes their process data, distributed clocks
and HAL pins, optionally compared with an lcec XML file. It only reads from
the bus. It needs a running userspace master (`sudo ethercat-uspace start`,
or LinuxCNC with `lcec_uspace` or `ecat`). Installed from the package it
is the command `ecatsetup`; from this repository, `python3 -m ecatsetup`:

```
python3 -m ecatsetup --esi <ESI folder> --ini <machine>.ini --compare ethercat-conf.xml
python3 -m ecatsetup --ini <machine>.ini --write ethercat.yaml --interface enp2s0
python3 -m ecatsetup --esi <ESI folder> --pdo-set "3=Standard 32 Bit (MDP 511)" --write ethercat.yaml
python3 -m ecatsetup --import ethercat-conf.xml --ini <machine>.ini --write ethercat.yaml
python3 -m ecatsetup --ini <machine>.ini --update ethercat.yaml
python3 -m ecatsetup --check ethercat.yaml
python3 -m ecatsetup --diagnose
python3 -m ecatsetup --gui ethercat.yaml --ini <machine>.ini --esi <ESI folder>
python3 -m unittest discover -s tests
```

With ESI files, a slave gets the ESI's predefined PDO assignment that
the ESI marks as default (TwinCAT's "Predefined PDO Assignment"), or the
one named with `--pdo-set`; the report lists them.
Unsigned 32-bit counters, latches and positions get `s32` pins, since they
count down past 0; a 16-bit counter is noted, as it wraps at 65536.
`--write` writes the proposals as an ecat configuration file, `--import`
converts an lcec XML file, and `--check` checks a file and compares the bus
with it. `--update` brings a file up to date with the bus and keeps what is
in it: slaves still on the bus keep their entries (names, pins, settings,
mapping), slaves moved by others put in or taken out before them keep
theirs at the new position, new slaves get proposals, missing ones are
left out, and each change is reported. The old file is kept as `.bak`.
`--diagnose` reports, per slave, why it is not in OP (the AL status code
in plain words, with what usually lies behind it), its links and the
error counters that grow while it watches (`--seconds`, pointing at a
cable or connector), the slave's own diagnosis history and error
register, and for a CiA 402 drive its state, error code and what is
missing to reach "Operation enabled". `--gui` opens EtherCAT Setup's window (needs `python3-pyqt5`), laid out
as the mockups in the design draft, with the pages Bus, Troubleshoot,
Test and HAL & LinuxCNC. Part 1 of the GUI: it reads the bus, lists the
slaves with their state and how they compare with the file, shows each
slave's process data, pins, settings and DC, lets you edit them (name, CiA 402 mode, pins, bit pins, scaling,
settings, DC, the master's interface and transport), checks as you edit,
proposes a slave again in another mode or PDO assignment, updates the
file from the bus, imports lcec XML, and saves with a `.bak` copy. The
Troubleshoot page shows a slave's way from INIT to OP, its AL status code
in plain words, its CiA 402 state, the error counters per port, and its
own messages (0x10F3). HAL & LinuxCNC lists the pins and the lines for a
HAL file; writing them into the configuration is step 5 of the plan. Like the rest of `ecatsetup`, it only reads from
the bus; nothing moves.

Part 2 adds the I/O test, and part 3 moving a CiA 402 drive in CSP in
small steps, hold-to-enable, with a heartbeat and limits written to the
drive: [docs/test-mode.md](docs/test-mode.md). The `ecat` component runs
the configuration being edited (saved or not) in `halrun` with a servo
thread, without LinuxCNC. Each slave's Test tab shows its pins live and
sets the outputs of I/O slaves; pins set during the test go back to 0
before it stops. SDOs can be read and written from the same tab whenever
a master runs. CiA 402 drives stay disabled unless moved as above: their
pins are shown, not set, and their CiA 402 objects are not written; objects that are
commands rather than settings (such as 0x2010 on the ECT60) and objects
in the process data are refused too. The kernel master must be stopped
(`sudo systemctl stop ethercat`) and LinuxCNC must not run. The
free-running master hands the bus over to the test and does not come
back when it stops.
The file format: [docs/config-format.md](docs/config-format.md).
What the prototype found on a real bus:
[docs/prototype-step1.md](docs/prototype-step1.md).

## The component

`component/` holds the `ecat` HAL component, lcec_uspace's code (moved here
with its history) configured from `ethercat.yaml` instead of lcec's XML
file. The package installs it; to build it from here, it needs the
userspace master package (or a master built from source, with
`USPACE_ETHERCAT=<its prefix>`), `linuxcnc-uspace-dev` and `libyaml-dev`:

```
cd component
make
sudo make install
make check     # unit tests
```

`make check` runs the cia402 profile's and the encoder function's read
and write functions cycle by cycle against a fake of HAL and the master
(`component/tests/fake.c`), without a bus, and reads configuration files
into the records the component is set up from, checking the records and
the errors.

In a HAL file, instead of `lcec_conf` and `lcec`:

```
loadrt ecat config=ethercat.yaml
addf ecat.read-all  servo-thread
addf ecat.write-all servo-thread
```

- Slave pins are `<slave name>.<pin>` as named in the file; the master's
  are `ecat.0.*` (`ecat.0.all-op`, ...) and `ecat.*`.
- For an orderly stop, put `loadusr -w ecat-take-down` in the HAL file
  named by `[HAL]SHUTDOWN` in the INI file (such as
  `SHUTDOWN = shutdown.hal`), which LinuxCNC runs before it stops its
  threads. It sets `ecat.take-down`: every output becomes zero and the
  slaves go to PREOP while process data still flows (with
  linuxcnc-ethercat-uspace 0.7 or newer; before that only the outputs).
  Without it, the slaves lose their process data in OP when LinuxCNC
  stops, and stop with an error (sync manager watchdog 0x001B,
  synchronization error 0x001A); CiA 402 drives also latch a
  communication fault (such as 0x7500). The test mode of EtherCAT Setup
  does the same before it stops.
- The cycle time is the servo thread's period, taken at the first call
  of `ecat.read-all`; distributed clocks are configured then, before the
  master is activated.
- Profiles `cia402`, `mdp` and `generic` are supported. A `cia402` drive
  gets the CiA 402 state machine and scaled position and velocity pins in
  the component, with the pins of the cia402 HAL component:
  [docs/cia402.md](docs/cia402.md).
- Incremental encoder channels laid out as in MDP 511 (EL5101, EL5151,
  EL5152, EL7041, ...) get position, velocity, index and latch pins, like
  LinuxCNC's encoder component: [docs/encoder.md](docs/encoder.md).

A configuration imported from lcec can keep lcec's pin names by naming
the slaves `lcec.0.<name>`, so that existing HAL files work with only the
lines above changed. It has run a plasma cutter's LinuxCNC configuration
(three CiA 402 drives driven through HAL, one I/O slave) this way, and
with the drives as `cia402` slaves instead of the cia402 HAL component.

## Background

`ecat` grows out of `lcec_uspace`, a build of
[linuxcnc-ethercat](https://github.com/linuxcnc-ethercat/linuxcnc-ethercat)
against a userspace build of the IgH EtherCAT master
([MetalMusings/ethercat](https://github.com/MetalMusings/ethercat), based on
[stratuMAK/ethercat](https://github.com/stratuMAK/ethercat)). `lcec_uspace`
can be tried today from
[MetalMusings/linuxcnc-ethercat](https://github.com/MetalMusings/linuxcnc-ethercat)
(see its `INSTALL-USPACE.md`). Unlike linuxcnc-ethercat, `ecat` will not
read lcec's XML files; they can be imported instead.

## License

GPL version 2, as linuxcnc-ethercat, from which code will be taken. See
[LICENSE](LICENSE).
