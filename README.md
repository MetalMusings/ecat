# ecat

EtherCAT for LinuxCNC, configured without hand-written XML.

**Status: design stage.** Nothing is built yet. This repository starts
with the design draft; code follows in steps.

`ecat` is planned as:

- **EtherCAT Setup**, an interactive program that shows the slaves on the
  bus, configures them from the slave's own data and its ESI file, helps
  bring up and troubleshoot a slave that does not work, tests it, connects
  it to HAL, and writes the result into the LinuxCNC configuration;
- **`ecat`**, a HAL component that runs the userspace IgH EtherCAT master
  inside LinuxCNC and reads the configuration written by EtherCAT Setup.

Read the design draft:
[docs/design/ethercat-setup.md](docs/design/ethercat-setup.md).

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
