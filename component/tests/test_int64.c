//
//    Unit tests of 64-bit integer entries as float pins
//    (devices/lcec_generic_int64.h).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <string.h>

#include "../devices/lcec_generic_int64.h"
#include "fake.h"

static void test_int64_read(void) {
  uint8_t d[8] = {0x01, 0, 0, 0, 0, 0, 0x20, 0};  // 2^53 + 1, little endian
  CHECK_NEAR(lcec_generic_read_int64(d, 0), 9007199254740992.0, 1.0);
  memset(d, 0xff, sizeof(d));
  CHECK_NEAR(lcec_generic_read_int64(d, 0), -1.0, 0);
  CHECK_NEAR(lcec_generic_read_int64(d, 1), 18446744073709551615.0, 0);
  memset(d, 0, sizeof(d));
  d[4] = 1;  // 2^32: beyond what a 32-bit read would see
  CHECK_NEAR(lcec_generic_read_int64(d, 0), 4294967296.0, 0);
}

static void test_int64_write(void) {
  uint8_t d[8];
  lcec_generic_write_int64(d, 0, -1234567890123.7);
  CHECK(EC_READ_S64(d) == -1234567890123LL);
  lcec_generic_write_int64(d, 0, 1e30);
  CHECK(EC_READ_S64(d) == INT64_MAX);
  lcec_generic_write_int64(d, 0, -1e30);
  CHECK(EC_READ_S64(d) == INT64_MIN);
  lcec_generic_write_int64(d, 1, -5.0);
  CHECK(EC_READ_U64(d) == 0);
  lcec_generic_write_int64(d, 1, 1e30);
  CHECK(EC_READ_U64(d) == UINT64_MAX);
  lcec_generic_write_int64(d, 1, 4294967296.0 * 3);
  CHECK(EC_READ_U64(d) == 12884901888ULL);
  lcec_generic_write_int64(d, 0, 0.0 / 0.0);
  CHECK(EC_READ_S64(d) == 0);
}

RUN_TESTS("int64", test_int64_read, test_int64_write)
