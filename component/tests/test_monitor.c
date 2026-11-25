//
//    Unit tests of the long-term monitoring (ecat_monitor_core.c).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <string.h>

#include "../ecat_monitor_core.h"
#include "fake.h"

#define S 1000000000ull

static ecat_events_t L;

static ecat_event_t *last(int back) { return &L.ev[(L.next - 1 - back) % ECAT_EVENTS_N]; }

static void test_monitor_counters(void) {
  ecat_mon_slave_t s;
  uint8_t r[0x14];
  uint64_t t = 1000 * S;

  ecat_events_init(&L, NULL);
  ecat_mon_slave_init(&s, 3, 0, t);
  memset(r, 0, sizeof(r));
  r[0x03] = 7;                 // port 1 phy: old count
  ecat_mon_errors(&L, &s, t, r);
  CHECK_EQ((int)L.next, 0);    // the first read is the base
  r[0x03] = 9;                 // grows by 2: an onset
  ecat_mon_errors(&L, &s, t + 1 * S, r);
  CHECK_EQ((int)L.next, 1);
  CHECK_EQ(last(0)->type, ECAT_EV_MON_ONSET);
  CHECK_EQ(last(0)->slave, 3);
  CHECK_EQ(last(0)->a, ecat_mon_code(ECAT_MON_PHY, 1));
  CHECK_EQ(last(0)->b, 2);
  r[0x03] = 10;                // grows again soon after: no new onset, counted in the sum
  ecat_mon_errors(&L, &s, t + 2 * S, r);
  CHECK_EQ((int)L.next, 1);
  r[0x03] = 12;                // quiet for a minute, then grows: a new onset
  ecat_mon_errors(&L, &s, t + 2 * S + ECAT_MON_QUIET_S * S, r);
  CHECK_EQ((int)L.next, 2);
  CHECK_EQ(last(0)->b, 2);
  r[0x10] = 1;                 // port 0 lost link, and the PDI error counter
  r[0x0D] = 3;
  ecat_mon_errors(&L, &s, t + 100 * S, r);
  CHECK_EQ((int)L.next, 4);
  CHECK_EQ(last(1)->a, ecat_mon_code(ECAT_MON_LOST, 0));
  CHECK_EQ(last(0)->a, ecat_mon_code(ECAT_MON_PDI, 0));
  CHECK_EQ(last(0)->b, 3);
  CHECK_EQ((int)s.total, 2 + 1 + 2 + 1 + 3);
  r[0x0D] = 1;                 // cleared, or the slave restarted: grew by the new value
  ecat_mon_errors(&L, &s, t + 101 * S, r);
  CHECK_EQ((int)s.total, 10);
  // the period's summary: one line per counter that grew, then nothing
  ecat_mon_slave_period(&L, &s, t + 200 * S);     // not over yet
  CHECK_EQ((int)L.next, 4);
  ecat_mon_slave_period(&L, &s, t + ECAT_MON_PERIOD_S * S);
  CHECK_EQ((int)L.next, 7);
  CHECK_EQ(last(2)->type, ECAT_EV_MON_SUM);
  CHECK_EQ(last(2)->a, ecat_mon_code(ECAT_MON_PHY, 1));
  CHECK_EQ(last(2)->b, 5);
  CHECK_EQ(last(1)->a, ecat_mon_code(ECAT_MON_LOST, 0));
  CHECK_EQ(last(0)->a, ecat_mon_code(ECAT_MON_PDI, 0));
  CHECK_EQ(last(0)->b, 4);
  ecat_mon_slave_period(&L, &s, t + 2 * ECAT_MON_PERIOD_S * S);
  CHECK_EQ((int)L.next, 7);
}

