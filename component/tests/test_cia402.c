//
//    Unit tests of the cia402 profile (devices/ecat_cia402.c).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <errno.h>
#include <math.h>
#include <string.h>

#include "../devices/ecat_cia402.h"
#include "fake.h"

#define PERIOD 1000000L  // ns

// Status words of the states (ETG.6010 table 4), with "remote" (bit 9).
#define SW_NOT_READY 0x0200
#define SW_SWITCH_ON_DISABLED 0x0250
#define SW_READY 0x0231
#define SW_SWITCHED_ON 0x0233
#define SW_ENABLED 0x0237
#define SW_QUICK_STOP 0x0217
#define SW_FAULT_REACTION 0x021F
#define SW_FAULT 0x0218

static const fake_entry_t csp_entries[] = {
    {0x6040, 0, 16}, {0x6060, 0, 8}, {0x607A, 0, 32},                    // outputs
    {0x603F, 0, 16}, {0x6041, 0, 16}, {0x6061, 0, 8}, {0x6064, 0, 32},  // inputs
    FAKE_END,
};

static lcec_slave_t *S;

static lcec_slave_t *csp_drive(void) {
  S = fake_slave("x", csp_entries, NULL);
  CHECK_EQ(ecat_cia402_init(0, S), 0);
  fake_set(S, 0x6061, 0, 8);  // the drive reports CSP
  return S;
}

/// One cycle with this status word; the control word written.
static unsigned cycle(unsigned sw) {
  fake_set(S, 0x6041, 0, sw);
  S->proc_read(S, PERIOD);
  S->proc_write(S, PERIOD);
  return fake_get(S, 0x6040, 0);
}

/// What a drive's state machine does with a control word, from the state
/// in its status word: the status word of the next cycle.
static unsigned drive(unsigned sw, unsigned cw, unsigned cw_before) {
  unsigned c = cw & 0x8F;
  if (sw == SW_FAULT) return ((cw & 0x80) && !(cw_before & 0x80)) ? SW_SWITCH_ON_DISABLED : SW_FAULT;
  if ((c & 0x02) == 0) return SW_SWITCH_ON_DISABLED;  // disable voltage
  if ((c & 0x07) == 0x06) return sw == SW_SWITCH_ON_DISABLED || sw == SW_READY || sw == SW_SWITCHED_ON || sw == SW_ENABLED ? SW_READY : sw;
  if (c == 0x07) return sw == SW_READY || sw == SW_SWITCHED_ON || sw == SW_ENABLED ? SW_SWITCHED_ON : sw;
  if (c == 0x0F) return sw == SW_READY ? SW_SWITCHED_ON : sw == SW_SWITCHED_ON || sw == SW_ENABLED ? SW_ENABLED : sw;
  return sw;
}

static unsigned sw_now, cw_now;

/// Run n cycles against the drive model, starting from sw_now.
static void run(int n) {
  while (n-- > 0) {
    unsigned cw = cycle(sw_now);
    sw_now = drive(sw_now, cw, cw_now);
    cw_now = cw;
  }
}

static void start(unsigned sw) {
  sw_now = sw;
  cw_now = 0;
}

// ---- tests -----------------------------------------------------------

static void test_defaults(void) {
  csp_drive();
  CHECK_EQ(PIN_S32("x.mode"), 8);
  CHECK_NEAR(PIN_FLOAT("x.pos-scale"), 1.0, 0);
  CHECK_NEAR(PIN_FLOAT("x.velo-scale"), 1.0, 0);
  CHECK_EQ(PIN_BIT("x.auto-fault-reset"), 1);
  CHECK_EQ(PIN_U32("x.enable-timeout-ms"), 2000);
  CHECK_EQ(PIN_U32("x.watchdog-ms"), 0);
  CHECK(fake_find("x.enable") != NULL && fake_find("x.drv-fault") != NULL);
}

static void test_needs_the_control_and_status_words(void) {
  static const fake_entry_t e[] = {{0x6041, 0, 16}, {0x6064, 0, 32}, FAKE_END};
  S = fake_slave("x", e, NULL);
  CHECK_EQ(ecat_cia402_init(0, S), -EINVAL);
  CHECK(strstr(fake_msgs, "0x6040") != NULL);
}

