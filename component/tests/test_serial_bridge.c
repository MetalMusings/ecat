//
//    Tests of the serial channels' ports (devices/ecat_serial_bridge.c):
//    a program opens the port and talks through it, while a thread runs
//    the channel against a simulated terminal every millisecond, as the
//    servo thread would. Last, libmodbus (which mb2hal uses) reads and
//    writes registers of a simulated Modbus RTU slave on the terminal's
//    wire.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <errno.h>
#include <fcntl.h>
#include <modbus/modbus.h>
#include <poll.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#include "../devices/ecat_serial_bridge.h"
#include "fake.h"
#include "sim_el60xx.h"

#define PERIOD 1000000L
#define B_9600 6
#define B_19200 7
#define B_115200 10
#define F_8N1 3
#define F_8E1 4

static ecat_serial_channel_t *CH;
static sim_el60xx_t T;
static pthread_t servo;
static volatile int servo_stop;
static char dir[64], port[96];

static void *servo_main(void *arg) {
  uint8_t out[2 + ECAT_SER_MAX_DATA] = {0};
  struct timespec next;
  (void)arg;
  clock_gettime(CLOCK_MONOTONIC, &next);
  while (!servo_stop) {
    ecat_ser_cycle(&CH->core, T.in, out, 1, PERIOD);
    sim_el60xx_step(&T, out, PERIOD);
    next.tv_nsec += PERIOD;
    if (next.tv_nsec >= 1000000000L) {
      next.tv_nsec -= 1000000000L;
      next.tv_sec++;
    }
    clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
  }
  return NULL;
}

/// A channel at baud/frame from the file, its terminal, its port, the
/// servo thread and the bridge.
static int start(uint8_t baud, uint8_t frame) {
  ecat_ser_sdo_t sdo;
  int bits = 8, stop = 1;
  char parity = 'N';
  strcpy(dir, "/tmp/ecat-serial-XXXXXX");
  if (mkdtemp(dir) == NULL) abort();
  snprintf(port, sizeof(port), "%s/ttyECAT0", dir);
  CH = calloc(1, sizeof(*CH));
  sim_el60xx_init(&T, 22, baud, frame, 128, 864);
  sdo = sim_el60xx_sdo(&T);
  ecat_ser_init(&CH->core, 22, 0x8000, baud, frame, &sdo);
  ecat_ser_frame_of(frame, &bits, &parity, &stop);
  strcpy(CH->name, "com.port");
  strcpy(CH->port, port);
  CH->data_bits = (char)bits;
  CH->parity = parity;
  servo_stop = 0;
  pthread_create(&servo, NULL, servo_main, NULL);
  return ecat_serial_bridge_start(CH);
}

static void finish(void) {
  ecat_serial_bridge_stop();
  servo_stop = 1;
  pthread_join(servo, NULL);
  free(CH);
  rmdir(dir);
}

static int open_port(speed_t speed) {
  struct termios t;
  int fd = open(port, O_RDWR | O_NOCTTY);
  if (fd < 0) return -1;
  tcgetattr(fd, &t);
  cfmakeraw(&t);
  cfsetispeed(&t, speed);
  cfsetospeed(&t, speed);
  tcsetattr(fd, TCSANOW, &t);
  return fd;
}

static size_t read_all(int fd, uint8_t *b, size_t n, int ms) {
  size_t have = 0;
  struct pollfd p = {fd, POLLIN, 0};
  while (have < n && poll(&p, 1, ms) > 0) {
    ssize_t r = read(fd, b + have, n - have);
    if (r <= 0) break;
    have += (size_t)r;
  }
  return have;
}

static void wait_ms(int ms) { usleep((useconds_t)ms * 1000); }

// ---- tests ----

static void test_port_is_a_link_to_a_pty(void) {
  char target[64];
  ssize_t n;
  CHECK_EQ(start(B_9600, F_8N1), 0);
  n = readlink(port, target, sizeof(target) - 1);
  CHECK(n > 0);
  if (n > 0) {
    target[n] = 0;
    CHECK(!strncmp(target, "/dev/pts/", 9));
  }
  finish();
  CHECK(access(port, F_OK) != 0);  // removed at stop
}

static void test_a_file_is_not_replaced(void) {
  int fd;
  strcpy(dir, "/tmp/ecat-serial-XXXXXX");
  if (mkdtemp(dir) == NULL) abort();
  snprintf(port, sizeof(port), "%s/ttyECAT0", dir);
  fd = open(port, O_CREAT | O_WRONLY, 0644);
  close(fd);
  CH = calloc(1, sizeof(*CH));
  ecat_ser_init(&CH->core, 22, 0x8000, B_9600, F_8N1, NULL);
  strcpy(CH->name, "com.port");
  strcpy(CH->port, port);
  CHECK(ecat_serial_bridge_start(CH) != 0);
  CHECK(strstr(fake_msgs, "is not a link to a pty: not replaced") != NULL);
  ecat_serial_bridge_stop();
  unlink(port);
  rmdir(dir);
  free(CH);
}

