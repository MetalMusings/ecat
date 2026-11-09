//
//    Unit tests of the automatic SYNC0 shift (lcec_autoshift.c).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include "../lcec_autoshift.h"
#include "fake.h"

/// Runs n cycles with send offsets cycling through the given values; returns
/// how many of them chose a new shift.
static int feed(lcec_autoshift_t *a, int ready, const int32_t *offsets, int count, int n) {
  int i, changes = 0;
  for (i = 0; i < n; i++) changes += lcec_autoshift_update(a, ready, offsets[i % count]);
  return changes;
}

/// Measured for the full time with send offsets lo..hi.
static void measure(lcec_autoshift_t *a, uint32_t period, int32_t lo, int32_t hi) {
  int32_t offs[] = {hi, (lo + hi) / 2, lo};  // the first is not the smallest
  feed(a, 1, offs, 3, (int)(LCEC_AUTOSHIFT_MEASURE_NS / period) + 1);
  (void)period;
}

static void test_autoshift_waits_for_the_bus(void) {
  lcec_autoshift_t a;
  int32_t offs[] = {60000};

  lcec_autoshift_init(&a, 1000000, 500000);
  CHECK_EQ(feed(&a, 0, offs, 1, 10000), 0);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_WAIT);
  CHECK_EQ((int)a.shift, 500000);
  // ready: measures 2 s, then chooses once
  CHECK_EQ(feed(&a, 1, offs, 1, 1999), 0);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_MEASURE);
  CHECK_EQ(feed(&a, 1, offs, 1, 1), 1);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_TWO);
  CHECK_EQ(feed(&a, 1, offs, 1, 10000), 0);

}

static void test_autoshift_two_cycles_in_the_middle(void) {
  lcec_autoshift_t a;

  // 1 ms, frames 50..80 us: window 130 us .. 1000 + 50 - 50 us
  lcec_autoshift_init(&a, 1000000, 500000);
  measure(&a, 1000000, 50000, 80000);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_TWO);
  CHECK_EQ((int)a.send_min, 50000);
  CHECK_EQ((int)a.send_max, 80000);
  CHECK_EQ((int)a.shift, 565000);
  CHECK_EQ(a.fits, 1);

  // 500 us, frames 40..60 us: 110 .. 490 us (the first is not the largest)
  lcec_autoshift_init(&a, 500000, 250000);
  {
    int32_t offs[] = {40000, 50000, 60000};
    feed(&a, 1, offs, 3, 4001);
  }
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_TWO);
  CHECK_EQ((int)a.shift, 300000);

}

static void test_autoshift_wraps_into_the_period(void) {
  lcec_autoshift_t a;

  // late frames, 300..320 us of 500: two cycles at 370 .. 750 us, the
  // middle 560 us is 60 us into the next period
  lcec_autoshift_init(&a, 500000, 250000);
  measure(&a, 500000, 300000, 320000);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_TWO);
  CHECK_EQ((int)a.pos, 560000);
  CHECK_EQ((int)a.shift, 60000);

}

static void test_autoshift_three_cycles_when_two_do_not_fit(void) {
  lcec_autoshift_t a;

  // 250 us with a slow input: two cycles need 210 .. 200 us (empty),
  // three fit at -40 .. 0 us: the middle, -20 us, is 230 us
  lcec_autoshift_init(&a, 250000, 125000);
  a.input_margin = 100000;
  measure(&a, 250000, 50000, 160000);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_THREE);
  CHECK_EQ(a.fits, 1);
  CHECK_EQ((int)a.pos, -20000);
  CHECK_EQ((int)a.shift, 230000);

  // just within the window for two
  lcec_autoshift_init(&a, 250000, 125000);
  a.input_margin = 100000;
  measure(&a, 250000, 50000, 150000);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_TWO);
  CHECK_EQ((int)a.shift, 200000);

}

static void test_autoshift_nothing_fits(void) {
  lcec_autoshift_t a;
  int32_t far[] = {0, 249000};

  // frames all over the period: three cycles as well as can be, and no
  // more changes, as nothing would be better
  lcec_autoshift_init(&a, 250000, 125000);
  measure(&a, 250000, 20000, 230000);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_THREE);
  CHECK_EQ(a.fits, 0);
  CHECK_EQ(feed(&a, 1, far, 2, 100), 0);

}