static void test_monitor_watchdogs_and_sync(void) {
  ecat_mon_slave_t s;
  uint8_t r[4] = {0, 0, 5, 1};
  uint64_t t = 1000 * S;

  ecat_events_init(&L, NULL);
  ecat_mon_slave_init(&s, 4, 0, t);
  ecat_mon_watchdogs(&L, &s, t, r, 0);
  r[2] = 9;                    // the process data watchdog grows outside OP: not counted
  ecat_mon_watchdogs(&L, &s, t + S, r, 0);
  CHECK_EQ((int)L.next, 0);
  ecat_mon_watchdogs(&L, &s, t + 2 * S, r, 1);   // in OP: the base
  r[2] = 11;
  r[3] = 2;                    // the PDI watchdog counts in any state
  ecat_mon_watchdogs(&L, &s, t + 3 * S, r, 1);
  CHECK_EQ((int)L.next, 2);
  CHECK_EQ(last(1)->a, ecat_mon_code(ECAT_MON_PD_WATCHDOG, 0));
  CHECK_EQ(last(1)->b, 2);
  CHECK_EQ(last(0)->a, ecat_mon_code(ECAT_MON_PDI_WATCHDOG, 0));
  ecat_mon_sync(&L, &s, t, ECAT_MON_SM_MISSED, 100);
  ecat_mon_sync(&L, &s, t + 10 * S, ECAT_MON_SM_MISSED, 103);
  CHECK_EQ((int)L.next, 3);
  CHECK_EQ(last(0)->a, ecat_mon_code(ECAT_MON_SM_MISSED, 0));
  CHECK_EQ(last(0)->b, 3);
}

static void test_monitor_dc(void) {
  ecat_mon_slave_t s;
  uint64_t t = 1000 * S;

  ecat_events_init(&L, NULL);
  ecat_mon_slave_init(&s, 2, 1, t);
  ecat_mon_dc(&L, &s, t, 7);
  CHECK_EQ(s.dc_diff, 7);
  ecat_mon_dc(&L, &s, t + S, 0x80000000u | 12);    // bit 31: the local copy is behind
  CHECK_EQ(s.dc_diff, -12);
  CHECK_EQ((int)s.dc_max, 12);
  CHECK_EQ((int)L.next, 0);
  ecat_mon_dc(&L, &s, t + 2 * S, 0x80000000u | 5000);   // beyond the limit: at once
  CHECK_EQ((int)L.next, 1);
  CHECK_EQ(last(0)->type, ECAT_EV_MON_DC_OVER);
  CHECK_EQ((int)last(0)->a, 5000);
  ecat_mon_dc(&L, &s, t + 3 * S, 6000);                 // within a minute: not again
  CHECK_EQ((int)L.next, 1);
  ecat_mon_dc(&L, &s, t + 2 * S + ECAT_MON_DC_OVER_EVERY_S * S, 3000);
  CHECK_EQ((int)L.next, 2);
  ecat_mon_slave_period(&L, &s, t + ECAT_MON_PERIOD_S * S);
  CHECK_EQ((int)L.next, 3);
  CHECK_EQ(last(0)->type, ECAT_EV_MON_DC);
  CHECK_EQ((int)last(0)->a, 6000);
  CHECK_EQ((int)last(0)->b, 5);
  CHECK_EQ((int)s.dc_max, 0);
  CHECK_EQ((int)s.dc_max_all, 6000);
  // a slave that is not watched for DC has no DC summary
  ecat_mon_slave_init(&s, 0, 0, t);
  ecat_mon_slave_period(&L, &s, t + ECAT_MON_PERIOD_S * S);
  CHECK_EQ((int)L.next, 3);
}

static void test_monitor_wc(void) {
  ecat_mon_master_t m;
  uint64_t t = 1000 * S;
  int i;

  ecat_events_init(&L, NULL);
  ecat_mon_master_init(&m, t);
  for (i = 0; i < 1000; i++) ecat_mon_wc(&m, 2);
  ecat_mon_master_period(&L, &m, t + ECAT_MON_PERIOD_S * S);
  CHECK_EQ((int)L.next, 0);                // all complete: nothing
  ecat_mon_wc(&m, 0);
  ecat_mon_wc(&m, 1);
  ecat_mon_wc(&m, 1);
  ecat_mon_master_period(&L, &m, t + 2 * ECAT_MON_PERIOD_S * S);
  CHECK_EQ((int)L.next, 1);
  CHECK_EQ(last(0)->type, ECAT_EV_MON_WC);
  CHECK_EQ(last(0)->slave, -1);
  CHECK_EQ((int)last(0)->a, 1);
  CHECK_EQ((int)last(0)->b, 2);
  CHECK_EQ((int)m.wc_bad_all, 3);
}

RUN_TESTS("monitor", test_monitor_counters, test_monitor_watchdogs_and_sync, test_monitor_dc, test_monitor_wc)
