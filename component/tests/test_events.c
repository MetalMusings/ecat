//
//    Unit tests of the flight recorder (ecat_events.c).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "../ecat_events.h"
#include "fake.h"

static ecat_events_t L;

static ecat_event_t *last(int back) { return &L.ev[(L.next - 1 - back) % ECAT_EVENTS_N]; }

static void test_events_ring(void) {
  int i;
  ecat_events_init(&L, "/x/ethercat.yaml");
  CHECK_EQ(L.magic, ECAT_EVENTS_MAGIC);
  CHECK_EQ(L.record_size, 32);
  CHECK(!strcmp(L.config, "/x/ethercat.yaml"));
  ecat_events_add(&L, ECAT_EV_START, -1, 4, 0);
  CHECK_EQ((int)L.next, 1);
  CHECK_EQ((int)L.ev[0].seq, 1);
  CHECK_EQ(L.ev[0].slave, -1);
  CHECK(L.ev[0].time_ns >= L.start_ns);
  // wraps: the oldest are overwritten, each record knows its index
  for (i = 0; i < ECAT_EVENTS_N + 5; i++) ecat_events_add(&L, ECAT_EV_LATE, 2, i, 0);
  CHECK_EQ((int)L.next, ECAT_EVENTS_N + 6);
  CHECK_EQ((int)L.ev[5].seq, ECAT_EVENTS_N + 6);
  CHECK_EQ((int)L.ev[5].a, ECAT_EVENTS_N + 4);
  ecat_events_add(NULL, ECAT_EV_START, -1, 0, 0);  // no recorder: nothing
}

static void test_events_limited(void) {
  ecat_events_limit_t lim = {0, 0};
  ecat_events_init(&L, NULL);
  ecat_events_add_limited(&L, &lim, ECAT_EV_LATE, -1, 100);
  ecat_events_add_limited(&L, &lim, ECAT_EV_LATE, -1, 200);
  ecat_events_add_limited(&L, &lim, ECAT_EV_LATE, -1, 300);
  CHECK_EQ((int)L.next, 1);
  CHECK_EQ((int)lim.suppressed, 2);
  lim.last_ns -= 1000000000ull;  // a second later
  ecat_events_add_limited(&L, &lim, ECAT_EV_LATE, -1, 400);
  CHECK_EQ((int)L.next, 2);
  CHECK_EQ(last(0)->a, 400);
  CHECK_EQ(last(0)->b, 2);  // two left out since the last one
}

static void test_events_changes(void) {
  ecat_events_master_t m = {0};
  ecat_events_slave_t s = {0};
  ecat_events_init(&L, NULL);
  // the first look records the states, not the link
  ecat_events_master(&L, &m, 1, 0x02, 4);
  CHECK_EQ((int)L.next, 2);
  CHECK_EQ(last(1)->type, ECAT_EV_MASTER_AL);
  CHECK_EQ(last(0)->type, ECAT_EV_RESPONDING);
  ecat_events_master(&L, &m, 1, 0x02, 4);  // nothing changed
  CHECK_EQ((int)L.next, 2);
  ecat_events_master(&L, &m, 0, 0x02, 0);
  CHECK_EQ((int)L.next, 4);
  CHECK_EQ(last(1)->type, ECAT_EV_LINK);
  CHECK_EQ(last(1)->a, 0);
  CHECK_EQ(last(0)->a, 0);
  CHECK_EQ(last(0)->b, 4);
  // working counter
  ecat_events_wc(&L, &m, 2);
  ecat_events_wc(&L, &m, 2);
  ecat_events_wc(&L, &m, 1);
  CHECK_EQ((int)L.next, 6);
  CHECK_EQ(last(0)->type, ECAT_EV_WC);
  CHECK_EQ(last(0)->a, 1);
  CHECK_EQ(last(0)->b, 2);
  // a slave: its state at first, then changes
  ecat_events_slave(&L, &s, 3, 0x02, 1, 0, 0);
  CHECK_EQ((int)L.next, 7);
  CHECK_EQ(last(0)->slave, 3);
  ecat_events_slave(&L, &s, 3, 0x08, 1, 1, 0);
  CHECK_EQ((int)L.next, 9);
  CHECK_EQ(last(1)->type, ECAT_EV_SLAVE_AL);
  CHECK_EQ(last(1)->a, 8);
  CHECK_EQ(last(1)->b, 2);
  CHECK_EQ(last(0)->type, ECAT_EV_SLAVE_OP);
  ecat_events_slave(&L, &s, 3, 0x08, 0, 1, 1);
  CHECK_EQ((int)L.next, 11);
  CHECK_EQ(last(1)->type, ECAT_EV_SLAVE_ONLINE);
  CHECK_EQ(last(0)->type, ECAT_EV_DC_SYNC);
}

static void test_events_text(void) {
  char long_text[400];
  int i;
  ecat_events_init(&L, NULL);
  CHECK_EQ((int)L.texts_n, ECAT_TEXTS_N);
  CHECK_EQ((int)L.text_size, 240);
  ecat_events_text(&L, 4, "EtherCAT WARNING 0-2: Slave seems not to support distributed clocks!\n");
  CHECK_EQ((int)L.text_next, 1);
  CHECK_EQ((int)L.tx[0].seq, 1);
  CHECK_EQ(L.tx[0].level, 4);
  CHECK(!strcmp(L.tx[0].text, "EtherCAT WARNING 0-2: Slave seems not to support distributed clocks!"));
  CHECK(L.tx[0].time_ns >= L.start_ns);
  // a long one is cut and stays terminated
  memset(long_text, 'x', sizeof(long_text) - 1);
  long_text[sizeof(long_text) - 1] = 0;
  ecat_events_text(&L, 3, long_text);
  CHECK_EQ((int)strlen(L.tx[1].text), ECAT_TEXT_LEN - 1);
  // a shorter one after it leaves nothing of the old text
  for (i = 0; i < ECAT_TEXTS_N - 1; i++) ecat_events_text(&L, 6, "x");
  ecat_events_text(&L, 6, "short");
  CHECK_EQ((int)L.tx[1].seq, ECAT_TEXTS_N + 2);
  CHECK(!strcmp(L.tx[1].text, "short"));
  CHECK_EQ(L.tx[1].text[ECAT_TEXT_LEN - 2], 0);
  ecat_events_text(NULL, 3, "nothing");  // no recorder: nothing
}

static void test_events_file(void) {
  char path[64];
  ecat_events_t *f;
  snprintf(path, sizeof(path), "/tmp/ecat-events-test-%d", (int)getpid());
  CHECK((f = ecat_events_open(path, "a.yaml")) != NULL);
  ecat_events_add(f, ECAT_EV_START, -1, 1, 0);
  ecat_events_close(f);
  // the next run keeps this one as .prev
  CHECK((f = ecat_events_open(path, "b.yaml")) != NULL);
  CHECK_EQ((int)f->next, 0);
  ecat_events_close(f);
  {
    char prev[80];
    FILE *p;
    ecat_events_t *old = malloc(sizeof(*old));
    snprintf(prev, sizeof(prev), "%s.prev", path);
    CHECK((p = fopen(prev, "rb")) != NULL);
    CHECK_EQ((int)fread(old, sizeof(*old), 1, p), 1);
    fclose(p);
    CHECK(!strcmp(old->config, "a.yaml"));
    CHECK_EQ((int)old->next, 1);
    free(old);
    unlink(prev);
  }
  unlink(path);
}

RUN_TESTS("events", test_events_ring, test_events_limited, test_events_changes, test_events_text,
          test_events_file)
