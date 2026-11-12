//
//    Unit tests of the generic device (devices/lcec_generic.c): integer pins
//    are 64 bits in LinuxCNC 2.10, the process data entries 8, 16, 32 or
//    (LINT, ULINT) 64 bits. A value is held within the entry's range when
//    it is written; reading gives the entry's value with its sign.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <string.h>

#include "../devices/lcec_generic.h"
#include "fake.h"

void lcec_addtypes(lcec_typelist_t types[], const char *sourcefile) {
  (void)types, (void)sourcefile;
}

// libethercat is not linked into the tests: the IEEE float accessors
float ecrt_read_real(const void *data) { float v; memcpy(&v, data, sizeof(v)); return v; }
double ecrt_read_lreal(const void *data) { double v; memcpy(&v, data, sizeof(v)); return v; }
void ecrt_write_real(void *data, float value) { memcpy(data, &value, sizeof(value)); }
void ecrt_write_lreal(void *data, double value) { memcpy(data, &value, sizeof(value)); }

static const fake_entry_t entries[] = {
    {0x6000, 1, 8}, {0x6000, 2, 16}, {0x6000, 3, 32}, {0x6000, 4, 64}, {0x6000, 5, 12}, FAKE_END,
};

#define MAXPINS 8
static lcec_generic_pin_t gp[MAXPINS];

/// A slave whose generic entries are: one pin of `type` and `dir` on each
/// of the entries 0x6000:`first`..., `bits` wide, named "e0", "e1"...
static lcec_slave_t *generic_slave(int n, const uint8_t *subs, const uint8_t *bits, hal_type_t type, hal_pdir_t dir) {
  lcec_slave_t *s = fake_slave("g", entries, NULL);
  int i;
  memset(gp, 0, sizeof(gp));
  for (i = 0; i < n; i++) {
    snprintf(gp[i].name, sizeof(gp[i].name), "e%d", i);
    gp[i].type = type;
    gp[i].dir = dir;
    gp[i].bitLength = bits[i];
    gp[i].pdo_idx = 0x6000;
    gp[i].pdo_sidx = subs[i];
  }
  s->hal_data = gp;
  s->generic_pdo_entry_count = n;
  CHECK_EQ(lcec_generic_init(0, s), 0);
  return s;
}

static uint8_t *pd_of(lcec_slave_t *s, int i) { return s->master->process_data + gp[i].pdo_os; }

static void test_read_signed(void) {
  const uint8_t subs[] = {1, 2, 3}, bits[] = {8, 16, 32};
  lcec_slave_t *s = generic_slave(3, subs, bits, HAL_SINT, HAL_OUT);
  EC_WRITE_U8(pd_of(s, 0), 0xFE);
  EC_WRITE_U16(pd_of(s, 1), 0x8000);
  EC_WRITE_U32(pd_of(s, 2), 0xFFFFFFFE);
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e0"), -2);
  CHECK_EQ(PIN_S32("g.e1"), -32768);
  CHECK_EQ(PIN_S32("g.e2"), -2);  // the pin has 64 bits: the sign must be kept
  EC_WRITE_U32(pd_of(s, 2), 0x7FFFFFFF);
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e2"), 2147483647);
}

static void test_read_unsigned(void) {
  const uint8_t subs[] = {1, 2, 3}, bits[] = {8, 16, 32};
  lcec_slave_t *s = generic_slave(3, subs, bits, HAL_UINT, HAL_OUT);
  EC_WRITE_U8(pd_of(s, 0), 0xFE);
  EC_WRITE_U16(pd_of(s, 1), 0x8000);
  EC_WRITE_U32(pd_of(s, 2), 0xFFFFFFFE);
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_U32("g.e0"), 254);
  CHECK_EQ(PIN_U32("g.e1"), 32768);
  CHECK_EQ((long long)PIN_U32("g.e2"), 4294967294LL);  // not sign-extended into the 64 bits
}

