//
//  64-bit integer entries (LINT, ULINT) as float pins.
//
//  LinuxCNC 2.9 has no 64-bit HAL pins, so such an entry is only given as
//  a float pin (a double: exact up to 2^53). Kept apart from
//  lcec_generic.c so that the unit tests can use it.
//

#ifndef _LCEC_GENERIC_INT64_H_
#define _LCEC_GENERIC_INT64_H_

#include <stdint.h>

#include "ecrt.h"

/// A 64-bit entry's value as a double.
static inline double lcec_generic_read_int64(const uint8_t *data, int is_unsigned) {
  return is_unsigned ? (double)EC_READ_U64(data) : (double)EC_READ_S64(data);
}

/// Write a double to a 64-bit entry, rounded toward zero and held within
/// the type's range; NaN writes 0.
static inline void lcec_generic_write_int64(uint8_t *data, int is_unsigned, double v) {
  if (v != v) v = 0.0;
  if (is_unsigned) {
    uint64_t u;
    if (v <= 0.0) {
      u = 0;
    } else if (v >= 18446744073709551616.0) {
      u = UINT64_MAX;
    } else {
      u = (uint64_t)v;
    }
    EC_WRITE_U64(data, u);
  } else {
    int64_t s;
    if (v >= 9223372036854775808.0) {
      s = INT64_MAX;
    } else if (v <= -9223372036854775808.0) {
      s = INT64_MIN;
    } else {
      s = (int64_t)v;
    }
    EC_WRITE_S64(data, s);
  }
}

#endif
