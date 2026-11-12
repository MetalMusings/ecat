//
//    Unit tests of the encoder function (devices/ecat_encoder.c).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <errno.h>
#include <string.h>

#include "../devices/ecat_encoder.h"
#include "fake.h"

#define PERIOD 1000000L  // ns

// An EL5101 channel with a 32-bit counter, as EtherCAT Setup proposes it.
static const fake_entry_t el5101[] = {
    {0x7000, 0x01, 1}, {0x7000, 0x02, 1}, {0x7000, 0x03, 1}, {0x7000, 0x04, 1}, {0x7000, 0x11, 32},
    {0x6000, 0x01, 1}, {0x6000, 0x02, 1}, {0x6000, 0x03, 1}, {0x6000, 0x11, 32}, {0x6000, 0x12, 32},
    FAKE_END,
};

// Only the counter, 16 bits.
static const fake_entry_t counter16[] = {{0x6000, 0x11, 16}, FAKE_END};

static lcec_slave_t *S;
static int own_reads, own_writes, check_order, order, own_write_order, enc_write_order;

static void own_read(lcec_slave_t *slave, long period) {
  (void)slave, (void)period;
  own_reads++;
}

static void own_write(lcec_slave_t *slave, long period) {
  (void)period;
  own_writes++;
  own_write_order = ++order;
  // the encoder's outputs are written before the slave's own write
  if (check_order && fake_get(slave, 0x7000, 0x01)) enc_write_order = order - 1;
}

/// A slave with one channel named "enc" at 0x6000, whose counter reads raw.
static lcec_slave_t *encoder(const fake_entry_t *entries, uint32_t raw) {
  lcec_slave_modparam_t mp[2] = {{.id = ECAT_MP_ENCODER}, {.id = -1}};
  strcpy(mp[0].value.str, "enc");
  S = fake_slave("e", entries, mp);
  S->proc_read = own_read;
  S->proc_write = own_write;
  own_reads = own_writes = 0;
  CHECK_EQ(ecat_encoder_init(0, S), 0);
  fake_set(S, 0x6000, 0x11, raw);
  return S;
}

static void cycle(void) {
  S->proc_read(S, PERIOD);
  S->proc_write(S, PERIOD);
}

/// Move the counter by d and run a cycle.
static void move(int32_t d) {
  fake_set(S, 0x6000, 0x11, fake_get(S, 0x6000, 0x11) + (uint32_t)d);
  cycle();
}

// ---- tests -----------------------------------------------------------

static void test_no_channels(void) {
  S = fake_slave("e", el5101, NULL);
  CHECK_EQ(ecat_encoder_init(0, S), 0);
  CHECK(S->proc_read == NULL && S->encoder_data == NULL);
  CHECK(fake_find("e.enc.counts") == NULL);
}

static void test_pins_follow_the_mapping(void) {
  encoder(el5101, 0);
  CHECK(fake_find("e.enc.counts") && fake_find("e.enc.pos") && fake_find("e.enc.velocity"));
  CHECK(fake_find("e.enc.index-enable") != NULL);
  CHECK(fake_find("e.enc.latch-enable-pos") && fake_find("e.enc.latch-pos"));
  CHECK_NEAR(PIN_FLOAT("e.enc.pos-scale"), 1.0, 0);
  CHECK_EQ(PIN_U32("e.enc.velocity-window-ms"), 10);
  // no index or latch pins without latch C and the latch value
  encoder(counter16, 0);
  CHECK(fake_find("e.enc.counts") != NULL);
  CHECK(fake_find("e.enc.index-enable") == NULL);
  CHECK(fake_find("e.enc.latch-pos") == NULL);
}

