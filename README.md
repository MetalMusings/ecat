# ecat

EtherCAT for LinuxCNC, configured without hand-written XML.

**Status: early development, tried on one machine.** The HAL component
runs a plasma cutter (three CiA 402 drives in CSP, one I/O slave); PP and
PV have been tried on its drives too. EtherCAT Setup reads, proposes,
checks, updates and diagnoses a configuration in its window or from the
command line, tests it (I/O, SDOs, moving a drive in small steps, homing),
writes it into a LinuxCNC configuration, and watches a running LinuxCNC
read only. Everything is built in steps, following the design draft.

ecat has two parts:

- **EtherCAT Setup** (`ecatsetup`), a program that shows the slaves on the
  bus, configures them from the slave's own data and its ESI file, helps
  bring up and troubleshoot a slave that does not work, tests it,
  connects it to HAL, and writes the result into the LinuxCNC
  configuration;
- **`ecat`**, a HAL component that runs the userspace IgH EtherCAT master
  inside LinuxCNC and reads the `ethercat.yaml` that EtherCAT Setup
  writes.

## Documentation

| | |
|---|---|
| [Getting started](docs/getting-started.md) | What gets installed, and how to try ecat step by step without disturbing your setup, from the safest. Start here. |
| [EtherCAT Setup](docs/ecatsetup.md) | The window, bringing up a new slave, the test mode, writing into LinuxCNC, the event log and report, quirk files, and the command line. |
| [The ecat component](docs/component.md) | Using the component in HAL, the `ethercat.yaml` file format, and what drives (cia402), encoder channels, analog and stepper terminals and modular devices give you. |
| [Developing ecat](docs/development.md) | Building and testing, the design draft, what the prototype found on a real bus, and the plan for fault tests at a machine. |

## Install

Packages for Debian 12 and 13 with LinuxCNC 2.9.4 or newer:

1. the userspace EtherCAT master, `linuxcnc-ethercat-uspace`, from the
   `uspace-v*` releases of
   [MetalMusings/ethercat](https://github.com/MetalMusings/ethercat/releases);
2. `linuxcnc-ecat` from this repository's `ecat-v*`
   [releases](https://github.com/MetalMusings/ecat/releases): the `ecat`
   HAL component and the `ecatsetup` command.

```
sudo apt install ./linuxcnc-ethercat-uspace_*_amd64.deb ./linuxcnc-ecat_*_amd64.deb
```

[Getting started](docs/getting-started.md) says what the packages install
and how to try them, and to undo it. To build the package yourself:
`scripts/build-deb.sh` ([Developing ecat](docs/development.md#build-and-test)).

## Quick start

```
sudo systemctl stop ethercat          # the kernel master, if you have one
sudo ethercat-uspace start -- -i enp2s0    # or answer y when a command offers it (uspace 1.2)
ecatsetup --gui ethercat.yaml --ini <machine>.ini
```

Starting the master by hand is optional: when no master answers, the window
(and the command line, on a terminal) shows the command and offers to run it
with sudo, see [Starting the master](docs/ecatsetup.md#starting-the-master).

The window opens on a *Start* page that lists the tasks and the steps of
each ([EtherCAT Setup](docs/ecatsetup.md)). It needs `python3-pyqt5`. The
same functions are available as options for scripts (`ecatsetup --help`,
[Command line](docs/ecatsetup.md#command-line)).

In a HAL file, the component replaces `lcec_conf` and `lcec`
([The ecat component](docs/component.md#using-the-component)):

```
loadrt ecat config=ethercat.yaml
addf ecat.read-all  servo-thread
addf ecat.write-all servo-thread
```

EtherCAT Setup writes these lines itself.

## Background

`ecat` grows out of `lcec_uspace`, a build of
[linuxcnc-ethercat](https://github.com/linuxcnc-ethercat/linuxcnc-ethercat)
against a userspace build of the IgH EtherCAT master
([MetalMusings/ethercat](https://github.com/MetalMusings/ethercat), based on
[stratuMAK/ethercat](https://github.com/stratuMAK/ethercat)). `lcec_uspace`
is frozen; it stays in
[MetalMusings/linuxcnc-ethercat](https://github.com/MetalMusings/linuxcnc-ethercat)
(see its `INSTALL-USPACE.md`), with master packages up to 1.0, and is not
needed for ecat. Unlike linuxcnc-ethercat, `ecat` will not
read lcec's XML files; they can be imported instead. ecat is a project
of its own: it keeps no backward compatibility with linuxcnc-ethercat
and does not take in its future changes. The userspace master is
developed actively as part of the project.

## License

GPL version 2, as linuxcnc-ethercat, from which code will be taken. See
[LICENSE](LICENSE).
