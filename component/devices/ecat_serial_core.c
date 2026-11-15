//
//    ecat: the serial function's core (see ecat_serial_core.h).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//
//    This program is distributed in the hope that it will be useful,
//    but WITHOUT ANY WARRANTY; without even the implied warranty of
//    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
//    GNU General Public License for more details.
//

/// @file
/// @brief The handshake with a Beckhoff serial terminal.
///
/// Sending: the data and their number go into the output window, and the
/// transmit request bit is toggled; the terminal has taken them when its
/// transmit accepted bit equals the request bit again. Receiving: the
/// terminal toggles receive request when it has put data in the input
/// window; they are taken by toggling receive accepted. Init: init request
/// until init accepted, then both off; after it the terminal starts its
/// toggles at 0, and so does this side (a request bit left at 1 would look
/// like "taken" and stop sending).
///
/// New settings (0x8000:11 baud rate, :15 data frame) are written with the
/// channel idle and the terminal's buffer sent out, then an init makes the
/// terminal use them. The bytes queued before a request go out with the
/// settings they were written for.

#include "ecat_serial_core.h"

#include <string.h>

// ---- byte queue ----

size_t ecat_ser_ring_used(ecat_ser_ring_t *r) {
  return (uint32_t)(atomic_load_explicit(&r->head, memory_order_acquire) -
                    atomic_load_explicit(&r->tail, memory_order_acquire));
}

size_t ecat_ser_ring_free(ecat_ser_ring_t *r) { return ECAT_SER_RING_SIZE - ecat_ser_ring_used(r); }

size_t ecat_ser_ring_put(ecat_ser_ring_t *r, const uint8_t *data, size_t n) {
  uint32_t head = atomic_load_explicit(&r->head, memory_order_relaxed);
  uint32_t tail = atomic_load_explicit(&r->tail, memory_order_acquire);
  size_t room = ECAT_SER_RING_SIZE - (uint32_t)(head - tail), i;
  if (n > room) n = room;
  for (i = 0; i < n; i++) r->buf[(head + i) & (ECAT_SER_RING_SIZE - 1)] = data[i];
  atomic_store_explicit(&r->head, head + (uint32_t)n, memory_order_release);
  return n;
}

size_t ecat_ser_ring_get(ecat_ser_ring_t *r, uint8_t *data, size_t n) {
  uint32_t tail = atomic_load_explicit(&r->tail, memory_order_relaxed);
  uint32_t head = atomic_load_explicit(&r->head, memory_order_acquire);
  size_t have = (uint32_t)(head - tail), i;
  if (n > have) n = have;
  for (i = 0; i < n; i++) data[i] = r->buf[(tail + i) & (ECAT_SER_RING_SIZE - 1)];
  atomic_store_explicit(&r->tail, tail + (uint32_t)n, memory_order_release);
  return n;
}

// ---- settings values (the terminals' CoE enumerations) ----

// value -> baud; 11 to 13 are not used; 14 and 15 from firmware 07 (EL6001, EL6021)
static const unsigned long bauds[] = {0, 300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 0, 0, 0, 12000, 14400};
#define N_BAUDS (sizeof(bauds) / sizeof(bauds[0]))

uint8_t ecat_ser_baud_value(unsigned long baud) {
  unsigned int i;
  for (i = 1; i < N_BAUDS; i++) {
    if (bauds[i] != 0 && bauds[i] == baud) return (uint8_t)i;
  }
  return 0;
}

unsigned long ecat_ser_baud_of(uint8_t value) { return value < N_BAUDS ? bauds[value] : 0; }

static const struct {
  uint8_t value;
  int bits;
  char parity;
  int stop;
} frames[] = {
    {1, 7, 'E', 1}, {2, 7, 'O', 1}, {3, 8, 'N', 1}, {4, 8, 'E', 1}, {5, 8, 'O', 1},
    {8, 7, 'N', 2}, {9, 7, 'E', 2}, {10, 7, 'O', 2}, {11, 8, 'N', 2}, {12, 8, 'E', 2}, {13, 8, 'O', 2},
};
#define N_FRAMES (sizeof(frames) / sizeof(frames[0]))

uint8_t ecat_ser_frame_value(int data_bits, char parity, int stop_bits) {
  unsigned int i;
  for (i = 0; i < N_FRAMES; i++) {
    if (frames[i].bits == data_bits && frames[i].parity == parity && frames[i].stop == stop_bits) return frames[i].value;
  }
  return 0;
}

