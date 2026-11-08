//
//    ecat: the encoder function, for incremental encoder channels laid out
//    as in ETG.5001 MDP 511 (Beckhoff EL5101, EL5151, EL5152, EL7041, ...).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#ifndef _ECAT_ENCODER_H_
#define _ECAT_ENCODER_H_

#include "../lcec.h"

/// modParam ids of encoder channels: ECAT_MP_ENCODER + the channel's number,
/// (status index - 0x6000) / 0x10. The value is the channel's pin name.
#define ECAT_MP_ENCODER 0x4100
#define ECAT_MP_ENCODER_LAST (ECAT_MP_ENCODER + 0xFF)

/// Set up the slave's encoder channels, if it has any, after its driver
/// or profile is set up: their pins, and read and write functions that
/// run after the slave's own.
int ecat_encoder_init(int comp_id, lcec_slave_t *slave);

#endif
