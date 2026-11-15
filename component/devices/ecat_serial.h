//
//    ecat: the serial function, for Beckhoff's serial terminals (EL6001,
//    EL6002, EL6021, EL6022): each channel a serial port on the host (a
//    pty with a fixed name), see ecat_serial_core.h for the handshake.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#ifndef _ECAT_SERIAL_H_
#define _ECAT_SERIAL_H_

#include "../lcec.h"
#include "ecat_serial_core.h"

/// modParam ids of serial channels: ECAT_MP_SERIAL + 8 * channel + one of
/// the ECAT_MP_SERIAL_* below. Channels are numbered in the file's order.
#define ECAT_MP_SERIAL 0x4200
#define ECAT_MP_SERIAL_MAX_CHANNELS 8
#define ECAT_MP_SERIAL_LAST (ECAT_MP_SERIAL + 8 * ECAT_MP_SERIAL_MAX_CHANNELS - 1)
#define ECAT_MP_SERIAL_NAME 0      // str: the pin prefix
#define ECAT_MP_SERIAL_PDOS 1      // u32: output PDO << 16 | input PDO
#define ECAT_MP_SERIAL_PORT 2      // str: the port's path, such as /dev/ttyECAT0
#define ECAT_MP_SERIAL_SETTINGS 3  // u32: settings index | baud value << 16 | frame value << 24

/// Set up the slave's serial channels, if it has any, after its driver or
/// profile and its encoder channels: their pins, SDO requests for settings
/// while running, and read and write functions that run after the slave's
/// own. The ports are opened by ecat_serial_bridge_start().
int ecat_serial_init(int comp_id, lcec_slave_t *slave);

/// The channels of all slaves, for the bridge (ecat_serial_bridge.c).
typedef struct ecat_serial_channel {
  ecat_ser_t core;
  char name[2 * LCEC_CONF_STR_MAXLEN];  // slave.channel, for messages
  char port[LCEC_CONF_STR_MAXLEN];
  char data_bits, parity;  // from the file (a pty does not pass them on)
  _Atomic int port_open;   // a program has the port open
  struct ecat_serial_channel *next;
} ecat_serial_channel_t;

extern ecat_serial_channel_t *ecat_serial_channels;

/// Free the channels (after the bridge has stopped and the servo thread no
/// longer runs the functions).
void ecat_serial_free(void);

#endif