static void test_loopback_and_the_programs_baud_rate(void) {
  static uint8_t data[2000], got[2000];
  size_t i;
  int fd;
  CHECK_EQ(start(B_9600, F_8N1), 0);
  wait_ms(50);
  CHECK_EQ(T.baud, B_9600);
  fd = open_port(B115200);
  CHECK(fd >= 0);
  for (i = 0; i < sizeof(data); i++) data[i] = (uint8_t)(i * 13 + 5);
  CHECK_EQ(write(fd, data, sizeof(data)), (ssize_t)sizeof(data));
  // 2000 bytes at 115200: under 0.2 s, plus the change of settings
  CHECK_EQ(read_all(fd, got, sizeof(got), 1000), sizeof(got));
  CHECK(!memcmp(data, got, sizeof(data)));
  // the program's rate is in the terminal; parity and bits are the file's
  CHECK_EQ(T.baud, B_115200);
  CHECK_EQ(T.frame, F_8N1);
  CHECK_EQ(atomic_load(&CH->port_open), 1);
  close(fd);
  wait_ms(300);
  CHECK_EQ(atomic_load(&CH->port_open), 0);
  finish();
}

static void test_two_stop_bits_from_the_program(void) {
  struct termios t;
  int fd;
  uint8_t b = 0x55, got = 0;
  CHECK_EQ(start(B_19200, F_8E1), 0);
  fd = open_port(B19200);
  tcgetattr(fd, &t);
  t.c_cflag |= CSTOPB;
  tcsetattr(fd, TCSANOW, &t);
  CHECK_EQ(write(fd, &b, 1), 1);
  CHECK_EQ(read_all(fd, &got, 1, 1000), 1);
  CHECK_EQ(T.frame, 12);  // 8E2: parity from the file, stop bits from the program
  CHECK_EQ(T.baud, B_19200);
  close(fd);
  finish();
}

// ---- Modbus RTU: libmodbus against a simulated slave on the wire ----

static uint16_t regs[64];
static uint8_t rtu_in[300];
static size_t rtu_n;
static int rtu_frames, rtu_bad;

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

/// Slave 1 at 19200 8E1: function 3 (read holding registers), 6 (write
/// one), 16 (write several). A terminal at other settings garbles the
/// bytes: the slave hears nothing.
static void rtu_wire(sim_el60xx_t *t, const uint8_t *bytes, size_t n, void *ctx) {
  size_t need;
  (void)ctx;
  if (t->baud != B_19200 || t->frame != F_8E1) {
    rtu_bad++;
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
    rtu_frames++;
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

static void test_modbus_rtu_through_the_port(void) {
  modbus_t *mb;
  uint16_t got[40], set[20];
  int i;
  for (i = 0; i < 64; i++) regs[i] = (uint16_t)(0x1000 + i);
  rtu_n = 0;
  rtu_frames = rtu_bad = 0;
  // the file says 9600 8E1; mb2hal asks for 19200 (parity E as in the file)
  CHECK_EQ(start(B_9600, F_8E1), 0);
  T.wire = rtu_wire;
  mb = modbus_new_rtu(port, 19200, 'E', 8, 1);
  CHECK(mb != NULL);
  if (mb == NULL) {
    finish();
    return;
  }
  modbus_set_slave(mb, 1);
  modbus_set_response_timeout(mb, 1, 0);
  CHECK_EQ(modbus_connect(mb), 0);
  // the first request goes out after the change of rate
  CHECK_EQ(modbus_read_registers(mb, 5, 2, got), 2);
  CHECK_EQ(got[0], 0x1005);
  CHECK_EQ(got[1], 0x1006);
  CHECK_EQ(modbus_write_register(mb, 3, 0xBEEF), 1);
  CHECK_EQ(regs[3], 0xBEEF);
  // an answer of 85 bytes: four windows of the terminal
  CHECK_EQ(modbus_read_registers(mb, 0, 40, got), 40);
  CHECK_EQ(got[3], 0xBEEF);
  CHECK_EQ(got[39], 0x1027);
  // a request of 49 bytes: three windows
  for (i = 0; i < 20; i++) set[i] = (uint16_t)(0x2000 + i);
  CHECK_EQ(modbus_write_registers(mb, 10, 20, set), 20);
  CHECK_EQ(regs[29], 0x2013);
  for (i = 0; i < 50; i++) {
    if (modbus_read_registers(mb, 0, 4, got) != 4) break;
  }
  CHECK_EQ(i, 50);
  CHECK_EQ(rtu_bad, 0);
  CHECK_EQ(T.baud, B_19200);
  modbus_close(mb);
  modbus_free(mb);
  finish();
}

RUN_TESTS("serial bridge", test_port_is_a_link_to_a_pty, test_a_file_is_not_replaced,
    test_loopback_and_the_programs_baud_rate, test_two_stop_bits_from_the_program, test_modbus_rtu_through_the_port)
