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

#ifndef _LCEC_GENERIC_H_
#define _LCEC_GENERIC_H_

#include "../lcec.h"
#include "../lcec_conf.h"

typedef struct {
  char name[LCEC_CONF_STR_MAXLEN];
  hal_type_t type;
  LCEC_PDOENT_TYPE_T subType;
  rtapi_real floatScale;
  rtapi_real floatOffset;
  uint8_t bitOffset;
  uint8_t bitLength;
  uint8_t invert;  // bit pins: the pin is the opposite of the bit
  uint8_t extend;  // input counters: the pin counts on past the wrap, in 64 bits
  uint8_t raw;     // a bit pattern: masked to the entry's width when written, not held at its top
  uint8_t extend_have;     // extend_value is valid
  int64_t extend_value;    // the counter so far, as the pin shows it
  hal_pdir_t dir;
  void *pin[LCEC_CONF_GENERIC_MAX_SUBPINS];
  uint16_t pdo_idx;
  uint8_t pdo_sidx;
  unsigned int pdo_os;
  unsigned int pdo_bp;
} lcec_generic_pin_t;

int lcec_generic_init(int comp_id, struct lcec_slave *slave);
void lcec_generic_read(struct lcec_slave *slave, long period);
void lcec_generic_write(struct lcec_slave *slave, long period);

#endif
