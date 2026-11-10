# Stepper and DC motor terminals

Stepper terminals such as the Beckhoff EL7031, EL7041, EL7041-0052 and
EP7041, and DC motor terminals such as the EL7342, follow ETG.5001. They
need no profile and no C code: EtherCAT Setup proposes them as `generic`
slaves. Each motor channel is set up like this:

| Object | Pin | |
|---|---|---|
| `0x7nn0:01` Enable | `enable` (bit, in) | The output stage is on while true. |
| `0x7nn0:02` Reset | `reset` (bit, in) | A rising edge clears an error. |
| `0x7nn0:03` Reduce torque | `reduce-torque` (bit, in) | The reduced current (`0x8nn0:02`) while true. |
| `0x7nn0:21` Velocity | `velocity` (float, in) | Velocity interface: -1..1 of the speed range. |
| `0x7nn0:11` Position | `position` (s32, in) | Position interface: the target in microsteps. |
| `0x6nn0:01`... Status | `ready-to-enable`, `ready`, `warning`, `error`, `moving-positive`, ... (bit, out) | |

`nn` is the channel: the EL7041's motor is `0x7010`/`0x6010`, its
encoder `0x7000`/`0x6000`. The encoder channel is handled by the
[encoder function](encoder.md) when its counter and latch C are mapped.

The velocity pin has `scale: 32767`: 1.0 is the top of the speed range
set in `0x8nn2:05` (0: 1000, 1: 2000, 2: 4000, 3: 8000, 4: 16000, 5: 32000
full steps per second). The terminal holds the written value within
±32767.

## Settings

The channel settings `0x8nn0` (motor: maximum and reduced current in mA,
nominal voltage, coil resistance, full steps, encoder increments, ...)
and `0x8nn2` (features: operation mode, speed range, feedback,
microsteps, ...) are offered as settings with their dictionary names.
Take the motor's values from its data sheet and the units from the
terminal's documentation.

The operation mode `0x8nn2:01` chooses the interface: the default
(automatic) follows the process data mapped; 3 is the position interface
(EL7031, EL7041-0052 with `0x7nn0:11`).

## A joint with the velocity interface

The terminal runs at a velocity; a PID closes the position loop on the
encoder (or on the terminal's internal microstep counter when
`0x8nn2:08`, feedback, is off):

```
loadrt pid names=pid.x
addf pid.x.do-pid-calcs servo-thread

setp x-stm.enc.pos-scale 800          # counts per mm
setp pid.x.Pgain 50
setp pid.x.FF1 0.01                   # 1 / (mm/s at velocity 1.0)
setp pid.x.maxoutput 1.0

net x-pos-cmd  joint.0.motor-pos-cmd => pid.x.command
net x-pos-fb   x-stm.enc.pos         => pid.x.feedback joint.0.motor-pos-fb
net x-vel      pid.x.output          => x-stm.velocity
net x-enable   joint.0.amp-enable-out => pid.x.enable x-stm.enable
net x-fault    x-stm.error           => joint.0.amp-fault-in
```

`FF1` is the inverse of the velocity at 1.0: with a 4000 full steps/s
range, a 200-step motor and a 5 mm lead, 1.0 is 100 mm/s, so `FF1` is
0.01.

## A joint with the position interface

The position pin is a raw `s32` in microsteps. In the file, make it a
float in machine units:

```yaml
    - {object: '0x7010:11', bits: 32, type: DINT, pin: position, hal-type: float, scale: 6400.0}
```

(`scale` is microsteps per mm), and connect it:

```
net x-pos-cmd joint.0.motor-pos-cmd => x-stm.position joint.0.motor-pos-fb
net x-enable  joint.0.amp-enable-out => x-stm.enable
```

The terminal reports no position back in this interface, so the
command is the feedback, as with lcec's el70x1 driver.

## From lcec

Importing an lcec XML file translates the stepper drivers' `modParam`s
into settings:

| lcec type | modParams |
|---|---|
| `EL7041`, `EL7041-1000`, `EP7041` | `maxCurrent`, `reducedCurrent` (A, written as mA), `nominalVoltage`, `coilResistance`, `motorEMF` (×1000, as lcec wrote them), `motorFullsteps`, `encoderIncrements`, `startVelocity`, `driveOnDelay`, `driveOffDelay`, `maxSpeed` (1000..32000 → 0..5), `encoder` (0x8012:08), `microsteps` (1..64 → 0..6) |
| `EL7031`, `EL7041-0052` | `maxCurrent`, `redCurrent`, `nomVoltage`, `coilRes`, `motorEMF` (as given), and `0x8012:01 = 3` (position interface), which the driver always set |

Others, such as `kp` and `ki`, get a note. lcec's EL7041 driver had no
`break` after `motorEMF` and the cases after it, so a `motorEMF` there was
also written into `motorFullsteps`, `encoderIncrements` and the following
objects; the import writes each value only into its own object. The pins
differ from lcec's (`enable` instead of `srv-enable`, and so on): the
HAL files must be changed.

## Not yet

- Tried with unit tests only, not on a terminal.
- No auto fault reset: lcec's EL7041 driver pulsed `reset` on a new
  `enable` after an error. Connect a reset button, or `enable` through a
  `oneshot`, to `reset`.
- Writing a joint to a stepper terminal is not in *HAL & LinuxCNC* yet;
  connect it in your HAL files as above.
