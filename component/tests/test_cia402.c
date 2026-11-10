//
//    Unit tests of the cia402 profile (devices/ecat_cia402.c).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <errno.h>
#include <stdlib.h>
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

#ifdef EC_HAVE_DC_SYNC_FAILED
static void test_dc_sync_failed_is_not_enabled(void) {
  csp_drive();
  S->state.dc_sync_failed = 1;  // the master's DC sync check timed out
  PIN_BIT("x.enable") = 1;
  start(SW_READY);
  run(10);
  CHECK(sw_now != SW_ENABLED);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 11);
  // synchronized after a new configuration: enabled on a new enable
  S->state.dc_sync_failed = 0;
  run(5);
  CHECK(sw_now != SW_ENABLED);
  PIN_BIT("x.enable") = 0;
  run(2);
  CHECK_EQ(PIN_S32("x.fault-reason"), 0);
  PIN_BIT("x.enable") = 1;
  run(5);
  CHECK_EQ(sw_now, SW_ENABLED);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
}

static void test_dc_sync_failed_while_running(void) {
  csp_drive();
  PIN_BIT("x.enable") = 1;
  PIN_FLOAT("x.pos-cmd") = 10;
  fake_set(S, 0x6064, 0, 3);
  start(SW_READY);
  run(5);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 10);
  S->state.dc_sync_failed = 1;  // reconfigured, e.g. after a bus loss
  CHECK_EQ(cycle(SW_ENABLED), 0x06);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 1);
  CHECK_EQ(PIN_S32("x.fault-reason"), 11);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 3);
}
#define DC_SYNC_TESTS test_dc_sync_failed_is_not_enabled, test_dc_sync_failed_while_running,
#else
#define DC_SYNC_TESTS
#endif

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
  CHECK(strstr(fake_msgs, "changing modes needs 0x6060 and 0x6061") != NULL);
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

// ---- profile velocity (PV) -------------------------------------------

// A drive in PV: once enabled it ramps its actual velocity by pv.ramp per
// cycle towards the target, or towards 0 while halted (control word bit 8).
static struct {
  int32_t vel, ramp;
  int stuck;  // never slows down
  unsigned sw;
} pv;

static const fake_entry_t pv_entries[] = {
    {0x6040, 0, 16}, {0x6060, 0, 8}, {0x60FF, 0, 32}, {0x6041, 0, 16}, {0x6061, 0, 8}, {0x606C, 0, 32}, FAKE_END,
};

static lcec_slave_t *pv_drive(void) {
  lcec_slave_modparam_t mp[2] = {{.id = ECAT_MP_CIA402_MODE}, {.id = -1}};
  mp[0].value.u32 = 3;
  S = fake_slave("s", pv_entries, mp);
  CHECK_EQ(ecat_cia402_init(0, S), 0);
  fake_set(S, 0x6061, 0, 3);
  memset(&pv, 0, sizeof(pv));
  pv.ramp = 100;
  pv.sw = SW_READY;
  return S;
}

/// One cycle; the control word.
static unsigned pv_cycle(void) {
  unsigned sw = pv.sw, cw;
  int32_t target = (int32_t)fake_get(S, 0x60FF, 0), goal;
  if (sw == SW_ENABLED) {
    goal = (cw_now & 0x100) ? 0 : target;
    if ((goal == 0 && pv.vel == 0) || (goal != 0 && pv.vel == goal)) sw |= 0x0400;  // reached
    if (pv.vel == 0) sw |= 0x1000;                                                   // speed zero
  }
  fake_set(S, 0x606C, 0, (uint32_t)pv.vel);
  cw = cycle(sw);
  pv.sw = drive(pv.sw, cw, cw_now);
  cw_now = cw;
  // the drive keeps turning when it is switched off, and stops by itself
  goal = pv.sw == SW_ENABLED && !(cw & 0x100) ? (int32_t)fake_get(S, 0x60FF, 0) : 0;
  if (pv.stuck) goal = pv.vel;
  if (pv.vel < goal) pv.vel = pv.vel + pv.ramp > goal ? goal : pv.vel + pv.ramp;
  if (pv.vel > goal) pv.vel = pv.vel - pv.ramp < goal ? goal : pv.vel - pv.ramp;
  return cw;
}

static void pv_run(int n) {
  while (n-- > 0) pv_cycle();
}

static void test_pv_pins(void) {
  csp_drive();
  CHECK(fake_find("x.at-speed") == NULL && fake_find("x.stop-timeout-ms") == NULL);
  pv_drive();
  CHECK(fake_find("s.at-speed") && fake_find("s.zero-speed"));
  CHECK_EQ(PIN_U32("s.stop-timeout-ms"), 10000);
  CHECK_EQ(PIN_S32("s.mode"), 3);
}

static void test_pv_needs_target_velocity(void) {
  static const fake_entry_t e[] = {{0x6040, 0, 16}, {0x6041, 0, 16}, {0x606C, 0, 32}, FAKE_END};
  lcec_slave_modparam_t mp[2] = {{.id = ECAT_MP_CIA402_MODE}, {.id = -1}};
  mp[0].value.u32 = 3;
  S = fake_slave("s", e, mp);
  CHECK_EQ(ecat_cia402_init(0, S), -EINVAL);
  CHECK(strstr(fake_msgs, "PV and CSV need 0x60FF") != NULL);
  mp[0].value.u32 = 1;  // PP: not yet
  S = fake_slave("s", pv_entries, mp);
  CHECK_EQ(ecat_cia402_init(0, S), -EINVAL);
}

static void test_pv_runs_at_the_command(void) {
  int i;
  pv_drive();
  PIN_FLOAT("s.velo-scale") = 100;
  PIN_FLOAT("s.velocity-cmd") = 10;
  pv_run(2);
  CHECK_EQ((int32_t)fake_get(S, 0x60FF, 0), 0);  // not enabled: no target
  PIN_BIT("s.enable") = 1;
  pv_run(3);
  CHECK_EQ(pv.sw, SW_ENABLED);
  CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 3);
  CHECK_EQ((int32_t)fake_get(S, 0x60FF, 0), 1000);
  CHECK_EQ(PIN_BIT("s.at-speed"), 0);  // ramping
  // the status word still shows the old target, 0, as reached: not at speed
  // until it answers the new one
  for (i = 0; i < 20; i++) {
    pv_run(1);
    if (pv.vel != 1000) CHECK_EQ(PIN_BIT("s.at-speed"), 0);
  }
  CHECK_EQ(pv.vel, 1000);
  CHECK_EQ(PIN_BIT("s.at-speed"), 1);
  CHECK_EQ(PIN_BIT("s.zero-speed"), 0);
  CHECK_NEAR(PIN_FLOAT("s.velocity-fb"), 10, 1e-12);
  CHECK_EQ(cw_now, 0x0F);
  // a new command: not at speed until the drive is there
  PIN_FLOAT("s.velocity-cmd") = -5;
  pv_run(1);
  CHECK_EQ((int32_t)fake_get(S, 0x60FF, 0), -500);
  CHECK_EQ(PIN_BIT("s.at-speed"), 0);
  pv_run(30);
  CHECK_EQ(PIN_BIT("s.at-speed"), 1);
}

