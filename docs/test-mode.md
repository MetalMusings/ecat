# Test mode

EtherCAT Setup's Test page runs the configuration being edited, saved or
not, in `halrun` with the `ecat` component and a servo thread, without
LinuxCNC. It is the same code that runs the machine.

Before starting it, stop LinuxCNC and the kernel master
(`sudo systemctl stop ethercat`). The free-running master
(`ethercat-uspace start`) hands the bus over to the test by itself and
does not come back when the test stops.

The test does not start when the master's network interface is missing,
does not exist on this computer, or is set to `xdp-native` on a driver
that cannot do it (such as `r8169`): the Master page says why. If the
component does not load, the test stops at once with the end of the
`halrun` log.

## I/O

Each slave's inputs show live; the outputs of I/O slaves can be set.
Pins set during the test go back to 0 before it stops. SDOs can be read
and written from the same page whenever a master runs; objects that are
commands rather than settings (such as `0x2010` on the ECT60), objects in
the process data, and a drive's CiA 402 objects are refused.

When the test stops, every drive is disabled, the outputs go to zero, and
the slaves are taken to PREOP while process data still flows
(`ecat.take-down`; the component's stop guard would do it too), so that
they stop without an error.

## Moving a drive

A CiA 402 drive can be moved in small steps: in CSP and PP by jogging, in
PV by running it while a button is held, and a drive with `hm` in its
`modes` can home by its own method (see [PP, PV and homing](#pp-pv-and-homing)).
Safety comes in layers, so that no single one has to be perfect:

1. **Nothing moves without a deliberate act.** The drive is enabled only
   while a button is held, with the mouse or the space bar, one at a
   time: *Hold to enable* enables it without moving it; *Jog −* and
   *Jog +* enable it by themselves and move one step. Releasing the
   button disables the drive; so does the window losing focus.
2. **Heartbeat.** Every drive's `watchdog-ms` is set to 500 ms in the
   test's HAL file, and the window counts the `watchdog` pin up every
   100 ms. If the window hangs or dies, the servo thread shuts the drive
   down by itself (`fault-reason` 5).
3. **Limits in the drive.** *Write limits to the drive* reads the drive's
   own values and writes, where the drive has the objects:
   - software position limits `0x607D` at ± the travel around the
     position at that moment;
   - a tight following-error window: `0x6065`, or the vendor's object
     (on the Rtelligent ECT60, `0x2022` "Position trace error Limit");
   - the maximum torque `0x6072`.

   The drive's own values are kept in
   `~/.local/state/ecatsetup/drive-limits.json` before each limit is
   written, and written back by *Done* or when the test stops; if the
   program dies in between, it offers to write them back at its next
   start.
4. **Limits in HAL.** The drive's `pos-cmd` comes from a `limit3` with the
   test's velocity, acceleration and travel, in the servo thread. Until
   the drive is in "Operation enabled", the `limit3` follows `pos-fb`, so
   enabling never makes a jump; a jog moves its target by one step, never
   past the travel.

Defaults: 5 mm/s, 50 mm/s², ± 20 mm, following error 2 mm, torque 30 %.
The scale (counts per mm) is taken from the configuration's HAL files
(`setp <drive>.pos-scale ...`) when they have it.

What the ECT60 has: `0x607D` and `0x2022`, no `0x6080`, `0x6072` or
`0x6065`. It ignores `0x607D` in CSP (tried: it moved 4.6 mm past a
limit of 1 mm, without an error or a status bit), so the test writes only
`0x2022` to it, and its travel, velocity and torque are limited in HAL.

### PP, PV and homing

In PP and PV the drive plans the motion itself; there is no `limit3` in
front of it. The layers 1 to 3 are the same, and instead of layer 4:

- **PP** (*Jog −*, *Jog +*): each step is a move to the drive's present
  position ± the step, never past the travel around the start;
  `pp-velocity` is the test's maximum velocity, and the maximum profile
  velocity `0x607F` is written to the drive where it has it. The drive
  ramps with its own acceleration (`0x6083`, `0x6084`). The move starts
  once the drive is in "Operation enabled" (the profile ignores
  `pp-start` before that); releasing the button stops it.
- **PV** (*Run −*, *Run +*): the drive runs at the test's maximum
  velocity while the button is held, and ramps down with its own
  deceleration when it is released. There is no travel limit in PV:
  keep the button short, or use PV for what turns (a spindle, a
  conveyor). `0x607F` is written where the drive has it.
- **Homing** (*Home (hold)*, for a drive with `modes: [..., hm]`): the
  drive homes by the method and speeds in its settings (`0x6098`,
  `0x6099`, `0x609A`); the card shows *in progress*, *homed* or *error*.
  Releasing the button stops the homing. Try the method first with the
  drive away from its end stops.

The scale (counts per mm) is also used as `velo-scale`: the drive's
velocity units are taken as counts per second, the CiA 402 default. For
a drive with other velocity units, the mm/s shown are off by that
factor; check with a short run first.

PP, PV and homing are built and unit-tested, not yet tried on a machine.

## First test at the machine

Done on the plasma cutter's three axes (ECT60 drives in CSP) on
2026-11-09, with the user at the machine, following the steps below; X
(100 counts/mm) first:

- limits written (0x607D at ±20 mm, 0x2022 at 2 mm = 200 counts) and put
  back afterwards (−1/−1, 4000);
- *Hold to enable*: "Operation enabled", no motion;
- jog 0.1 mm and 1 mm, both directions, as expected;
- heartbeat: with the window stopped (`kill -STOP`) while enabled, the
  servo thread shut the drive down 0.38-0.44 s later (`fault-reason` 5,
  the last heartbeat up to 0.1 s before), and kept it down while
  `enable` stayed set;
- the test stopped cleanly: the slaves in PREOP, `0x603F` 0 on all three
  drives;
- `0x607D` set to ±1 mm by hand, with the HAL travel at ±20 mm: the
  ECT60 followed the target to +4.6 mm without an error. It ignores
  `0x607D` in CSP; the test no longer writes it to an ECT60.
- Y (66.67 counts/mm) and Z (212.2 counts/mm, travel ±5 mm; Z holds
  when disabled) the same way: *Jog +* moves Y+ and Z+ (up), steps of
  0.1, 1 and 5 mm, no errors; all three drives' own values back
  afterwards, the slaves in PREOP, `0x603F` 0.
- When a drive is disabled, the stepper lets go of its holding torque
  and moves back 4-6 counts (0.06 mm on X); the next jog starts from
  there.

The steps:

1. E-stop within reach; the axis away from its ends.
2. Start the test, choose the drive (X first), check the scale, and
   *Write limits to the drive*. The banner lists what was written.
3. If the drive shows Fault, hold *Reset fault*.
4. Hold *Hold to enable* without jogging: the drive should reach
   "Operation enabled" and not move. Release: it goes back to "Switched
   on" or "Ready to switch on".
5. Step 0.1 mm, hold *Jog +* alone (it enables the drive by itself)
   until the step is done: the position should change by 0.1 mm. Then
   1 mm, both directions.
6. Check the heartbeat: while holding *Hold to enable*, stop the window
   (for example `kill -STOP` of its process from a terminal). The drive
   should be disabled within half a second (`fault-reason` 5). `kill -CONT`
   it again.
7. *Done* writes the drive's own limits back; check them with an SDO
   read of what the banner listed (`0x2022` on the ECT60).
8. The other axes the same way. Stopping the test takes the slaves to
   PREOP; `0x603F` should be 0 on every drive afterwards.
