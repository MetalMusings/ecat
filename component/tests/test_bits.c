//
//    Unit tests of bit pins with an optional inversion
//    (devices/lcec_generic_bits.h).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <string.h>

#include "../devices/lcec_generic_bits.h"
#include "fake.h"

static void test_read(void) {
  uint8_t pd[2] = {0x05, 0x80};  // bits 0, 2 and 15 set
  CHECK_EQ(lcec_generic_read_bit(pd, 0, 0), 1);
  CHECK_EQ(lcec_generic_read_bit(pd, 1, 0), 0);
  CHECK_EQ(lcec_generic_read_bit(pd, 15, 0), 1);
  // inverted: the opposite, across a byte boundary too
  CHECK_EQ(lcec_generic_read_bit(pd, 0, 1), 0);
  CHECK_EQ(lcec_generic_read_bit(pd, 1, 1), 1);
  CHECK_EQ(lcec_generic_read_bit(pd, 15, 1), 0);
  CHECK_EQ(lcec_generic_read_bit(pd, 14, 1), 1);
}

static void test_write(void) {
  uint8_t pd[2] = {0, 0};
  lcec_generic_write_bit(pd, 3, 0, 1);
  CHECK_EQ(pd[0], 0x08);
  lcec_generic_write_bit(pd, 3, 0, 0);
  CHECK_EQ(pd[0], 0x00);
  // inverted: a low pin sets the bit, a high one clears it; neighbours stay
  lcec_generic_write_bit(pd, 9, 1, 0);
  CHECK_EQ(pd[1], 0x02);
  lcec_generic_write_bit(pd, 8, 1, 1);
  CHECK_EQ(pd[1], 0x02);
  lcec_generic_write_bit(pd, 9, 1, 1);
  CHECK_EQ(pd[1], 0x00);
  // any non-zero pin value counts as 1
  lcec_generic_write_bit(pd, 0, 0, 7);
  CHECK_EQ(pd[0], 0x01);
  lcec_generic_write_bit(pd, 1, 1, 7);
  CHECK_EQ(pd[0], 0x01);
}

RUN_TESTS("bits", test_read, test_write)
