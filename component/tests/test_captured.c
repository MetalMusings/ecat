//
//    Unit tests with fake slaves from a capture (captured_*.h, written by
//    ecatsetup --capture-c): the profiles on the process data layout the
//    devices have on a real bus, and the replay of captured cycles.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <string.h>

#include "../devices/ecat_cia402.h"
#include "captured_plasma.h"
#include "captured_plasma_op.h"
#include "fake.h"

#define PERIOD 1000000L  // ns

static unsigned cycle(lcec_slave_t *s, unsigned sw) {
  fake_set(s, 0x6041, 0, sw);
  s->proc_read(s, PERIOD);
  s->proc_write(s, PERIOD);
  return fake_get(s, 0x6040, 0);
}

static void test_every_captured_slave_can_be_made(void) {
  int i, n;
  for (i = 0; i < PLASMA_N_SLAVES; i++) {
    const fake_capture_slave_t *c = &plasma_slaves[i];
    lcec_slave_t *s = fake_capture_slave("c", c, NULL);
    CHECK(s != NULL);
    for (n = 0; c->entries[n].index != 0; n++) {
    }
    CHECK_EQ(n, c->n_entries);
    CHECK(c->n_outputs <= c->n_entries);
  }
  CHECK_EQ(plasma_slaves[3].vendor_id, 0xD0BED1D0);
}

/// The cia402 profile on the ECT60's own assignment (no 0x603F in it).
static void test_cia402_on_the_ect60_layout(void) {
  const fake_capture_slave_t *c = &plasma_slaves[0];
  lcec_slave_t *s = fake_capture_slave("x", c, NULL);
  CHECK_EQ(c->product_code, 0x0A880002);
  CHECK_EQ(ecat_cia402_init(0, s), 0);
  fake_set(s, 0x6061, 0, 8);
  CHECK_EQ(cycle(s, 0x0250), 0x06);
  PIN_BIT("x.enable") = 1;
  CHECK_EQ(cycle(s, 0x0231), 0x07);
  CHECK_EQ(cycle(s, 0x0233), 0x0F);
  CHECK_EQ(cycle(s, 0x0237), 0x0F);
  CHECK_EQ(PIN_S32("x.cia-state"), 4);
}

/// Fed the inputs the ECT60s sent in OP (not enabled), the profile writes
/// what it wrote on the bus then: Shutdown, CSP, the actual position held.
static void test_cia402_writes_what_was_on_the_bus(void) {
  int i, n;
  for (i = 0; i < 3; i++) {
    const fake_capture_slave_t *c = &plasma_op_slaves[i];
    lcec_slave_t *s = fake_capture_slave("x", c, NULL);
    CHECK_EQ(ecat_cia402_init(0, s), 0);
    CHECK(c->n_cycles >= 10);
    for (n = 0; n < c->n_cycles; n++) {
      CHECK_EQ(fake_capture_inputs(s, c, n), 1);
      s->proc_read(s, PERIOD);
      s->proc_write(s, PERIOD);
      if (n < 2) continue;  // the status words of the cycles before
      CHECK_EQ(fake_get(s, 0x6040, 0), fake_capture_output(c, n, 0x6040, 0));
      CHECK_EQ(fake_get(s, 0x6060, 0), fake_capture_output(c, n, 0x6060, 0));
      CHECK_EQ(fake_get(s, 0x607A, 0), fake_capture_output(c, n, 0x607A, 0));
    }
    CHECK_EQ(PIN_S32("x.cia-state"), 2);
  }
}

// A captured slave by hand: one output, two inputs, two cycles.
static const fake_entry_t two_entries[] = {{0x7000, 1, 16}, {0x6000, 1, 8}, {0x6000, 2, 32}, FAKE_END};
static const uint32_t two_values[] = {
    0x11, 0x22, 0x33333333,
    0x44, 0x55, 0x66666666,
};
static const fake_capture_slave_t two = {0, 2, 0x1234, 1, "two", two_entries, 3, 1, 2, two_values};
static const fake_capture_slave_t none = {0, 2, 0x1234, 1, "none", two_entries, 3, 1, 0, NULL};

static void test_replay_of_cycles(void) {
  lcec_slave_t *s = fake_capture_slave("t", &two, NULL);
  fake_set(s, 0x7000, 1, 0x99);
  CHECK_EQ(fake_capture_inputs(s, &two, 0), 1);
  CHECK_EQ(fake_get(s, 0x6000, 1), 0x22);
  CHECK_EQ(fake_get(s, 0x6000, 2), 0x33333333);
  CHECK_EQ(fake_get(s, 0x7000, 1), 0x99);  // outputs are left alone
  CHECK_EQ(fake_capture_inputs(s, &two, 3), 1);  // counted round
  CHECK_EQ(fake_get(s, 0x6000, 1), 0x55);
  CHECK_EQ(fake_capture_output(&two, 1, 0x7000, 1), 0x44);
  CHECK_EQ(fake_capture_output(&two, 2, 0x7000, 1), 0x11);
  CHECK_EQ(fake_capture_output(&two, 0, 0x6000, 1), -1);  // an input
  CHECK_EQ(fake_capture_inputs(s, &none, 0), 0);
  CHECK_EQ(fake_capture_output(&none, 0, 0x7000, 1), -1);
}

RUN_TESTS("captured", test_every_captured_slave_can_be_made, test_cia402_on_the_ect60_layout,
    test_cia402_writes_what_was_on_the_bus, test_replay_of_cycles)
