# Modular devices

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
