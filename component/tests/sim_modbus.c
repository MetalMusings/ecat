//
//    A simulated Modbus RTU slave (see sim_modbus.h).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include "sim_modbus.h"

#include <string.h>

uint16_t sim_modbus_regs[64];
uint8_t sim_modbus_baud = 7, sim_modbus_frame = 4;
int sim_modbus_frames, sim_modbus_bad;
static uint8_t rtu_in[300];
static size_t rtu_n;

void sim_modbus_reset(uint8_t baud, uint8_t frame) {
  sim_modbus_baud = baud;
  sim_modbus_frame = frame;
  sim_modbus_frames = sim_modbus_bad = 0;
  rtu_n = 0;
}

static uint16_t crc16(const uint8_t *b, size_t n) {
  uint16_t crc = 0xFFFF;
  size_t i;
  int k;
  for (i = 0; i < n; i++) {
    crc ^= b[i];
    for (k = 0; k < 8; k++) crc = (crc & 1) ? (uint16_t)((crc >> 1) ^ 0xA001) : (uint16_t)(crc >> 1);
  }
  return crc;
}

static void rtu_answer(sim_el60xx_t *t, uint8_t *r, size_t n) {
  uint16_t crc = crc16(r, n);
  r[n] = crc & 0xff;
  r[n + 1] = crc >> 8;
  sim_el60xx_receive(t, r, n + 2);
}

void sim_modbus_wire(sim_el60xx_t *t, const uint8_t *bytes, size_t n, void *ctx) {
  uint16_t *regs = sim_modbus_regs;
  size_t need;
  (void)ctx;
  if (t->baud != sim_modbus_baud || t->frame != sim_modbus_frame) {
    sim_modbus_bad++;
    return;
  }
  if (rtu_n + n > sizeof(rtu_in)) rtu_n = 0;
  memcpy(rtu_in + rtu_n, bytes, n);
  rtu_n += n;
  if (rtu_n < 8) return;
  need = rtu_in[1] == 16 ? (size_t)9 + rtu_in[6] : 8;
  if (rtu_n < need) return;
  if (rtu_in[0] == 1 && crc16(rtu_in, need - 2) == (rtu_in[need - 2] | rtu_in[need - 1] << 8)) {
    uint8_t r[300];
    unsigned a = rtu_in[2] << 8 | rtu_in[3], q = rtu_in[4] << 8 | rtu_in[5], i;
    sim_modbus_frames++;
    r[0] = 1;
    r[1] = rtu_in[1];
    if (rtu_in[1] == 3 && a + q <= 64) {
      r[2] = (uint8_t)(2 * q);
      for (i = 0; i < q; i++) {
        r[3 + 2 * i] = regs[a + i] >> 8;
        r[4 + 2 * i] = regs[a + i] & 0xff;
      }
      rtu_answer(t, r, 3 + 2 * q);
    } else if (rtu_in[1] == 6 && a < 64) {
      regs[a] = (uint16_t)q;
      memcpy(r, rtu_in, 6);
      rtu_answer(t, r, 6);
    } else if (rtu_in[1] == 16 && a + q <= 64) {
      for (i = 0; i < q; i++) regs[a + i] = (uint16_t)(rtu_in[7 + 2 * i] << 8 | rtu_in[8 + 2 * i]);
      memcpy(r, rtu_in, 6);
      rtu_answer(t, r, 6);
    }
  }
  rtu_n = 0;
}
