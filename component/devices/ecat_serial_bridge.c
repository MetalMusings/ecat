//
//    ecat: the serial ports on the computer (see ecat_serial_bridge.h).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//
//    This program is distributed in the hope that it will be useful,
//    but WITHOUT ANY WARRANTY; without even the implied warranty of
//    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
//    GNU General Public License for more details.
//

/// @file
/// @brief Serial channels as ptys.
///
/// Each channel gets a pty; its port path (from the file, such as
/// /dev/ttyECAT0) is a symbolic link to the pty's /dev/pts/N, which a
/// program opens like any serial port. The thread reads what the program
/// writes into the channel's transmit queue and writes what the terminal
/// received to the program.
///
/// A pty passes on the baud rate and the stop bits a program sets
/// (tcsetattr), but not the parity and data bits (the Linux pty driver
/// keeps CS8 without parity): those come from the file. Before the bytes
/// a program wrote are queued, the thread compares the port's settings
/// with the last it asked for; when they differ, it asks the channel for
/// new ones and waits until they are in the terminal, so that bytes
/// written after tcsetattr() go out with the new settings. (Bytes written
/// before it but not yet read by the thread go out with them too: programs
/// set the port before they write, as libmodbus does.)

#include "ecat_serial_bridge.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <sys/fsuid.h>
#include <sys/stat.h>
#include <termios.h>
#include <unistd.h>

#define MAX_PORTS 64
#define POLL_MS 1          // the thread also drains the receive queues
#define HUNG_CHECK_MS 100  // how often a port nobody has open is looked at

typedef struct {
  ecat_serial_channel_t *ch;
  int fd;          // pty master
  char pts[64];    // its /dev/pts/N
  int linked;      // the port path is our link
  int open;        // a program has the port open (last seen)
  int waiting;     // for new settings to be in the terminal
  uint8_t asked_baud, asked_frame;
  speed_t last_speed;
  int last_stop;
  int warned_baud;
  uint8_t pending[256];  // received bytes the pty did not take yet
  size_t pending_n;
  long hung_ms;
} port_t;

static port_t ports[MAX_PORTS];
static int n_ports;
static pthread_t thread;
static int running;
static volatile int stop;

static unsigned long speed_to_baud(speed_t s) {
  switch (s) {
    case B300: return 300;
    case B600: return 600;
    case B1200: return 1200;
    case B2400: return 2400;
    case B4800: return 4800;
    case B9600: return 9600;
    case B19200: return 19200;
    case B38400: return 38400;
    case B57600: return 57600;
    case B115200: return 115200;
    case B230400: return 230400;
    default: return 0;
  }
}

static speed_t baud_to_speed(unsigned long b) {
  switch (b) {
    case 300: return B300;
    case 600: return B600;
    case 1200: return B1200;
    case 2400: return B2400;
    case 4800: return B4800;
    case 19200: return B19200;
    case 38400: return B38400;
    case 57600: return B57600;
    case 115200: return B115200;
    default: return B9600;
  }
}

/// Make the port path a link to the pty, as root (the component runs
/// setuid root with the user's fsuid).
static int link_port(port_t *p) {
  struct stat st;
  char target[64];
  ssize_t n;
  uid_t user = setfsuid(0);
  int err = 0;
  if (lstat(p->ch->port, &st) == 0) {
    // ours from before (a component that did not stop), or another's
    n = S_ISLNK(st.st_mode) ? readlink(p->ch->port, target, sizeof(target) - 1) : -1;
    if (n < 0 || (target[n] = 0, strncmp(target, "/dev/pts/", 9) != 0)) {
      rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s: %s exists and is not a link to a pty: not replaced\n", p->ch->name, p->ch->port);
      setfsuid(user);
      return -EEXIST;
    }
    unlink(p->ch->port);
  }
  if (symlink(p->pts, p->ch->port) < 0) {
    err = -errno;
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s: cannot make %s: %s\n", p->ch->name, p->ch->port, strerror(errno));
  } else {
    p->linked = 1;
  }
  setfsuid(user);
  return err;
}

