//
//    Unit tests of the serial function's core (devices/ecat_serial_core.c)
//    against a simulated terminal (sim_el60xx.c).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <string.h>

#include "../devices/ecat_serial.h"
#include "../devices/ecat_serial_core.h"
#include "fake.h"
#include "sim_el60xx.h"

#define PERIOD 1000000L  // ns
#define B9600 6
#define B19200 7
#define B115200 10
#define F8N1 3
#define F8E1 4

static ecat_ser_t S;
static sim_el60xx_t T;
static uint8_t OUT[2 + ECAT_SER_MAX_DATA];
static int bus_ok;

static void setup(uint8_t baud, uint8_t frame) {
  ecat_ser_sdo_t sdo;
  sim_el60xx_init(&T, 22, baud, frame, 128, 864);
  sdo = sim_el60xx_sdo(&T);
  ecat_ser_init(&S, 22, 0x8000, baud, frame, &sdo);
  memset(OUT, 0, sizeof(OUT));
  bus_ok = 1;
}

static void cycle(void) {
  ecat_ser_cycle(&S, T.in, OUT, bus_ok, PERIOD);
  if (bus_ok) sim_el60xx_step(&T, OUT, PERIOD);
}

static void cycles(int n) {
  while (n-- > 0) cycle();
}

static int until_ready(void) {
  int i;
  for (i = 0; i < 200 && S.state != ECAT_SER_READY; i++) cycle();
  return S.state == ECAT_SER_READY;
}

static void pattern(uint8_t *b, size_t n, unsigned seed) {
  size_t i;
  for (i = 0; i < n; i++) b[i] = (uint8_t)((i * 7 + seed + (i >> 8)) & 0xff);
}

/// Run until n bytes came back (reading the receive queue as the bridge
/// would), at most max cycles; returns how many came.
static size_t collect(uint8_t *got, size_t n, int max) {
  size_t have = 0;
  while (max-- > 0 && have < n) {
    cycle();
    have += ecat_ser_ring_get(&S.rx, got + have, n - have);
  }
  return have;
}

// ---- tests ----

static void test_values(void) {
  int bits = 0, stop = 0;
  char parity = 0;
  CHECK_EQ(ecat_ser_baud_value(19200), 7);
  CHECK_EQ(ecat_ser_baud_value(115200), 10);
  CHECK_EQ(ecat_ser_baud_value(28800), 0);
  CHECK_EQ(ecat_ser_baud_of(6), 9600);
  CHECK_EQ(ecat_ser_baud_of(0), 0);
  CHECK_EQ(ecat_ser_baud_of(99), 0);
  CHECK_EQ(ecat_ser_frame_value(8, 'E', 1), 4);
  CHECK_EQ(ecat_ser_frame_value(8, 'N', 2), 11);
  CHECK_EQ(ecat_ser_frame_value(7, 'O', 2), 10);
  CHECK_EQ(ecat_ser_frame_value(7, 'N', 1), 0);
  CHECK_EQ(ecat_ser_frame_value(7, 'N', 2), 8);  // the manual's table
  CHECK_EQ(ecat_ser_baud_value(12000), 14);
  CHECK_EQ(ecat_ser_baud_value(14400), 15);
  CHECK_EQ(ecat_ser_baud_of(11), 0);
  CHECK(ecat_ser_frame_of(12, &bits, &parity, &stop));
  CHECK(bits == 8 && parity == 'E' && stop == 2);
  CHECK(!ecat_ser_frame_of(6, &bits, &parity, &stop));
}

static void test_ring_wraps(void) {
  static ecat_ser_ring_t r;
  uint8_t in[1000], out[1000];
  int round;
  memset(&r, 0, sizeof(r));
  for (round = 0; round < 50; round++) {
    pattern(in, sizeof(in), (unsigned)round);
    CHECK_EQ(ecat_ser_ring_put(&r, in, sizeof(in)), sizeof(in));
    CHECK_EQ(ecat_ser_ring_get(&r, out, sizeof(out)), sizeof(out));
    CHECK(!memcmp(in, out, sizeof(in)));
  }
  // full: takes what fits
  CHECK_EQ(ecat_ser_ring_put(&r, in, sizeof(in)), 1000);
  CHECK_EQ(ecat_ser_ring_put(&r, in, sizeof(in)), 1000);
  CHECK_EQ(ecat_ser_ring_put(&r, in, sizeof(in)), 1000);
  CHECK_EQ(ecat_ser_ring_put(&r, in, sizeof(in)), 1000);
  CHECK_EQ(ecat_ser_ring_put(&r, in, sizeof(in)), ECAT_SER_RING_SIZE - 4000);
  CHECK_EQ(ecat_ser_ring_free(&r), 0);
}