static void test_pv_halts_before_switching_off(void) {
  int i, halted = 0;
  pv_drive();
  PIN_FLOAT("s.velocity-cmd") = 1000;
  PIN_BIT("s.enable") = 1;
  pv_run(20);
  CHECK_EQ(pv.vel, 1000);
  PIN_BIT("s.enable") = 0;
  for (i = 0; i < 9; i++) {
    CHECK_EQ(pv_cycle(), 0x010F);  // halt, still enabled
    halted++;
    CHECK_EQ(PIN_BIT("s.at-speed"), 0);
  }
  CHECK_EQ((int32_t)fake_get(S, 0x60FF, 0), 0);
  pv_run(2);
  CHECK_EQ(pv.vel, 0);
  CHECK_EQ(PIN_BIT("s.zero-speed"), 1);
  CHECK_EQ(pv_cycle(), 0x06);  // stopped: switched off
  CHECK_EQ(PIN_BIT("s.drv-fault"), 0);
  CHECK(halted == 9);
}

static void test_pv_enable_again_while_halting(void) {
  pv_drive();
  PIN_FLOAT("s.velocity-cmd") = 1000;
  PIN_BIT("s.enable") = 1;
  pv_run(20);
  PIN_BIT("s.enable") = 0;
  pv_run(3);
  CHECK(pv.vel < 1000 && pv.vel > 0);
  PIN_BIT("s.enable") = 1;
  CHECK_EQ(pv_cycle(), 0x0F);
  pv_run(10);
  CHECK_EQ(pv.vel, 1000);
  CHECK_EQ(PIN_BIT("s.at-speed"), 1);
}

static void test_pv_stop_timeout(void) {
  int i;
  pv_drive();
  PIN_U32("s.stop-timeout-ms") = 50;
  PIN_FLOAT("s.velocity-cmd") = 1000;
  PIN_BIT("s.enable") = 1;
  pv_run(20);
  pv.stuck = 1;
  PIN_BIT("s.enable") = 0;
  for (i = 0; i < 50; i++) CHECK_EQ(pv_cycle(), 0x010F);
  CHECK_EQ(pv_cycle(), 0x06);  // switched off anyway
  // 0: switched off at once
  pv_drive();
  PIN_U32("s.stop-timeout-ms") = 0;
  PIN_FLOAT("s.velocity-cmd") = 1000;
  PIN_BIT("s.enable") = 1;
  pv_run(20);
  PIN_BIT("s.enable") = 0;
  CHECK_EQ(pv_cycle(), 0x06);
}

static void test_pv_standstill_switches_off_after_the_status_answers(void) {
  int i;
  pv_drive();
  PIN_BIT("s.enable") = 1;
  pv_run(10);
  CHECK_EQ(PIN_BIT("s.zero-speed"), 1);
  PIN_BIT("s.enable") = 0;
  // halted until the status can answer the halt, two cycles
  for (i = 0; i < 2; i++) CHECK_EQ(pv_cycle(), 0x010F);
  CHECK_EQ(pv_cycle(), 0x06);
  // started and stopped at once: "speed zero" is still the old answer, and
  // the drive has begun to turn; it is halted until it really stands still
  pv_drive();
  PIN_BIT("s.enable") = 1;
  pv_run(10);
  PIN_FLOAT("s.velocity-cmd") = 1000;
  pv_run(1);
  PIN_BIT("s.enable") = 0;
  for (i = 0; i < 20 && pv_cycle() == 0x010F; i++) {
  }
  CHECK_EQ(cw_now, 0x06);
  CHECK_EQ(pv.vel, 0);
}

static void test_pv_halts_on_a_fault(void) {
  int i;
  pv_drive();
  PIN_FLOAT("s.velocity-cmd") = 1000;
  PIN_BIT("s.enable") = 1;
  pv_run(20);
  PIN_FLOAT("s.velocity-cmd") = NAN;
  CHECK_EQ(pv_cycle(), 0x010F);
  CHECK_EQ(PIN_BIT("s.drv-fault"), 1);
  CHECK_EQ(PIN_S32("s.fault-reason"), 7);
  CHECK_EQ((int32_t)fake_get(S, 0x60FF, 0), 0);
  for (i = 0; i < 20 && pv_cycle() == 0x010F; i++) {
  }
  CHECK_EQ(cw_now, 0x06);
  CHECK_EQ(pv.vel, 0);
  // the bus lost: nothing can be commanded, no halt
  pv_drive();
  PIN_FLOAT("s.velocity-cmd") = 1000;
  PIN_BIT("s.enable") = 1;
  pv_run(20);
  fake_bus(S, 0);
  CHECK_EQ(pv_cycle(), 0x00);
}

static void test_pv_not_at_speed_in_another_mode(void) {
  pv_drive();
  PIN_FLOAT("s.velocity-cmd") = 1000;
  PIN_BIT("s.enable") = 1;
  pv_run(20);
  CHECK_EQ(PIN_BIT("s.at-speed"), 1);
  fake_set(S, 0x6061, 0, 9);
  pv_run(1);
  CHECK_EQ(PIN_BIT("s.at-speed"), 0);
  CHECK_EQ(PIN_BIT("s.zero-speed"), 0);
  // bit 12 means "speed zero" only in PV
  fake_set(S, 0x6061, 0, 3);
  PIN_FLOAT("s.velocity-cmd") = 0;
  pv_run(20);
  CHECK_EQ(PIN_BIT("s.zero-speed"), 1);
  fake_set(S, 0x6061, 0, 9);
  pv_run(1);
  CHECK_EQ(PIN_BIT("s.zero-speed"), 0);
}

// ---- profile position (PP) -------------------------------------------

// A drive in PP: after a rising edge of control word bit 4 it acknowledges
// (status word bit 12) and takes the set-point; it drops the acknowledge
// when bit 4 is low, and moves pp.step counts per cycle towards the
// target; halted (bit 8), it stops.
static struct {
  int32_t pos, target, step;
  int ack, moving, no_ack;
  int ack_hold;   // cycles the acknowledge stays after bit 4 goes low
  int start_in;   // cycles until an accepted set-point starts to move
  unsigned sw, cw_old;
} ppd;

static const fake_entry_t pp_entries[] = {
    {0x6040, 0, 16}, {0x6060, 0, 8}, {0x607A, 0, 32}, {0x6081, 0, 32},
    {0x6041, 0, 16}, {0x6061, 0, 8}, {0x6064, 0, 32}, FAKE_END,
};

static lcec_slave_t *pp_drive(const fake_entry_t *entries) {
  lcec_slave_modparam_t mp[2] = {{.id = ECAT_MP_CIA402_MODE}, {.id = -1}};
  mp[0].value.u32 = 1;
  S = fake_slave("t", entries, mp);
  CHECK_EQ(ecat_cia402_init(0, S), 0);
  if (entries == pp_entries) fake_set(S, 0x6061, 0, 1);
  memset(&ppd, 0, sizeof(ppd));
  ppd.step = 10;
  ppd.sw = SW_READY;
  cw_now = 0;
  return S;
}

static unsigned pp_cycle(void) {
  unsigned sw = ppd.sw, cw;
  if (sw == SW_ENABLED) {
    if (ppd.ack) sw |= 0x1000;
    if (!ppd.moving || (cw_now & 0x100)) sw |= 0x0400;  // halted: reached means standing still
  }
  fake_set(S, 0x6064, 0, (uint32_t)ppd.pos);
  cw = cycle(sw);
  ppd.sw = drive(ppd.sw, cw, cw_now);
  cw_now = cw;
  if (ppd.sw == SW_ENABLED) {
    if (ppd.start_in > 0 && --ppd.start_in == 0) ppd.moving = ppd.pos != ppd.target;
    if ((cw & 0x10) && !(ppd.cw_old & 0x10) && !ppd.ack && !ppd.no_ack) {
      int32_t t = (int32_t)fake_get(S, 0x607A, 0);
      ppd.target = (cw & 0x40) ? ppd.target + t : t;
      ppd.ack = 1;
      ppd.ack_hold = 2;
      ppd.start_in = 2;  // target reached still shows the last move until then
    }
    if (!(cw & 0x10) && ppd.ack && ppd.ack_hold-- <= 0) ppd.ack = 0;
    if (ppd.moving && !(cw & 0x100)) {
      int32_t d = ppd.target - ppd.pos;
      ppd.pos += d > ppd.step ? ppd.step : d < -ppd.step ? -ppd.step : d;
      ppd.moving = ppd.pos != ppd.target;
    }
  } else {
    ppd.moving = 0;
  }
  ppd.cw_old = cw;
  return cw;
}