static void unlink_port(port_t *p) {
  char target[64];
  ssize_t n;
  uid_t user;
  if (!p->linked) return;
  user = setfsuid(0);
  // only if it is still ours
  n = readlink(p->ch->port, target, sizeof(target) - 1);
  if (n > 0 && (target[n] = 0, !strcmp(target, p->pts))) unlink(p->ch->port);
  setfsuid(user);
  p->linked = 0;
}

static int open_port(port_t *p) {
  struct termios t;
  int fd = posix_openpt(O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC), slave;
  unsigned long baud = ecat_ser_baud_of(p->ch->core.start_baud);
  int bits = 8, stop_bits = 1;
  char parity = 'N';
  if (fd < 0 || grantpt(fd) < 0 || unlockpt(fd) < 0 || ptsname_r(fd, p->pts, sizeof(p->pts)) != 0) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s: cannot open a pty: %s\n", p->ch->name, strerror(errno));
    if (fd >= 0) close(fd);
    return -errno;
  }
  // the port starts as the file sets it: raw, at its baud rate
  ecat_ser_frame_of(p->ch->core.start_frame, &bits, &parity, &stop_bits);
  if ((slave = open(p->pts, O_RDWR | O_NOCTTY | O_CLOEXEC)) >= 0) {
    if (tcgetattr(slave, &t) == 0) {
      cfmakeraw(&t);
      cfsetispeed(&t, baud_to_speed(baud));
      cfsetospeed(&t, baud_to_speed(baud));
      if (stop_bits == 2) t.c_cflag |= CSTOPB; else t.c_cflag &= ~CSTOPB;
      t.c_cflag |= CLOCAL | CREAD;
      tcsetattr(slave, TCSANOW, &t);
    }
    close(slave);
  }
  p->fd = fd;
  p->asked_baud = p->ch->core.start_baud;
  p->asked_frame = p->ch->core.start_frame;
  p->last_speed = baud_to_speed(baud);
  p->last_stop = stop_bits;
  return 0;
}

/// The port's settings: ask the channel for them if they changed.
/// Returns 1 while new settings are on their way.
static int settings(port_t *p) {
  ecat_ser_t *s = &p->ch->core;
  struct termios t;
  if (p->waiting) {
    if (atomic_load_explicit(&s->done_seq, memory_order_acquire) != atomic_load(&s->want_seq)) return 1;
    p->waiting = 0;
  }
  if (tcgetattr(p->fd, &t) < 0) return 0;  // the master sees the pty's settings
  {
    speed_t sp = cfgetospeed(&t);
    int stop_bits = (t.c_cflag & CSTOPB) ? 2 : 1;
    unsigned long baud;
    uint8_t bv, fv;
    if (sp == p->last_speed && stop_bits == p->last_stop) return 0;
    p->last_speed = sp;
    p->last_stop = stop_bits;
    baud = speed_to_baud(sp);
    bv = ecat_ser_baud_value(baud);
    if (bv == 0) {
      if (!p->warned_baud) {
        rtapi_print_msg(RTAPI_MSG_WARN, LCEC_MSG_PFX "%s: the terminal has no baud rate %lu; it stays at %lu\n", p->ch->name, baud,
            ecat_ser_baud_of(p->asked_baud));
        p->warned_baud = 1;
      }
      bv = p->asked_baud;
    }
    fv = ecat_ser_frame_value(p->ch->data_bits, p->ch->parity, stop_bits);
    if (fv == 0) fv = p->asked_frame;
    if (bv == p->asked_baud && fv == p->asked_frame) return 0;
    p->asked_baud = bv;
    p->asked_frame = fv;
    atomic_store(&s->want_baud, bv);
    atomic_store(&s->want_frame, fv);
    atomic_fetch_add_explicit(&s->want_seq, 1, memory_order_release);
    p->waiting = 1;
    return 1;
  }
}

