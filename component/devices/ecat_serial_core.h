//
//    ecat: the serial function's core, for Beckhoff's serial terminals
//    (EL6001, EL6002, EL6021, EL6022): the handshake with the terminal, the
//    byte queues to and from the host side, and setting baud rate and data
//    frame while running. No HAL and no master calls here, so that the
//    tests can run it against a simulated terminal.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#ifndef _ECAT_SERIAL_CORE_H_
#define _ECAT_SERIAL_CORE_H_

#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>

// The process data of a channel (PDO 0x1602/0x1A02 or 0x1604/0x1A04 of an
// EL6021, and the same layout on the other terminals): a control byte and
// an output length, then the data bytes; a status byte and an input length,
// then the data bytes.
#define ECAT_SER_CW_TRANSMIT_REQUEST 0x01
#define ECAT_SER_CW_RECEIVE_ACCEPTED 0x02
#define ECAT_SER_CW_INIT_REQUEST 0x04
#define ECAT_SER_CW_SEND_CONTINUOUS 0x08
#define ECAT_SER_SW_TRANSMIT_ACCEPTED 0x01
#define ECAT_SER_SW_RECEIVE_REQUEST 0x02
#define ECAT_SER_SW_INIT_ACCEPTED 0x04
#define ECAT_SER_SW_BUFFER_FULL 0x08  // the receive buffer (0x8000:1A bytes, 864)
#define ECAT_SER_SW_PARITY_ERROR 0x10
#define ECAT_SER_SW_FRAMING_ERROR 0x20
#define ECAT_SER_SW_OVERRUN_ERROR 0x40
#define ECAT_SER_MAX_DATA 22
// The terminal's send buffer: what does not fit is lost (Beckhoff's
// manual), so the channel gives no more than the terminal can hold.
#define ECAT_SER_TX_BUFFER 128

// The settings object (0x8000 for the first channel, + 0x10 per channel).
#define ECAT_SER_SUB_BAUD 0x11
#define ECAT_SER_SUB_FRAME 0x15

/// A queue of bytes with one writer and one reader (the servo thread and
/// the bridge thread), without locks. Size: a power of two.
#define ECAT_SER_RING_SIZE 4096
typedef struct {
  uint8_t buf[ECAT_SER_RING_SIZE];
  _Atomic uint32_t head;  // written by the writer
  _Atomic uint32_t tail;  // written by the reader
} ecat_ser_ring_t;

size_t ecat_ser_ring_used(ecat_ser_ring_t *r);
size_t ecat_ser_ring_free(ecat_ser_ring_t *r);
/// Writer: copies up to n bytes; returns how many.
size_t ecat_ser_ring_put(ecat_ser_ring_t *r, const uint8_t *data, size_t n);
/// Reader: copies up to n bytes out; returns how many.
size_t ecat_ser_ring_get(ecat_ser_ring_t *r, uint8_t *data, size_t n);

/// The channel's state, as the state pin shows it.
enum {
  ECAT_SER_DOWN = 0,      // no process data (bus not working, slave not in OP)
  ECAT_SER_SETTINGS = 1,  // writing baud rate or data frame
  ECAT_SER_INIT = 2,      // init handshake
  ECAT_SER_READY = 3,     // moving data
};

/// Writing a CoE object while running (in the servo thread: start, then
/// poll). The component uses SDO requests; the tests a fake.
typedef struct {
  /// Start writing value (one byte) to index:subindex. 0, or < 0 if it
  /// cannot be started.
  int (*start)(void *ctx, uint16_t index, uint8_t subindex, uint8_t value);
  /// 0 while busy, 1 when written, < 0 on an error.
  int (*poll)(void *ctx);
  void *ctx;
} ecat_ser_sdo_t;

typedef struct {
  // ---- set up once ----
  int n_data;              // data bytes per direction (at most ECAT_SER_MAX_DATA)
  uint16_t settings_index; // 0x8000 + 0x10 * channel
  ecat_ser_sdo_t sdo;
  uint8_t start_baud, start_frame;  // the values the file sets at start (0x8000:11 and :15)

  // ---- shared with the bridge thread ----
  ecat_ser_ring_t tx;  // host -> terminal (the bridge writes)
  ecat_ser_ring_t rx;  // terminal -> host (the servo thread writes)
  /// The bridge asks for other settings: it writes want_baud and
  /// want_frame, then increments want_seq; once they are in the terminal
  /// (or could not be set), the servo thread sets done_seq to that value.
  /// Everything the bridge put in tx before is sent with the old settings.
  _Atomic uint8_t want_baud, want_frame;
  _Atomic uint32_t want_seq, done_seq;
  _Atomic int settings_failed;  // the last request could not be written

  // ---- the servo thread's ----
  int state;
  uint8_t cw;                  // our control bits
  uint8_t out_len;             // bytes in this window
  uint8_t out[ECAT_SER_MAX_DATA];
  int inflight;                // a window is waiting for "transmit accepted"
  uint8_t cur_baud, cur_frame; // in the terminal (as far as we know)
  uint8_t to_baud, to_frame;   // being written
  uint32_t applying_seq;
  int sdo_step;                // 0: baud, 1: frame
  int sdo_busy;
  long long hold_ns;           // wait before writing settings: the terminal still sends
  uint8_t last_sw;
  // counters
  uint64_t tx_bytes, rx_bytes;
  uint32_t parity_errors, framing_errors, overrun_errors, buffer_full, inits;
} ecat_ser_t;

/// Set up a channel (zeroes it first).
void ecat_ser_init(ecat_ser_t *s, int n_data, uint16_t settings_index, uint8_t start_baud, uint8_t start_frame,
    const ecat_ser_sdo_t *sdo);

/// One servo cycle: in is the channel's input process data (status,
/// length, data), out its output process data, bus_ok whether both are
/// valid this cycle, period_ns the cycle time.
void ecat_ser_cycle(ecat_ser_t *s, const uint8_t *in, uint8_t *out, int bus_ok, long period_ns);

/// Baud rate value of 0x8000:11 for a baud rate, 0 if the terminals have none.
uint8_t ecat_ser_baud_value(unsigned long baud);
/// The baud rate of a value of 0x8000:11, 0 if unknown.
unsigned long ecat_ser_baud_of(uint8_t value);
/// Data frame value of 0x8000:15 (data bits 7 or 8, parity 'N', 'E' or 'O',
/// stop bits 1 or 2), 0 if the terminals have none (7N1).
uint8_t ecat_ser_frame_value(int data_bits, char parity, int stop_bits);
/// The other way round; 0 if unknown.
int ecat_ser_frame_of(uint8_t value, int *data_bits, char *parity, int *stop_bits);

#endif
