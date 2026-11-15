//
//    A simulated Beckhoff serial terminal channel (see sim_el60xx.h).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include "sim_el60xx.h"

#include <string.h>

static size_t fifo_put(sim_fifo_t *f, const uint8_t *b, size_t n) {
  size_t i;
  if (n > f->cap - f->n) n = f->cap - f->n;
  for (i = 0; i < n; i++) f->b[(f->head + f->n + i) % SIM_FIFO] = b[i];
  f->n += n;
  return n;
}

static size_t fifo_get(sim_fifo_t *f, uint8_t *b, size_t n) {
  size_t i;
  if (n > f->n) n = f->n;
  for (i = 0; i < n; i++) b[i] = f->b[(f->head + i) % SIM_FIFO];
  f->head = (f->head + n) % SIM_FIFO;
  f->n -= n;
  return n;
}

static void loopback(sim_el60xx_t *t, const uint8_t *bytes, size_t n, void *ctx) {
  (void)ctx;
  sim_el60xx_receive(t, bytes, n);
}

void sim_el60xx_init(sim_el60xx_t *t, int n_data, uint8_t baud, uint8_t frame, size_t tx_cap, size_t rx_cap) {
  memset(t, 0, sizeof(*t));
  t->n_data = n_data;
  t->delay = 2;
  t->tx.cap = tx_cap > SIM_FIFO ? SIM_FIFO : tx_cap;
  t->rx.cap = rx_cap > SIM_FIFO ? SIM_FIFO : rx_cap;
  t->obj_baud = t->baud = baud;
  t->obj_frame = t->frame = frame;
  t->wire = loopback;
  t->sdo_latency = 3;
}

void sim_el60xx_receive(sim_el60xx_t *t, const uint8_t *bytes, size_t n) {
  if (fifo_put(&t->rx, bytes, n) < n) t->err_pending |= ECAT_SER_SW_OVERRUN_ERROR;
}

void sim_el60xx_error(sim_el60xx_t *t, uint8_t bits) { t->err_pending |= bits; }

static int char_bits(uint8_t frame) {
  int bits = 8, stop = 1;
  char parity = 'N';
  if (!ecat_ser_frame_of(frame, &bits, &parity, &stop)) return 11;
  return 1 + bits + (parity != 'N') + stop;
}

