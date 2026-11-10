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
/// same stream from its YAML file (docs/component.md), in memory, so the
/// rest of the code is unchanged.
///
/// Two differences from lcec's stream: the master's appTimePeriod is 0 (the
/// servo thread's period is used, see lcec_read_master), and a slave's DC
/// sync0Cycle is a number of servo periods until that period is known.

#ifndef _ECAT_CONFIG_H_
#define _ECAT_CONFIG_H_

#include <stddef.h>

/// @brief Read a configuration file.
/// @return A malloc'd buffer of LCEC_CONF_* records ending with
/// lcecConfTypeNone, or NULL after printing an error. Free it with free().
char *ecat_config_load(const char *path, size_t *length);

#endif
