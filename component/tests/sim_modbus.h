//
//    A simulated Modbus RTU slave on a simulated serial terminal's wire
//    (sim_el60xx.h), for the serial channels' tests: slave 1, holding
//    registers 0-63, functions 3, 6 and 16. Bytes at other settings than
//    its own are garbled: it hears nothing then.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#ifndef _SIM_MODBUS_H_
#define _SIM_MODBUS_H_

#include "sim_el60xx.h"

extern uint16_t sim_modbus_regs[64];
extern uint8_t sim_modbus_baud, sim_modbus_frame;  // its settings (0x8000:11, :15 values)
extern int sim_modbus_frames, sim_modbus_bad;      // requests answered; bytes at other settings

/// Start over (registers kept).
void sim_modbus_reset(uint8_t baud, uint8_t frame);

/// The terminal's wire (sim_el60xx_t.wire).
void sim_modbus_wire(sim_el60xx_t *t, const uint8_t *bytes, size_t n, void *ctx);

#endif
