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

/// modParam id for cia402.zero-at-start (value 1): positions count from
/// where the drive stands when the bus first reaches OP.
#define ECAT_MP_CIA402_ZERO_AT_START 0x4022

/// modParam id for one axis of a drive with several (cia402.axes; value:
/// the axis's pin prefix). Given once per axis, in order: axis n uses the
/// CiA 402 objects at index + n * ECAT_CIA402_AXIS_OFFSET.
#define ECAT_MP_CIA402_AXIS 0x4023
#define ECAT_CIA402_AXIS_OFFSET 0x800
#define ECAT_CIA402_MAX_AXES 4

/// Set up a cia402 slave: its generic pins, then the profile's pins.
int ecat_cia402_init(int comp_id, lcec_slave_t *slave);

#endif