static void test_held_ready_without_enable(void) {
  csp_drive();
  start(SW_SWITCH_ON_DISABLED);
  run(20);
  CHECK_EQ(sw_now, SW_READY);
  CHECK_EQ(cw_now, 0x06);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
  CHECK_EQ(PIN_S32("x.cia-state"), 2);
}

static void test_enables_step_by_step(void) {
  csp_drive();
  CHECK_EQ(cycle(SW_SWITCH_ON_DISABLED), 0x06);
  PIN_BIT("x.enable") = 1;
  CHECK_EQ(cycle(SW_READY), 0x07);
  CHECK_EQ(cycle(SW_SWITCHED_ON), 0x0F);
  CHECK_EQ(cycle(SW_ENABLED), 0x0F);
  CHECK_EQ(PIN_S32("x.cia-state"), 4);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
  CHECK_EQ(PIN_S32("x.fault-reason"), 0);
  // the mode is written every cycle
  CHECK_EQ(fake_get(S, 0x6060, 0), 8);
  CHECK_EQ(PIN_S32("x.opmode"), 8);
  // disable: back to ready to switch on, no fault
  PIN_BIT("x.enable") = 0;
  CHECK_EQ(cycle(SW_ENABLED), 0x06);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
}

static void test_target_holds_actual_until_enabled(void) {
  csp_drive();
  PIN_FLOAT("x.pos-scale") = 100;
  PIN_FLOAT("x.pos-cmd") = 5.0;
  fake_set(S, 0x6064, 0, 1234);
  PIN_BIT("x.enable") = 1;
  cycle(SW_READY);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 1234);
  cycle(SW_SWITCHED_ON);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 1234);
  cycle(SW_ENABLED);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 500);
  CHECK_EQ(PIN_S32("x.drv-target-position"), 500);
  // after disable, the drive is held where it is
  PIN_BIT("x.enable") = 0;
  fake_set(S, 0x6064, 0, (uint32_t)-77);
  cycle(SW_ENABLED);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), -77);
}

static void test_position_is_clamped(void) {
  csp_drive();
  PIN_BIT("x.enable") = 1;
  start(SW_READY);
  run(5);
  CHECK_EQ(sw_now, SW_ENABLED);
  PIN_FLOAT("x.pos-cmd") = 1e12;
  cycle(SW_ENABLED);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), INT32_MAX);
  PIN_FLOAT("x.pos-cmd") = -1e12;
  cycle(SW_ENABLED);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), INT32_MIN);
}

static void test_feedback_and_status_pins(void) {
  csp_drive();
  PIN_FLOAT("x.pos-scale") = 100;
  fake_set(S, 0x6064, 0, (uint32_t)-250);
  fake_set(S, 0x603F, 0, 0x7500);
  cycle(SW_ENABLED | 0x0400);  // target reached
  CHECK_NEAR(PIN_FLOAT("x.pos-fb"), -2.5, 1e-12);
  CHECK_EQ(PIN_S32("x.drv-actual-position"), -250);
  CHECK_EQ(PIN_U32("x.error-code"), 0x7500);
  CHECK_EQ(PIN_U32("x.statusword"), SW_ENABLED | 0x0400);
  CHECK_EQ(PIN_BIT("x.stat-op-enabled"), 1);
  CHECK_EQ(PIN_BIT("x.stat-remote"), 1);
  CHECK_EQ(PIN_BIT("x.stat-target-reached"), 1);
  CHECK_EQ(PIN_BIT("x.stat-fault"), 0);
  CHECK_EQ(PIN_BIT("x.opmode-cyclic-position"), 1);
  CHECK_EQ(PIN_S32("x.opmode-display"), 8);
  // a scale of 0 counts as 1
  PIN_FLOAT("x.pos-scale") = 0;
  cycle(SW_ENABLED);
  CHECK_NEAR(PIN_FLOAT("x.pos-fb"), -250, 0);
  // the generic driver's pins are read and written too
  CHECK_EQ(fake_generic_reads, 2);
  CHECK_EQ(fake_generic_writes, 2);
}

