# Getting started

ecat is experimental. This is for trying it on a machine that already
works, with LinuxCNC configurations and perhaps another EtherCAT master
that you want to keep. Everything below can be undone.

Where to go next: [EtherCAT Setup](ecatsetup.md) for the program (its
window, the test mode, writing into LinuxCNC, troubleshooting) and
[The ecat component](component.md) for the configuration file and what
each kind of slave gives you in HAL.

## What gets installed, and what does not change

| Package | Files | |
|---|---|---|
| `linuxcnc-ethercat-uspace` | `/opt/linuxcnc-ethercat-uspace/` (the master, its tool, headers), `/usr/bin/ethercat-uspace` | The userspace master. Not a service: nothing starts by itself. |
| `linuxcnc-ecat` | `/usr/lib/linuxcnc/modules/ecat.so`, `/usr/bin/ecatsetup`, `/usr/bin/ecat-take-down`, the Python package `ecatsetup`, documents in `/usr/share/doc/linuxcnc-ecat/` | The `ecat` HAL component and EtherCAT Setup. |

The packages do **not**:

- touch your LinuxCNC configurations, `ethercat.service`, the kernel
  master, the kernel or the network configuration;
- load or start anything: `ecat` is loaded only by a HAL file that says
  `loadrt ecat`, and the master runs only inside LinuxCNC, in the test
  mode of `ecatsetup`, or when you start it yourself;
- conflict with the kernel master or with `lcec`: the component is called
  `ecat`, and the two masters are separate installs.

One thing to know:

- **Only one master can use a network interface at a time.** The kernel
  master (`sudo systemctl stop ethercat`) must be stopped while ecat's
  master runs on the same interface, and `ecat` refuses to start while
  the kernel master has it. With two network cards, give ecat the one
  your kernel master does not use.

The program keeps a little of its own, all in your home directory:
`~/.config/ecatsetup/` (the ESI folders you gave, your quirk files),
`~/.local/state/ecatsetup/drive-limits.json` (only while a drive test has
written limits to a drive), and `/dev/shm/ecat-events` (the event log,
gone at the next boot).

## Install

For Debian 12 or 13 with LinuxCNC 2.9.4 or newer, download the two
packages from the releases ([README](../README.md#install)) and:

```
sudo apt install ./linuxcnc-ethercat-uspace_*_amd64.deb ./linuxcnc-ecat_*_amd64.deb
```

To remove them again:

```
sudo apt remove linuxcnc-ecat linuxcnc-ethercat-uspace
```

## Try it in steps, from the safest

**1. Without a bus.** Nothing is read from the bus and nothing can move:

```
ecatsetup --gui
ecatsetup --check path/to/ethercat.yaml
ecatsetup --import path/to/ethercat-conf.xml --ini machine.ini --write /tmp/ethercat.yaml
```

The window opens on a *Start* page (a tour of it: [the window](ecatsetup.md#the-window)). `--import` turns an lcec XML file
into an ecat file; `--check` checks a file (see
[file format](component.md#the-configuration-file)). Open the result and look at what
it proposes.

**2. Reading the bus.** Reading needs a master. Stop the kernel master
and start ecat's, on the interface the slaves are on (`ip link`):

```
sudo systemctl stop ethercat
sudo ethercat-uspace start -- -i enp2s0
ethercat-uspace slaves
ecatsetup --gui
```

This only reads. When you are done:

```
sudo ethercat-uspace stop
sudo systemctl start ethercat
```

**3. Writing into a copy of your configuration.** Work on a copy, never
on the configuration that runs your machine:

```
cp -r ~/linuxcnc/configs/mymachine ~/linuxcnc/configs/mymachine-ecat
ecatsetup --gui ~/linuxcnc/configs/mymachine-ecat/ethercat.yaml \
          --ini ~/linuxcnc/configs/mymachine-ecat/mymachine.ini
```

*HAL & LinuxCNC* then shows what would be written (`ethercat.yaml`,
`ethercat-load.hal`, `ethercat.hal`, the INI changes and the HAL lines
that would be commented out) before it writes, and keeps each changed
file as `.bak`. [writing into LinuxCNC](ecatsetup.md#writing-the-configuration-into-linuxcnc) says what is written and how
the check of your HAL files works.

**4. The test mode.** It runs the `ecat` component in `halrun` without
LinuxCNC and shows the slaves' pins live. The kernel master and LinuxCNC
must be stopped, and the free-running master hands the bus over to the
test; afterwards start the kernel master again as above. Inputs and
outputs of I/O slaves can be read and set; a drive does not move until
you hold a button, and the test sets limits in the drive (and puts them
back). Before moving a drive:

- keep the emergency stop within reach and the axis away from its ends;
- set the scale (counts per mm) first;
- check what the drive's homing method (`0x6098`) does before using
  *Home*: only method 35 leaves the axis where it is;
- read [test mode](ecatsetup.md#test-mode).

**5. In LinuxCNC.** Only now run the copy of your configuration. Start
with the machine unable to do harm (no power to the axes, the
spindle or the torch off). *Watch LinuxCNC that runs (read only)* on the
*Test* page shows the pins live without changing anything, and if
something is wrong the *Slave status* page, the event log and *Save
report...* give what to send when you ask for help
([event log](ecatsetup.md#event-log-and-report)).

## Going back

- Your own configuration was never changed: delete the copy.
- Kernel master: `sudo systemctl start ethercat`.
- Packages: `sudo apt remove linuxcnc-ecat linuxcnc-ethercat-uspace`.
- A drive left with a limit from an interrupted drive test: the next
  `ecatsetup` start offers to write the drive's own values back, from
  `~/.local/state/ecatsetup/drive-limits.json`.
- Settings and quirk files: remove `~/.config/ecatsetup/`.

## When something does not work

Most problems are the two masters on one interface, the wrong network
interface in the file, or a network card without XDP (use the transport
`raw`). The
list of the usual symptoms and what they come from is on the
*Troubleshoot* page of the window, and `ecatsetup --diagnose` reports
per slave why it is not in OP.