static void test_starts_with_init(void) {
  setup(B19200, F8E1);
  cycle();
  CHECK_EQ(S.state, ECAT_SER_INIT);  // no settings to write: they are the file's
  CHECK(until_ready());
  CHECK_EQ(T.inits, 1);
  CHECK_EQ(S.inits, 1);
  CHECK_EQ(T.sdo_writes, 0);
  CHECK_EQ(OUT[0], 0);
}

static void test_loopback(void) {
  static uint8_t data[3000], got[3000];
  setup(B115200, F8N1);
  CHECK(until_ready());
  pattern(data, sizeof(data), 1);
  CHECK_EQ(ecat_ser_ring_put(&S.tx, data, sizeof(data)), sizeof(data));
  // 115200 baud, 10 bits: 11.5 bytes/ms
  CHECK_EQ(collect(got, sizeof(got), 1000), sizeof(got));
  CHECK(!memcmp(data, got, sizeof(data)));
  CHECK_EQ(T.tx_lost, 0);
  CHECK_EQ(S.tx_bytes, sizeof(data));
  CHECK_EQ(S.rx_bytes, sizeof(data));
}

static void test_slow_baud_waits_for_the_terminal(void) {
  static uint8_t data[1000], got[1000];
  setup(B9600, F8N1);
  CHECK(until_ready());
  pattern(data, sizeof(data), 2);
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  // 960 bytes/s: about 1.05 s
  CHECK_EQ(collect(got, sizeof(got), 1500), sizeof(got));
  CHECK(!memcmp(data, got, sizeof(data)));
  CHECK_EQ(T.sw & ECAT_SER_SW_OVERRUN_ERROR, 0);
  // the terminal takes every window and loses what does not fit in its
  // 128-byte send buffer: the channel gives no more than fits
  CHECK_EQ(T.tx_lost, 0);
}

static void test_slowest_rate_and_two_stop_bits(void) {
  static uint8_t data[300], got[300];
  setup(4, 12);  // 2400 baud 8E2: 12 bits, 200 bytes/s
  CHECK(until_ready());
  pattern(data, sizeof(data), 9);
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  CHECK_EQ(collect(got, sizeof(got), 2000), sizeof(got));
  CHECK(!memcmp(data, got, sizeof(data)));
  CHECK_EQ(T.tx_lost, 0);
  CHECK(T.tx.n <= ECAT_SER_TX_BUFFER);
}

static void test_receive_waits_for_room(void) {
  // more than the receive queue holds, less than it and the terminal's
  // buffer together
  static uint8_t data[4500], got[4500];
  size_t sent = 0, have = 0;
  int i;
  setup(B115200, F8N1);
  CHECK(until_ready());
  pattern(data, sizeof(data), 3);
  // the far end sends; the host does not read for a while
  for (i = 0; i < 3000 && sent < sizeof(data); i++) {
    size_t room = T.rx.cap - T.rx.n, n = sizeof(data) - sent;
    if (n > room) n = room;
    if (n > 40) n = 40;
    sim_el60xx_receive(&T, data + sent, n);
    sent += n;
    cycle();
  }
  CHECK_EQ(sent, sizeof(data));
  cycles(500);
  CHECK(ecat_ser_ring_free(&S.rx) < ECAT_SER_MAX_DATA);  // full as far as windows go
  CHECK(T.rx.n > 0);                                    // the rest waits in the terminal
  have = ecat_ser_ring_get(&S.rx, got, sizeof(got));
  have += collect(got + have, sizeof(got) - have, 2000);
  CHECK_EQ(have, sizeof(data));
  CHECK(!memcmp(data, got, sizeof(data)));
}

static void test_turn_around_1_and_3(void) {
  static uint8_t data[500], got[500];
  int delay;
  for (delay = 1; delay <= 3; delay += 2) {
    setup(B115200, F8N1);
    T.delay = delay;
    CHECK(until_ready());
    pattern(data, sizeof(data), (unsigned)delay);
    ecat_ser_ring_put(&S.tx, data, sizeof(data));
    CHECK_EQ(collect(got, sizeof(got), 1000), sizeof(got));
    CHECK(!memcmp(data, got, sizeof(data)));
  }
}