static void test_states(void) {
  static const struct {
    unsigned sw;
    int state;
  } t[] = {
      {SW_NOT_READY, 0}, {SW_SWITCH_ON_DISABLED, 1}, {SW_READY, 2}, {SW_SWITCHED_ON, 3}, {SW_ENABLED, 4},
      {SW_QUICK_STOP, 5}, {SW_FAULT_REACTION, 6}, {SW_FAULT, 7}, {0x0001, -1},
      // bits that do not count: voltage enabled, warning, remote, target reached
      {SW_ENABLED | 0x0490, 4}, {SW_FAULT & ~0x0010, 7},
  };
  csp_drive();
  for (unsigned i = 0; i < sizeof(t) / sizeof(t[0]); i++) {
    cycle(t[i].sw);
    if (PIN_S32("x.cia-state") != t[i].state) {
      fprintf(stderr, "status word 0x%04X: state %d, not %d\n", t[i].sw, PIN_S32("x.cia-state"), t[i].state);
      fake_failures++;
    }
  }
}

static void test_wrong_mode_is_not_enabled(void) {
  csp_drive();
  fake_set(S, 0x6061, 0, 0);  // the drive is in no mode
  PIN_U32("x.enable-timeout-ms") = 10;
  PIN_BIT("x.enable") = 1;
  start(SW_READY);
  run(5);
  CHECK_EQ(sw_now, SW_SWITCHED_ON);  // switched on, not enabled
  CHECK_EQ(cw_now, 0x07);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
  run(10);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 6);
  CHECK_EQ(sw_now, SW_READY);  // shut down
}

static void test_enable_timeout(void) {
  csp_drive();
  PIN_U32("x.enable-timeout-ms") = 10;
  PIN_BIT("x.enable") = 1;
  // a drive that stays in "switch on disabled"
  cycle(SW_SWITCH_ON_DISABLED);
  for (int i = 0; i < 9; i++) cycle(SW_SWITCH_ON_DISABLED);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
  cycle(SW_SWITCH_ON_DISABLED);
  cycle(SW_SWITCH_ON_DISABLED);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 3);
}

static void test_no_timeout_when_zero(void) {
  csp_drive();
  PIN_U32("x.enable-timeout-ms") = 0;
  PIN_BIT("x.enable") = 1;
  for (int i = 0; i < 5000; i++) cycle(SW_SWITCH_ON_DISABLED);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
}

static void test_fault_while_enabled_shuts_down_until_enable_drops(void) {
  csp_drive();
  PIN_BIT("x.enable") = 1;
  start(SW_READY);
  run(5);
  CHECK_EQ(sw_now, SW_ENABLED);
  CHECK_EQ(cycle(SW_FAULT_REACTION), 0x00);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 1);
  // a fault reset clears the drive's fault, but the profile stays shut down
  PIN_BIT("x.fault-reset") = 1;
  start(SW_FAULT);
  run(20);
  CHECK_EQ(sw_now, SW_READY);
  CHECK_EQ(cw_now, 0x06);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 1);
  // until enable goes false
  PIN_BIT("x.fault-reset") = 0;
  PIN_BIT("x.enable") = 0;
  run(1);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
  CHECK_EQ(PIN_S32("x.fault-reason"), 0);
  PIN_BIT("x.enable") = 1;
  run(5);
  CHECK_EQ(sw_now, SW_ENABLED);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
}

static void test_fault_reset_on_enable(void) {
  csp_drive();
  start(SW_FAULT);
  run(3);
  CHECK_EQ(cw_now, 0x00);
  CHECK_EQ(PIN_S32("x.cia-state"), 7);
  // a rising edge of enable resets the fault: bit 7 low, then high
  PIN_BIT("x.enable") = 1;
  CHECK_EQ(cycle(SW_FAULT), 0x00);
  CHECK_EQ(cycle(SW_FAULT), 0x80);
  cw_now = 0x80;
  sw_now = SW_SWITCH_ON_DISABLED;
  run(6);
  CHECK_EQ(sw_now, SW_ENABLED);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
}

static void test_no_fault_reset_on_enable_without_auto(void) {
  csp_drive();
  PIN_BIT("x.auto-fault-reset") = 0;
  PIN_BIT("x.enable") = 1;
  start(SW_FAULT);
  run(10);
  CHECK_EQ(sw_now, SW_FAULT);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 1);
}

