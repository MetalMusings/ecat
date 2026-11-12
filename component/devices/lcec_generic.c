//
//    Copyright (C) 2011 Sascha Ittner <sascha.ittner@modusoft.de>
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
//    You should have received a copy of the GNU General Public License
//    along with this program; if not, write to the Free Software
//    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
//

/// @file
/// @brief Driver for Generic devices, controlled by XML

#include "lcec_generic.h"
#include "lcec_generic_bits.h"
#include "lcec_generic_int64.h"

#include "../lcec.h"

static lcec_typelist_t types[] = {
    {"generic", 0, 0, 0, NULL, lcec_generic_init},
    {NULL},
};
ADD_TYPES(types)

void lcec_generic_read(lcec_slave_t *slave, long period);
void lcec_generic_write(lcec_slave_t *slave, long period);

int64_t lcec_generic_read_sint(uint8_t *pd, lcec_generic_pin_t *hal_data);
uint64_t lcec_generic_read_uint(uint8_t *pd, lcec_generic_pin_t *hal_data);
void lcec_generic_write_sint(uint8_t *pd, lcec_generic_pin_t *hal_data, int64_t sval);
void lcec_generic_write_uint(uint8_t *pd, lcec_generic_pin_t *hal_data, uint64_t uval);

/// @brief Initialize a generic device.
///
/// Not static because it's called directly from `lcec_main`, unlike
/// "normal" devices.
int lcec_generic_init(int comp_id, lcec_slave_t *slave) {
  lcec_generic_pin_t *hal_data = (lcec_generic_pin_t *)slave->hal_data;
  int i, j;
  int err;

  // initialize callbacks
  slave->proc_read = lcec_generic_read;
  slave->proc_write = lcec_generic_write;

  // initialize pins
  for (i = 0; i < slave->generic_pdo_entry_count; i++, hal_data++) {
    // PDO mapping
    lcec_pdo_init(slave, hal_data->pdo_idx, hal_data->pdo_sidx, &hal_data->pdo_os, &hal_data->pdo_bp);
    if (hal_data->bitLength == 64 && (hal_data->pdo_bp != 0 || hal_data->bitOffset != 0)) {
      rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s.%s: a 64-bit entry must start on a byte\n", slave->name, hal_data->name);
      return -EINVAL;
    }

    switch (hal_data->type) {
      case HAL_BOOL:
        if (hal_data->bitLength == 1) {
          // single bit pin
          err = lcec_pin_newf(
              hal_data->type, hal_data->dir, &hal_data->pin[0], "%s.%s", slave->name, hal_data->name);
          if (err != 0) {
            return err;
          }
        } else {
          // bit pin array
          for (j = 0; j < LCEC_CONF_GENERIC_MAX_SUBPINS && j < hal_data->bitLength; j++) {
            err = lcec_pin_newf(hal_data->type, hal_data->dir, &hal_data->pin[j], "%s.%s-%d", slave->name, hal_data->name, j);
            if (err != 0) {
              return err;
            }
          }
        }
        break;

      case HAL_SINT:
      case HAL_UINT:
        // check data size: up to 32 bits anywhere, or 64 bits on a byte (LINT, ULINT)
        if (hal_data->bitLength > 32 && hal_data->bitLength != 64) {
          rtapi_print_msg(RTAPI_MSG_WARN, LCEC_MSG_PFX "unable to export pin %s.%s: invalid process data bitlen!\n", slave->name, hal_data->name);
          continue;
        }

        // export pin
        err = lcec_pin_newf(
            hal_data->type, hal_data->dir, &hal_data->pin[0], "%s.%s", slave->name, hal_data->name);
        if (err != 0) {
          return err;
        }
        break;

      case HAL_REAL:
        // check data size
        if ((hal_data->bitLength > 32) && (hal_data->subType != lcecPdoEntTypeFloatDoubleIeee)) {
          rtapi_print_msg(RTAPI_MSG_WARN, LCEC_MSG_PFX "unable to export pin %s.%s: invalid process data bitlen!\n", slave->name, hal_data->name);
          continue;
        }

        // export pin
        err = lcec_pin_newf(
            hal_data->type, hal_data->dir, &hal_data->pin[0], "%s.%s", slave->name, hal_data->name);
        if (err != 0) {
          return err;
        }
        break;

      default:
        rtapi_print_msg(RTAPI_MSG_WARN, LCEC_MSG_PFX "unsupported pin type %d!\n", hal_data->type);
    }
  }

  return 0;
}