static void move(port_t *p) {
  ecat_ser_t *s = &p->ch->core;
  uint8_t buf[512];
  ssize_t n;
  size_t room;

  // program -> terminal (unless settings are on their way)
  if (!settings(p) && (room = ecat_ser_ring_free(&s->tx)) > 0) {
    n = read(p->fd, buf, room < sizeof(buf) ? room : sizeof(buf));
    if (n > 0) {
      ecat_ser_ring_put(&s->tx, buf, (size_t)n);
      p->open = 1;
    } else if (n < 0 && errno == EIO) {
      p->open = 0;  // nobody has the port open
    } else if (n < 0 && errno == EAGAIN) {
      p->open = 1;
    }
  }

  // terminal -> program
  for (;;) {
    if (p->pending_n == 0) p->pending_n = ecat_ser_ring_get(&s->rx, p->pending, sizeof(p->pending));
    if (p->pending_n == 0) break;
    if (!p->open) {
      p->pending_n = 0;  // nobody to give them to
      continue;
    }
    n = write(p->fd, p->pending, p->pending_n);
    if (n <= 0) break;  // the pty is full: later
    memmove(p->pending, p->pending + n, p->pending_n - (size_t)n);
    p->pending_n -= (size_t)n;
  }
  atomic_store_explicit(&p->ch->port_open, p->open, memory_order_relaxed);
}

static void *bridge_main(void *arg) {
  struct pollfd pfd[MAX_PORTS];
  int i;
  (void)arg;
  while (!stop) {
    for (i = 0; i < n_ports; i++) {
      // a port nobody has open signals a hang-up all the time: leave it
      // out of poll() and look at it now and then
      pfd[i].fd = ports[i].open ? ports[i].fd : -1;
      pfd[i].events = POLLIN;
      pfd[i].revents = 0;
    }
    poll(pfd, (nfds_t)n_ports, POLL_MS);
    for (i = 0; i < n_ports; i++) {
      port_t *p = &ports[i];
      if (!p->open) {
        p->hung_ms += POLL_MS;
        if (p->hung_ms < HUNG_CHECK_MS) {
          // still drain what the terminal received
          uint8_t b[256];
          while (ecat_ser_ring_get(&p->ch->core.rx, b, sizeof(b)) > 0) {
          }
          continue;
        }
        p->hung_ms = 0;
      }
      move(p);
    }
  }
  return NULL;
}

int ecat_serial_bridge_start(ecat_serial_channel_t *channels) {
  ecat_serial_channel_t *ch;
  int i, err;
  n_ports = 0;
  stop = 0;
  for (ch = channels; ch != NULL; ch = ch->next) {
    port_t *p;
    if (n_ports == MAX_PORTS) {
      rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "more than %d serial channels\n", MAX_PORTS);
      break;
    }
    p = &ports[n_ports];
    memset(p, 0, sizeof(*p));
    p->ch = ch;
    p->fd = -1;
    if ((err = open_port(p)) != 0) goto fail;
    n_ports++;
    if ((err = link_port(p)) != 0) goto fail;
  }
  if (n_ports == 0) return 0;
  if ((err = -pthread_create(&thread, NULL, bridge_main, NULL)) != 0) goto fail;
  running = 1;
  return 0;
fail:
  for (i = 0; i < n_ports; i++) {
    unlink_port(&ports[i]);
    close(ports[i].fd);
  }
  n_ports = 0;
  return err;
}

void ecat_serial_bridge_stop(void) {
  int i;
  if (running) {
    stop = 1;
    pthread_join(thread, NULL);
    running = 0;
  }
  for (i = 0; i < n_ports; i++) {
    unlink_port(&ports[i]);
    if (ports[i].fd >= 0) close(ports[i].fd);
    ports[i].fd = -1;
  }
  n_ports = 0;
}
