# Fault tests at the machine

The event log and the report are tried so far on a bus that behaves. This
is the plan for provoking real faults and checking that the recorder
catches them, that the diagnosis and the symptom guide name them, and that
Watch shows them live. Not done yet: each row is filled in when tried.

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

(Empty until tried. Date, row, what the log showed, what was wrong in the
texts or the guide, and the fix.)