// ---- settings while running ----

static uint8_t wire_baud[4096];
static size_t wire_n;
static void logging_wire(sim_el60xx_t *t, const uint8_t *b, size_t n, void *ctx) {
  size_t i;
  (void)ctx;
  for (i = 0; i < n && wire_n < sizeof(wire_baud); i++) wire_baud[wire_n++] = t->baud;
  sim_el60xx_receive(t, b, n);
}

static int sdo_start_tx_left = -1;
static int (*sim_start)(void *, uint16_t, uint8_t, uint8_t);
static int start_noting(void *ctx, uint16_t index, uint8_t sub, uint8_t value) {
  if (sdo_start_tx_left < 0) sdo_start_tx_left = (int)T.tx.n;
  return sim_start(ctx, index, sub, value);
}

static void ask(uint8_t baud, uint8_t frame) {
  atomic_store(&S.want_baud, baud);
  atomic_store(&S.want_frame, frame);
  atomic_fetch_add(&S.want_seq, 1);
}

static int until_done(void) {
  int i;
  for (i = 0; i < 2000 && atomic_load(&S.done_seq) != atomic_load(&S.want_seq); i++) {
    uint8_t b[64];
    cycle();
    while (ecat_ser_ring_get(&S.rx, b, sizeof(b)) > 0) {
    }
  }
  return atomic_load(&S.done_seq) == atomic_load(&S.want_seq);
}

static void test_new_baud_after_the_queued_bytes(void) {
  static uint8_t data[200], got[300];
  size_t i;
  setup(B19200, F8N1);
  T.wire = logging_wire;
  wire_n = 0;
  sdo_start_tx_left = -1;
  sim_start = S.sdo.start;
  S.sdo.start = start_noting;
  CHECK(until_ready());
  pattern(data, sizeof(data), 4);
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  ask(B115200, F8N1);
  CHECK(until_done());
  CHECK_EQ(atomic_load(&S.settings_failed), 0);
  // the queued bytes all went out at the old rate, the terminal's buffer
  // empty when the new one was written
  CHECK_EQ(wire_n, sizeof(data));
  for (i = 0; i < wire_n; i++) CHECK_EQ(wire_baud[i], B19200);
  CHECK_EQ(sdo_start_tx_left, 0);
  CHECK_EQ(T.sdo_writes, 1);
  CHECK_EQ(T.sdo_log_sub[0], ECAT_SER_SUB_BAUD);
  CHECK_EQ(T.sdo_log_value[0], B115200);
  CHECK_EQ(T.inits, 2);
  CHECK_EQ(T.baud, B115200);
  // what comes after goes at the new rate
  ecat_ser_ring_put(&S.tx, data, 100);
  CHECK_EQ(collect(got, 100, 500), 100);
  CHECK(!memcmp(data, got, 100));
  for (i = sizeof(data); i < wire_n; i++) CHECK_EQ(wire_baud[i], B115200);
}

static void test_toggles_restart_after_init(void) {
  static uint8_t data[300], got[300];
  setup(B115200, F8N1);
  CHECK(until_ready());
  // an odd number of windows leaves the request bit at 1
  pattern(data, sizeof(data), 5);
  ecat_ser_ring_put(&S.tx, data, 11);
  CHECK_EQ(collect(got, 11, 200), 11);
  CHECK(S.cw & ECAT_SER_CW_TRANSMIT_REQUEST);
  ask(B115200, F8E1);
  CHECK(until_done());
  CHECK_EQ(S.cw & (ECAT_SER_CW_TRANSMIT_REQUEST | ECAT_SER_CW_RECEIVE_ACCEPTED), 0);
  // and sending still works
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  CHECK_EQ(collect(got, sizeof(got), 1000), sizeof(got));
  CHECK(!memcmp(data, got, sizeof(data)));
}