static void test_fault_reset_is_held_at_most_100_ms(void) {
  int i, high = 0;
  csp_drive();
  PIN_BIT("x.enable") = 1;
  // a drive that does not leave Fault
  for (i = 0; i < 200; i++) {
    if (cycle(SW_FAULT) & 0x80) high++;
    if (i == 98) CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
  }
  CHECK(high >= 95 && high <= 100);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 1);
}

static void test_fault_reset_pin_without_enable(void) {
  csp_drive();
  start(SW_FAULT);
  run(2);
  PIN_BIT("x.fault-reset") = 1;
  run(5);
  CHECK_EQ(sw_now, SW_READY);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
}

static void test_drive_leaving_enabled(void) {
  csp_drive();
  PIN_BIT("x.enable") = 1;
  start(SW_READY);
  run(5);
  cycle(SW_SWITCH_ON_DISABLED);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 4);
  // and a quick stop by the drive
  PIN_BIT("x.enable") = 0;
  cycle(SW_READY);
  PIN_BIT("x.enable") = 1;
  start(SW_READY);
  run(5);
  CHECK_EQ(sw_now, SW_ENABLED);
  CHECK_EQ(cycle(SW_QUICK_STOP), 0x00);
  CHECK_EQ(PIN_S32("x.fault-reason"), 4);
}

static void test_bus_loss(void) {
  csp_drive();
  PIN_BIT("x.enable") = 1;
  PIN_FLOAT("x.pos-cmd") = 10;
  fake_set(S, 0x6064, 0, 3);
  start(SW_READY);
  run(5);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 10);
  fake_bus(S, 0);
  CHECK_EQ(cycle(SW_ENABLED), 0x00);
  CHECK_EQ(PIN_S32("x.cia-state"), -1);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 2);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 3);
  // the bus comes back: still shut down
  fake_bus(S, 1);
  CHECK_EQ(cycle(SW_ENABLED), 0x06);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
}

static void test_watchdog(void) {
  int i;
  csp_drive();
  PIN_U32("x.watchdog-ms") = 5;
  PIN_BIT("x.enable") = 1;
  start(SW_READY);
  for (i = 0; i < 50; i++) {
    PIN_U32("x.watchdog") += 1;
    run(1);
  }
  CHECK_EQ(sw_now, SW_ENABLED);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
  run(4);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
  run(2);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 5);
  CHECK_EQ(sw_now, SW_READY);
}

static void test_command_not_a_number(void) {
  csp_drive();
  PIN_FLOAT("x.pos-cmd") = 7;
  fake_set(S, 0x6064, 0, 5);
  PIN_BIT("x.enable") = 1;
  start(SW_READY);
  run(5);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 7);
  // NaN: shut down at once, holding the actual position, never INT32_MIN
  PIN_FLOAT("x.pos-cmd") = NAN;
  CHECK_EQ(cycle(SW_ENABLED), 0x06);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 5);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 7);
  // still shut down when the command is a number again
  PIN_FLOAT("x.pos-cmd") = 7;
  CHECK_EQ(cycle(SW_READY), 0x06);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  // infinite, and while enabling
  PIN_BIT("x.enable") = 0;
  cycle(SW_READY);
  PIN_FLOAT("x.pos-cmd") = -INFINITY;
  PIN_BIT("x.enable") = 1;
  CHECK_EQ(cycle(SW_READY), 0x06);
  CHECK_EQ(PIN_S32("x.fault-reason"), 7);
}

