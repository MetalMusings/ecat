//
//  Long-term monitoring of the bus: the logic (see ecat_monitor_core.h).
//

#include "ecat_monitor_core.h"

#include <string.h>

#define NS_PER_S 1000000000ull

/// The index of a counter in the arrays, and back to its code.
static int index_of(int kind, int port) {
  switch (kind) {
    case ECAT_MON_RX: return port;
    case ECAT_MON_PHY: return 4 + port;
    case ECAT_MON_LOST: return 8 + port;
    default: return 12 + (kind - ECAT_MON_PROCESSING);
  }
}

static uint32_t code_of(int i) {
  if (i < 4) return ecat_mon_code(ECAT_MON_RX, i);
  if (i < 8) return ecat_mon_code(ECAT_MON_PHY, i - 4);
  if (i < 12) return ecat_mon_code(ECAT_MON_LOST, i - 8);
  return ecat_mon_code(ECAT_MON_PROCESSING + (i - 12), 0);
}

void ecat_mon_slave_init(ecat_mon_slave_t *s, int position, int dc, uint64_t now_ns) {
  memset(s, 0, sizeof(*s));
  s->position = position;
  s->dc = dc;
  s->period_ns = now_ns;
}

void ecat_mon_master_init(ecat_mon_master_t *m, uint64_t now_ns) {
  memset(m, 0, sizeof(*m));
  m->period_ns = now_ns;
}

/// A counter read: its growth since the last read is recorded. The first
/// read only sets the base. A value below the last means the counter was
/// cleared (or the slave restarted): it grew by the new value.
static void counter(ecat_events_t *log, ecat_mon_slave_t *s, uint64_t now_ns, int kind, int port, uint32_t value) {
  int i = index_of(kind, port);
  uint32_t grew;

  if (!s->have[i]) {
    s->have[i] = 1;
    s->last[i] = value;
    return;
  }
  grew = value >= s->last[i] ? value - s->last[i] : value;
  s->last[i] = value;
  if (grew == 0) return;
  if (s->grew_ns[i] == 0 || now_ns - s->grew_ns[i] >= (uint64_t)ECAT_MON_QUIET_S * NS_PER_S) {
    ecat_events_add(log, ECAT_EV_MON_ONSET, s->position, code_of(i), grew);
  }
  s->grew_ns[i] = now_ns;
  s->sum[i] += grew;
  s->total += grew;
}

void ecat_mon_errors(ecat_events_t *log, ecat_mon_slave_t *s, uint64_t now_ns, const uint8_t *r) {
  int p;

  for (p = 0; p < 4; p++) {
    counter(log, s, now_ns, ECAT_MON_RX, p, r[2 * p]);
    counter(log, s, now_ns, ECAT_MON_PHY, p, r[2 * p + 1]);
    counter(log, s, now_ns, ECAT_MON_LOST, p, r[0x10 + p]);
  }
  counter(log, s, now_ns, ECAT_MON_PROCESSING, 0, r[0x0C]);
  counter(log, s, now_ns, ECAT_MON_PDI, 0, r[0x0D]);
}

void ecat_mon_watchdogs(ecat_events_t *log, ecat_mon_slave_t *s, uint64_t now_ns, const uint8_t *r, int op) {
  int i = index_of(ECAT_MON_PD_WATCHDOG, 0);

  // the process data watchdog expires whenever the slave is not in OP: only its growth in OP counts
  if (op) {
    counter(log, s, now_ns, ECAT_MON_PD_WATCHDOG, 0, r[2]);
  } else {
    s->have[i] = 0;
  }
  counter(log, s, now_ns, ECAT_MON_PDI_WATCHDOG, 0, r[3]);
}

void ecat_mon_sync(ecat_events_t *log, ecat_mon_slave_t *s, uint64_t now_ns, int kind, uint16_t value) {
  counter(log, s, now_ns, kind, 0, value);
}

void ecat_mon_dc(ecat_events_t *log, ecat_mon_slave_t *s, uint64_t now_ns, uint32_t raw, int op) {
  uint32_t mag = raw & 0x7FFFFFFFu;

  if (!op) return;

  s->dc_diff = (raw & 0x80000000u) ? -(int32_t)mag : (int32_t)mag;
  s->dc_samples++;
  if (mag > s->dc_max) s->dc_max = mag;
  if (mag > s->dc_max_all) s->dc_max_all = mag;
  if (mag > ECAT_MON_DC_LIMIT_NS &&
      (s->dc_over_ns == 0 || now_ns - s->dc_over_ns >= (uint64_t)ECAT_MON_DC_OVER_EVERY_S * NS_PER_S)) {
    ecat_events_add(log, ECAT_EV_MON_DC_OVER, s->position, mag, ECAT_MON_DC_LIMIT_NS);
    s->dc_over_ns = now_ns;
  }
}

void ecat_mon_slave_period(ecat_events_t *log, ecat_mon_slave_t *s, uint64_t now_ns) {
  int i;

  if (now_ns - s->period_ns < (uint64_t)ECAT_MON_PERIOD_S * NS_PER_S) return;
  for (i = 0; i < ECAT_MON_COUNTERS; i++) {
    if (s->sum[i]) ecat_events_add(log, ECAT_EV_MON_SUM, s->position, code_of(i), s->sum[i]);
    s->sum[i] = 0;
  }
  if (s->dc && s->dc_samples) ecat_events_add(log, ECAT_EV_MON_DC, s->position, s->dc_max, s->dc_samples);
  s->dc_max = 0;
  s->dc_samples = 0;
  s->period_ns = now_ns;
}

void ecat_mon_wc(ecat_mon_master_t *m, unsigned wc_state) {
  if (wc_state == 0) {
    m->wc_zero++;
    m->wc_bad_all++;
  } else if (wc_state == 1) {
    m->wc_incomplete++;
    m->wc_bad_all++;
  }
}

void ecat_mon_master_period(ecat_events_t *log, ecat_mon_master_t *m, uint64_t now_ns) {
  if (now_ns - m->period_ns < (uint64_t)ECAT_MON_PERIOD_S * NS_PER_S) return;
  if (m->wc_zero || m->wc_incomplete) ecat_events_add(log, ECAT_EV_MON_WC, -1, m->wc_zero, m->wc_incomplete);
  if (m->lost_sum) ecat_events_add(log, ECAT_EV_MON_SUM, -1, ecat_mon_code(ECAT_MON_FRAMES_LOST, 0), m->lost_sum);
  m->wc_zero = m->wc_incomplete = 0;
  m->lost_sum = 0;
  m->period_ns = now_ns;
}

void ecat_mon_frames(ecat_events_t *log, ecat_mon_master_t *m, uint64_t now_ns, uint64_t tx, uint64_t rx) {
  uint64_t lost = tx >= rx ? tx - rx : 0, grew;

  if (!m->have_lost) {
    m->have_lost = 1;
    m->last_lost = lost;
    return;
  }
  // a frame still on its way at a read comes back later: the difference can shrink
  grew = lost > m->last_lost ? lost - m->last_lost : 0;
  m->last_lost = lost;
  if (grew == 0) return;
  if (m->lost_grew_ns == 0 || now_ns - m->lost_grew_ns >= (uint64_t)ECAT_MON_QUIET_S * NS_PER_S) {
    ecat_events_add(log, ECAT_EV_MON_ONSET, -1, ecat_mon_code(ECAT_MON_FRAMES_LOST, 0), (uint32_t)grew);
  }
  m->lost_grew_ns = now_ns;
  m->lost_sum += (uint32_t)grew;
  m->lost_all += (uint32_t)grew;
}