static void test_baud_and_frame(void) {
  setup(B9600, F8N1);
  CHECK(until_ready());
  ask(B19200, F8E1);
  CHECK(until_done());
  CHECK_EQ(T.sdo_writes, 2);
  CHECK_EQ(T.sdo_log_sub[0], ECAT_SER_SUB_BAUD);
  CHECK_EQ(T.sdo_log_sub[1], ECAT_SER_SUB_FRAME);
  CHECK_EQ(T.sdo_log_value[1], F8E1);
  CHECK_EQ(T.inits, 2);  // one init for both
  CHECK_EQ(T.frame, F8E1);
  // the same again: nothing written, but the request is answered
  ask(B19200, F8E1);
  CHECK(until_done());
  CHECK_EQ(T.sdo_writes, 2);
}

static void test_settings_that_fail(void) {
  static uint8_t data[100], got[100];
  setup(B9600, F8N1);
  CHECK(until_ready());
  T.sdo_fail = 1;
  ask(B115200, F8N1);
  CHECK(until_done());
  CHECK_EQ(atomic_load(&S.settings_failed), 1);
  CHECK_EQ(S.state, ECAT_SER_READY);
  CHECK_EQ(T.baud, B9600);
  CHECK_EQ(S.cur_baud, B9600);
  pattern(data, sizeof(data), 6);
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  CHECK_EQ(collect(got, sizeof(got), 500), sizeof(got));
  CHECK(!memcmp(data, got, sizeof(data)));
}

static void test_bus_loss(void) {
  static uint8_t data[200], got[200];
  setup(B9600, F8N1);
  CHECK(until_ready());
  ask(B115200, F8N1);
  CHECK(until_done());
  CHECK_EQ(T.sdo_writes, 1);
  bus_ok = 0;
  cycles(5);
  CHECK_EQ(S.state, ECAT_SER_DOWN);
  CHECK_EQ(OUT[0], 0);
  // the slave was power cycled: the master wrote the file's settings
  T.obj_baud = T.baud = B9600;
  bus_ok = 1;
  CHECK(until_ready());
  // the rate asked for is written again
  CHECK_EQ(T.sdo_writes, 2);
  CHECK_EQ(T.baud, B115200);
  pattern(data, sizeof(data), 7);
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  CHECK_EQ(collect(got, sizeof(got), 500), sizeof(got));
  CHECK(!memcmp(data, got, sizeof(data)));
}

static void test_errors_are_counted(void) {
  setup(B9600, F8N1);
  CHECK(until_ready());
  sim_el60xx_error(&T, ECAT_SER_SW_PARITY_ERROR);
  cycles(5);
  sim_el60xx_error(&T, ECAT_SER_SW_FRAMING_ERROR);
  cycles(5);
  sim_el60xx_error(&T, ECAT_SER_SW_PARITY_ERROR);
  cycles(5);
  CHECK_EQ(S.parity_errors, 2);
  CHECK_EQ(S.framing_errors, 1);
  CHECK_EQ(S.overrun_errors, 0);
}

// ---- send continuous (0x8000:04) ----

static void continuous(int terminal_too) {
  setup(B115200, F8N1);
  T.obj_continuous = terminal_too;
  S.continuous = 1;
  CHECK(until_ready());
}

static void test_without_continuous_a_long_frame_pauses_at_115200(void) {
  static uint8_t data[100], got[100];
  setup(B115200, F8N1);
  CHECK(until_ready());
  pattern(data, sizeof(data), 10);
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  CHECK_EQ(collect(got, sizeof(got), 500), sizeof(got));
  // 22 bytes every second cycle do not keep up: the line runs dry inside
  // the frame (why continuous sending exists)
  CHECK(T.gaps > 0);
}

static void test_continuous_sends_a_frame_whole(void) {
  static uint8_t data[100], got[100];
  continuous(1);
  pattern(data, sizeof(data), 11);
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  CHECK_EQ(collect(got, sizeof(got), 500), sizeof(got));
  CHECK(!memcmp(data, got, sizeof(data)));
  CHECK_EQ(T.gaps, 0);
  CHECK_EQ(T.tx_lost, 0);
  CHECK_EQ(S.bursts, 1);
  CHECK_EQ(S.burst_timeouts, 0);
  CHECK_EQ(S.cw & ECAT_SER_CW_SEND_CONTINUOUS, 0);
  // the next frame: another burst
  ecat_ser_ring_put(&S.tx, data, 30);
  CHECK_EQ(collect(got, 30, 500), 30);
  CHECK_EQ(S.bursts, 2);
}