static void test_csv(void) {
  static const fake_entry_t e[] = {
      {0x6040, 0, 16}, {0x60FF, 0, 32}, {0x6041, 0, 16}, {0x606C, 0, 32}, FAKE_END,
  };
  lcec_slave_modparam_t mp[2] = {{.id = ECAT_MP_CIA402_MODE}, {.id = -1}};
  mp[0].value.u32 = 9;
  S = fake_slave("v", e, mp);
  CHECK_EQ(ecat_cia402_init(0, S), 0);
  CHECK_EQ(PIN_S32("v.mode"), 9);
  PIN_FLOAT("v.velo-scale") = 1000;
  PIN_FLOAT("v.velocity-cmd") = 2.5;
  fake_set(S, 0x606C, 0, 1500);
  PIN_BIT("v.enable") = 1;
  start(SW_READY);
  run(1);
  CHECK_EQ((int32_t)fake_get(S, 0x60FF, 0), 0);
  CHECK_NEAR(PIN_FLOAT("v.velocity-fb"), 1.5, 1e-12);
  run(4);
  CHECK_EQ(sw_now, SW_ENABLED);
  CHECK_EQ((int32_t)fake_get(S, 0x60FF, 0), 2500);
  // in CSV, the velocity command counts, not pos-cmd
  PIN_FLOAT("v.pos-cmd") = NAN;
  run(1);
  CHECK_EQ(PIN_BIT("v.drv-fault"), 0);
  PIN_FLOAT("v.velocity-cmd") = NAN;
  run(1);
  CHECK_EQ(PIN_S32("v.fault-reason"), 7);
  CHECK_EQ((int32_t)fake_get(S, 0x60FF, 0), 0);
  PIN_BIT("v.enable") = 0;
  run(1);
  CHECK_EQ((int32_t)fake_get(S, 0x60FF, 0), 0);
}

// ---- homing (HM) -----------------------------------------------------

// A drive that homes: it follows 0x6060 one cycle later, homes for
// hm.cycles cycles after a rising edge of control word bit 4 in mode 6,
// and then has its position at hm.home.
static struct {
  int cycles, left, active, done, error, stuck_mode, stale;
  int32_t home;
  unsigned cw_old;
} hm;

static lcec_slave_t *homing_drive(void) {
  lcec_slave_modparam_t mp[2] = {{.id = ECAT_MP_CIA402_MODES}, {.id = -1}};
  mp[0].value.u32 = 1u << 6;
  S = fake_slave("x", csp_entries, mp);
  CHECK_EQ(ecat_cia402_init(0, S), 0);
  fake_set(S, 0x6061, 0, 8);
  memset(&hm, 0, sizeof(hm));
  hm.cycles = 30;
  return S;
}

/// One cycle of an enabled drive; the control word.
static unsigned hm_cycle(void) {
  int display = (int8_t)fake_get(S, 0x6061, 0);
  unsigned sw = SW_ENABLED, cw;
  if (display == 6) {
    if (hm.error) sw |= 0x2000;
    else if (hm.active) sw |= 0;
    else if (hm.done || hm.stale) sw |= 0x1400;  // attained, reached
    else sw |= 0x0400;                            // not started
  } else {
    sw |= 0x1400;  // in CSP bit 12 means "follows the target"
  }
  cw = cycle(sw);
  if (!hm.stuck_mode) fake_set(S, 0x6061, 0, fake_get(S, 0x6060, 0));
  if (display == 6 && (cw & 0x10) && !(hm.cw_old & 0x10)) {
    hm.active = 1;
    hm.left = hm.cycles;
    hm.done = 0;
  }
  if (hm.active && !(cw & 0x10)) hm.active = 0;  // interrupted
  if (hm.active && --hm.left <= 0) {
    hm.active = 0;
    hm.done = 1;
    fake_set(S, 0x6064, 0, (uint32_t)hm.home);
  }
  hm.cw_old = cw;
  return cw;
}

static void hm_enable(void) {
  PIN_BIT("x.enable") = 1;
  start(SW_READY);
  run(5);
  CHECK_EQ(sw_now, SW_ENABLED);
}

static void test_homing_pins_only_with_hm(void) {
  csp_drive();
  CHECK(fake_find("x.index-enable") == NULL && fake_find("x.home") == NULL);
  homing_drive();
  CHECK(fake_find("x.index-enable") && fake_find("x.home") && fake_find("x.stat-homed") && fake_find("x.home-error"));
  CHECK_EQ(PIN_U32("x.home-timeout-ms"), 60000);
}

static void test_homing_needs_the_mode_objects(void) {
  static const fake_entry_t e[] = {{0x6040, 0, 16}, {0x607A, 0, 32}, {0x6041, 0, 16}, {0x6064, 0, 32}, FAKE_END};
  lcec_slave_modparam_t mp[2] = {{.id = ECAT_MP_CIA402_MODES}, {.id = -1}};
  mp[0].value.u32 = 1u << 6;
  S = fake_slave("x", e, mp);
  CHECK_EQ(ecat_cia402_init(0, S), -EINVAL);
  CHECK(strstr(fake_msgs, "homing needs 0x6060 and 0x6061") != NULL);
}