static void test_latch_must_match_the_counter(void) {
  static const fake_entry_t e[] = {{0x6000, 0x11, 32}, {0x6000, 0x12, 16}, FAKE_END};
  lcec_slave_modparam_t mp[2] = {{.id = ECAT_MP_ENCODER}, {.id = -1}};
  strcpy(mp[0].value.str, "enc");
  S = fake_slave("e", e, mp);
  CHECK_EQ(ecat_encoder_init(0, S), -EINVAL);
  CHECK(strstr(fake_msgs, "0x6000:12") != NULL);
}

static void test_starts_at_zero(void) {
  encoder(el5101, 1000);
  cycle();
  CHECK_EQ(PIN_S32("e.enc.counts"), 0);
  CHECK_EQ(PIN_S32("e.enc.raw-count"), 1000);
  CHECK_EQ(PIN_BIT("e.enc.ok"), 1);
  move(5);
  CHECK_EQ(PIN_S32("e.enc.counts"), 5);
  move(-12);
  CHECK_EQ(PIN_S32("e.enc.counts"), -7);
  CHECK_NEAR(PIN_FLOAT("e.enc.pos"), -7, 0);
}

static void test_16_bit_counter_is_extended(void) {
  int i;
  encoder(counter16, 0xFFF0);
  cycle();
  for (i = 0; i < 10; i++) move(0x3000);
  CHECK_EQ(PIN_S32("e.enc.counts"), 10 * 0x3000);
  CHECK_EQ(PIN_S32("e.enc.raw-count"), (int16_t)(uint16_t)(0xFFF0 + 10 * 0x3000));
  for (i = 0; i < 20; i++) move(-0x3000);
  CHECK_EQ(PIN_S32("e.enc.counts"), -10 * 0x3000);
  CHECK_NEAR(PIN_FLOAT("e.enc.pos"), -10 * 0x3000, 0);
}

static void test_32_bit_counter_is_extended(void) {
  encoder(el5101, 0xFFFFFFF0u);
  cycle();
  move(0x20);
  CHECK_EQ(PIN_S32("e.enc.counts"), 0x20);
  CHECK_EQ(PIN_S32("e.enc.raw-count"), 0x10);
  // beyond the 32-bit range, pos goes on
  move(0x40000000);
  move(0x40000000);
  move(0x40000000);
  CHECK_NEAR(PIN_FLOAT("e.enc.pos"), 3.0 * 0x40000000 + 0x20, 0);
}

static void test_scale_and_reset(void) {
  encoder(el5101, 0);
  PIN_FLOAT("e.enc.pos-scale") = 4;
  cycle();
  move(10);
  CHECK_NEAR(PIN_FLOAT("e.enc.pos"), 2.5, 1e-12);
  PIN_BIT("e.enc.reset") = 1;
  move(3);
  CHECK_EQ(PIN_S32("e.enc.counts"), 0);
  move(3);
  CHECK_EQ(PIN_S32("e.enc.counts"), 0);  // held at 0 while reset
  PIN_BIT("e.enc.reset") = 0;
  move(8);
  CHECK_NEAR(PIN_FLOAT("e.enc.pos"), 2.0, 1e-12);
  // a scale of 0 counts as 1
  PIN_FLOAT("e.enc.pos-scale") = 0;
  cycle();
  CHECK_NEAR(PIN_FLOAT("e.enc.pos"), 8, 0);
}

static void test_index(void) {
  encoder(el5101, 1000);
  cycle();
  move(50);
  // latch C valid is still set from an earlier index: not taken
  fake_set(S, 0x6000, 0x01, 1);
  fake_set(S, 0x6000, 0x12, 900);
  PIN_BIT("e.enc.index-enable") = 1;
  cycle();
  CHECK_EQ(PIN_BIT("e.enc.index-enable"), 1);
  CHECK_EQ(PIN_S32("e.enc.counts"), 50);
  CHECK_EQ(fake_get(S, 0x7000, 0x01), 1);  // latch C enabled
  // the terminal clears valid when latch C is enabled
  fake_set(S, 0x6000, 0x01, 0);
  move(20);
  CHECK_EQ(PIN_BIT("e.enc.index-enable"), 1);
  // the index, at 1100; the counter is at 1130 when it is seen
  fake_set(S, 0x6000, 0x01, 1);
  fake_set(S, 0x6000, 0x12, 1100);
  move(60);
  CHECK_EQ(PIN_BIT("e.enc.index-enable"), 0);
  CHECK_EQ(PIN_S32("e.enc.counts"), 30);
  CHECK_EQ(fake_get(S, 0x7000, 0x01), 0);
  // the position goes on from the index; the counter is not set
  move(-40);
  CHECK_EQ(PIN_S32("e.enc.counts"), -10);
  CHECK_EQ(PIN_S32("e.enc.raw-count"), 1090);
  CHECK_EQ(fake_get(S, 0x7000, 0x03), 0);
}