static void test_write_is_held_within_the_entry(void) {
  const uint8_t subs[] = {1, 2, 3}, bits[] = {8, 16, 32};
  lcec_slave_t *s = generic_slave(3, subs, bits, HAL_SINT, HAL_IN);
  PIN_S32("g.e0") = 1LL << 40;
  PIN_S32("g.e1") = -(1LL << 40);
  PIN_S32("g.e2") = 1LL << 40;
  lcec_generic_write(s, 1000000);
  CHECK_EQ((int8_t)EC_READ_S8(pd_of(s, 0)), 127);
  CHECK_EQ(EC_READ_S16(pd_of(s, 1)), -32768);
  CHECK_EQ(EC_READ_S32(pd_of(s, 2)), 2147483647);
  PIN_S32("g.e0") = -5;
  PIN_S32("g.e2") = -(1LL << 40);
  lcec_generic_write(s, 1000000);
  CHECK_EQ(EC_READ_S8(pd_of(s, 0)), -5);
  CHECK_EQ(EC_READ_S32(pd_of(s, 2)), -2147483647 - 1);
}

static void test_unsigned_write_is_held_within_the_entry(void) {
  const uint8_t subs[] = {1, 2, 3}, bits[] = {8, 16, 32};
  lcec_slave_t *s = generic_slave(3, subs, bits, HAL_UINT, HAL_IN);
  PIN_U32("g.e0") = 1ULL << 40;
  PIN_U32("g.e1") = 70000;
  PIN_U32("g.e2") = 1ULL << 40;
  lcec_generic_write(s, 1000000);
  CHECK_EQ(EC_READ_U8(pd_of(s, 0)), 255);
  CHECK_EQ(EC_READ_U16(pd_of(s, 1)), 65535);
  CHECK_EQ((long long)EC_READ_U32(pd_of(s, 2)), 4294967295LL);
  PIN_U32("g.e2") = 4000000000ULL;
  lcec_generic_write(s, 1000000);
  CHECK_EQ((long long)EC_READ_U32(pd_of(s, 2)), 4000000000LL);
}

static void test_raw_bit_pattern_is_cut(void) {
  const uint8_t subs[] = {2, 3}, bits[] = {16, 32};
  lcec_slave_t *s = generic_slave(2, subs, bits, HAL_UINT, HAL_IN);
  gp[0].raw = gp[1].raw = 1;
  PIN_U32("g.e0") = 0x12345;  // bit 16 is not there
  PIN_U32("g.e1") = 0x1FFFF0003ULL;
  lcec_generic_write(s, 1000000);
  CHECK_EQ(EC_READ_U16(pd_of(s, 0)), 0x2345);
  CHECK_EQ((long long)EC_READ_U32(pd_of(s, 1)), 0xFFFF0003LL);
}

static void test_odd_width_signed(void) {
  // a 12-bit entry: not aligned to a width the fast path knows
  const uint8_t subs[] = {5}, bits[] = {12};
  lcec_slave_t *s = generic_slave(1, subs, bits, HAL_SINT, HAL_OUT);
  EC_WRITE_U16(pd_of(s, 0), 0x0FFF);
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e0"), -1);
  EC_WRITE_U16(pd_of(s, 0), 0x0800);
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e0"), -2048);
  EC_WRITE_U16(pd_of(s, 0), 0x07FF);
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e0"), 2047);
}

static void test_odd_width_write(void) {
  const uint8_t subs[] = {5}, bits[] = {12};
  lcec_slave_t *s = generic_slave(1, subs, bits, HAL_SINT, HAL_IN);
  PIN_S32("g.e0") = 100000;
  lcec_generic_write(s, 1000000);
  CHECK_EQ(EC_READ_U16(pd_of(s, 0)) & 0x0FFF, 0x07FF);
  PIN_S32("g.e0") = -100000;
  lcec_generic_write(s, 1000000);
  CHECK_EQ(EC_READ_U16(pd_of(s, 0)) & 0x0FFF, 0x0800);
}

static void test_misaligned_32_bits(void) {
  // 32 bits that start one bit into the entry: read bit by bit
  const uint8_t subs[] = {3}, bits[] = {32};
  lcec_slave_t *s = generic_slave(1, subs, bits, HAL_UINT, HAL_OUT);
  gp[0].bitOffset = 1;
  memset(pd_of(s, 0), 0xFF, 4);
  pd_of(s, 0)[4] = 0x01;  // bit 32 of the data
  lcec_generic_read(s, 1000000);
  CHECK_EQ((long long)PIN_U32("g.e0"), 4294967295LL);  // bit 31 must not be sign-extended
  s = generic_slave(1, subs, bits, HAL_SINT, HAL_OUT);
  gp[0].bitOffset = 1;
  memset(pd_of(s, 0), 0xFF, 4);
  pd_of(s, 0)[4] = 0x01;
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e0"), -1);
}

