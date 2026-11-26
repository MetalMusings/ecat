"""The start page's tasks: what people come to EtherCAT Setup to do, with
the steps in order and where each is done. A step's target is a key the
window maps to a page or an action (gui.MainWindow.go), or "help:<topic>"
for the Help tab at a helpdocs.TOPICS key."""

# (title, when, [(step, target or None, button text)])
TASKS = [
    ("A new machine, from a simulated configuration",
     "You copied a LinuxCNC sim configuration into configs/ and want its axes on real drives.",
     [("Read the bus: the slaves and what they can do.", "scan", "Rescan the bus"),
      ("Check the slaves' names, modes and pins.", "bus", "Bus"),
      ("Choose the INI file, connect each joint to a drive, and write: the simulation is "
       "replaced, the HAL files checked.", "hal", "HAL && LinuxCNC"),
      ("Try the I/O and a drive's first moves without LinuxCNC.", "test", "Workbench")]),
    ("Moving an lcec configuration to ecat",
     "The machine runs lcec (lcec_conf and an XML file) and is to run on ecat instead. Work in a "
     "copy of its configuration folder: the original is the way back.",
     [("Read the bus: the slaves get ecat's proposal, the drives the cia402 profile.", "scan",
       "Rescan the bus"),
      ("Choose the machine's INI file (in the copy).", "ini", "Machine INI file..."),
      ("On the From lcec card, choose an ecat pin for every old pin (Use the suggestions), and "
       "add the lcec settings the configuration lacks.", "hal", "HAL && LinuxCNC"),
      ("Write: lcec's lines and the cia402 component are commented out, the new pins added.",
       "hal", "HAL && LinuxCNC"),
      ("Try the I/O and the drives on the workbench before LinuxCNC.", "test", "Workbench")]),
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
      ("Run it on the workbench.", "test", "Workbench"),
      ("Watch the synchronisation and the slaves' state.", "live", "Live"),
      ("Keep what you chose as a quirk file for the device.", "bus", "Bus"),
      ("If it does not work, save a capture of the bus for the developer: the device as the "
       "program sees it.", "status", "Diagnose")]),
    ("Using a slave by hand",
     "Reading and setting I/O, moving a drive, trying homing, with no LinuxCNC.",
     [("Read the bus.", "scan", "Rescan the bus"),
      ("Start the workbench, pick the slave, set outputs or move a drive in small steps.", "test",
       "Workbench")]),
    ("Homing by the drive",
     "A joint that LinuxCNC homes with its own search and latch is to use the drive's homing "
     "method instead. Work in a copy of the configuration folder.",
     [("On the drive's page, tick homing by the drive's own method, choose the method and the "
       "speeds on the homing card, and save.", "bus", "Bus"),
      ("Start the workbench: check the switches on the Switches card, then hold Home (hold) on "
       "the Move card, with the emergency stop within reach.", "test", "Workbench"),
      ("Write into LinuxCNC; put the homing card's INI lines into the joint's section and its "
       "HAL lines (the guard) into your own HAL file.", "hal", "HAL && LinuxCNC"),
      ("Home in LinuxCNC and measure HOME_OFFSET: the guide's step 7.", "help:guide:homing",
       "The guide")]),
    ("Tuning a servo drive",
     "A CSP drive lags or overshoots, or you want FERROR right: measure what its following "
     "error is made of and change one thing at a time, with no LinuxCNC.",
     [("Open the machine's ethercat.yaml.", "open", "Open..."),
      ("Have the drive's own following error (0x60F4) and torque (0x6077) in its process data "
       "(the Response card can add them).", "bus", "Bus"),
      ("Start the workbench, choose the drive on the Move card and write its limits; the machine "
       "must be free to move and someone at it.", "test", "Workbench"),
      ("Measure on the Response card and read what it points at; try one setting at a time "
       "and keep what helps.", "test", "Workbench"),
      ("For a drive with 0x60B1 / 0x60B2: try LinuxCNC's feedforward on the same card and "
       "keep it in the configuration.", "test", "Workbench"),
      ("Save, write into LinuxCNC, and set FERROR from the card's last line.", "hal",
       "HAL && LinuxCNC")]),
    ("Checking the computer's timing",
     "Before trusting a computer with a machine, or when cycles come late: measure how late the "
     "real-time threads start, see what comes with the late ones, and change one thing at a time.",
     [("Choose the machine's INI file (its threads and I/O).", "ini", "Machine INI file..."),
      ("Read the computer and measure twice the same way: beside LinuxCNC while the machine is "
       "used, or the threads alone with load.", "timing", "Timing"),
      ("Read the findings; in Improve mode try one run-time change, measure again, keep or undo "
       "it. Kernel parameters and the BIOS are for you to do.", "timing", "Timing"),
      ("Save the report: what you saw, did and got.", "timing", "Timing")]),
    ("When something goes wrong in a running machine",
     "Something unexpected happens and a slave may be the cause.",
     [("A red or amber dot on the Diagnose tab: open it; what is new is in bold in the event "
       "log, and its tooltip names the first.", "status", "Diagnose"),
      ("Watch the pins live without touching anything.", "watch", "Watch LinuxCNC"),
      ("Read the slaves' states and error counters, the event log and the symptom guide.",
       "status", "Diagnose"),
      ("Save a report to attach when asking for help.", "status", "Diagnose")]),
]
