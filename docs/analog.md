# Analog inputs and outputs

Analog terminals that follow ETG.5001 (Beckhoff EL3xxx inputs, EL4xxx
outputs, and others laid out the same way) need no profile and no C code.
EtherCAT Setup proposes them as `generic` slaves whose values are float
pins in the terminal's unit:

```yaml
  - pdo: '0x1A00'
    sm: 3
    entries:
    - {object: '0x6000:01', bits: 1, type: BOOL, pin: underrange, hal-type: bit}
    - {object: '0x6000:07', bits: 1, type: BOOL, pin: error, hal-type: bit}
    - {object: '0x6000:11', bits: 16, type: INT, pin: value, hal-type: float, scale: 0.00030518509475997192}
```

## The range

The value of a channel (`0x6nn0:11` for inputs, `0x7nn0:11` for outputs,
16 bits) has its full scale `0x7FFF` at the top of the measuring range.
The range is taken from the device's name, as the slave or its ESI file
gives it: `+/-10V`, `±75 mV`, `0-10V`, `4..20 mA`. Then:

| Range | Raw 0 | Raw 0x7FFF | `scale` (input) | `offset` (input) |
|---|---|---|---|---|
| ±10 V | 0 V | 10 V | 10/32767 | 0 |
| 0..10 V | 0 V | 10 V | 10/32767 | 0 |
| 4..20 mA | 4 mA | 20 mA | 16/32767 | 4 |

An input pin is `raw * scale + offset`. An output is written as
`(pin + offset) * scale`, so an output's `scale` is the inverse
(32767/10 for 0..10 V) and its `offset` the negative low end (-4 for
4..20 mA). The program writes both.

A terminal whose name has no range (temperature terminals, for example,
which report 0.1 °C per count) keeps an `s32` pin in raw counts, with a
note. Change `hal-type` to `float` and set `scale` by hand, or leave it.

## Status and settings

The status bits (`underrange`, `overrange`, `limit-1`, `limit-2`,
`error`, ...) are bit pins under their own names. Connect `error` to
whatever must stop when the signal is lost, such as the THC's enable.

The channel settings `0x8nn0` (filter, user scaling, presentation) are
offered as settings rows with their dictionary names. A terminal with
user scaling enabled (`0x8nn0:01`) reports scaled raw values; the pin's
`scale` must then be set to match.

## Not yet

- Tried with unit tests only, not on a terminal.
- Ranges come from the name. A quirk entry per product
  (`ecatsetup/quirks.py`) can give one where the name does not.