static void test_index_disarmed_when_index_enable_drops(void) {
  encoder(el5101, 0);
  cycle();
  PIN_BIT("e.enc.index-enable") = 1;
  cycle();  // valid false: armed
  PIN_BIT("e.enc.index-enable") = 0;
  cycle();
  // an index now is not taken, also if index-enable comes back with
  // valid already set
  fake_set(S, 0x6000, 0x01, 1);
  fake_set(S, 0x6000, 0x12, 0);
  move(10);
  PIN_BIT("e.enc.index-enable") = 1;
  move(10);
  CHECK_EQ(PIN_BIT("e.enc.index-enable"), 1);
  CHECK_EQ(PIN_S32("e.enc.counts"), 20);
}

static void test_external_latch(void) {
  encoder(el5101, 1000);
  PIN_FLOAT("e.enc.pos-scale") = 10;
  cycle();
  PIN_BIT("e.enc.latch-enable-pos") = 1;
  cycle();
  CHECK_EQ(fake_get(S, 0x7000, 0x02), 1);
  CHECK_EQ(fake_get(S, 0x7000, 0x04), 0);
  move(1000);
  fake_set(S, 0x6000, 0x02, 1);
  fake_set(S, 0x6000, 0x12, 1990);
  cycle();
  CHECK_NEAR(PIN_FLOAT("e.enc.latch-pos"), 99.0, 1e-12);
  CHECK_EQ(PIN_BIT("e.enc.latch-enable-pos"), 0);
  CHECK_EQ(fake_get(S, 0x7000, 0x02), 0);
  CHECK_NEAR(PIN_FLOAT("e.enc.pos"), 100.0, 1e-12);  // not moved by the latch
}

static void test_velocity(void) {
  int i;
  encoder(el5101, 0);
  PIN_FLOAT("e.enc.pos-scale") = 100;
  for (i = 0; i < 20; i++) cycle();
  CHECK_NEAR(PIN_FLOAT("e.enc.velocity"), 0, 0);
  // 10 counts per 1 ms: over the 10 ms window, 1000 counts/s per ms of motion
  for (i = 0; i < 5; i++) move(10);
  CHECK_NEAR(PIN_FLOAT("e.enc.velocity"), 50.0, 1e-9);
  for (i = 0; i < 20; i++) move(10);
  CHECK_NEAR(PIN_FLOAT("e.enc.velocity"), 100.0, 1e-9);
  // window 0: from the last cycle only
  PIN_U32("e.enc.velocity-window-ms") = 0;
  move(-3);
  CHECK_NEAR(PIN_FLOAT("e.enc.velocity"), -30.0, 1e-9);
  // a window longer than the history: the history
  PIN_U32("e.enc.velocity-window-ms") = 1000;
  for (i = 0; i < 300; i++) move(10);
  CHECK_NEAR(PIN_FLOAT("e.enc.velocity"), 100.0, 1e-9);
  // the parameter has 64 bits: a huge one is the whole history, not what its low
  // bits say (10 ms). A burst at the end tells them apart: 10 ms sees all of it
  // (about 370), the history only a small part (about 110).
  PIN_U32("e.enc.velocity-window-ms") = (1ULL << 32) + 10;
  for (i = 0; i < 3; i++) move(100);
  CHECK(PIN_FLOAT("e.enc.velocity") > 100.0 && PIN_FLOAT("e.enc.velocity") < 130.0);
  PIN_U32("e.enc.velocity-window-ms") = ~0ULL;
  move(10);
  CHECK(PIN_FLOAT("e.enc.velocity") > 100.0 && PIN_FLOAT("e.enc.velocity") < 130.0);
}

