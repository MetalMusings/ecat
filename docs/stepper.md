# Stepper and DC motor terminals

Stepper terminals such as the Beckhoff EL7031, EL7041, EL7041-0052 and
EP7041, and DC motor terminals such as the EL7342, are handled as
ordinary `generic` slaves: no profile and no code of their own. EtherCAT
Setup proposes a pin for each object, named after it (`enable`, `reset`,
`reduce-torque`, `velocity`, `ready-to-enable`, `error`, ...).

What is special about a product goes into a [quirk file](quirks.md).
The EL7041 has one, `beckhoff-el7041.yaml`, which gives:

| Object | Pin | |
|---|---|---|
| `0x7010:01` Enable | `enable` (bit, in) | The output stage is on while true. |
| `0x7010:02` Reset | `reset` (bit, in) | A rising edge clears an error. |
| `0x7010:03` Reduce torque | `reduce-torque` (bit, in) | The reduced current (`0x8010:02`) while true. |
| `0x7010:21` Velocity | `velocity` (float, in) | Velocity interface: -1..1 of the speed range. |
| `0x6010:04` Error | `error` (bit, out) | |

and HAL lines that reset an error when `enable` comes on again (below).
For another terminal, copy that file, change `match` and the objects, and
put it in `~/.config/ecatsetup/devices/`. Without a quirk file, the
velocity is an `s32` pin in raw values (±32767).

`nn` is the channel: the EL7041's motor is `0x7010`/`0x6010`, its
encoder `0x7000`/`0x6000`. The encoder channel is handled by the
[encoder function](encoder.md) when its counter and latch C are mapped.

With the quirk file, the velocity pin has `scale: 32767`: 1.0 is the top of the speed range
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

## Resetting an error

lcec's EL7041 driver pulsed `reset` when `enable` came on again after an
error. The quirk file does the same in HAL: when the configuration is
written into LinuxCNC, `ethercat.hal` gets, for a terminal `x-stm`,

```
loadrt and2 names=x-stm-auto-reset
loadrt edge names=x-stm-enable-edge
addf x-stm-enable-edge servo-thread
addf x-stm-auto-reset servo-thread
...
setp x-stm-enable-edge.in-edge FALSE
setp x-stm-enable-edge.out-width-ns 10000000
net x-enable => x-stm-enable-edge.in
net x-stm-enable-rise x-stm-enable-edge.out => x-stm-auto-reset.in0
net x-stm-error x-stm.error
net x-stm-error => x-stm-auto-reset.in1
net x-stm-reset x-stm-auto-reset.out => x-stm.reset
```

The lines are left out, with a note, when nothing drives `enable` yet,
when your HAL files connect `reset` themselves, or when they load `edge`
or `and2` (HAL loads a component once).

## Not yet

- Tried with unit tests only, not on a terminal.
- Writing a joint to a stepper terminal is not in *HAL & LinuxCNC*;
  connect it in your HAL files as above.