static void pp_run(int n) {
  while (n-- > 0) pp_cycle();
}

static void pp_enable(void) {
  ppd.target = ppd.pos;
  PIN_BIT("t.enable") = 1;
  pp_run(4);
  CHECK_EQ(ppd.sw, SW_ENABLED);
}

static void test_pp_pins(void) {
  static const fake_entry_t no_velocity[] = {
      {0x6040, 0, 16}, {0x607A, 0, 32}, {0x6041, 0, 16}, {0x6064, 0, 32}, FAKE_END,
  };
  pp_drive(pp_entries);
  CHECK(fake_find("t.pp-target") && fake_find("t.pp-start") && fake_find("t.pp-done") && fake_find("t.pp-velocity"));
  CHECK_EQ(PIN_U32("t.pp-timeout-ms"), 60000);
  CHECK_EQ(PIN_U32("t.stop-timeout-ms"), 10000);
  CHECK(fake_find("t.at-speed") == NULL);
  pp_drive(no_velocity);
  CHECK(fake_find("t.pp-velocity") == NULL);
}

static void test_pp_needs_positions(void) {
  static const fake_entry_t e[] = {{0x6040, 0, 16}, {0x6041, 0, 16}, {0x6064, 0, 32}, FAKE_END};
  lcec_slave_modparam_t mp[2] = {{.id = ECAT_MP_CIA402_MODE}, {.id = -1}};
  mp[0].value.u32 = 1;
  S = fake_slave("t", e, mp);
  CHECK_EQ(ecat_cia402_init(0, S), -EINVAL);
  CHECK(strstr(fake_msgs, "PP and CSP need 0x607A and 0x6064") != NULL);
}

static void test_pp_moves_only_on_start(void) {
  int i;
  pp_drive(pp_entries);
  PIN_FLOAT("t.pos-scale") = 10;
  PIN_FLOAT("t.velo-scale") = 100;
  PIN_FLOAT("t.pp-target") = 50;
  PIN_FLOAT("t.pp-velocity") = 2;
  ppd.pos = 123;
  pp_enable();
  pp_run(20);
  CHECK_EQ(ppd.pos, 123);  // enabled, no start: still
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 123);
  CHECK_EQ(fake_get(S, 0x6081, 0), 0);
  CHECK_EQ(PIN_BIT("t.pp-done"), 0);
  // start: set-point 500 at 200 counts/s, with the handshake
  PIN_BIT("t.pp-start") = 1;
  CHECK_EQ(pp_cycle(), 0x003F);  // enable operation, new set-point, change immediately
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 500);
  CHECK_EQ(fake_get(S, 0x6081, 0), 200);
  CHECK_EQ(pp_cycle() & 0x10, 0);  // acknowledged: bit 4 low
  CHECK_EQ(PIN_BIT("t.pp-done"), 0);
  for (i = 0; i < 100 && !PIN_BIT("t.pp-done"); i++) {
    pp_cycle();
    if (!PIN_BIT("t.pp-done")) CHECK(ppd.pos != 500 || i > 0);
  }
  CHECK_EQ(ppd.pos, 500);
  CHECK_EQ(PIN_BIT("t.pp-done"), 1);
  CHECK_NEAR(PIN_FLOAT("t.pos-fb"), 50, 1e-12);
  // pp-start held high: no new move
  PIN_FLOAT("t.pp-target") = 0;
  pp_run(20);
  CHECK_EQ(ppd.pos, 500);
  CHECK_EQ(PIN_BIT("t.pp-done"), 1);
}

static void test_pp_done_not_from_an_old_target_reached(void) {
  int i;
  pp_drive(pp_entries);
  pp_enable();
  // the drive stands at its last target: target reached is set
  PIN_FLOAT("t.pp-target") = 300;
  PIN_BIT("t.pp-start") = 1;
  for (i = 0; i < 4; i++) {
    pp_cycle();
    CHECK_EQ(PIN_BIT("t.pp-done"), 0);
  }
  CHECK(ppd.moving);
}

static void test_pp_relative_and_a_new_move(void) {
  int i;
  pp_drive(pp_entries);
  ppd.pos = 100;
  pp_enable();
  PIN_FLOAT("t.pp-target") = 30;
  PIN_BIT("t.pp-relative") = 1;
  PIN_BIT("t.pp-start") = 1;
  CHECK_EQ(pp_cycle(), 0x007F);  // relative
  for (i = 0; i < 20; i++) pp_cycle();
  CHECK_EQ(ppd.pos, 130);
  CHECK_EQ(PIN_BIT("t.pp-done"), 1);
  // a new start during a move replaces it
  PIN_BIT("t.pp-start") = 0;
  PIN_BIT("t.pp-relative") = 0;
  PIN_FLOAT("t.pp-target") = 1000;
  pp_cycle();
  PIN_BIT("t.pp-start") = 1;
  pp_run(5);
  CHECK(ppd.moving && ppd.target == 1000);
  PIN_BIT("t.pp-start") = 0;
  pp_cycle();
  PIN_FLOAT("t.pp-target") = 0;
  PIN_BIT("t.pp-start") = 1;
  for (i = 0; i < 100 && !PIN_BIT("t.pp-done"); i++) pp_cycle();
  CHECK_EQ(ppd.pos, 0);
}

static void test_pp_start_while_acknowledging(void) {
  int i;
  pp_drive(pp_entries);
  pp_enable();
  PIN_FLOAT("t.pp-target") = 100;
  PIN_BIT("t.pp-start") = 1;
  pp_cycle();  // set-point
  pp_cycle();  // acknowledged
  // a new start at once, while the drive still acknowledges: not lost
  PIN_BIT("t.pp-start") = 0;
  PIN_FLOAT("t.pp-target") = -100;
  ppd.ack = 1;
  pp_cycle();
  PIN_BIT("t.pp-start") = 1;
  for (i = 0; i < 100 && !PIN_BIT("t.pp-done"); i++) pp_cycle();
  CHECK_EQ(ppd.target, -100);
  CHECK_EQ(ppd.pos, -100);
}

static void test_pp_halt(void) {
  int i;
  pp_drive(pp_entries);
  pp_enable();
  PIN_FLOAT("t.pp-target") = 1000;
  PIN_BIT("t.pp-start") = 1;
  pp_run(10);
  PIN_BIT("t.pp-halt") = 1;
  pp_run(2);
  int32_t at = ppd.pos;
  pp_run(20);
  CHECK_EQ(ppd.pos, at);
  CHECK_EQ(PIN_BIT("t.pp-done"), 0);  // standing still, not at the target
  CHECK_EQ(cw_now & 0x100, 0x100);
  // released: it goes on to the target
  PIN_BIT("t.pp-halt") = 0;
  for (i = 0; i < 200 && !PIN_BIT("t.pp-done"); i++) pp_cycle();
  CHECK_EQ(ppd.pos, 1000);
}

