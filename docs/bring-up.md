# Bringing up a new slave

Getting a slave to work with the master: its process data, its clock
and synchronisation, and its settings. EtherCAT Setup proposes all of
it from what it can read; this page is about seeing what the proposal
rests on, checking it on the bus, and keeping what you chose.

## Where the proposal comes from

The slave's page on the *Bus* page has a card *Where the configuration
comes from*, one line per source, marked ✓ (used), ✕ (missing or not
matching) or – (not used):

| Source | What it gives |
|---|---|
| The slave's EEPROM (SII) | Identity, PDOs, sync managers; always read. |
| The CoE dictionary | The names and types of settings, the drive's supported modes. Without it names come from the SII. |
| An ESI file | Predefined PDO sets (*Propose again* offers them), DC modes, units and value names of settings. The line says which file matched and whether it is for the slave's revision; a file for another revision is used, with a warning to check what it gave. |
| Process data, DC, profile | Which of the above each came from. |
| A quirk file | When one matches the device ([quirks.md](quirks.md)). |

## ESI files

For now you provide the ESI file: the vendor's XML, from its web site
or the device's CD. Without one EtherCAT Setup works from the SII and
the CoE dictionary, and says what is missing. *Choose ESI files...*
(or `--esi FOLDER` on the command line) reads every XML file in a
folder. A folder chosen in the window is remembered, in
`~/.config/ecatsetup/settings.yaml` (or `$ECATSETUP_SETTINGS`), and read
again at the next start; a folder that no longer exists is skipped.

## Synchronisation, in the test mode

While a test runs (*Test* page), the *Synchronisation* card shows, from
the master's pins (`ecat.0.*`):

- whether all slaves are in OP;
- distributed clocks: how many slaves use them, and whether the
  master's DC sync check failed for one (`dc-sync-failed`);
- with `servo-thread-follows-ref-clock`, whether the servo thread is
  locked to the reference clock (`dc-phased`, `pll-err`);
- where SYNC0 is (`shift-time`) and the turn-around (`turnaround-cycles`):
  2 is what drive profiles count on; with `sync0-shift-ns: auto` it says
  so while it still measures (2 s after all OP), and warns at 3 or more;
- how much the moment the frame is sent varies (`send-offset`, the last
  50 samples): more than a quarter of the period points at the servo
  thread's latency.

A slave without DC has no SYNC0 to place; the card then says only
whether the slaves are in OP.

## Keeping what you chose

*Save as quirk file...* on the slave's page saves its pin names, scales
and settings as a quirk file of your own, for the device's vendor and
product. The next slave of the same device is proposed with them, on
this machine or, with the file shared, on another ([quirks.md](quirks.md)).