static void test_autoshift_follows_later_frames(void) {
  lcec_autoshift_t a;
  int32_t a_bit_later[] = {300000};
  int32_t much_later[] = {560000};
  int32_t early[] = {10000};

  lcec_autoshift_init(&a, 1000000, 500000);
  measure(&a, 1000000, 50000, 80000);
  CHECK_EQ((int)a.shift, 565000);
  // later frames that keep half the margin: no change
  CHECK_EQ(feed(&a, 1, a_bit_later, 1, 10), 0);
  CHECK_EQ((int)a.send_max, 300000);
  CHECK_EQ((int)a.shift, 565000);
  // one too close to SYNC0: a new choice, once
  CHECK_EQ(feed(&a, 1, much_later, 1, 10), 1);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_TWO);
  // 610 .. 1000 us
  CHECK_EQ((int)a.shift, 805000);
  // an early frame that leaves the inputs too little time
  CHECK_EQ(feed(&a, 1, early, 1, 10), 0);  // 805 <= 1000 + 10 - 50 + 25
  CHECK_EQ((int)a.send_min, 10000);

}

static void test_autoshift_input_side(void) {
  lcec_autoshift_t a;
  int32_t early[] = {0};

  // frames 400..420 us: 470 .. 1350 us, SYNC0 at 910 us
  lcec_autoshift_init(&a, 1000000, 500000);
  measure(&a, 1000000, 400000, 420000);
  CHECK_EQ((int)a.shift, 910000);
  a.input_margin = 200000;
  // a frame at 0 us leaves the inputs 90 us, they need 200: 910 > 1000 + 0
  // - 200 + 100, a new choice
  CHECK_EQ(feed(&a, 1, early, 1, 1), 1);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_TWO);
  // 470 .. 800 us
  CHECK_EQ((int)a.shift, 635000);

}

static void test_autoshift_two_to_three(void) {
  lcec_autoshift_t a;
  int32_t late[] = {160000};

  // 250 us with a slow input: two cycles at 150 .. 200 us
  lcec_autoshift_init(&a, 250000, 125000);
  a.input_margin = 100000;
  measure(&a, 250000, 50000, 100000);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_TWO);
  CHECK_EQ((int)a.shift, 175000);
  // the frames now spread 50..160 us: two cycles no longer fit, three at
  // -40 .. 0 us
  CHECK_EQ(feed(&a, 1, late, 1, 1), 1);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_THREE);
  CHECK_EQ(a.fits, 1);
  CHECK_EQ((int)a.shift, 230000);

}

static void test_autoshift_keeps_half_the_margins(void) {
  lcec_autoshift_t a;
  int32_t later[] = {530000};
  int32_t too_late[] = {545000};
  int32_t early[] = {0};
  int32_t three_late[] = {185000};
  int32_t three_early[] = {25000};

  // two cycles at 565 us; a frame at 530 us leaves 35 us, more than half
  // the margin: kept, so that SYNC0 does not wander with every new frame
  lcec_autoshift_init(&a, 1000000, 500000);
  measure(&a, 1000000, 50000, 80000);
  CHECK_EQ(feed(&a, 1, later, 1, 1), 0);
  CHECK_EQ((int)a.shift, 565000);
  CHECK_EQ(feed(&a, 1, too_late, 1, 1), 1);  // 20 us left

  // the input side: SYNC0 at 910 us, inputs need 100 us; a frame at 0 us
  // leaves them 90 us, more than half: kept
  lcec_autoshift_init(&a, 1000000, 500000);
  measure(&a, 1000000, 400000, 420000);
  a.input_margin = 100000;
  CHECK_EQ(feed(&a, 1, early, 1, 1), 0);
  CHECK_EQ((int)a.shift, 910000);

  // three cycles at -20 us (-40 .. 0 us): frames at 185 and 25 us leave
  // SYNC0 inside the window widened by half the margin
  lcec_autoshift_init(&a, 250000, 125000);
  a.input_margin = 100000;
  measure(&a, 250000, 50000, 160000);
  CHECK_EQ(a.state, LCEC_AUTOSHIFT_THREE);
  CHECK_EQ(feed(&a, 1, three_late, 1, 1), 0);
  CHECK_EQ(feed(&a, 1, three_early, 1, 1), 0);
  CHECK_EQ((int)a.shift, 230000);

}

RUN_TESTS("autoshift", test_autoshift_waits_for_the_bus, test_autoshift_two_cycles_in_the_middle, test_autoshift_wraps_into_the_period, test_autoshift_three_cycles_when_two_do_not_fit, test_autoshift_nothing_fits, test_autoshift_follows_later_frames, test_autoshift_input_side, test_autoshift_two_to_three, test_autoshift_keeps_half_the_margins)