static void test_pp_halts_before_switching_off(void) {
  int i;
  pp_drive(pp_entries);
  pp_enable();
  PIN_FLOAT("t.pp-target") = 1000;
  PIN_FLOAT("t.pp-velocity") = 5;
  PIN_BIT("t.pp-start") = 1;
  pp_run(10);
  CHECK(ppd.moving);
  CHECK_EQ(fake_get(S, 0x6081, 0), 5);
  PIN_BIT("t.enable") = 0;
  CHECK_EQ(pp_cycle(), 0x010F);
  for (i = 0; i < 10 && pp_cycle() == 0x010F; i++) {
  }
  CHECK_EQ(cw_now, 0x06);
  CHECK(ppd.pos < 1000);
  CHECK_EQ(PIN_BIT("t.drv-fault"), 0);
  CHECK_EQ(PIN_BIT("t.pp-done"), 0);
  CHECK_EQ(fake_get(S, 0x6081, 0), 0);  // no profile velocity while off
  // enabled again: no move until a new start
  PIN_BIT("t.enable") = 1;
  pp_run(20);
  CHECK(!ppd.moving);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), ppd.pos);
}

static void test_pp_timeouts(void) {
  int i;
  pp_drive(pp_entries);
  PIN_U32("t.pp-timeout-ms") = 30;
  pp_enable();
  // no acknowledge
  ppd.no_ack = 1;
  PIN_FLOAT("t.pp-target") = 1000;
  PIN_BIT("t.pp-start") = 1;
  for (i = 0; i < 29; i++) CHECK_EQ(pp_cycle() & 0x10, 0x10);
  pp_run(3);
  CHECK_EQ(PIN_BIT("t.drv-fault"), 1);
  CHECK_EQ(PIN_S32("t.fault-reason"), 10);
  // too slow
  pp_drive(pp_entries);
  PIN_U32("t.pp-timeout-ms") = 30;
  ppd.step = 1;
  pp_enable();
  PIN_FLOAT("t.pp-target") = 1000;
  PIN_BIT("t.pp-start") = 1;
  pp_run(40);
  CHECK_EQ(PIN_S32("t.fault-reason"), 10);
  CHECK(cw_now == 0x010F || cw_now == 0x06);  // halted, then off
}

static void test_pp_timeout_paused_while_halted(void) {
  pp_drive(pp_entries);
  PIN_U32("t.pp-timeout-ms") = 30;
  pp_enable();
  PIN_FLOAT("t.pp-target") = 100;
  PIN_BIT("t.pp-start") = 1;
  pp_run(3);
  PIN_BIT("t.pp-halt") = 1;
  pp_run(100);
  CHECK_EQ(PIN_BIT("t.drv-fault"), 0);
  PIN_BIT("t.pp-halt") = 0;
  pp_run(20);
  CHECK_EQ(PIN_BIT("t.pp-done"), 1);
  CHECK_EQ(PIN_BIT("t.drv-fault"), 0);
}

static void test_pp_start_waits_for_the_mode(void) {
  int i;
  pp_drive(pp_entries);
  pp_enable();
  fake_set(S, 0x6061, 0, 8);  // the drive shows another mode
  PIN_FLOAT("t.pp-target") = 100;
  PIN_BIT("t.pp-start") = 1;
  for (i = 0; i < 10; i++) CHECK_EQ(pp_cycle() & 0x10, 0);
  CHECK(!ppd.ack);
  // in PP: the start is given now
  fake_set(S, 0x6061, 0, 1);
  for (i = 0; i < 40 && !PIN_BIT("t.pp-done"); i++) pp_cycle();
  CHECK_EQ(ppd.pos, 100);
}

static void test_pp_bad_command(void) {
  pp_drive(pp_entries);
  pp_enable();
  PIN_FLOAT("t.pp-velocity") = NAN;
  pp_cycle();
  CHECK_EQ(PIN_S32("t.fault-reason"), 7);
  pp_drive(pp_entries);
  pp_enable();
  PIN_FLOAT("t.pp-target") = INFINITY;
  PIN_BIT("t.pp-start") = 1;
  pp_cycle();
  CHECK_EQ(PIN_S32("t.fault-reason"), 7);
  CHECK(!ppd.moving);
}

// ---- changing modes (opmode-cmd) -------------------------------------

// A drive with CSP, CSV, PV and PP: it shows 0x6060 in 0x6061 a cycle
// later (unless mm.stuck), and moves as each mode says: CSP to the target
// position, CSV at the target velocity, PV ramping by mm.ramp, PP to an
// acknowledged set-point; halted (bit 8) it ramps down to a stop.
static struct {
  int32_t pos, vel, ramp, pp_target;
  int stuck, never_stops, ack, mode_old;
  int home_left, homed;  // HM: cycles left homing, done
  int32_t home;          // HM: the position after homing
  unsigned sw, cw_old;
} mm;

static const fake_entry_t mm_entries[] = {
    {0x6040, 0, 16}, {0x6060, 0, 8}, {0x607A, 0, 32}, {0x60FF, 0, 32}, {0x6081, 0, 32},
    {0x6041, 0, 16}, {0x6061, 0, 8}, {0x6064, 0, 32}, {0x606C, 0, 32}, FAKE_END,
};

/// A drive in mode, allowed to change to the modes in others (bit n: mode n).
static lcec_slave_t *mm_drive(int mode, unsigned others) {
  lcec_slave_modparam_t mp[3] = {{.id = ECAT_MP_CIA402_MODE}, {.id = ECAT_MP_CIA402_MODES}, {.id = -1}};
  mp[0].value.u32 = mode;
  mp[1].value.u32 = others;
  S = fake_slave("m", mm_entries, mp);
  CHECK_EQ(ecat_cia402_init(0, S), 0);
  fake_set(S, 0x6061, 0, mode);
  memset(&mm, 0, sizeof(mm));
  mm.ramp = 100;
  mm.sw = SW_READY;
  cw_now = 0;
  return S;
}

static int8_t mm_mode(void) { return (int8_t)fake_get(S, 0x6061, 0); }

static unsigned mm_cycle(void) {
  unsigned sw = mm.sw, cw;
  int m = mm_mode();
  int halted = (cw_now & 0x100) != 0;
  int32_t tv = (int32_t)fake_get(S, 0x60FF, 0);
  if (m == 1 && mm.mode_old != 1) mm.pp_target = mm.pos;  // PP starts where it is
  mm.mode_old = m;
  if (sw == SW_ENABLED) {
    if (m == 3) {
      int32_t goal = halted ? 0 : tv;
      if (mm.vel == goal) sw |= 0x0400;
      if (mm.vel == 0) sw |= 0x1000;
    } else if (m == 1) {
      if (mm.ack) sw |= 0x1000;
      if ((mm.pos == mm.pp_target && !halted) || (halted && mm.vel == 0)) sw |= 0x0400;
    } else if (m == 6) {
      if (mm.homed) sw |= 0x1400;
      else if (!mm.home_left) sw |= 0x0400;
    } else {
      sw |= 0x1400;
    }
  }
  fake_set(S, 0x6064, 0, (uint32_t)mm.pos);
  fake_set(S, 0x606C, 0, (uint32_t)mm.vel);
  cw = cycle(sw);
  mm.sw = drive(mm.sw, cw, cw_now);
  cw_now = cw;
  if (!mm.stuck) fake_set(S, 0x6061, 0, fake_get(S, 0x6060, 0));
  if (mm.sw != SW_ENABLED) {
    mm.vel = 0;
  } else if (m == 8) {
    int32_t t = (int32_t)fake_get(S, 0x607A, 0);
    mm.vel = t - mm.pos;
    mm.pos = t;
  } else if (m == 9) {
    mm.vel = tv;
    mm.pos += tv;
  } else if (m == 6) {
    // homes in 10 cycles after a rising edge of bit 4; it does not move
    // in the model, it only gets its new position
    mm.vel = 0;
    if ((cw & 0x10) && !(mm.cw_old & 0x10)) {
      mm.home_left = 10;
      mm.homed = 0;
    }
    if (!(cw & 0x10)) mm.home_left = 0;
    if (mm.home_left > 0 && --mm.home_left == 0) {
      mm.homed = 1;
      mm.pos = mm.home;
    }
  } else {
    int32_t goal = m == 3 ? tv : 0;
    if (m == 1) {
      if ((cw & 0x10) && !(mm.cw_old & 0x10)) {
        mm.pp_target = (int32_t)fake_get(S, 0x607A, 0);
        mm.ack = 1;
      }
      if (!(cw & 0x10)) mm.ack = 0;
      goal = mm.pp_target > mm.pos ? 10 : mm.pp_target < mm.pos ? -10 : 0;
      if (abs(mm.pp_target - mm.pos) < 10) goal = mm.pp_target - mm.pos;
    }
    if (cw & 0x100) goal = 0;
    if (mm.never_stops) goal = mm.vel;
    if (m == 1) {
      mm.vel = goal;
    } else {
      if (mm.vel < goal) mm.vel = mm.vel + mm.ramp > goal ? goal : mm.vel + mm.ramp;
      if (mm.vel > goal) mm.vel = mm.vel - mm.ramp < goal ? goal : mm.vel - mm.ramp;
    }
    mm.pos += mm.vel;
  }
  mm.cw_old = cw;
  return cw;
}