static void test_extended_counter(void) {
  const uint8_t subs[] = {2, 3}, bits[] = {16, 32};
  lcec_slave_t *s = generic_slave(2, subs, bits, HAL_SINT, HAL_OUT);
  gp[0].extend = gp[1].extend = 1;
  // counting up through the wrap of 16 bits, and of 32
  EC_WRITE_U16(pd_of(s, 0), 65530);
  EC_WRITE_U32(pd_of(s, 1), 0xFFFFFFF0);
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e0"), -6);  // the first value is signed
  CHECK_EQ(PIN_S32("g.e1"), -16);
  EC_WRITE_U16(pd_of(s, 0), 4);
  EC_WRITE_U32(pd_of(s, 1), 8);
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e0"), 4);
  CHECK_EQ(PIN_S32("g.e1"), 8);
  // on past 0x8000: no wrap in 64 bits
  EC_WRITE_U16(pd_of(s, 0), 0x7000);
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e0"), 0x7000);
  EC_WRITE_U16(pd_of(s, 0), 0x7000 + 0x6000);  // +0x6000: under half a turn
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e0"), 0xD000);
  EC_WRITE_U16(pd_of(s, 0), 0x2000);  // +0x5000, through the wrap
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e0"), 0x12000);
  // and down again
  EC_WRITE_U16(pd_of(s, 0), 0xF000);
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e0"), 0xF000);
  // 32 bits, down through 0
  EC_WRITE_U32(pd_of(s, 1), 2);
  lcec_generic_read(s, 1000000);
  EC_WRITE_U32(pd_of(s, 1), 0xFFFFFFFE);
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e1"), -2);
}

static void test_counter_without_extend_wraps(void) {
  const uint8_t subs[] = {3}, bits[] = {32};
  lcec_slave_t *s = generic_slave(1, subs, bits, HAL_SINT, HAL_OUT);
  EC_WRITE_U32(pd_of(s, 0), 0x7FFFFFFF);
  lcec_generic_read(s, 1000000);
  EC_WRITE_U32(pd_of(s, 0), 0x80000000);
  lcec_generic_read(s, 1000000);
  CHECK_EQ(PIN_S32("g.e0"), -2147483647 - 1);  // as it was: the entry's own value
}

static void test_64_bit_entries(void) {
  const uint8_t subs[] = {4}, bits[] = {64};
  lcec_slave_t *s = generic_slave(1, subs, bits, HAL_SINT, HAL_OUT);
  EC_WRITE_S64(pd_of(s, 0), -1234567890123456789LL);
  lcec_generic_read(s, 1000000);
  CHECK(PIN_S32("g.e0") == -1234567890123456789LL);  // exact: no float in between
  s = generic_slave(1, subs, bits, HAL_UINT, HAL_OUT);
  EC_WRITE_U64(pd_of(s, 0), 18446744073709551615ULL);
  lcec_generic_read(s, 1000000);
  CHECK(PIN_U32("g.e0") == 18446744073709551615ULL);
  s = generic_slave(1, subs, bits, HAL_SINT, HAL_IN);
  PIN_S32("g.e0") = -9223372036854775807LL - 1;
  lcec_generic_write(s, 1000000);
  CHECK(EC_READ_S64(pd_of(s, 0)) == -9223372036854775807LL - 1);
  s = generic_slave(1, subs, bits, HAL_UINT, HAL_IN);
  PIN_U32("g.e0") = 18446744073709551615ULL;
  lcec_generic_write(s, 1000000);
  CHECK(EC_READ_U64(pd_of(s, 0)) == 18446744073709551615ULL);
}

RUN_TESTS("generic", test_read_signed, test_read_unsigned, test_raw_bit_pattern_is_cut, test_write_is_held_within_the_entry,
    test_unsigned_write_is_held_within_the_entry, test_odd_width_signed, test_odd_width_write, test_misaligned_32_bits, test_extended_counter, test_counter_without_extend_wraps, test_64_bit_entries)
