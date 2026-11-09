//
//    ecat: the cia402 profile, for CiA 402 (ETG.6010) drives.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#ifndef _ECAT_CIA402_H_
#define _ECAT_CIA402_H_

#include "../lcec.h"

/// modParam id for the cyclic mode of a cia402 slave (value: 8 CSP, 9 CSV).
#define ECAT_MP_CIA402_MODE 0x4020

/// modParam id for the other modes a cia402 slave may switch to (value: a
/// bit mask, bit n for mode n; only homing, bit 6, so far).
#define ECAT_MP_CIA402_MODES 0x4021

/// Set up a cia402 slave: its generic pins, then the profile's pins.
int ecat_cia402_init(int comp_id, lcec_slave_t *slave);

#endif
