# Encoder channels

Status: first version, 2026-11-08. It has run an EL5101 with a 3000-line
(12000 counts per turn) encoder, 32-bit counter: counting both ways,
velocity, and the index found twice 12000 counts apart.

An incremental encoder channel whose objects are laid out as in ETG.5001
MDP 511 gets position, velocity, index and latch pins from the `ecat`
component, like LinuxCNC's `encoder` component. This is not tied to one
terminal: Beckhoff uses the layout in the EL5101, EL5151, EL5152, EL7041,
EL7047, EL7342, EP5101 and others (about 40 families in Beckhoff's ESI
files), and a slave can have several channels, also next to a stepper or
servo channel.

A channel has its inputs at a status index S (`0x6000 + 0x10 × n`) and
its outputs at S + `0x1000`. The channel handles these entries, which are
`use: profile` in the file:

| Object | | |
|---|---|---|
| S:01 | input, 1 bit | latch C valid |
| S:02 | input, 1 bit | latch extern valid |
| S:03 | input, 1 bit | set counter done |
| S:11 | input, 16 or 32 bits | counter value; needed |
| S:12 | input, as S:11 | latch value |
| S+0x1000:01 | output, 1 bit | enable latch C |
| S+0x1000:02 | output, 1 bit | enable latch extern on positive edge |
| S+0x1000:03 | output, 1 bit | set counter |
| S+0x1000:04 | output, 1 bit | enable latch extern on negative edge |
| S+0x1000:11 | output, as S:11 | set counter value |

The channel's other entries, such as the states of inputs A, B and C,
open circuit or frequency, keep ordinary pins.

## In the configuration file

```yaml
- position: 3
  name: io-3
  profile: mdp
  encoders:
  - name: enc
    index: '0x6010'
  process-data:
    outputs:
    - pdo: '0x1603'
      sm: 2
      entries:
      - {object: '0x7010:01', bits: 1, type: BOOL, use: profile}
      ...
      - {object: '0x7010:11', bits: 32, type: UDINT, use: profile}
    inputs:
    - pdo: '0x1A04'
      sm: 3
      entries:
      - {object: '0x6010:01', bits: 1, type: BOOL, use: profile}
      ...
      - {object: '0x6010:09', bits: 1, type: BOOL, pin: status-of-input-a, hal-type: bit}
      ...
      - {object: '0x6010:11', bits: 32, type: UDINT, use: profile}
      - {object: '0x6010:12', bits: 32, type: UDINT, use: profile}
```

EtherCAT Setup finds the channels, a "Counter value" at S:11 with
"Latch C valid" at S:01, and proposes them this way, named `enc`, or
`enc-1`, `enc-2`, ... when a slave has several. The EL5101's default PDO
assignment has a 16-bit counter; `--pdo-set "3=Standard 32 Bit (MDP 511)"`
gives the 32-bit one. Both work, since the count is extended (below).

## Pins

Pins are `<slave name>.<channel name>.<pin>`.

| Pin | Type | Dir | |
|---|---|---|---|
| `pos` | float | out | Position: counts / `pos-scale`, from the index or reset. |
| `velocity` | float | out | Velocity in units per second, over `velocity-window-ms`. |
| `counts` | s32 | out | Counts from the index or reset. |
| `raw-count` | s32 | out | The terminal's counter as read. |
| `index-enable` | bit | I/O | Set it to find the index: at the index pulse, `pos` becomes 0 and the pin is cleared. Connect it to `joint.N.index-enable` or `spindle.N.index-enable`. Only there when latch C is mapped. |
| `reset` | bit | in | While true, `pos` is 0 at the current count. |
| `latch-enable-pos`, `latch-enable-neg` | bit | I/O | Arm the external latch input for a rising or falling edge; cleared when it latches. Only there when the external latch is mapped. |
| `latch-pos` | float | out | The position at the latched edge. |
| `ok` | bit | out | The process data is valid (working counter, slave in OP). |

Parameters:

| Parameter | Default | |
|---|---|---|
| `pos-scale` | 1.0 | Counts per unit. |
| `velocity-window-ms` | 10 | Time over which `velocity` is measured, at most 256 cycles. Its resolution is one count per window: 100 counts/s at 10 ms. |

## Behaviour

- The counter, 16 or 32 bits, is extended to 64 bits, so `pos` does not
  wrap. The counter must move less than half its range in one cycle
  (32767 counts for 16 bits).
- `pos` is 0 when the component starts.
- The terminal's counter is never set; `index-enable` and `reset` move a
  reference instead. So `pos` jumps only at the index or a reset, never
  through a counter write that lands a cycle late.
- An index or latch is taken only after its "valid" bit was seen false
  since the pin was set, so an old latch is not taken for a new one.
- When the process data is not valid, `ok` is false, `pos` holds and
  `velocity` is 0. When it is valid again, counting goes on from there;
  if the terminal lost power meanwhile, the position is off and the axis
  must be homed again.

## From lcec

lcec's EL5101 driver had pins such as `enc-pos`, `enc-index-c-enable` and
`enc-pos-scale` (a pin there, a parameter here):

| lcec | ecat |
|---|---|
| `lcec.0.X.enc-pos` | `X.enc.pos` |
| `lcec.0.X.enc-count` | `X.enc.counts` |
| `lcec.0.X.enc-raw-count` | `X.enc.raw-count` |
| `lcec.0.X.enc-index-c-enable` | `X.enc.index-enable` |
| `lcec.0.X.enc-reset` | `X.enc.reset` |
| `lcec.0.X.enc-pos-scale` | `X.enc.pos-scale` (parameter: `setp`) |
| `lcec.0.X.enc-index-ext-pos-enable` | `X.enc.latch-enable-pos` (sets `latch-pos`, does not reset `pos`) |

lcec's EL5101 driver mapped the old (non-MDP) objects at `0x6000`/`0x7000`;
those are not handled here.