void sim_el60xx_step(sim_el60xx_t *t, const uint8_t *out, long period_ns) {
  uint8_t cw = out[0], len = out[1];
  uint8_t status[2 + ECAT_SER_MAX_DATA];
  unsigned long baud = ecat_ser_baud_of(t->baud);
  int i;

  // error bits show for one status
  t->sw &= (uint8_t)~(ECAT_SER_SW_PARITY_ERROR | ECAT_SER_SW_FRAMING_ERROR | ECAT_SER_SW_OVERRUN_ERROR);
  t->sw |= t->err_pending;
  t->err_pending = 0;

  if (cw & ECAT_SER_CW_INIT_REQUEST) {
    if (!(t->sw & ECAT_SER_SW_INIT_ACCEPTED)) {
      // init: buffers emptied, new settings used, toggles start at 0
      t->tx.n = t->rx.n = 0;
      t->baud = t->obj_baud;
      t->frame = t->obj_frame;
      t->continuous = t->obj_continuous;
      t->cs_sending = t->cs_done = 0;
      t->sw = ECAT_SER_SW_INIT_ACCEPTED;
      t->tx_pending = 0;
      t->inits++;
    }
  } else if ((t->sw & ECAT_SER_SW_INIT_ACCEPTED) && !t->cs_done) {
    t->sw &= (uint8_t)~ECAT_SER_SW_INIT_ACCEPTED;
  } else {
    // continuous: a rising edge of CW.3 sends the buffer; SW.2 when it is
    // out, cleared when CW.3 drops
    if (t->continuous) {
      if ((cw & ECAT_SER_CW_SEND_CONTINUOUS) && !(t->last_cw & ECAT_SER_CW_SEND_CONTINUOUS)) t->cs_sending = 1;
      if (t->cs_done && !(cw & ECAT_SER_CW_SEND_CONTINUOUS)) {
        t->cs_done = 0;
        t->sw &= (uint8_t)~ECAT_SER_SW_INIT_ACCEPTED;
      }
    }
    // a window to send: the request bit toggled (an edge, not a
    // difference to the accepted bit: a request bit left at 1 through an
    // init makes the next request look like none, and sending stops,
    // as was seen on hardware)
    if ((cw ^ t->last_cw) & ECAT_SER_CW_TRANSMIT_REQUEST) t->tx_pending = 1;
    // (Beckhoff's manual: the send buffer is written to; once it is
    // full, all further data is lost)
    if (t->tx_pending) {
      if (len > t->n_data) len = (uint8_t)t->n_data;
      t->tx_lost += len - fifo_put(&t->tx, out + 2, len);
      t->sw ^= ECAT_SER_SW_TRANSMIT_ACCEPTED;
      t->windows_taken++;
      t->tx_pending = 0;
    }
    // a window for the master, once it took the last one
    if (!!(t->sw & ECAT_SER_SW_RECEIVE_REQUEST) == !!(cw & ECAT_SER_CW_RECEIVE_ACCEPTED) && t->rx.n > 0) {
      size_t n = fifo_get(&t->rx, t->in_next, (size_t)t->n_data);
      t->in_len = (uint8_t)n;
      t->sw ^= ECAT_SER_SW_RECEIVE_REQUEST;
      t->windows_given++;
    }
  }

  // the wire
  if (baud > 0 && (!t->continuous || t->cs_sending)) {
    uint8_t b[SIM_FIFO];
    size_t n;
    t->credit += (double)baud / char_bits(t->frame) * (double)period_ns * 1e-9;
    if (t->tx.n == 0 && t->credit > 1.0) t->credit = 1.0;  // idle line saves nothing up
    n = (size_t)t->credit;
    n = fifo_get(&t->tx, b, n);
    t->credit -= (double)n;
    if (n > 0 && t->dry) t->gaps++;
    // ran dry with time left: a pause, if more comes
    t->dry = (n > 0 || t->dry) && t->tx.n == 0 && t->credit >= 1.0;
    if (n > 0 && t->wire != NULL) t->wire(t, b, n, t->wire_ctx);
    if (t->continuous && t->tx.n == 0) {
      t->cs_sending = 0;
      t->cs_done = 1;
      t->sw |= ECAT_SER_SW_INIT_ACCEPTED;
      t->dry = 0;  // the burst is over: what comes next is another
    }
  } else if (t->continuous) {
    t->credit = 0;
  }

  // SW.3: the receive buffer is full (0x8000:1A, its size by default)
  if (t->rx.n >= t->rx.cap) t->sw |= ECAT_SER_SW_BUFFER_FULL; else t->sw &= (uint8_t)~ECAT_SER_SW_BUFFER_FULL;
  t->last_cw = cw;
  status[0] = t->sw;
  status[1] = t->in_len;
  memcpy(status + 2, t->in_next, ECAT_SER_MAX_DATA);

  // the master sees it after the delay
  if (t->delay <= 1) {
    memcpy(t->in, status, sizeof(status));
    return;
  }
  memcpy(t->pipe[t->pipe_n], status, sizeof(status));
  t->pipe_n++;
  if (t->pipe_n >= t->delay) {
    memcpy(t->in, t->pipe[0], sizeof(status));
    for (i = 1; i < t->pipe_n; i++) memcpy(t->pipe[i - 1], t->pipe[i], sizeof(status));
    t->pipe_n--;
  }
}

// ---- the fake SDO request ----

static int sdo_start(void *ctx, uint16_t index, uint8_t subindex, uint8_t value) {
  sim_el60xx_t *t = ctx;
  (void)index;
  t->sdo_sub = subindex;
  t->sdo_value = value;
  t->sdo_left = t->sdo_latency;
  if (t->sdo_writes < 16) {
    t->sdo_log_sub[t->sdo_writes] = subindex;
    t->sdo_log_value[t->sdo_writes] = value;
  }
  t->sdo_writes++;
  return 0;
}

static int sdo_poll(void *ctx) {
  sim_el60xx_t *t = ctx;
  if (t->sdo_left > 0) {
    t->sdo_left--;
    return 0;
  }
  if (t->sdo_fail) return -1;
  if (t->sdo_sub == ECAT_SER_SUB_BAUD) t->obj_baud = t->sdo_value;
  if (t->sdo_sub == ECAT_SER_SUB_FRAME) t->obj_frame = t->sdo_value;
  return 1;
}

ecat_ser_sdo_t sim_el60xx_sdo(sim_el60xx_t *t) {
  ecat_ser_sdo_t s = {sdo_start, sdo_poll, t};
  return s;
}
