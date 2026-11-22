//
//    ecat: read the configuration file (ethercat.yaml).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

/// @file
/// @brief Read ethercat.yaml into lcec's configuration records.
///
/// lcec read its configuration as a stream of LCEC_CONF_* records that
/// lcec_conf made from the XML file and put in shared memory. ecat makes the
/// same stream from its YAML file (docs/reference.md), in memory, so the
/// rest of the code is unchanged.
///
/// Two differences from lcec's stream: the master's appTimePeriod is 0 (the
/// servo thread's period is used, see lcec_read_master), and a slave's DC
/// sync0Cycle is a number of servo periods until that period is known.

#ifndef _ECAT_CONFIG_H_
#define _ECAT_CONFIG_H_

#include <stddef.h>
#include <stdint.h>

/// @brief Read a configuration file.
/// @return A malloc'd buffer of LCEC_CONF_* records ending with
/// lcecConfTypeNone, or NULL after printing an error. Free it with free().
char *ecat_config_load(const char *path, size_t *length);

/// @brief A slave's SYNC1 register value (ESC 0x09A4, ETG.2000's CycleTime1) for
/// SYNC1 every n SYNC0 cycles of sync0_ns, shift_ns after SYNC0; 0 for n = 0.
/// The configuration's sync1Cycle is n until the SYNC0 cycle is known.
uint32_t ecat_sync1_cycle(uint32_t n, uint32_t sync0_ns, int32_t shift_ns);

#endif
