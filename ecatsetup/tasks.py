"""The start page's tasks: what people come to EtherCAT Setup to do, with
the steps in order and where each is done. A step's target is a key the
window maps to a page or an action (gui.MainWindow.go)."""

# (title, when, [(step, target or None, button text)])
TASKS = [
    ("A new machine, from a simulated configuration",
     "You copied a LinuxCNC sim configuration into configs/ and want its axes on real drives.",
     [("Read the bus: the slaves and what they can do.", "scan", "Rescan the bus"),
      ("Check the slaves' names, modes and pins.", "bus", "Bus"),
      ("Choose the INI file, connect each joint to a drive, and write: the simulation is "
       "replaced, the HAL files checked.", "hal", "HAL && LinuxCNC"),
      ("Try the I/O and a drive's first moves without LinuxCNC.", "test", "Test")]),
    ("Changing a configuration that runs",
     "You edited HAL files, or the bus changed (a slave added, moved or replaced).",
     [("Open the machine's ethercat.yaml.", "open", "Open..."),
      ("See what changed on the bus and what depends on it.", "update", "Update from bus"),
      ("Check your HAL files against the configuration.", "hal", "HAL && LinuxCNC"),
      ("Write the files again; edits by hand are noticed first.", "hal", "HAL && LinuxCNC")]),
    ("Bringing up a new slave",
     "A device you have not used with ecat before.",
     [("Give it ESI files if you have them: they add PDO sets, DC modes and names.", "esi",
       "Choose ESI files..."),
      ("Read the bus, and look at where the proposal comes from.", "scan", "Rescan the bus"),
      ("Check its process data, pins, sync and settings.", "bus", "Bus"),
      ("Run it and watch the synchronisation.", "test", "Test"),
      ("Keep what you chose as a quirk file for the device.", "bus", "Bus")]),
    ("Using a slave by hand",
     "Reading and setting I/O, moving a drive, trying homing, with no LinuxCNC.",
     [("Read the bus.", "scan", "Rescan the bus"),
      ("Start the test, pick the slave, set outputs or move a drive in small steps.", "test",
       "Test")]),
    ("Troubleshooting while LinuxCNC runs",
     "Something unexpected happens and a slave may be the cause.",
     [("Watch the pins live without touching anything.", "watch", "Watch LinuxCNC"),
      ("Read the slaves' states and error counters, the event log and the symptom guide.",
       "status", "Slave status"),
      ("Save a report to attach when asking for help.", "status", "Slave status")]),
]