static void mm_run(int n) {
  while (n-- > 0) mm_cycle();
}

#define BIT(m) (1u << (m))

static void test_opmode_cmd_only_with_more_modes(void) {
  csp_drive();
  CHECK(fake_find("x.opmode-cmd") == NULL);
  homing_drive();
  CHECK(fake_find("x.opmode-cmd") == NULL);
  mm_drive(8, BIT(3));
  CHECK(fake_find("m.opmode-cmd") != NULL);
  CHECK(fake_find("m.at-speed") != NULL);       // PV is allowed
  CHECK(fake_find("m.pp-start") == NULL);       // PP is not
}

static void test_opmode_cmd_not_in_the_file(void) {
  mm_drive(8, BIT(3));
  PIN_S32("m.opmode-cmd") = 1;  // PP: not allowed
  mm_run(3);
  CHECK_EQ(PIN_BIT("m.drv-fault"), 0);  // not enabled: no fault
  CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 8);
  PIN_BIT("m.enable") = 1;
  mm_run(1);
  CHECK_EQ(PIN_BIT("m.drv-fault"), 1);
  CHECK_EQ(PIN_S32("m.fault-reason"), 7);
  PIN_S32("m.opmode-cmd") = 6;  // homing goes by its own pins
  PIN_BIT("m.enable") = 0;
  mm_run(1);
  PIN_BIT("m.enable") = 1;
  mm_run(1);
  CHECK_EQ(PIN_S32("m.fault-reason"), 7);
}

static void test_mode_change_while_disabled(void) {
  int i;
  mm_drive(8, BIT(3));
  PIN_S32("m.opmode-cmd") = 3;
  mm_run(1);
  CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 3);
  PIN_FLOAT("m.velocity-cmd") = 500;
  PIN_BIT("m.enable") = 1;
  for (i = 0; i < 20; i++) mm_cycle();
  CHECK_EQ(mm.sw, SW_ENABLED);
  CHECK_EQ(mm.vel, 500);
  CHECK_EQ(PIN_BIT("m.at-speed"), 1);
  // 0: back to the file's mode
  PIN_BIT("m.enable") = 0;
  mm_run(20);
  PIN_S32("m.opmode-cmd") = 0;
  mm_run(1);
  CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 8);
}

static void test_mode_change_halts_first(void) {
  int i, switched = -1;
  mm_drive(3, BIT(1));
  PIN_FLOAT("m.velocity-cmd") = 500;
  PIN_BIT("m.enable") = 1;
  mm_run(20);
  CHECK_EQ(mm.vel, 500);
  // to PP while turning: halted, then switched when it stands still
  PIN_S32("m.opmode-cmd") = 1;
  for (i = 0; i < 30; i++) {
    unsigned cw = mm_cycle();
    if (switched < 0 && fake_get(S, 0x6060, 0) == 1) switched = i;
    if (switched < 0) {
      CHECK_EQ(cw & 0x100, 0x100);
      CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 3);
    }
  }
  CHECK(switched >= 5);
  CHECK_EQ(mm.vel, 0);
  CHECK_EQ(mm_mode(), 1);
  CHECK_EQ(PIN_BIT("m.drv-fault"), 0);
  CHECK_EQ(PIN_BIT("m.at-speed"), 0);
  // in PP: no move until pp-start
  int32_t at = mm.pos;
  PIN_FLOAT("m.pp-target") = at + 200;
  mm_run(20);
  CHECK_EQ(mm.pos, at);
  PIN_BIT("m.pp-start") = 1;
  for (i = 0; i < 60 && !PIN_BIT("m.pp-done"); i++) mm_cycle();
  CHECK_EQ(mm.pos, at + 200);
}

static void test_mode_change_waits_for_the_answer(void) {
  int i;
  // started from standstill and changed at once: "speed zero" is still the
  // old answer while the drive has begun to turn
  mm_drive(3, BIT(8));
  PIN_BIT("m.enable") = 1;
  mm_run(10);
  PIN_FLOAT("m.velocity-cmd") = 500;
  mm_run(1);
  PIN_S32("m.opmode-cmd") = 8;
  for (i = 0; i < 30; i++) {
    mm_cycle();
    if (fake_get(S, 0x6060, 0) == 8) CHECK_EQ(mm.vel, 0);
  }
  CHECK_EQ(mm_mode(), 8);
}

static void test_mode_change_from_csp_holds_the_position(void) {
  int i;
  mm_drive(8, BIT(3));
  PIN_BIT("m.enable") = 1;
  mm_run(5);
  PIN_FLOAT("m.pos-cmd") = 100;
  mm_run(1);
  CHECK_EQ(mm.pos, 100);
  // to PV: the target is held at the actual position while pos-cmd moves on
  PIN_S32("m.opmode-cmd") = 3;
  for (i = 0; i < 6; i++) {
    PIN_FLOAT("m.pos-cmd") += 10;
    mm_cycle();
  }
  CHECK_EQ(mm.pos, 100);
  CHECK_EQ(mm_mode(), 3);
  PIN_FLOAT("m.velocity-cmd") = 300;
  mm_run(10);
  CHECK_EQ(mm.vel, 300);
}

static void test_mode_change_into_csp_waits_for_the_command(void) {
  int i;
  mm_drive(3, BIT(8));
  PIN_BIT("m.enable") = 1;
  PIN_FLOAT("m.velocity-cmd") = 50;
  mm_run(20);
  PIN_FLOAT("m.velocity-cmd") = 0;
  mm_run(10);
  int32_t at = mm.pos;
  CHECK(at > 100);
  // to CSP with pos-cmd far away: held at the actual position
  PIN_FLOAT("m.pos-cmd") = 0;
  PIN_S32("m.opmode-cmd") = 8;
  mm_run(20);
  CHECK_EQ(mm_mode(), 8);
  CHECK_EQ(mm.pos, at);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), at);
  // pos-cmd brought to the drive: followed from then on
  PIN_FLOAT("m.pos-cmd") = at + 5;
  mm_run(1);
  CHECK_EQ(mm.pos, at + 5);
  for (i = 0; i < 10; i++) {
    PIN_FLOAT("m.pos-cmd") += 3;
    mm_run(1);
  }
  CHECK_EQ(mm.pos, at + 35);
}

