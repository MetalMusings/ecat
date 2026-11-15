//
//    A serial channel's port with a simulated terminal behind it and a
//    simulated Modbus RTU slave on its wire, until stopped (SIGINT,
//    SIGTERM): for trying a real program, such as mb2hal in halrun, on the
//    port without a terminal. scripts/serial-mb2hal-test uses it.
//
//    serial_sim_port PORT FILE_BAUD SLAVE_BAUD FRAME [continuous]
//      PORT        the port's path (a link to a pty is made there)
//      FILE_BAUD   the file's baud rate: the terminal starts at it
//      SLAVE_BAUD  the Modbus slave's (the program on the port must set it)
//      FRAME       the file's data frame, and the slave's (such as 8E1)
//
//    At the end it prints the requests answered, the bytes the slave could
//    not read (other settings), pauses on the wire and registers 0-15.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "../devices/ecat_serial_bridge.h"
#include "sim_el60xx.h"
#include "sim_modbus.h"

#define PERIOD 1000000L

static volatile sig_atomic_t stop;
static void on_signal(int sig) {
  (void)sig;
  stop = 1;
}


int main(int argc, char **argv) {
  static ecat_serial_channel_t ch;
  static sim_el60xx_t t;
  uint8_t out[2 + ECAT_SER_MAX_DATA] = {0}, baud, slave_baud, frame;
  ecat_ser_sdo_t sdo;
  struct timespec next;
  int bits = 8, stop_bits = 1, i;
  char parity = 'N';

  if (argc < 5 || (baud = ecat_ser_baud_value(strtoul(argv[2], NULL, 10))) == 0 ||
      (slave_baud = ecat_ser_baud_value(strtoul(argv[3], NULL, 10))) == 0 || strlen(argv[4]) != 3 ||
      (frame = ecat_ser_frame_value(argv[4][0] - '0', argv[4][1], argv[4][2] - '0')) == 0) {
    fprintf(stderr, "usage: %s PORT FILE_BAUD SLAVE_BAUD FRAME [continuous]\n", argv[0]);
    return 2;
  }
  for (i = 0; i < 64; i++) sim_modbus_regs[i] = (uint16_t)(0x1000 + i);
  sim_modbus_reset(slave_baud, frame);
  sim_el60xx_init(&t, 22, baud, frame, 128, 864);
  t.wire = sim_modbus_wire;
  sdo = sim_el60xx_sdo(&t);
  ecat_ser_init(&ch.core, 22, 0x8000, baud, frame, &sdo);
  if (argc > 5 && !strcmp(argv[5], "continuous")) {
    ch.core.continuous = 1;
    t.obj_continuous = t.continuous = 1;
  }
  ecat_ser_frame_of(frame, &bits, &parity, &stop_bits);
  snprintf(ch.name, sizeof(ch.name), "sim.port");
  snprintf(ch.port, sizeof(ch.port), "%s", argv[1]);
  ch.data_bits = (char)bits;
  ch.parity = parity;
  if (ecat_serial_bridge_start(&ch) != 0) return 1;
  signal(SIGINT, on_signal);
  signal(SIGTERM, on_signal);
  printf("ready: %s\n", argv[1]);
  fflush(stdout);

  // the servo thread's part, every millisecond
  clock_gettime(CLOCK_MONOTONIC, &next);
  while (!stop) {
    ecat_ser_cycle(&ch.core, t.in, out, 1, PERIOD);
    sim_el60xx_step(&t, out, PERIOD);
    next.tv_nsec += PERIOD;
    if (next.tv_nsec >= 1000000000L) {
      next.tv_nsec -= 1000000000L;
      next.tv_sec++;
    }
    clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
  }
  ecat_serial_bridge_stop();
  printf("answered %d bad %d gaps %d baud %lu bursts %u\nregs", sim_modbus_frames, sim_modbus_bad, t.gaps,
      ecat_ser_baud_of(t.baud), ch.core.bursts);
  for (i = 0; i < 16; i++) printf(" %d:0x%04X", i, sim_modbus_regs[i]);
  printf("\n");
  return 0;
}
