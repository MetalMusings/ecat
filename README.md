# ecat

EtherCAT for LinuxCNC, configured without hand-written XML.

**Status: early development.** The design draft, a prototype of the
setup program's core (no GUI yet) and the HAL component exist; they are
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

## Prototype

`ecatsetup` is the first step: it reads the slaves on the bus (SII, CoE
dictionary, ESI files) and proposes their process data, distributed clocks
and HAL pins, optionally compared with an lcec XML file. It only reads from
the bus. It needs a running userspace master (`sudo ethercat-uspace start`,
or LinuxCNC with `lcec_uspace`):

```
python3 -m ecatsetup --esi <ESI folder> --ini <machine>.ini --compare ethercat-conf.xml
python3 -m ecatsetup --ini <machine>.ini --write ethercat.yaml --interface enp2s0
python3 -m ecatsetup --esi <ESI folder> --pdo-set "3=Standard 32 Bit (MDP 511)" --write ethercat.yaml
python3 -m ecatsetup --import ethercat-conf.xml --ini <machine>.ini --write ethercat.yaml
python3 -m ecatsetup --ini <machine>.ini --update ethercat.yaml
python3 -m ecatsetup --check ethercat.yaml
python3 -m ecatsetup --diagnose
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
missing to reach "Operation enabled". The file format: [docs/config-format.md](docs/config-format.md).
What the prototype found on a real bus:
[docs/prototype-step1.md](docs/prototype-step1.md).

## The component

`component/` holds the `ecat` HAL component, lcec_uspace's code (moved here
with its history) configured from `ethercat.yaml` instead of lcec's XML
file. It needs the userspace master in `/opt/ethercat-uspace` (see
Background), `linuxcnc-uspace-dev` and `libyaml-dev`:

```
cd component
make
sudo make install
```

In a HAL file, instead of `lcec_conf` and `lcec`:

```
loadrt ecat config=ethercat.yaml
addf ecat.read-all  servo-thread
addf ecat.write-all servo-thread
```

- Slave pins are `<slave name>.<pin>` as named in the file; the master's
  are `ecat.0.*` (`ecat.0.all-op`, ...) and `ecat.*`.
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