int ecat_ser_frame_of(uint8_t value, int *data_bits, char *parity, int *stop_bits) {
  unsigned int i;
  for (i = 0; i < N_FRAMES; i++) {
    if (frames[i].value == value) {
      *data_bits = frames[i].bits;
      *parity = frames[i].parity;
      *stop_bits = frames[i].stop;
      return 1;
    }
  }
  return 0;
}

/// Bits on the wire per character with this data frame (start bit included).
static int char_bits(uint8_t frame) {
  int bits = 8, stop = 1;
  char parity = 'N';
  if (!ecat_ser_frame_of(frame, &bits, &parity, &stop)) return 11;
  return 1 + bits + (parity != 'N') + stop;
}

// ---- the channel ----

void ecat_ser_init(ecat_ser_t *s, int n_data, uint16_t settings_index, uint8_t start_baud, uint8_t start_frame,
    const ecat_ser_sdo_t *sdo) {
  memset(s, 0, sizeof(*s));
  s->n_data = n_data > ECAT_SER_MAX_DATA ? ECAT_SER_MAX_DATA : n_data;
  s->settings_index = settings_index;
  if (sdo != NULL) s->sdo = *sdo;
  s->start_baud = start_baud;
  s->start_frame = start_frame;
  s->cur_baud = s->to_baud = start_baud;
  s->cur_frame = s->to_frame = start_frame;
  atomic_store(&s->want_baud, start_baud);
  atomic_store(&s->want_frame, start_frame);
  s->state = ECAT_SER_DOWN;
}

static void finish_request(ecat_ser_t *s) {
  atomic_store_explicit(&s->done_seq, s->applying_seq, memory_order_release);
}

static void start_init(ecat_ser_t *s) {
  s->state = ECAT_SER_INIT;
  s->sdo_step = 0;  // used as the init phase: 0 request, 1 wait for the answer to go
  s->inflight = 0;
  s->out_len = 0;
}

/// Write the next setting that differs, or go on to the init.
static void next_setting(ecat_ser_t *s) {
  uint16_t sub = 0;
  uint8_t value = 0;
  if (s->cur_baud != s->to_baud && s->to_baud != 0) {
    sub = ECAT_SER_SUB_BAUD;
    value = s->to_baud;
    s->sdo_step = 0;
  } else if (s->cur_frame != s->to_frame && s->to_frame != 0) {
    sub = ECAT_SER_SUB_FRAME;
    value = s->to_frame;
    s->sdo_step = 1;
  }
  if (sub == 0) {
    start_init(s);
    return;
  }
  if (s->sdo.start == NULL || s->sdo.start(s->sdo.ctx, s->settings_index, (uint8_t)sub, value) < 0) {
    atomic_store(&s->settings_failed, 1);
    s->to_baud = s->cur_baud;
    s->to_frame = s->cur_frame;
    start_init(s);
    return;
  }
  s->sdo_busy = 1;
}

static void settings(ecat_ser_t *s) {
  int r;
  if (s->sdo_busy) {
    r = s->sdo.poll != NULL ? s->sdo.poll(s->sdo.ctx) : -1;
    if (r == 0) return;
    s->sdo_busy = 0;
    if (r < 0) {
      // keep what the terminal has; the init below makes sure it runs
      atomic_store(&s->settings_failed, 1);
      s->to_baud = s->cur_baud;
      s->to_frame = s->cur_frame;
      start_init(s);
      return;
    }
    if (s->sdo_step == 0) {
      s->cur_baud = s->to_baud;
    } else {
      s->cur_frame = s->to_frame;
    }
  }
  next_setting(s);
}

static void init(ecat_ser_t *s, uint8_t sw) {
  if (s->sdo_step == 0) {
    if (sw & ECAT_SER_SW_INIT_ACCEPTED) {
      s->cw = 0;
      s->sdo_step = 1;
    } else {
      s->cw = ECAT_SER_CW_INIT_REQUEST;
    }
    return;
  }
  s->cw = 0;
  if (!(sw & ECAT_SER_SW_INIT_ACCEPTED)) {
    s->state = ECAT_SER_READY;
    s->inits++;
    s->inflight = 0;
    s->out_len = 0;
    finish_request(s);
  }
}