static void test_homing_by_index_enable(void) {
  int i;
  homing_drive();
  hm.home = 0;
  PIN_FLOAT("x.pos-scale") = 100;
  fake_set(S, 0x6064, 0, 12345);
  PIN_FLOAT("x.pos-cmd") = 123.45;
  hm_enable();
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 12345);

  // motion arms the index and moves the joint slowly
  PIN_BIT("x.index-enable") = 1;
  CHECK_EQ(hm_cycle(), 0x0F);
  CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 6);
  CHECK_EQ(PIN_BIT("x.stat-homing"), 1);
  CHECK_EQ(hm_cycle() & 0x1F, 0x1F);  // started
  for (i = 0; i < 10; i++) {
    PIN_FLOAT("x.pos-cmd") += 0.01;
    CHECK_EQ(hm_cycle() & 0x1F, 0x1F);
    // no following error for motion, no target from motion for the drive
    CHECK_NEAR(PIN_FLOAT("x.pos-fb"), PIN_FLOAT("x.pos-cmd"), 0);
    CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), (int32_t)fake_get(S, 0x6064, 0));
  }
  CHECK_EQ(PIN_BIT("x.index-enable"), 1);
  for (i = 0; i < 40 && PIN_BIT("x.index-enable"); i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.index-enable"), 0);
  CHECK_EQ(PIN_BIT("x.stat-homed"), 1);
  CHECK_EQ(PIN_BIT("x.stat-homing"), 0);
  CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 8);
  CHECK_EQ(fake_get(S, 0x6040, 0), 0x0F);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 0);  // held at the actual position
  // the next cycle, pos-fb is the drive's new position, as an encoder's
  // count at its index; then the target follows motion again
  PIN_FLOAT("x.pos-cmd") = 0.02;
  hm_cycle();
  CHECK_NEAR(PIN_FLOAT("x.pos-fb"), 0, 0);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 2);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
}