static void test_zero_at_start_mode_change_into_csp(void) {
  lcec_slave_modparam_t mp[4] = {{.id = ECAT_MP_CIA402_MODE}, {.id = ECAT_MP_CIA402_MODES},
                                 {.id = ECAT_MP_CIA402_ZERO_AT_START}, {.id = -1}};
  mp[0].value.u32 = 3;
  mp[1].value.u32 = BIT(8);
  mp[2].value.u32 = 1;
  S = fake_slave("m", mm_entries, mp);
  CHECK_EQ(ecat_cia402_init(0, S), 0);
  fake_set(S, 0x6061, 0, 3);
  memset(&mm, 0, sizeof(mm));
  mm.ramp = 100;
  mm.sw = SW_READY;
  cw_now = 0;
  mm.pos = 1000;
  PIN_BIT("m.enable") = 1;
  PIN_FLOAT("m.velocity-cmd") = 50;
  mm_run(20);
  PIN_FLOAT("m.velocity-cmd") = 0;
  mm_run(10);
  CHECK_EQ(PIN_S32("m.pos-zero"), 1000);
  int32_t at = mm.pos;
  CHECK(at > 1100);
  // to CSP: held until pos-cmd, counted from the zero, is at the drive
  PIN_FLOAT("m.pos-cmd") = 0;
  PIN_S32("m.opmode-cmd") = 8;
  mm_run(20);
  CHECK_EQ(mm_mode(), 8);
  CHECK_EQ(mm.pos, at);
  PIN_FLOAT("m.pos-cmd") = at - 1000 + 5;
  mm_run(1);
  CHECK_EQ(mm.pos, at + 5);
}

static void test_mode_change_into_csv_waits_for_zero(void) {
  mm_drive(3, BIT(9));
  PIN_BIT("m.enable") = 1;
  mm_run(5);
  PIN_FLOAT("m.velocity-cmd") = 200;  // for PV and, later, CSV
  PIN_S32("m.opmode-cmd") = 9;
  mm_run(20);
  CHECK_EQ(mm_mode(), 9);
  CHECK_EQ(mm.vel, 0);  // not a step to 200
  PIN_FLOAT("m.velocity-cmd") = 0;
  mm_run(1);
  PIN_FLOAT("m.velocity-cmd") = 7;
  mm_run(2);
  CHECK_EQ(mm.vel, 7);
}

static void test_mode_change_not_taken(void) {
  int i;
  mm_drive(8, BIT(3));
  PIN_BIT("m.enable") = 1;
  mm_run(5);
  mm.stuck = 1;
  PIN_S32("m.opmode-cmd") = 3;
  for (i = 0; i < 1000; i++) mm_cycle();
  CHECK_EQ(PIN_BIT("m.drv-fault"), 0);
  mm_run(10);
  CHECK_EQ(PIN_BIT("m.drv-fault"), 1);
  CHECK_EQ(PIN_S32("m.fault-reason"), 6);
}

static void test_mode_change_drive_does_not_stop(void) {
  mm_drive(3, BIT(8));
  PIN_U32("m.stop-timeout-ms") = 50;
  PIN_FLOAT("m.velocity-cmd") = 500;
  PIN_BIT("m.enable") = 1;
  mm_run(20);
  mm.never_stops = 1;
  PIN_S32("m.opmode-cmd") = 8;
  mm_run(45);
  CHECK_EQ(PIN_BIT("m.drv-fault"), 0);
  mm_run(10);
  CHECK_EQ(PIN_BIT("m.drv-fault"), 1);
  CHECK_EQ(PIN_S32("m.fault-reason"), 6);
  CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 3);  // never switched while turning
}

static void test_mode_change_asked_back(void) {
  mm_drive(3, BIT(8));
  PIN_FLOAT("m.velocity-cmd") = 500;
  PIN_BIT("m.enable") = 1;
  mm_run(20);
  PIN_S32("m.opmode-cmd") = 8;
  mm_run(2);
  CHECK(mm.vel < 500);
  PIN_S32("m.opmode-cmd") = 0;
  mm_run(20);
  CHECK_EQ(mm_mode(), 3);
  CHECK_EQ(mm.vel, 500);
  CHECK_EQ(PIN_BIT("m.drv-fault"), 0);
}

// ---- homing among the modes ------------------------------------------

static void test_homing_halts_pv_first(void) {
  int i, switched = -1;
  mm_drive(3, BIT(6));
  mm.home = 4000;
  PIN_FLOAT("m.velocity-cmd") = 500;
  PIN_BIT("m.enable") = 1;
  mm_run(20);
  CHECK_EQ(mm.vel, 500);
  // homing while turning: halted, then HM once it stands still
  PIN_BIT("m.home") = 1;
  for (i = 0; i < 30 && switched < 0; i++) {
    unsigned cw = mm_cycle();
    if (switched < 0 && fake_get(S, 0x6060, 0) == 6) {
      switched = i;
      CHECK_EQ(mm.vel, 0);
    }
    if (switched < 0) {
      CHECK_EQ(cw & 0x110, 0x100);
      CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 3);
      CHECK_EQ((int32_t)fake_get(S, 0x60FF, 0), 0);
      CHECK_EQ(PIN_BIT("m.stat-homing"), 1);
    }
  }
  CHECK(switched >= 5);
  for (i = 0; i < 40 && PIN_BIT("m.stat-homing"); i++) mm_cycle();
  CHECK_EQ(PIN_BIT("m.stat-homed"), 1);
  CHECK_EQ(mm_mode(), 3);
  CHECK_EQ(mm.pos, 4000);
  // back in PV: it ramps to the command again
  mm_run(20);
  CHECK_EQ(mm.vel, 500);
  CHECK_EQ(PIN_BIT("m.drv-fault"), 0);
}

static void test_homing_halts_pp_first(void) {
  int i, switched = -1;
  mm_drive(1, BIT(6));
  PIN_BIT("m.enable") = 1;
  mm_run(10);
  PIN_FLOAT("m.pp-target") = 1000;
  PIN_BIT("m.pp-start") = 1;
  mm_run(20);
  CHECK(mm.vel != 0);
  PIN_BIT("m.home") = 1;
  for (i = 0; i < 20; i++) {
    unsigned cw = mm_cycle();
    if (switched < 0 && fake_get(S, 0x6060, 0) == 6) {
      switched = i;
      CHECK_EQ(mm.vel, 0);
    }
    if (switched < 0) CHECK_EQ(cw & 0x100, 0x100);
  }
  CHECK(switched >= 2);  // the status answers after two cycles
  for (i = 0; i < 40 && PIN_BIT("m.stat-homing"); i++) mm_cycle();
  CHECK_EQ(PIN_BIT("m.stat-homed"), 1);
  CHECK_EQ(mm_mode(), 1);
  // the move does not go on by itself
  int32_t at = mm.pos;
  mm_run(20);
  CHECK_EQ(mm.pos, at);
  CHECK_EQ(PIN_BIT("m.pp-done"), 0);
  CHECK_EQ(PIN_BIT("m.drv-fault"), 0);
}

