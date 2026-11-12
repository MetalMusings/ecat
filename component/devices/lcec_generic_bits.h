//
//  Bit pins of generic entries, with an optional inversion.
//
//  A bit pin can be inverted (invert: in ethercat.yaml): it reads as the
//  opposite of the bit in the process data, and writes the opposite of the
//  pin. For normally closed switches, so that no `not` is needed in HAL.
//  Kept apart from lcec_generic.c so that the unit tests can use it.
//

#ifndef _LCEC_GENERIC_BITS_H_
#define _LCEC_GENERIC_BITS_H_

#include <stdint.h>

#include "ecrt.h"

/// The bit at bit offset `offset` of the process data, inverted if asked.
static inline int lcec_generic_read_bit(const uint8_t *pd, int offset, int invert) {
  return (EC_READ_BIT(&pd[offset >> 3], offset & 0x07) ? 1 : 0) ^ (invert ? 1 : 0);
}

/// Write a pin's value to that bit, inverted if asked. Any non-zero pin
/// value counts as 1.
static inline void lcec_generic_write_bit(uint8_t *pd, int offset, int invert, int value) {
  EC_WRITE_BIT(&pd[offset >> 3], offset & 0x07, ((value != 0) ? 1 : 0) ^ (invert ? 1 : 0));
}

#endif
