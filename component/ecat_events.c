//
//  The flight recorder, see ecat_events.h.
//

#include "ecat_events.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

uint64_t ecat_events_now(void) {
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

void ecat_events_init(ecat_events_t *log, const char *config) {
  memset(log, 0, sizeof(*log));
  log->magic = ECAT_EVENTS_MAGIC;
  log->version = ECAT_EVENTS_VERSION;
  log->n = ECAT_EVENTS_N;
  log->record_size = sizeof(ecat_event_t);
  log->start_ns = ecat_events_now();
  log->pid = (uint32_t)getpid();
  log->texts_n = ECAT_TEXTS_N;
  log->text_size = sizeof(ecat_text_t);
  if (config != NULL) snprintf(log->config, sizeof(log->config), "%s", config);
}

ecat_events_t *ecat_events_open(const char *path, const char *config) {
  char prev[256];
  ecat_events_t *log;
  int fd, saved;

  snprintf(prev, sizeof(prev), "%s.prev", path);
  if (rename(path, prev) < 0 && errno != ENOENT) {
    unlink(path);  // keep going without the previous run
  }
  if ((fd = open(path, O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC | O_NOFOLLOW, 0644)) < 0) return NULL;
  if (ftruncate(fd, sizeof(ecat_events_t)) < 0) {
    saved = errno;
    close(fd);
    errno = saved;
    return NULL;
  }
  log = mmap(NULL, sizeof(ecat_events_t), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  saved = errno;
  close(fd);
  if (log == MAP_FAILED) {
    errno = saved;
    return NULL;
  }
  // touches every page now, so that the servo thread never faults on one
  ecat_events_init(log, config);
  mlock(log, sizeof(*log));
  return log;
}

void ecat_events_close(ecat_events_t *log) {
  if (log != NULL) munmap(log, sizeof(*log));
}

void ecat_events_add(ecat_events_t *log, int type, int slave, uint32_t a, uint32_t b) {
  uint64_t i;
  ecat_event_t *e;

  if (log == NULL) return;
  // the servo thread, the stop guard and the activator may all write
  i = __atomic_fetch_add(&log->next, 1, __ATOMIC_ACQ_REL);
  e = &log->ev[i % ECAT_EVENTS_N];
  __atomic_store_n(&e->seq, 0, __ATOMIC_RELEASE);
  e->time_ns = ecat_events_now();
  e->type = (uint16_t)type;
  e->slave = (int16_t)slave;
  e->a = a;
  e->b = b;
  e->pad = 0;
  __atomic_store_n(&e->seq, i + 1, __ATOMIC_RELEASE);
}

void ecat_events_text(ecat_events_t *log, int level, const char *text) {
  uint64_t i;
  ecat_text_t *t;
  size_t n;

  if (log == NULL || text == NULL) return;
  // the servo thread and the master's own threads may all write
  i = __atomic_fetch_add(&log->text_next, 1, __ATOMIC_ACQ_REL);
  t = &log->tx[i % ECAT_TEXTS_N];
  __atomic_store_n(&t->seq, 0, __ATOMIC_RELEASE);
  t->time_ns = ecat_events_now();
  t->level = level;
  t->pad = 0;
  n = strnlen(text, ECAT_TEXT_LEN - 1);
  while (n > 0 && (text[n - 1] == '\n' || text[n - 1] == '\r')) n--;
  memcpy(t->text, text, n);
  memset(t->text + n, 0, ECAT_TEXT_LEN - n);
  __atomic_store_n(&t->seq, i + 1, __ATOMIC_RELEASE);
}

void ecat_events_add_limited(ecat_events_t *log, ecat_events_limit_t *lim, int type, int slave, uint32_t a) {
  uint64_t now = ecat_events_now();
  if (lim->last_ns != 0 && now - lim->last_ns < 1000000000ull) {
    lim->suppressed++;
    return;
  }
  ecat_events_add(log, type, slave, a, lim->suppressed);
  lim->last_ns = now;
  lim->suppressed = 0;
}

void ecat_events_master(ecat_events_t *log, ecat_events_master_t *last, unsigned link_up, unsigned al_states,
    unsigned responding) {
  if (last->known && link_up != last->link_up) ecat_events_add(log, ECAT_EV_LINK, -1, link_up, last->link_up);
  if (!last->known || al_states != last->al_states) {
    ecat_events_add(log, ECAT_EV_MASTER_AL, -1, al_states, last->known ? last->al_states : 0);
  }
  if (!last->known || responding != last->responding) {
    ecat_events_add(log, ECAT_EV_RESPONDING, -1, responding, last->known ? last->responding : 0);
  }
  last->link_up = link_up;
  last->al_states = al_states;
  last->responding = responding;
  last->known = 1;
}

void ecat_events_wc(ecat_events_t *log, ecat_events_master_t *last, unsigned wc_state) {
  if (wc_state != last->wc_state) ecat_events_add(log, ECAT_EV_WC, -1, wc_state, last->wc_state);
  last->wc_state = wc_state;
}

void ecat_events_slave(ecat_events_t *log, ecat_events_slave_t *last, int position, unsigned al_state, unsigned online,
    unsigned operational, unsigned dc_sync_failed) {
  if (!last->known || al_state != last->al_state) {
    ecat_events_add(log, ECAT_EV_SLAVE_AL, position, al_state, last->known ? last->al_state : 0);
  }
  if (last->known && online != last->online) ecat_events_add(log, ECAT_EV_SLAVE_ONLINE, position, online, 0);
  if (last->known ? operational != last->operational : operational) {
    ecat_events_add(log, ECAT_EV_SLAVE_OP, position, operational, 0);
  }
  if (last->known ? dc_sync_failed != last->dc_sync_failed : dc_sync_failed) {
    ecat_events_add(log, ECAT_EV_DC_SYNC, position, dc_sync_failed, 0);
  }
  last->al_state = al_state;
  last->online = online;
  last->operational = operational;
  last->dc_sync_failed = dc_sync_failed;
  last->known = 1;
}