static void test_continuous_more_than_the_buffer(void) {
  static uint8_t data[300], got[300];
  continuous(1);
  pattern(data, sizeof(data), 12);
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  CHECK_EQ(collect(got, sizeof(got), 1000), sizeof(got));
  CHECK(!memcmp(data, got, sizeof(data)));
  CHECK_EQ(T.tx_lost, 0);   // never more than 128 bytes in the buffer
  CHECK_EQ(S.bursts, 3);    // 128, 128, 44
}

static void test_continuous_without_the_terminals_setting(void) {
  static uint8_t data[50], got[50];
  // 0x8000:04 not set in the terminal: it sends as it gets the bytes and
  // never confirms; the channel goes on after its time-out
  continuous(0);
  pattern(data, sizeof(data), 13);
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  CHECK_EQ(collect(got, sizeof(got), 500), sizeof(got));
  cycles(200);
  CHECK_EQ(S.burst_timeouts, 1);
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  CHECK_EQ(collect(got, sizeof(got), 500), sizeof(got));
  CHECK(!memcmp(data, got, sizeof(data)));
}

static void test_continuous_then_new_settings(void) {
  static uint8_t data[60], got[60];
  continuous(1);
  pattern(data, sizeof(data), 14);
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  ask(B19200, F8N1);
  CHECK(until_done());
  CHECK_EQ(T.baud, B19200);
  CHECK_EQ(S.bursts, 1);  // the bytes before went out first, as one
  ecat_ser_ring_put(&S.tx, data, sizeof(data));
  CHECK_EQ(collect(got, sizeof(got), 500), sizeof(got));
  CHECK(!memcmp(data, got, sizeof(data)));
  CHECK_EQ(T.gaps, 0);
}

// ---- the function on a slave (devices/ecat_serial.c) ----

// No master in the tests: the SDO requests are not made (slave->config is
// NULL), so settings while running fail, which the settings-error pin shows.
ec_sdo_request_t *ecrt_slave_config_create_sdo_request(ec_slave_config_t *sc, uint16_t index, uint8_t subindex, size_t size) {
  (void)sc, (void)index, (void)subindex, (void)size;
  return NULL;
}
uint8_t *ecrt_sdo_request_data(const ec_sdo_request_t *req) { (void)req; return NULL; }
int ecrt_sdo_request_write(ec_sdo_request_t *req) { (void)req; return 0; }
int ecrt_sdo_request_timeout(ec_sdo_request_t *req, uint32_t timeout) { (void)req, (void)timeout; return 0; }
ec_request_state_t ecrt_sdo_request_state(ec_sdo_request_t *req) { (void)req; return EC_REQUEST_ERROR; }

static lcec_slave_t *SL;

/// An EL6021 slave (PDOs 0x1602 and 0x1A02) with a serial channel "port".
static void el6021_slave(void) {
  static fake_entry_t e[2 * 23 + 1];
  lcec_slave_modparam_t mp[5];
  ec_pdo_info_t *pdo;
  ec_sync_info_t *sm;
  int i;
  for (i = 0; i < 23; i++) {
    e[i] = (fake_entry_t){0x3003, (uint8_t)(i + 1), i == 0 ? 16 : 8};
    e[23 + i] = (fake_entry_t){0x3103, (uint8_t)(i + 1), i == 0 ? 16 : 8};
  }
  e[46] = (fake_entry_t)FAKE_END;
  memset(mp, 0, sizeof(mp));
  mp[0].id = ECAT_MP_SERIAL + ECAT_MP_SERIAL_NAME;
  strcpy(mp[0].value.str, "port");
  mp[1].id = ECAT_MP_SERIAL + ECAT_MP_SERIAL_PDOS;
  mp[1].value.u32 = 0x16021A02;
  mp[2].id = ECAT_MP_SERIAL + ECAT_MP_SERIAL_PORT;
  strcpy(mp[2].value.str, "/dev/ttyECAT0");
  mp[3].id = ECAT_MP_SERIAL + ECAT_MP_SERIAL_SETTINGS;
  mp[3].value.u32 = 0x8000 | B115200 << 16 | F8E1 << 24;
  mp[4].id = -1;
  SL = fake_slave("com", e, mp);
  // two PDOs instead of the fake's one
  pdo = calloc(2, sizeof(*pdo));
  sm = calloc(3, sizeof(*sm));
  pdo[0] = SL->generic_sync_managers[0].pdos[0];
  pdo[0].index = 0x1602;
  pdo[0].n_entries = 23;
  pdo[1] = pdo[0];
  pdo[1].index = 0x1A02;
  pdo[1].entries = pdo[0].entries + 23;
  sm[0] = (ec_sync_info_t){2, EC_DIR_OUTPUT, 1, &pdo[0], EC_WD_DEFAULT};
  sm[1] = (ec_sync_info_t){3, EC_DIR_INPUT, 1, &pdo[1], EC_WD_DEFAULT};
  sm[2].index = 0xff;
  SL->generic_sync_managers = sm;
  ecat_serial_channels = NULL;
  CHECK_EQ(ecat_serial_init(0, SL), 0);
  sim_el60xx_init(&T, 22, B115200, F8E1, 128, 864);
}

