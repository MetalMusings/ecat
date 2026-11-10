"""The symptom guide of the Troubleshoot page: what users see in LinuxCNC,
what it usually comes from, and where in EtherCAT Setup to look. Learnt
from real machines; the causes are the common ones, not the only ones."""

# (symptom, usually, where to look)
SYMPTOMS = [
    ("A following error as soon as a drive is enabled",
     "The drive keeps its position from the last run and LinuxCNC starts at 0.",
     "Slave page, Start position: Position counts from 0 at start (cia402.zero-at-start). "
     "Do not write a drive's own Zero Position command."),
    ("A drive switches itself off, or will not enable",
     "The profile shut it down: the heartbeat (watchdog) stopped, the enable timed out, or the "
     "process data was not valid.",
     "The slave's fault-reason pin and drive fault events (Event log); the Test page shows the "
     "CiA 402 state and statusword; its error code (0x603F) is in the diagnosis."),
    ("Slaves drop out for a moment (working counter incomplete)",
     "A cable or connector, or a slave losing power; the master does not see the frames come "
     "back.",
     "Event log: working counter, slave states, link; Status page: the port error counters "
     "(Rx errors and lost links point at the cable between the two slaves)."),
    ("The cycle jitters, or the servo thread reports late cycles",
     "The turn-around is not steady (3 cycles now and then), or the servo thread is delayed by "
     "other work.",
     "Test page, Synchronisation: turnaround-cycles should be 2 and the send-offset spread "
     "small; sync0-shift-ns: auto places SYNC0 for it. Event log: servo thread late. The "
     "real-time computer's settings (isolated CPUs, latency) matter most."),
    ("A slave stays in PREOP or SAFEOP",
     "The slave refuses the next state: the AL status code says why (a PDO mapping or sync "
     "manager it does not accept, DC settings it does not match).",
     "Status page: the slave's AL status code and the steps of the state machine; Bus page: "
     "the slave's process data and Sync settings against its ESI file."),
    ("A slave stops with an error when LinuxCNC ends",
     "It lost the process data while in OP. The component takes the slaves to PREOP first; "
     "this goes wrong when the servo thread stops early or LinuxCNC is killed.",
     "Event log: the stop guard (did the slaves reach PREOP?)."),
    ("A pin does nothing in LinuxCNC",
     "Nothing writes its signal, it is renamed, or two signals fight over it.",
     "HAL page, Check: the HAL files against the configuration; or watch the pin on the Test "
     "page while LinuxCNC runs."),
    ("LinuxCNC will not start ecat, or says the slaves do not match",
     "The start check found other slaves on the bus than ethercat.yaml describes.",
     "Bus page, Update from the bus: what moved, is new, or is gone, and what depends on it."),
]


def html():
    """The guide as rich text."""
    out = []
    for symptom, usually, look in SYMPTOMS:
        out.append("<p><b>%s</b><br>Usually: %s<br>Look at: %s</p>" % (symptom, usually, look))
    return "".join(out)