/// @brief Read from a generic device.
void lcec_generic_read(lcec_slave_t *slave, long period) {
  lcec_master_t *master = slave->master;
  lcec_generic_pin_t *hal_data = (lcec_generic_pin_t *)slave->hal_data;
  uint8_t *pd = master->process_data;
  int i, j, offset;
  rtapi_real fval;

  // read data
  for (i = 0; i < slave->generic_pdo_entry_count; i++, hal_data++) {
    // skip wrong direction and uninitialized pins
    if (hal_data->dir != HAL_OUT || hal_data->pin[0] == NULL) {
      continue;
    }

    switch (hal_data->type) {
      case HAL_BOOL:
        offset = ((hal_data->pdo_os << 3) | (hal_data->pdo_bp & 0x07)) + hal_data->bitOffset;
        for (j = 0; j < LCEC_CONF_GENERIC_MAX_SUBPINS && hal_data->pin[j] != NULL; j++, offset++) {
          hal_set_bool((hal_bool_t)hal_data->pin[j], lcec_generic_read_bit(pd, offset, hal_data->invert));
        }
        break;

      case HAL_SINT:
        if (hal_data->extend) {
          // a counter that wraps at its width: count the wraps. The first value
          // is taken as signed (a counter that counts down past 0 is negative).
          int64_t low = lcec_generic_read_sint(pd, hal_data);
          hal_data->extend_value = hal_data->extend_have ? hal_extend_counter(hal_data->extend_value, low, hal_data->bitLength) : low;
          hal_data->extend_have = 1;
          hal_set_sint((hal_sint_t)hal_data->pin[0], hal_data->extend_value);
          break;
        }
        hal_set_sint((hal_sint_t)hal_data->pin[0], hal_data->bitLength == 64 ? EC_READ_S64(&pd[hal_data->pdo_os]) : lcec_generic_read_sint(pd, hal_data));
        break;

      case HAL_UINT:
        hal_set_uint((hal_uint_t)hal_data->pin[0], hal_data->bitLength == 64 ? EC_READ_U64(&pd[hal_data->pdo_os]) : lcec_generic_read_uint(pd, hal_data));
        break;

      case HAL_REAL:
        if (hal_data->bitLength == 64) {
          // LINT, ULINT: byte aligned, checked when the file is read
          fval = lcec_generic_read_int64(&pd[hal_data->pdo_os], hal_data->subType == lcecPdoEntTypeFloatUnsigned);
        } else if (hal_data->subType == lcecPdoEntTypeFloatUnsigned) {
          fval = lcec_generic_read_uint(pd, hal_data);
        } else if (hal_data->subType == lcecPdoEntTypeFloatIeee) {
          fval = EC_READ_REAL(&pd[hal_data->pdo_os]);
        } else if (hal_data->subType == lcecPdoEntTypeFloatDoubleIeee) {
          fval = EC_READ_LREAL(&pd[hal_data->pdo_os]);
        } else {
          fval = lcec_generic_read_sint(pd, hal_data);
        }

        fval *= hal_data->floatScale;
        fval += hal_data->floatOffset;
        hal_set_real((hal_real_t)hal_data->pin[0], fval);
        break;

      default:
        continue;
    }
  }
}

/// @brief Write to a generic device.
void lcec_generic_write(lcec_slave_t *slave, long period) {
  lcec_master_t *master = slave->master;
  lcec_generic_pin_t *hal_data = (lcec_generic_pin_t *)slave->hal_data;
  uint8_t *pd = master->process_data;
  int i, j, offset;
  rtapi_real fval;

  // write data
  for (i = 0; i < slave->generic_pdo_entry_count; i++, hal_data++) {
    // skip wrong direction and uninitialized pins
    if (hal_data->dir != HAL_IN || hal_data->pin[0] == NULL) {
      continue;
    }

    switch (hal_data->type) {
      case HAL_BOOL:
        offset = ((hal_data->pdo_os << 3) | (hal_data->pdo_bp & 0x07)) + hal_data->bitOffset;
        for (j = 0; j < LCEC_CONF_GENERIC_MAX_SUBPINS && hal_data->pin[j] != NULL; j++, offset++) {
          lcec_generic_write_bit(pd, offset, hal_data->invert, hal_get_bool((hal_bool_t)hal_data->pin[j]));
        }
        break;

      case HAL_SINT:
        if (hal_data->bitLength == 64) {
          EC_WRITE_S64(&pd[hal_data->pdo_os], hal_get_sint((hal_sint_t)hal_data->pin[0]));
        } else {
          lcec_generic_write_sint(pd, hal_data, hal_get_sint((hal_sint_t)hal_data->pin[0]));
        }
        break;

      case HAL_UINT:
        if (hal_data->bitLength == 64) {
          EC_WRITE_U64(&pd[hal_data->pdo_os], hal_get_uint((hal_uint_t)hal_data->pin[0]));
        } else {
          lcec_generic_write_uint(pd, hal_data, hal_get_uint((hal_uint_t)hal_data->pin[0]));
        }
        break;

      case HAL_REAL:
        fval = hal_get_real((hal_real_t)hal_data->pin[0]);
        fval += hal_data->floatOffset;
        fval *= hal_data->floatScale;

        if (hal_data->bitLength == 64) {
          lcec_generic_write_int64(&pd[hal_data->pdo_os], hal_data->subType == lcecPdoEntTypeFloatUnsigned, fval);
        } else if (hal_data->subType == lcecPdoEntTypeFloatUnsigned) {
          lcec_generic_write_uint(pd, hal_data, (uint64_t)fval);
        } else if (hal_data->subType == lcecPdoEntTypeFloatIeee) {
          EC_WRITE_REAL(&pd[hal_data->pdo_os], fval);
        } else if (hal_data->subType == lcecPdoEntTypeFloatDoubleIeee) {
          EC_WRITE_LREAL(&pd[hal_data->pdo_os], fval);
        } else {
          lcec_generic_write_sint(pd, hal_data, (int64_t)fval);
        }
        break;

      default:
        continue;
    }
  }
}

