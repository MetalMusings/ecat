//
//    Unit tests of the cia402 profile (devices/ecat_cia402.c).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <errno.h>
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
  CHECK(strstr(fake_last_msg, "0x6040") != NULL);
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
  PIN_BIT("v.enable") = 0;
  run(1);
  CHECK_EQ((int32_t)fake_get(S, 0x60FF, 0), 0);
}

RUN_TESTS("cia402", test_defaults, test_needs_the_control_and_status_words, test_held_ready_without_enable,
    test_enables_step_by_step, test_target_holds_actual_until_enabled, test_position_is_clamped,
    test_feedback_and_status_pins, test_states, test_wrong_mode_is_not_enabled, test_enable_timeout, test_no_timeout_when_zero,
    test_fault_while_enabled_shuts_down_until_enable_drops, test_fault_reset_on_enable,
    test_no_fault_reset_on_enable_without_auto, test_fault_reset_is_held_at_most_100_ms,
    test_fault_reset_pin_without_enable, test_drive_leaving_enabled, test_bus_loss, test_watchdog, test_csv)