static void test_velocity_from_the_start(void) {
  encoder(el5101, 500);
  cycle();
  CHECK_NEAR(PIN_FLOAT("e.enc.velocity"), 0, 0);
  move(7);
  CHECK_NEAR(PIN_FLOAT("e.enc.velocity"), 7000.0, 1e-9);
}

static void test_bus_loss(void) {
  int i;
  encoder(el5101, 0);
  cycle();
  for (i = 0; i < 20; i++) move(10);
  PIN_BIT("e.enc.index-enable") = 1;
  cycle();  // armed
  fake_bus(S, 0);
  move(500);
  CHECK_EQ(PIN_BIT("e.enc.ok"), 0);
  CHECK_NEAR(PIN_FLOAT("e.enc.velocity"), 0, 0);
  CHECK_EQ(PIN_S32("e.enc.counts"), 200);  // held
  // back: it goes on from the held position, without the counts moved
  // meanwhile; the index is armed again only after valid is false
  fake_bus(S, 1);
  fake_set(S, 0x6000, 0x01, 1);
  cycle();
  CHECK_EQ(PIN_BIT("e.enc.ok"), 1);
  CHECK_EQ(PIN_S32("e.enc.counts"), 200);
  move(5);
  CHECK_EQ(PIN_S32("e.enc.counts"), 205);
  CHECK_EQ(PIN_BIT("e.enc.index-enable"), 1);
}

static void test_chained_with_the_slave(void) {
  encoder(el5101, 0);
  order = own_write_order = enc_write_order = 0;
  check_order = 1;
  PIN_BIT("e.enc.index-enable") = 1;
  cycle();
  CHECK_EQ(own_reads, 1);
  CHECK_EQ(own_writes, 1);
  CHECK_EQ(enc_write_order, own_write_order - 1);
  check_order = 0;
  // set counter is never written
  fake_set(S, 0x7000, 0x03, 1);
  fake_set(S, 0x7000, 0x11, 1234);
  cycle();
  CHECK_EQ(fake_get(S, 0x7000, 0x03), 0);
  CHECK_EQ(fake_get(S, 0x7000, 0x11), 0);
}

static void test_two_channels(void) {
  static const fake_entry_t e[] = {{0x6000, 0x11, 32}, {0x6010, 0x11, 32}, FAKE_END};
  lcec_slave_modparam_t mp[3] = {{.id = ECAT_MP_ENCODER}, {.id = ECAT_MP_ENCODER + 1}, {.id = -1}};
  strcpy(mp[0].value.str, "a");
  strcpy(mp[1].value.str, "b");
  S = fake_slave("e", e, mp);
  CHECK_EQ(ecat_encoder_init(0, S), 0);
  cycle();
  fake_set(S, 0x6000, 0x11, 5);
  fake_set(S, 0x6010, 0x11, (uint32_t)-9);
  cycle();
  CHECK_EQ(PIN_S32("e.a.counts"), 5);
  CHECK_EQ(PIN_S32("e.b.counts"), -9);
}

RUN_TESTS("encoder", test_no_channels, test_pins_follow_the_mapping, test_latch_must_match_the_counter,
    test_starts_at_zero, test_16_bit_counter_is_extended, test_32_bit_counter_is_extended, test_scale_and_reset,
    test_index, test_index_disarmed_when_index_enable_drops, test_external_latch, test_velocity,
    test_velocity_from_the_start, test_bus_loss, test_chained_with_the_slave, test_two_channels)