static void test_homing_by_the_home_pin(void) {
  int i;
  homing_drive();
  hm.home = 500;
  fake_set(S, 0x6064, 0, 77);
  hm_enable();
  PIN_BIT("x.home") = 1;
  for (i = 0; i < 5; i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.stat-homing"), 1);
  CHECK_NEAR(PIN_FLOAT("x.pos-fb"), 77, 0);  // not started by motion: the real position
  for (i = 0; i < 40 && PIN_BIT("x.stat-homing"); i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.stat-homed"), 1);
  CHECK_EQ(PIN_BIT("x.index-enable"), 0);
  hm_cycle();
  CHECK_NEAR(PIN_FLOAT("x.pos-fb"), 500, 0);
  // home held high does not start it again
  for (i = 0; i < 40; i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.stat-homing"), 0);
}

static void test_homing_error(void) {
  int i;
  homing_drive();
  hm_enable();
  PIN_BIT("x.home") = 1;
  for (i = 0; i < 5; i++) hm_cycle();
  hm.error = 1;
  CHECK_EQ(hm_cycle(), 0x06);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 8);
  CHECK_EQ(PIN_BIT("x.home-error"), 1);
  CHECK_EQ(PIN_BIT("x.stat-homing"), 0);
  CHECK_EQ(PIN_BIT("x.stat-homed"), 0);
  CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 8);  // back to the configured mode
}

static void test_homing_timeout(void) {
  int i;
  homing_drive();
  hm.cycles = 1000000;
  PIN_U32("x.home-timeout-ms") = 50;
  hm_enable();
  PIN_BIT("x.home") = 1;
  for (i = 0; i < 45; i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
  for (i = 0; i < 10; i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 9);
}

static void test_homing_abandoned_by_motion(void) {
  int i;
  homing_drive();
  hm_enable();
  PIN_BIT("x.index-enable") = 1;
  for (i = 0; i < 5; i++) hm_cycle();
  CHECK(hm.active);
  PIN_BIT("x.index-enable") = 0;
  CHECK_EQ(hm_cycle() & 0x10, 0);  // bit 4 low: the drive stops homing
  CHECK(!hm.active);
  for (i = 0; i < 3; i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.stat-homing"), 0);
  CHECK_EQ(PIN_BIT("x.stat-homed"), 0);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
  CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 8);
}

static void test_homing_mode_not_taken(void) {
  int i;
  homing_drive();
  hm_enable();
  hm.stuck_mode = 1;
  PIN_BIT("x.home") = 1;
  for (i = 0; i < 1000; i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
  for (i = 0; i < 2; i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 6);
}

static void test_homing_slow_way_back(void) {
  int i;
  homing_drive();
  hm.cycles = 3;
  hm_enable();
  PIN_BIT("x.index-enable") = 1;
  for (i = 0; i < 6 && !hm.done; i++) hm_cycle();
  CHECK(hm.done);
  // the drive is slow to go back to CSP: index-enable stays set and the
  // target is held, and after a second it is a fault
  hm.stuck_mode = 1;
  for (i = 0; i < 900; i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.index-enable"), 1);
  CHECK_EQ(PIN_BIT("x.stat-homing"), 1);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
  PIN_FLOAT("x.pos-cmd") = 50;
  hm_cycle();
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), (int32_t)fake_get(S, 0x6064, 0));
  for (i = 0; i < 110; i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 6);
  CHECK_EQ(PIN_BIT("x.stat-homed"), 1);  // the drive did home
}

static void test_homing_stopped_by_disable(void) {
  int i;
  homing_drive();
  hm_enable();
  PIN_BIT("x.home") = 1;
  for (i = 0; i < 5; i++) hm_cycle();
  PIN_BIT("x.enable") = 0;
  CHECK_EQ(hm_cycle(), 0x06);
  CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 8);
  CHECK_EQ(PIN_BIT("x.stat-homing"), 0);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
}

static void test_homing_not_while_disabled(void) {
  int i;
  homing_drive();
  PIN_BIT("x.index-enable") = 1;
  start(SW_READY);
  run(5);
  CHECK_EQ(PIN_BIT("x.stat-homing"), 0);
  // enabling later does not start it: only a new edge does
  PIN_BIT("x.enable") = 1;
  run(5);
  CHECK_EQ(sw_now, SW_ENABLED);
  for (i = 0; i < 5; i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.stat-homing"), 0);
  CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 8);
}

static void test_homing_with_stale_result_bits(void) {
  int i, n;
  // a drive that shows "attained and reached" from an earlier homing and
  // homes in one cycle: done after the settle time, not at once
  homing_drive();
  hm.stale = 1;
  hm.cycles = 1000000;
  hm_enable();
  PIN_BIT("x.home") = 1;
  hm_cycle();  // to HM
  for (n = 0; n < 100 && PIN_BIT("x.stat-homing"); n++) {
    hm.active = 0;  // never reports "homing"
    hm_cycle();
  }
  CHECK(n >= 19 && n <= 23);
  CHECK_EQ(PIN_BIT("x.stat-homed"), 1);
  // seen homing first: done as soon as the drive reports it
  homing_drive();
  hm.cycles = 3;
  hm_enable();
  PIN_BIT("x.home") = 1;
  for (i = 0; i < 8; i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.stat-homed"), 1);
}

RUN_TESTS("cia402", test_defaults, test_needs_the_control_and_status_words, test_held_ready_without_enable,
    test_enables_step_by_step, test_target_holds_actual_until_enabled, test_position_is_clamped,
    test_feedback_and_status_pins, test_states, test_wrong_mode_is_not_enabled, test_enable_timeout, test_no_timeout_when_zero,
    test_fault_while_enabled_shuts_down_until_enable_drops, test_fault_reset_on_enable,
    test_no_fault_reset_on_enable_without_auto, test_fault_reset_is_held_at_most_100_ms,
    test_fault_reset_pin_without_enable, test_drive_leaving_enabled, test_bus_loss, test_watchdog,
    test_command_not_a_number, test_csv, test_homing_pins_only_with_hm, test_homing_needs_the_mode_objects,
    test_homing_by_index_enable, test_homing_by_the_home_pin, test_homing_error, test_homing_timeout,
    test_homing_abandoned_by_motion, test_homing_mode_not_taken, test_homing_slow_way_back, test_homing_stopped_by_disable,
    test_homing_not_while_disabled, test_homing_with_stale_result_bits)