/// @brief Read a possibly-misaligned signed integer (up to 32 bits).
int64_t lcec_generic_read_sint(uint8_t *pd, lcec_generic_pin_t *hal_data) {
  int i, offset;
  int64_t sval;

  if (hal_data->pdo_bp == 0 && hal_data->bitOffset == 0) {
    switch (hal_data->bitLength) {
      case 8:
        return EC_READ_S8(&pd[hal_data->pdo_os]);
      case 16:
        return EC_READ_S16(&pd[hal_data->pdo_os]);
      case 32:
        return EC_READ_S32(&pd[hal_data->pdo_os]);
    }
  }

  offset = ((hal_data->pdo_os << 3) | (hal_data->pdo_bp & 0x07)) + hal_data->bitOffset;
  for (sval = 0, i = 0; i < hal_data->bitLength; i++, offset++) {
    if (EC_READ_BIT(&pd[offset >> 3], offset & 0x07)) {
      sval |= (1LL << i);
    }
  }

  // sign-extend shorter integers (the pin has 64 bits)
  if (hal_data->bitLength > 0 && hal_data->bitLength < 64 && (sval & (1LL << (hal_data->bitLength - 1)))) {
    sval |= ~0LL << hal_data->bitLength;
  }

  return sval;
}

/// @brief Read a possibly-misaligned unsigned integer (up to 32 bits).
uint64_t lcec_generic_read_uint(uint8_t *pd, lcec_generic_pin_t *hal_data) {
  int i, offset;
  uint64_t uval;

  if (hal_data->pdo_bp == 0 && hal_data->bitOffset == 0) {
    switch (hal_data->bitLength) {
      case 8:
        return EC_READ_U8(&pd[hal_data->pdo_os]);
      case 16:
        return EC_READ_U16(&pd[hal_data->pdo_os]);
      case 32:
        return EC_READ_U32(&pd[hal_data->pdo_os]);
    }
  }

  offset = ((hal_data->pdo_os << 3) | (hal_data->pdo_bp & 0x07)) + hal_data->bitOffset;
  for (uval = 0, i = 0; i < hal_data->bitLength; i++, offset++) {
    if (EC_READ_BIT(&pd[offset >> 3], offset & 0x07)) {
      uval |= (1ULL << i);
    }
  }
  return uval;
}

/// @brief Write a possibly-misaligned signed integer.
void lcec_generic_write_sint(uint8_t *pd, lcec_generic_pin_t *hal_data, int64_t sval) {
  int i, offset;

  int64_t lim = ((1LL << hal_data->bitLength) >> 1) - 1LL;  // the entry's range; the pin has 64 bits
  if (sval > lim) sval = lim;
  lim = ~lim;
  if (sval < lim) sval = lim;

  if (hal_data->pdo_bp == 0 && hal_data->bitOffset == 0) {
    switch (hal_data->bitLength) {
      case 8:
        EC_WRITE_S8(&pd[hal_data->pdo_os], sval);
        return;
      case 16:
        EC_WRITE_S16(&pd[hal_data->pdo_os], sval);
        return;
      case 32:
        EC_WRITE_S32(&pd[hal_data->pdo_os], sval);
        return;
    }
  }

  offset = ((hal_data->pdo_os << 3) | (hal_data->pdo_bp & 0x07)) + hal_data->bitOffset;
  for (i = 0; i < hal_data->bitLength; i++, offset++) {
    EC_WRITE_BIT(&pd[offset >> 3], offset & 0x07, sval & 1);
    sval >>= 1;
  }
}

/// @brief Write a possibly-misaligned unsigned integer.
void lcec_generic_write_uint(uint8_t *pd, lcec_generic_pin_t *hal_data, uint64_t uval) {
  int i, offset;

  uint64_t lim = (1LL << hal_data->bitLength) - 1LL;
  // the pin has 64 bits: a number is held at the top of the entry, a bit pattern cut to its width
  if (hal_data->raw) {
    uval &= lim;
  } else if (uval > lim) {
    uval = lim;
  }

  if (hal_data->pdo_bp == 0 && hal_data->bitOffset == 0) {
    switch (hal_data->bitLength) {
      case 8:
        EC_WRITE_U8(&pd[hal_data->pdo_os], uval);
        return;
      case 16:
        EC_WRITE_U16(&pd[hal_data->pdo_os], uval);
        return;
      case 32:
        EC_WRITE_U32(&pd[hal_data->pdo_os], uval);
        return;
    }
  }

  offset = ((hal_data->pdo_os << 3) | (hal_data->pdo_bp & 0x07)) + hal_data->bitOffset;
  for (i = 0; i < hal_data->bitLength; i++, offset++) {
    EC_WRITE_BIT(&pd[offset >> 3], offset & 0x07, uval & 1);
    uval >>= 1;
  }
}
