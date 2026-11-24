# ecat

EtherCAT for LinuxCNC, configured without hand-written XML.

**Status: early development, tried on two machines.** The HAL component
runs a plasma cutter (three CiA 402 drives in CSP, one I/O slave; PP and
PV have been tried on its drives too) and a lathe's bus (two drives and a
spindle encoder: homing, jogging, cable pull and E-stop). EtherCAT Setup reads, proposes,
checks, updates and diagnoses a configuration in its window or from the
command line, tests it (I/O, SDOs, moving a drive in small steps, homing),
writes it into a LinuxCNC configuration, and watches a running LinuxCNC
read only. Beckhoff's serial terminals (EL6001, EL6021, ...) give serial
ports on the computer, for `mb2hal` and Modbus VFDs: tried over a real
wire against a slave of our own that behaves as an EL6021, not yet on a
real terminal. A slave that speaks Ethernet over EtherCAT (EoE) gets an IP
address, so that its web page opens in a browser while LinuxCNC runs. Everything is built
in steps, following the design draft.

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
| [EtherCAT Setup](docs/ecatsetup.md) | The window, bringing up a new slave (with how-tos for a spindle encoder, a Modbus VFD on a serial terminal and a device's web page over EoE), the test mode (with [tuning a servo drive, step by step](docs/ecatsetup.md#tuning-a-servo-drive-step-by-step) and a test of home and limit switches), writing into LinuxCNC (with [moving a joint from LinuxCNC's homing to the drive's](docs/ecatsetup.md#from-linuxcncs-homing-to-the-drives)), [what the slaves give in LinuxCNC](docs/ecatsetup.md#what-the-slaves-give-in-linuxcnc) (the component, how short a servo period can be, the pins and behaviour of drives, encoder and serial channels, analog and stepper terminals and modular devices), the computer's timing, the event log and report, and quirk files. |
| [Reference](docs/reference.md) | The `ethercat.yaml` file format (which you do not need to edit: the window writes it), loading the component by hand, every command-line option, and the format of quirk files. |
| [Developing ecat](docs/development.md) | Building and testing, the design draft, what the prototype found on a real bus, the plan for fault tests at a machine, how to get at devices we do not have, and the design of the real-time check. |

## Install

For Debian 12 and 13 with **LinuxCNC 2.9** (2.9.4 or newer, as packages) or
**LinuxCNC 2.10**. The same ecat is built for both. 2.10 is not released yet
and has no Debian packages, so most people run it run-in-place (built from
source, see [Developing ecat](docs/development.md#linuxcnc-210)): ecat then
comes as an archive with an installer, tested against one LinuxCNC commit
(named in the release notes). On 2.9 the integer pins have 32 bits instead of
64 ([the file format](docs/reference.md#pdos-and-entries)) and HAL names take 47 characters instead of
55 (`ecatsetup --check` names a pin that is too long); otherwise ecat does the
same on both.

1. the userspace EtherCAT master, `linuxcnc-ethercat-uspace`, from the
   `uspace-v*` releases of
   [MetalMusings/ethercat](https://github.com/MetalMusings/ethercat/releases);
2. ecat from this repository's `ecat-v*`
   [releases](https://github.com/MetalMusings/ecat/releases):
   - LinuxCNC 2.9 (Debian's or linuxcnc.org's packages):
     `linuxcnc-ecat_*+lcnc2.9~debian12|13_amd64.deb`;
   - LinuxCNC 2.10 run-in-place: `ecat-rip-*-debian12|13.tar.gz` (unpack,
     `./install`);
   - LinuxCNC 2.10 as a package: `linuxcnc-ecat_*.deb` without `+lcnc2.9`.

```
sudo apt install ./linuxcnc-ethercat-uspace_*_amd64.deb
sudo apt install ./linuxcnc-ecat_*+lcnc2.9~debian13_amd64.deb       # LinuxCNC 2.9
tar xzf ecat-rip-*-debian13.tar.gz && cd ecat-rip-*-debian13 && ./install   # 2.10 run-in-place
```

[Getting started](docs/getting-started.md) says what gets installed and how
to try it, and to undo it. To build it yourself: `scripts/build-deb.sh` (the
package) or `scripts/build-rip-archive` (the archive),
[Developing ecat](docs/development.md#build-and-test).

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
[Command line](docs/reference.md#command-line)).

In a HAL file, the component replaces `lcec_conf` and `lcec`
([Loading the component by hand](docs/reference.md#loading-the-component-by-hand)):

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
read lcec's XML files; they can be imported instead, and EtherCAT Setup
moves a whole lcec configuration, its HAL files too, to ecat
([from an lcec configuration](docs/ecatsetup.md#from-an-lcec-configuration)). ecat is a project
of its own: it keeps no backward compatibility with linuxcnc-ethercat
and does not take in its future changes. The userspace master is
developed actively as part of the project.

## License

GPL version 2, as linuxcnc-ethercat, from which code will be taken. See
[LICENSE](LICENSE).
