# Fault tests at the machine

The event log and the report are tried so far on a bus that behaves. This
is the plan for provoking real faults and checking that the recorder
catches them, that the diagnosis and the symptom guide name them, and that
Watch shows them live. Results are at the end.

## Before

- LinuxCNC runs with `ecat` (the machine can stay switched off, no motion
  is needed for most rows). The kernel master is stopped.
- In a terminal: `scripts/ecat-events-follow`. It prints each event as the
  component records it.
- In the window: *Troubleshoot* page, *Watch LinuxCNC (read only)*.
- Note the time of each action; the log is to the millisecond.
- After each row: *Save report...*, and look at the diagnosis and at which
  symptom in the guide fits. Restart LinuxCNC between rows when a row says
  so (each start drops the older of the two logs, so save first).

## Rows

| # | Provoke | Expect in the log | Expect elsewhere |
|---|---|---|---|
| 1 | Pull the cable between two slaves, put it back | `slaves respond` fewer, `working counter incomplete`, `slave N OFFLINE`; then back: online, operational | Watch: slave state; diagnosis: lost-link counter on the port; symptom "a slave drops out" |
| 2 | Pull the cable at the master (the card) | `link DOWN`, working counter zero | Symptom for link down |
| 3 | Switch off the last slave's power | `slave N OFFLINE`, working counter incomplete | same as row 1 |
| 4 | Stop the servo thread (`halcmd stop`, or pause it) | `servo thread late`, then `stop guard` taking the slaves to PREOP, `done` | slaves in PREOP, outputs zero |
| 5 | Start the servo thread again (`halcmd start`) | `stop guard: the servo thread runs the bus again`, slaves back to OP | backlog items 4 and 5 (DC slaves stay in SAFEOP?) are watched here |
| 6 | Force a drive fault (drive over-travel input, or a wrong parameter that trips it) | `drive fault <text> (status word ...)`, then `the profile shut the drive down` | Watch: CiA 402 state Fault; symptom "a drive switches itself off" |
| 7 | Clear the fault (fault-reset) | `drive fault cleared` | |
| 8 | Hold the machine's enable and unplug the bus while the drive is enabled | `drive left Operation enabled` or no valid process data as `fault-reason` | the drive's own state afterwards |
| 9 | Run with a heavy load on the CPU (a build, `stress`) | `servo thread late` lines (at most one a second) | whether the report's diagnosis points at latency |

Rows 1-3, 6 and 7 are the ones a user can meet in practice; 4 and 5 test
the guard; 8 and 9 are stress.

## What to check in each row

1. The event appears within the cycle or the second it is recorded for
   (states are read once a second, the working counter every cycle).
2. The text says what to look at (cable, power, drive) without the reader
   knowing EtherCAT.
3. The diagnosis in the saved report agrees with the log.
4. The symptom guide has an entry that fits, and Watch shows the same live.
5. No event is missing or false (for example a working counter error at a
   normal start).

## Results

2026-11-10, plasma cutter, LinuxCNC (QtPlasmaC, 1 ms servo thread), 3 drives
and the EaserCAT, machine switched off. Rows 6-8 need a real drive fault or
a drive that is enabled and were not tried.

| # | Tried | What the log showed |
|---|---|---|
| 1 | Cable between slave 0 port 1 and slave 1 pulled and put back | Working counter incomplete, "1 slaves respond (was 4)", working counter zero. Back: 4 respond, all in OP 10 s later. |
| 2 | Cable at the master | Working counter zero, "0 slaves respond", all OFFLINE, and a second later `link DOWN`. Back: `link up`, all in SAFEOP, in OP 10 s later. |
| 3 | Slave 2 powered off and on | "2 slaves respond (was 4)", correct. Back: 4 respond, in OP 10 s later. |
| 4, 5 | `halcmd stop`, 6 s, `halcmd start` | `servo thread late`, `stop guard` to PREOP and done (60 ms), then `runs the bus again`, all in OP 1 s later. The DC slaves did not stay in SAFEOP. |
| 9 | Busy loops on every CPU for 25 s | No events. The servo thread's maximum time did not grow. |

In every row the drives stayed in "Ready to switch on" and LinuxCNC kept
running: the bus recovers by itself, without a restart.

### Found, and what was done

- Fixed: the diagnosis said "No problems found" with `lost 1` at both ends
  of the pulled cable. It now adds a note that names the cable when two
  ports at the ends of one cable count the same number of lost links
  (`lost_link_notes`). It is a note, not a problem: the counters are not
  cleared and the master's counts since power-on stay.
- Fixed: `scripts/ecat-events-follow` printed "a new run started" in the
  middle of a run.
- Not fixed: when the topology changes, the master reports every slave
  as not online for a moment: rows 1 and 3 list slaves that were still on
  the bus (slave 0 and 1 in row 3) as OFFLINE, and again when the
  slave behind them returns. "N slaves respond" is right. Read the count of
  responding slaves first; the cause is in how the master reports the
  slave configurations, so the component needs a closer look.
- Not fixed: a `working counter zero` of a millisecond, logged as ERROR,
  comes while the bus climbs back to OP (after rows 1-3 and 5). It is
  harmless, but the log cannot tell it from a real one.
- The port counters of the master side and of a slave that lost power
  start again from zero, so those show nothing: the event log is the
  record for them.