static void ready(ecat_ser_t *s, uint8_t sw, const uint8_t *in, long period_ns) {
  int accepted = !!(sw & ECAT_SER_SW_TRANSMIT_ACCEPTED) == !!(s->cw & ECAT_SER_CW_TRANSMIT_REQUEST);
  uint32_t seq = atomic_load_explicit(&s->want_seq, memory_order_acquire);
  int pending = seq != s->applying_seq;
  unsigned long baud;

  // sending
  if (s->inflight && accepted) {
    s->tx_bytes += s->out_len;
    baud = ecat_ser_baud_of(s->cur_baud);
    if (baud > 0) s->hold_ns += (long long)s->out_len * char_bits(s->cur_frame) * 1000000000LL / (long long)baud;
    s->inflight = 0;
    s->out_len = 0;
  }
  if (!s->inflight && accepted) {
    // no more than the terminal's send buffer holds: it takes a window
    // whether or not it fits, and loses what does not. hold_ns is how
    // long it still sends what it has (counted from when it took it, so
    // rather too long than too short).
    size_t room = (size_t)s->n_data, n;
    baud = ecat_ser_baud_of(s->cur_baud);
    if (baud > 0) {
      long long bits = char_bits(s->cur_frame);
      long long held = (s->hold_ns * (long long)baud + bits * 1000000000LL - 1) / (bits * 1000000000LL);
      long long free_bytes = ECAT_SER_TX_BUFFER - held;
      if (free_bytes < 0) free_bytes = 0;
      if ((size_t)free_bytes < room) room = (size_t)free_bytes;
    }
    n = room > 0 ? ecat_ser_ring_get(&s->tx, s->out, room) : 0;
    if (n > 0) {
      s->out_len = (uint8_t)n;
      s->cw ^= ECAT_SER_CW_TRANSMIT_REQUEST;
      s->inflight = 1;
    }
  }

  // receiving: only when the bytes fit, so that none are lost (the
  // terminal keeps them meanwhile)
  if (!!(sw & ECAT_SER_SW_RECEIVE_REQUEST) != !!(s->cw & ECAT_SER_CW_RECEIVE_ACCEPTED)) {
    size_t n = in[1] > s->n_data ? (size_t)s->n_data : in[1];
    if (ecat_ser_ring_free(&s->rx) >= n) {
      ecat_ser_ring_put(&s->rx, in + 2, n);
      s->rx_bytes += n;
      s->cw ^= ECAT_SER_CW_RECEIVE_ACCEPTED;
    }
  }

  // new settings: once all bytes queued before the request are sent and
  // the terminal has had the time to put them on the wire
  if (pending && !s->inflight && ecat_ser_ring_used(&s->tx) == 0 && s->hold_ns <= 0) {
    s->applying_seq = seq;
    s->to_baud = atomic_load(&s->want_baud);
    s->to_frame = atomic_load(&s->want_frame);
    atomic_store(&s->settings_failed, 0);
    s->state = ECAT_SER_SETTINGS;
    next_setting(s);
  }
  (void)period_ns;
}

void ecat_ser_cycle(ecat_ser_t *s, const uint8_t *in, uint8_t *out, int bus_ok, long period_ns) {
  uint8_t sw, rising;

  s->hold_ns -= period_ns;
  if (s->hold_ns < 0) s->hold_ns = 0;

  if (!bus_ok) {
    if (s->state != ECAT_SER_DOWN) {
      s->state = ECAT_SER_DOWN;
      s->inflight = 0;
      s->out_len = 0;
      s->sdo_busy = 0;
      s->cw = 0;
    }
    out[0] = 0;
    out[1] = 0;
    return;
  }

  sw = in[0];
  rising = sw & (uint8_t)~s->last_sw;
  if (rising & ECAT_SER_SW_PARITY_ERROR) s->parity_errors++;
  if (rising & ECAT_SER_SW_FRAMING_ERROR) s->framing_errors++;
  if (rising & ECAT_SER_SW_OVERRUN_ERROR) s->overrun_errors++;
  if (rising & ECAT_SER_SW_BUFFER_FULL) s->buffer_full++;
  s->last_sw = sw;

  switch (s->state) {
    case ECAT_SER_DOWN:
      // Back (or the first time). After a power cycle of the slave the
      // master wrote the file's settings at its start; settings asked for
      // since then are written again.
      s->cur_baud = s->start_baud;
      s->cur_frame = s->start_frame;
      s->cw = 0;
      s->state = ECAT_SER_SETTINGS;
      next_setting(s);
      break;
    case ECAT_SER_SETTINGS:
      settings(s);
      break;
    case ECAT_SER_INIT:
      init(s, sw);
      break;
    default:
      ready(s, sw, in, period_ns);
      break;
  }

  out[0] = s->cw;
  out[1] = s->out_len;
  if (s->out_len > 0) memcpy(out + 2, s->out, s->out_len);
}