/// A cycle of the slave against the simulated terminal: the master copies
/// the terminal's inputs in, the function runs, its outputs go out.
static void slave_cycle(void) {
  uint8_t *pd = SL->master->process_data;
  // 0x3003 at byte 0 (24 bytes), 0x3103 after it
  memcpy(pd + 24, T.in, 24);
  SL->proc_read(SL, PERIOD);
  SL->proc_write(SL, PERIOD);
  sim_el60xx_step(&T, pd, PERIOD);
}

static void test_function_on_a_slave(void) {
  static uint8_t data[500], got[500];
  ecat_serial_channel_t *ch;
  size_t have = 0;
  int i;
  el6021_slave();
  ch = ecat_serial_channels;
  CHECK(ch != NULL);
  if (ch == NULL) return;
  CHECK(!strcmp(ch->name, "com.port"));
  CHECK(!strcmp(ch->port, "/dev/ttyECAT0"));
  CHECK_EQ(ch->data_bits, 8);
  CHECK_EQ(ch->parity, 'E');
  CHECK_EQ(ch->core.n_data, 22);
  CHECK_EQ(ch->core.continuous, 0);  // no flags modparam: off
  CHECK_EQ(PIN_U32("com.port.burst-timeouts"), 0);
  for (i = 0; i < 50 && PIN_U32("com.port.state") != ECAT_SER_READY; i++) slave_cycle();
  CHECK_EQ(PIN_U32("com.port.state"), ECAT_SER_READY);
  CHECK_EQ(PIN_U32("com.port.baud"), 115200);
  pattern(data, sizeof(data), 8);
  ecat_ser_ring_put(&ch->core.tx, data, sizeof(data));
  for (i = 0; i < 1000 && have < sizeof(got); i++) {
    slave_cycle();
    have += ecat_ser_ring_get(&ch->core.rx, got + have, sizeof(got) - have);
  }
  CHECK_EQ(have, sizeof(data));
  CHECK(!memcmp(data, got, sizeof(data)));
  CHECK_EQ(PIN_U32("com.port.tx-bytes"), sizeof(data));
  CHECK_EQ(PIN_U32("com.port.rx-bytes"), sizeof(data));
  CHECK_EQ(PIN_BIT("com.port.settings-error"), 0);
  // no SDO requests here: a new rate cannot be written
  atomic_store(&ch->core.want_baud, B9600);
  atomic_fetch_add(&ch->core.want_seq, 1);
  for (i = 0; i < 100; i++) slave_cycle();
  CHECK_EQ(PIN_BIT("com.port.settings-error"), 1);
  CHECK_EQ(PIN_U32("com.port.baud"), 115200);
  // bus loss
  fake_bus(SL, 0);
  slave_cycle();
  CHECK_EQ(PIN_U32("com.port.state"), ECAT_SER_DOWN);
}

RUN_TESTS("serial", test_values, test_ring_wraps, test_starts_with_init, test_loopback,
    test_slow_baud_waits_for_the_terminal, test_slowest_rate_and_two_stop_bits, test_receive_waits_for_room, test_turn_around_1_and_3,
    test_new_baud_after_the_queued_bytes, test_toggles_restart_after_init, test_baud_and_frame,
    test_settings_that_fail, test_bus_loss, test_errors_are_counted, test_without_continuous_a_long_frame_pauses_at_115200,
    test_continuous_sends_a_frame_whole, test_continuous_more_than_the_buffer, test_continuous_without_the_terminals_setting,
    test_continuous_then_new_settings, test_function_on_a_slave)
