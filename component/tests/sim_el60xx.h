//
//    A simulated Beckhoff serial terminal channel (EL6001/EL6021 kind) for
//    the serial function's tests, built from the ESI file (process data,
//    CoE 0x8000), the handshake as Beckhoff describes it and IgH's example
//    (examples/tty/serial.c), and what others found on hardware (after an
//    init the terminal starts its toggles at 0). Captures from real
//    terminals are to correct it where it differs.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#ifndef _SIM_EL60XX_H_
#define _SIM_EL60XX_H_

#include <stddef.h>
#include <stdint.h>

#include "../devices/ecat_serial_core.h"

#define SIM_FIFO 1024
typedef struct {
  uint8_t b[SIM_FIFO];
  size_t head, n, cap;
} sim_fifo_t;

typedef struct sim_el60xx sim_el60xx_t;

/// What leaves the terminal on the wire goes here (default: back into its
/// own receive buffer, a loopback cable). baud and frame: the terminal's.
typedef void (*sim_wire_t)(sim_el60xx_t *t, const uint8_t *bytes, size_t n, void *ctx);

struct sim_el60xx {
  // the process data the master sees: status, input length, data
  uint8_t in[2 + ECAT_SER_MAX_DATA];
  int n_data;
  // turn-around: the status the terminal makes after a cycle's outputs is
  // seen this many cycles later (2 on our buses)
  int delay;
  uint8_t pipe[8][2 + ECAT_SER_MAX_DATA];
  int pipe_n;

  uint8_t sw;
  uint8_t err_pending;  // error bits for the next status
  uint8_t in_next[ECAT_SER_MAX_DATA], in_len;  // the input window being given
  sim_fifo_t tx, rx;  // send buffer (to the wire), receive buffer (from it)
  double credit;      // bytes the wire may carry now
  // CoE 0x8000:11 and :15 as written, and as used since the last init
  uint8_t obj_baud, obj_frame, baud, frame;
  sim_wire_t wire;
  void *wire_ctx;

  // the fake SDO request
  int sdo_latency, sdo_left, sdo_fail;
  uint8_t sdo_sub, sdo_value;
  int sdo_writes;
  uint8_t sdo_log_sub[16], sdo_log_value[16];

  // what the terminal did
  int inits, windows_taken, windows_given;
  uint8_t last_cw;
  int tx_pending;  // a request seen, not yet taken (buffer full)
};

/// A terminal with this many data bytes per window, these settings, and
/// buffers of these sizes (EL6021: 128 to send, 864 received).
void sim_el60xx_init(sim_el60xx_t *t, int n_data, uint8_t baud, uint8_t frame, size_t tx_cap, size_t rx_cap);

/// The terminal's part of a cycle: it takes the outputs the master sent
/// (control, length, data), moves bytes on the wire for period_ns, and
/// makes its status, which shows up in in[] after the delay.
void sim_el60xx_step(sim_el60xx_t *t, const uint8_t *out, long period_ns);

/// Bytes arriving from the wire (for a far end that answers).
void sim_el60xx_receive(sim_el60xx_t *t, const uint8_t *bytes, size_t n);

/// Set error bits for one status (parity 0x10, framing 0x20, overrun 0x40).
void sim_el60xx_error(sim_el60xx_t *t, uint8_t bits);

/// The SDO functions for ecat_ser_init(), writing into this terminal.
ecat_ser_sdo_t sim_el60xx_sdo(sim_el60xx_t *t);

#endif