static void test_homing_pv_drive_does_not_stop(void) {
  int i;
  mm_drive(3, BIT(6));
  PIN_FLOAT("m.velocity-cmd") = 500;
  PIN_BIT("m.enable") = 1;
  mm_run(20);
  mm.never_stops = 1;
  PIN_U32("m.stop-timeout-ms") = 20;
  PIN_BIT("m.home") = 1;
  for (i = 0; i < 40 && !PIN_BIT("m.drv-fault"); i++) {
    mm_cycle();
    CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 3);
  }
  CHECK_EQ(PIN_BIT("m.drv-fault"), 1);
  CHECK_EQ(PIN_S32("m.fault-reason"), 6);
  CHECK_EQ(PIN_BIT("m.stat-homing"), 0);
}

static void test_homing_csp_waits_for_the_command(void) {
  int i;
  homing_drive();
  hm.home = 500;
  fake_set(S, 0x6064, 0, 77);
  PIN_FLOAT("x.pos-cmd") = 77;
  hm_enable();
  PIN_BIT("x.home") = 1;
  for (i = 0; i < 60 && !PIN_BIT("x.stat-homed"); i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.stat-homed"), 1);
  // pos-cmd is still where the drive was before homing: no jump back
  for (i = 0; i < 5; i++) {
    hm_cycle();
    CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 500);
  }
  PIN_FLOAT("x.pos-cmd") = 505;
  hm_cycle();
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 505);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
}

static void test_homing_pp_drive_does_not_stop(void) {
  int i;
  mm_drive(1, BIT(6));
  PIN_BIT("m.enable") = 1;
  mm_run(10);
  PIN_FLOAT("m.pp-target") = 100000;
  PIN_BIT("m.pp-start") = 1;
  mm_run(20);
  mm.never_stops = 1;
  PIN_U32("m.stop-timeout-ms") = 20;
  PIN_BIT("m.home") = 1;
  for (i = 0; i < 40 && !PIN_BIT("m.drv-fault"); i++) {
    mm_cycle();
    CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 1);
  }
  CHECK_EQ(PIN_S32("m.fault-reason"), 6);
}

static void test_homing_abandoned_while_halting(void) {
  int i;
  mm_drive(1, BIT(6));
  PIN_BIT("m.enable") = 1;
  mm_run(10);
  PIN_BIT("m.index-enable") = 1;
  mm_cycle();
  CHECK_EQ(PIN_BIT("m.stat-homing"), 1);
  PIN_BIT("m.index-enable") = 0;
  for (i = 0; i < 20; i++) {
    CHECK_EQ(mm_cycle() & 0x100, 0);  // no longer halted
    CHECK_EQ((int8_t)fake_get(S, 0x6060, 0), 1);
  }
  CHECK_EQ(PIN_BIT("m.stat-homing"), 0);
  CHECK_EQ(PIN_BIT("m.drv-fault"), 0);
}

// ---- zero at start -----------------------------------------------------

static lcec_slave_t *zero_drive(unsigned modes) {
  lcec_slave_modparam_t mp[3] = {{.id = ECAT_MP_CIA402_ZERO_AT_START}, {.id = ECAT_MP_CIA402_MODES}, {.id = -1}};
  mp[0].value.u32 = 1;
  mp[1].value.u32 = modes;
  if (!modes) mp[1].id = -1;
  S = fake_slave("x", csp_entries, mp);
  CHECK_EQ(ecat_cia402_init(0, S), 0);
  fake_set(S, 0x6061, 0, 8);
  return S;
}

static void test_zero_pin_only_with_the_option(void) {
  csp_drive();
  CHECK(fake_find("x.pos-zero") == NULL);
  zero_drive(0);
  CHECK(fake_find("x.pos-zero") != NULL);
}

static void test_zero_at_start(void) {
  int i;
  zero_drive(0);
  PIN_FLOAT("x.pos-scale") = 100;
  // before the bus is up, nothing is taken and pos-fb is 0
  fake_bus(S, 0);
  fake_set(S, 0x6064, 0, 999);
  cycle(SW_READY);
  CHECK_NEAR(PIN_FLOAT("x.pos-fb"), 0, 0);
  CHECK_EQ(PIN_S32("x.pos-zero"), 0);
  // the first cycle in OP takes the drive's position
  fake_bus(S, 1);
  fake_set(S, 0x6064, 0, 12345);
  cycle(SW_READY);
  CHECK_EQ(PIN_S32("x.pos-zero"), 12345);
  CHECK_NEAR(PIN_FLOAT("x.pos-fb"), 0, 0);
  CHECK_EQ(PIN_S32("x.drv-actual-position"), 12345);
  // motion follows pos-fb while disabled; enabling does not move the drive
  PIN_FLOAT("x.pos-cmd") = 0;
  PIN_BIT("x.enable") = 1;
  start(SW_READY);
  run(5);
  CHECK_EQ(sw_now, SW_ENABLED);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 12345);
  // the command counts from there
  PIN_FLOAT("x.pos-cmd") = 0.5;
  cycle(SW_ENABLED);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 12395);
  CHECK_EQ(PIN_S32("x.drv-target-position"), 12395);
  fake_set(S, 0x6064, 0, 12395);
  cycle(SW_ENABLED);
  CHECK_NEAR(PIN_FLOAT("x.pos-fb"), 0.5, 1e-12);
  // taken once: a bus loss and return keeps it
  PIN_BIT("x.enable") = 0;
  fake_bus(S, 0);
  for (i = 0; i < 3; i++) cycle(SW_READY);
  fake_bus(S, 1);
  fake_set(S, 0x6064, 0, 20000);
  cycle(SW_READY);
  CHECK_EQ(PIN_S32("x.pos-zero"), 12345);
  CHECK_NEAR(PIN_FLOAT("x.pos-fb"), 76.55, 1e-9);
}

static void test_zero_at_start_wraps_as_the_drive(void) {
  zero_drive(0);
  fake_set(S, 0x6064, 0, (uint32_t)(INT32_MAX - 5));
  cycle(SW_READY);
  CHECK_EQ(PIN_S32("x.pos-zero"), INT32_MAX - 5);
  PIN_BIT("x.enable") = 1;
  start(SW_READY);
  run(5);
  PIN_FLOAT("x.pos-cmd") = 10;  // 10 counts on: past the drive's INT32_MAX
  cycle(SW_ENABLED);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), INT32_MIN + 4);
  fake_set(S, 0x6064, 0, (uint32_t)(INT32_MIN + 4));
  cycle(SW_ENABLED);
  CHECK_NEAR(PIN_FLOAT("x.pos-fb"), 10, 0);
}

static void test_zero_at_start_cleared_by_homing(void) {
  int i;
  zero_drive(1u << 6);
  memset(&hm, 0, sizeof(hm));
  hm.cycles = 30;
  hm.home = 0;
  PIN_FLOAT("x.pos-scale") = 100;
  fake_set(S, 0x6064, 0, 5000);
  hm_enable();
  CHECK_EQ(PIN_S32("x.pos-zero"), 5000);
  CHECK_NEAR(PIN_FLOAT("x.pos-fb"), 0, 0);
  PIN_BIT("x.home") = 1;
  for (i = 0; i < 60 && !PIN_BIT("x.stat-homed"); i++) hm_cycle();
  for (i = 0; i < 10; i++) hm_cycle();
  CHECK_EQ(PIN_BIT("x.stat-homed"), 1);
  CHECK_EQ(PIN_BIT("x.stat-homing"), 0);
  // the drive's position is the homed one: no offset any more
  CHECK_EQ(PIN_S32("x.pos-zero"), 0);
  CHECK_NEAR(PIN_FLOAT("x.pos-fb"), 0, 0);
  CHECK_EQ(PIN_BIT("x.drv-fault"), 0);
}

