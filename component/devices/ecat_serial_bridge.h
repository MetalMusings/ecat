//
//    ecat: the serial ports on the computer for the serial channels: a
//    thread (not real-time) that moves bytes between a pty per channel and
//    the channel's queues, and turns the baud rate and stop bits a program
//    sets on the port into new settings for the terminal.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#ifndef _ECAT_SERIAL_BRIDGE_H_
#define _ECAT_SERIAL_BRIDGE_H_

#include "ecat_serial.h"

/// Open a pty for every channel in the list, make each channel's port a
/// symbolic link to it, and start the thread. 0 if there are no channels.
/// A port path that exists and is not a link to a pty is an error.
int ecat_serial_bridge_start(ecat_serial_channel_t *channels);

/// Stop the thread, remove the links, close the ptys.
void ecat_serial_bridge_stop(void);

#endif