static void test_zero_at_start_pp(void) {
  int i;
  lcec_slave_modparam_t mp[3] = {{.id = ECAT_MP_CIA402_MODE}, {.id = ECAT_MP_CIA402_ZERO_AT_START}, {.id = -1}};
  mp[0].value.u32 = 1;
  mp[1].value.u32 = 1;
  S = fake_slave("t", pp_entries, mp);
  CHECK_EQ(ecat_cia402_init(0, S), 0);
  fake_set(S, 0x6061, 0, 1);
  memset(&ppd, 0, sizeof(ppd));
  ppd.step = 10;
  ppd.sw = SW_READY;
  cw_now = 0;
  PIN_FLOAT("t.pos-scale") = 10;
  ppd.pos = 123;
  pp_enable();
  CHECK_EQ(PIN_S32("t.pos-zero"), 123);
  // absolute: from the zero
  PIN_FLOAT("t.pp-target") = 5;
  PIN_BIT("t.pp-start") = 1;
  for (i = 0; i < 100 && !PIN_BIT("t.pp-done"); i++) pp_cycle();
  CHECK_EQ(ppd.pos, 173);
  CHECK_NEAR(PIN_FLOAT("t.pos-fb"), 5, 1e-12);
  // relative: as it is
  PIN_BIT("t.pp-start") = 0;
  pp_cycle();
  PIN_BIT("t.pp-relative") = 1;
  PIN_FLOAT("t.pp-target") = 2;
  PIN_BIT("t.pp-start") = 1;
  for (i = 0; i < 5; i++) pp_cycle();
  for (i = 0; i < 100 && !PIN_BIT("t.pp-done"); i++) pp_cycle();
  CHECK_EQ(ppd.pos, 193);
  CHECK_NEAR(PIN_FLOAT("t.pos-fb"), 7, 1e-12);
}

// ---- several axes ----------------------------------------------------

static const fake_entry_t two_axes[] = {
    {0x6040, 0, 16}, {0x607A, 0, 32}, {0x6840, 0, 16}, {0x687A, 0, 32},  // outputs
    {0x6041, 0, 16}, {0x6064, 0, 32}, {0x6841, 0, 16}, {0x6864, 0, 32},  // inputs
    FAKE_END,
};

static void test_two_axes(void) {
  lcec_slave_modparam_t mp[3] = {{.id = ECAT_MP_CIA402_AXIS}, {.id = ECAT_MP_CIA402_AXIS}, {.id = -1}};
  strcpy(mp[0].value.str, "x1");
  strcpy(mp[1].value.str, "x2");
  S = fake_slave("xy", two_axes, mp);
  CHECK_EQ(ecat_cia402_init(0, S), 0);
  // each axis has its own pins, under its own name; none under the slave's
  CHECK(fake_find("x1.enable") != NULL && fake_find("x2.enable") != NULL);
  CHECK(fake_find("xy.enable") == NULL);
  PIN_FLOAT("x1.pos-scale") = 10.0;
  PIN_FLOAT("x2.pos-scale") = 100.0;

  // x2 is enabled, x1 not; their status words are their own
  fake_set(S, 0x6041, 0, SW_SWITCH_ON_DISABLED);
  fake_set(S, 0x6064, 0, 500);
  fake_set(S, 0x6864, 0, 7000);
  PIN_BIT("x2.enable") = 1;
  unsigned sw2 = SW_SWITCH_ON_DISABLED, cw2 = 0;
  int i;
  for (i = 0; i < 10; i++) {
    unsigned cw;
    fake_set(S, 0x6841, 0, sw2);
    PIN_FLOAT("x2.pos-cmd") = PIN_FLOAT("x2.pos-fb");
    S->proc_read(S, PERIOD);
    S->proc_write(S, PERIOD);
    cw = fake_get(S, 0x6840, 0);
    sw2 = drive(sw2, cw, cw2);
    cw2 = cw;
  }
  CHECK_EQ(PIN_BIT("x2.stat-op-enabled"), 1);
  CHECK_EQ(PIN_BIT("x1.stat-op-enabled"), 0);
  CHECK((fake_get(S, 0x6040, 0) & 0x0F) != 0x0F);  // x1 is not enabled
  CHECK_NEAR(PIN_FLOAT("x1.pos-fb"), 50.0, 1e-9);
  CHECK_NEAR(PIN_FLOAT("x2.pos-fb"), 70.0, 1e-9);
  // x2 follows its command into its own target; x1 holds its actual position
  PIN_FLOAT("x2.pos-cmd") = 70.5;
  fake_set(S, 0x6841, 0, sw2);
  S->proc_read(S, PERIOD);
  S->proc_write(S, PERIOD);
  CHECK_EQ((int32_t)fake_get(S, 0x687A, 0), 7050);
  CHECK_EQ((int32_t)fake_get(S, 0x607A, 0), 500);
}

RUN_TESTS("cia402", test_defaults, test_needs_the_control_and_status_words, test_held_ready_without_enable,
    test_enables_step_by_step, test_target_holds_actual_until_enabled, test_position_is_clamped,
    test_feedback_and_status_pins, test_states, test_wrong_mode_is_not_enabled, test_enable_timeout, test_no_timeout_when_zero,
    test_fault_while_enabled_shuts_down_until_enable_drops, test_fault_reset_on_enable,
    test_no_fault_reset_on_enable_without_auto, test_fault_reset_is_held_at_most_100_ms,
    test_fault_reset_pin_without_enable, test_drive_leaving_enabled, test_bus_loss, DC_SYNC_TESTS test_watchdog,
    test_command_not_a_number, test_csv, test_homing_pins_only_with_hm, test_homing_needs_the_mode_objects,
    test_homing_by_index_enable, test_homing_by_the_home_pin, test_homing_error, test_homing_timeout,
    test_homing_abandoned_by_motion, test_homing_mode_not_taken, test_homing_slow_way_back, test_homing_stopped_by_disable,
    test_homing_not_while_disabled, test_homing_with_stale_result_bits, test_pv_pins, test_pv_needs_target_velocity,
    test_pv_runs_at_the_command, test_pv_halts_before_switching_off, test_pv_enable_again_while_halting,
    test_pv_stop_timeout, test_pv_standstill_switches_off_after_the_status_answers, test_pv_halts_on_a_fault,
    test_pv_not_at_speed_in_another_mode, test_pp_pins, test_pp_needs_positions, test_pp_moves_only_on_start,
    test_pp_done_not_from_an_old_target_reached, test_pp_relative_and_a_new_move, test_pp_start_while_acknowledging, test_pp_halt,
    test_pp_halts_before_switching_off, test_pp_timeouts, test_pp_timeout_paused_while_halted,
    test_pp_start_waits_for_the_mode, test_pp_bad_command, test_opmode_cmd_only_with_more_modes,
    test_opmode_cmd_not_in_the_file, test_mode_change_while_disabled, test_mode_change_halts_first, test_mode_change_waits_for_the_answer,
    test_mode_change_from_csp_holds_the_position, test_mode_change_into_csp_waits_for_the_command,
    test_mode_change_into_csv_waits_for_zero, test_mode_change_not_taken, test_mode_change_drive_does_not_stop,
    test_mode_change_asked_back, test_homing_halts_pv_first, test_homing_halts_pp_first,
    test_homing_pv_drive_does_not_stop, test_homing_csp_waits_for_the_command,
    test_homing_pp_drive_does_not_stop, test_homing_abandoned_while_halting, test_zero_pin_only_with_the_option,
    test_zero_at_start, test_zero_at_start_wraps_as_the_drive, test_zero_at_start_cleared_by_homing, test_zero_at_start_pp,
    test_zero_at_start_mode_change_into_csp, test_two_axes)
