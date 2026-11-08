//
//    ecat: the cia402 profile, for CiA 402 (ETG.6010) drives.
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
/// @brief The cia402 profile: the CiA 402 state machine and scaled pins.
///
/// The profile owns the drive's control and status words, modes of
/// operation, target and actual position and velocity, and error code.
/// Their entries are `use: profile` in ethercat.yaml; the slave's other
/// entries get ordinary pins from the generic driver.
///
/// The pins follow the cia402 HAL component (hal-cia402), so that a HAL
/// file that used lcec and cia402 needs few changes.
///
/// Safety, in every cycle of the servo thread:
/// - The drive is enabled only while `enable` is true, and only in the mode
///   from the configuration (if the drive reports its mode).
/// - Until the drive is in "Operation enabled", the target position is the
///   actual position (CSP) or the target velocity 0 (CSV), so it does not
///   move when it is enabled.
/// - If the drive faults, leaves "Operation enabled" by itself, does not
///   get there within `enable-timeout-ms`, the bus stops exchanging data, or
///   the `watchdog` pin stops changing, the drive is shut down and
///   `drv-fault` is set. It stays shut down until `enable` goes false;
///   `fault-reason` says why.

#include "ecat_cia402.h"

#include <errno.h>
#include <math.h>
#include <stdint.h>

#include "lcec_generic.h"

// CiA 402 objects
#define OBJ_ERROR_CODE 0x603F
#define OBJ_CONTROLWORD 0x6040
#define OBJ_STATUSWORD 0x6041
#define OBJ_MODES 0x6060
#define OBJ_MODES_DISPLAY 0x6061
#define OBJ_ACTUAL_POSITION 0x6064
#define OBJ_ACTUAL_VELOCITY 0x606C
#define OBJ_TARGET_POSITION 0x607A
#define OBJ_TARGET_VELOCITY 0x60FF

#define MODE_CSP 8
#define MODE_CSV 9
#define MODE_HOMING 6

// Control words (ETG.6010 table 3)
#define CW_DISABLE_VOLTAGE 0x0000
#define CW_SHUTDOWN 0x0006
#define CW_SWITCH_ON 0x0007
#define CW_ENABLE_OPERATION 0x000F
#define CW_FAULT_RESET 0x0080

// States, from the status word; the value of the cia-state pin.
enum {
  ST_UNKNOWN = -1,
  ST_NOT_READY = 0,
  ST_SWITCH_ON_DISABLED,
  ST_READY_TO_SWITCH_ON,
  ST_SWITCHED_ON,
  ST_OPERATION_ENABLED,
  ST_QUICK_STOP_ACTIVE,
  ST_FAULT_REACTION_ACTIVE,
  ST_FAULT,
};

// Why the drive was shut down; the value of the fault-reason pin.
enum {
  WHY_NONE = 0,
  WHY_DRIVE_FAULT,       // the drive is in Fault
  WHY_BUS,               // no valid process data (working counter, slave not OP)
  WHY_ENABLE_TIMEOUT,    // "Operation enabled" not reached in time
  WHY_LEFT_ENABLED,      // the drive left "Operation enabled" by itself
  WHY_WATCHDOG,          // the watchdog pin stopped changing
  WHY_WRONG_MODE,        // the drive does not report the configured mode
};

#define FAULT_RESET_NS 100000000LL  // how long fault reset is held at most

typedef struct {
  unsigned int os, bp;
  int mapped;
} obj_t;

typedef struct {
  // in
  hal_bit_t *enable;
  hal_float_t *pos_cmd;
  hal_float_t *velocity_cmd;
  hal_bit_t *fault_reset;
  hal_u32_t *watchdog;
  // out
  hal_float_t *pos_fb;
  hal_float_t *velocity_fb;
  hal_bit_t *drv_fault;
  hal_s32_t *fault_reason;
  hal_s32_t *cia_state;
  hal_u32_t *statusword;
  hal_u32_t *controlword;
  hal_s32_t *opmode;
  hal_s32_t *opmode_display;
  hal_u32_t *error_code;
  hal_s32_t *drv_actual_position;
  hal_s32_t *drv_actual_velocity;
  hal_s32_t *drv_target_position;
  hal_s32_t *drv_target_velocity;
  hal_bit_t *stat_switchon_ready;
  hal_bit_t *stat_switched_on;
  hal_bit_t *stat_op_enabled;
  hal_bit_t *stat_fault;
  hal_bit_t *stat_voltage_enabled;
  hal_bit_t *stat_quick_stop;
  hal_bit_t *stat_switchon_disabled;
  hal_bit_t *stat_warning;
  hal_bit_t *stat_remote;
  hal_bit_t *stat_target_reached;
  hal_bit_t *opmode_no_mode;
  hal_bit_t *opmode_homing;
  hal_bit_t *opmode_cyclic_position;
  hal_bit_t *opmode_cyclic_velocity;
  // parameters
  hal_float_t pos_scale;
  hal_float_t velo_scale;
  hal_bit_t auto_fault_reset;
  hal_u32_t enable_timeout_ms;
  hal_u32_t watchdog_ms;
  hal_s32_t mode;  // read only: 8 CSP, 9 CSV

  // process data
  obj_t error_code_o, controlword_o, statusword_o, modes_o, modes_display_o;
  obj_t actual_position_o, actual_velocity_o, target_position_o, target_velocity_o;

  // state
  int state;
  int bus_ok;
  int enable_old;
  int fault_reset_old;
  int blocked;          // shut down until enable goes false
  int was_enabled;      // the drive reached "Operation enabled" for this enable
  long long enable_ns;  // time since enable, until "Operation enabled"
  long long reset_ns;   // fault reset in progress, time left
  int reset_start;      // first cycle of a fault reset: bit 7 low
  uint32_t watchdog_old;
  long long watchdog_ns;
} cia402_t;

static const lcec_pindesc_t pins[] = {
    {HAL_BIT, HAL_IN, offsetof(cia402_t, enable), "%s.enable"},
    {HAL_FLOAT, HAL_IN, offsetof(cia402_t, pos_cmd), "%s.pos-cmd"},
    {HAL_FLOAT, HAL_IN, offsetof(cia402_t, velocity_cmd), "%s.velocity-cmd"},
    {HAL_BIT, HAL_IN, offsetof(cia402_t, fault_reset), "%s.fault-reset"},
    {HAL_U32, HAL_IN, offsetof(cia402_t, watchdog), "%s.watchdog"},
    {HAL_FLOAT, HAL_OUT, offsetof(cia402_t, pos_fb), "%s.pos-fb"},
    {HAL_FLOAT, HAL_OUT, offsetof(cia402_t, velocity_fb), "%s.velocity-fb"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, drv_fault), "%s.drv-fault"},
    {HAL_S32, HAL_OUT, offsetof(cia402_t, fault_reason), "%s.fault-reason"},
    {HAL_S32, HAL_OUT, offsetof(cia402_t, cia_state), "%s.cia-state"},
    {HAL_U32, HAL_OUT, offsetof(cia402_t, statusword), "%s.statusword"},
    {HAL_U32, HAL_OUT, offsetof(cia402_t, controlword), "%s.controlword"},
    {HAL_S32, HAL_OUT, offsetof(cia402_t, opmode), "%s.opmode"},
    {HAL_S32, HAL_OUT, offsetof(cia402_t, opmode_display), "%s.opmode-display"},
    {HAL_U32, HAL_OUT, offsetof(cia402_t, error_code), "%s.error-code"},
    {HAL_S32, HAL_OUT, offsetof(cia402_t, drv_actual_position), "%s.drv-actual-position"},
    {HAL_S32, HAL_OUT, offsetof(cia402_t, drv_actual_velocity), "%s.drv-actual-velocity"},
    {HAL_S32, HAL_OUT, offsetof(cia402_t, drv_target_position), "%s.drv-target-position"},
    {HAL_S32, HAL_OUT, offsetof(cia402_t, drv_target_velocity), "%s.drv-target-velocity"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, stat_switchon_ready), "%s.stat-switchon-ready"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, stat_switched_on), "%s.stat-switched-on"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, stat_op_enabled), "%s.stat-op-enabled"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, stat_fault), "%s.stat-fault"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, stat_voltage_enabled), "%s.stat-voltage-enabled"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, stat_quick_stop), "%s.stat-quick-stop"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, stat_switchon_disabled), "%s.stat-switchon-disabled"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, stat_warning), "%s.stat-warning"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, stat_remote), "%s.stat-remote"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, stat_target_reached), "%s.stat-target-reached"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, opmode_no_mode), "%s.opmode-no-mode"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, opmode_homing), "%s.opmode-homing"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, opmode_cyclic_position), "%s.opmode-cyclic-position"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, opmode_cyclic_velocity), "%s.opmode-cyclic-velocity"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static const lcec_paramdesc_t params[] = {
    {HAL_FLOAT, HAL_RW, offsetof(cia402_t, pos_scale), "%s.pos-scale"},
    {HAL_FLOAT, HAL_RW, offsetof(cia402_t, velo_scale), "%s.velo-scale"},
    {HAL_BIT, HAL_RW, offsetof(cia402_t, auto_fault_reset), "%s.auto-fault-reset"},
    {HAL_U32, HAL_RW, offsetof(cia402_t, enable_timeout_ms), "%s.enable-timeout-ms"},
    {HAL_U32, HAL_RW, offsetof(cia402_t, watchdog_ms), "%s.watchdog-ms"},
    {HAL_S32, HAL_RO, offsetof(cia402_t, mode), "%s.mode"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static void cia402_read(lcec_slave_t *slave, long period);
static void cia402_write(lcec_slave_t *slave, long period);

/// Find an object in the slave's process data and register it.
static int find(lcec_slave_t *slave, uint16_t index, obj_t *o) {
  const ec_sync_info_t *sm;
  unsigned int i, j;

  for (sm = slave->generic_sync_managers; sm != NULL && sm->index != 0xff; sm++) {
    for (i = 0; i < sm->n_pdos; i++) {
      for (j = 0; j < sm->pdos[i].n_entries; j++) {
        if (sm->pdos[i].entries[j].index == index && sm->pdos[i].entries[j].subindex == 0) {
          o->mapped = 1;
          return lcec_pdo_init(slave, index, 0, &o->os, &o->bp);
        }
      }
    }
  }
  o->mapped = 0;
  return 0;
}

int ecat_cia402_init(int comp_id, lcec_slave_t *slave) {
  lcec_slave_modparam_t *p;
  cia402_t *d;
  int err;

  if ((err = lcec_generic_init(comp_id, slave)) != 0) return err;

  d = LCEC_HAL_ALLOCATE(cia402_t);
  slave->profile_data = d;
  slave->proc_read = cia402_read;
  slave->proc_write = cia402_write;

  if (find(slave, OBJ_ERROR_CODE, &d->error_code_o) || find(slave, OBJ_CONTROLWORD, &d->controlword_o) ||
      find(slave, OBJ_STATUSWORD, &d->statusword_o) || find(slave, OBJ_MODES, &d->modes_o) ||
      find(slave, OBJ_MODES_DISPLAY, &d->modes_display_o) || find(slave, OBJ_ACTUAL_POSITION, &d->actual_position_o) ||
      find(slave, OBJ_ACTUAL_VELOCITY, &d->actual_velocity_o) || find(slave, OBJ_TARGET_POSITION, &d->target_position_o) ||
      find(slave, OBJ_TARGET_VELOCITY, &d->target_velocity_o)) {
    return -EIO;
  }
  // ecat_config.c has checked these; a safety net
  if (!d->controlword_o.mapped || !d->statusword_o.mapped) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s: cia402 needs 0x6040 and 0x6041 in the process data\n", slave->name);
    return -EINVAL;
  }

  if ((err = lcec_pin_newf_list(d, pins, slave->name)) != 0) return err;
  if ((err = lcec_param_newf_list(d, params, slave->name)) != 0) return err;

  // after the parameters are made, which zeroes them
  d->mode = MODE_CSP;
  for (p = slave->modparams; p != NULL && p->id != -1; p++) {
    if (p->id == ECAT_MP_CIA402_MODE) d->mode = p->value.u32;
  }
  d->pos_scale = 1.0;
  d->velo_scale = 1.0;
  d->auto_fault_reset = 1;
  d->enable_timeout_ms = 2000;
  d->watchdog_ms = 0;
  d->state = ST_UNKNOWN;
  return 0;
}

static int decode_state(uint16_t sw) {
  if ((sw & 0x4F) == 0x00) return ST_NOT_READY;
  if ((sw & 0x4F) == 0x40) return ST_SWITCH_ON_DISABLED;
  if ((sw & 0x6F) == 0x21) return ST_READY_TO_SWITCH_ON;
  if ((sw & 0x6F) == 0x23) return ST_SWITCHED_ON;
  if ((sw & 0x6F) == 0x27) return ST_OPERATION_ENABLED;
  if ((sw & 0x6F) == 0x07) return ST_QUICK_STOP_ACTIVE;
  if ((sw & 0x4F) == 0x0F) return ST_FAULT_REACTION_ACTIVE;
  if ((sw & 0x4F) == 0x08) return ST_FAULT;
  return ST_UNKNOWN;
}

static int32_t clamp_s32(double v) {
  if (!(v > -2147483648.0)) return INT32_MIN;  // also NaN
  if (v > 2147483647.0) return INT32_MAX;
  return (int32_t)lrint(v);
}

static double scale_or_one(hal_float_t s) { return (s > -1e-20 && s < 1e-20) ? 1.0 : s; }

static void cia402_read(lcec_slave_t *slave, long period) {
  cia402_t *d = slave->profile_data;
  uint8_t *pd = slave->master->process_data;
  uint16_t sw;
  int8_t md = 0;

  lcec_generic_read(slave, period);

  d->bus_ok = slave->master->domain_state.wc_state == EC_WC_COMPLETE && slave->state.operational;
  sw = EC_READ_U16(&pd[d->statusword_o.os]);
  d->state = d->bus_ok ? decode_state(sw) : ST_UNKNOWN;

  *d->statusword = sw;
  *d->cia_state = d->state;
  *d->stat_switchon_ready = (sw >> 0) & 1;
  *d->stat_switched_on = (sw >> 1) & 1;
  *d->stat_op_enabled = (sw >> 2) & 1;
  *d->stat_fault = (sw >> 3) & 1;
  *d->stat_voltage_enabled = (sw >> 4) & 1;
  *d->stat_quick_stop = (sw >> 5) & 1;
  *d->stat_switchon_disabled = (sw >> 6) & 1;
  *d->stat_warning = (sw >> 7) & 1;
  *d->stat_remote = (sw >> 9) & 1;

  if (d->modes_display_o.mapped) {
    md = EC_READ_S8(&pd[d->modes_display_o.os]);
    *d->opmode_display = md;
  }
  *d->opmode_no_mode = md == 0;
  *d->opmode_homing = md == MODE_HOMING;
  *d->opmode_cyclic_position = md == MODE_CSP;
  *d->opmode_cyclic_velocity = md == MODE_CSV;
  *d->stat_target_reached = (md == MODE_CSP || md == MODE_CSV) && ((sw >> 10) & 1);

  if (d->error_code_o.mapped) *d->error_code = EC_READ_U16(&pd[d->error_code_o.os]);
  if (d->actual_position_o.mapped) {
    *d->drv_actual_position = EC_READ_S32(&pd[d->actual_position_o.os]);
    *d->pos_fb = *d->drv_actual_position / scale_or_one(d->pos_scale);
  }
  if (d->actual_velocity_o.mapped) {
    *d->drv_actual_velocity = EC_READ_S32(&pd[d->actual_velocity_o.os]);
    *d->velocity_fb = *d->drv_actual_velocity / scale_or_one(d->velo_scale);
  }
}

static void block(cia402_t *d, int why) {
  if (!d->blocked) {
    d->blocked = 1;
    *d->fault_reason = why;
  }
}

static void cia402_write(lcec_slave_t *slave, long period) {
  cia402_t *d = slave->profile_data;
  uint8_t *pd = slave->master->process_data;
  int enable = *d->enable;
  int enable_edge = enable && !d->enable_old;
  int reset_edge = *d->fault_reset && !d->fault_reset_old;
  int faulted = d->state == ST_FAULT || d->state == ST_FAULT_REACTION_ACTIVE;
  int mode_ok = !d->modes_display_o.mapped || *d->opmode_display == d->mode;
  int run;
  uint16_t cw;

  d->enable_old = enable;
  d->fault_reset_old = *d->fault_reset;

  // watchdog: the pin must change within watchdog-ms
  if (d->watchdog_ms == 0 || *d->watchdog != d->watchdog_old) {
    d->watchdog_ns = 0;
  } else if (d->watchdog_ns < (long long)d->watchdog_ms * 1000000LL) {
    d->watchdog_ns += period;
  }
  d->watchdog_old = *d->watchdog;

  // fault reset: on the fault-reset pin, or on enable with auto-fault-reset
  if (faulted && (reset_edge || (enable_edge && d->auto_fault_reset))) {
    d->reset_ns = FAULT_RESET_NS;
    d->reset_start = 1;
  }
  if (!faulted) d->reset_ns = 0;

  if (!enable) {
    d->blocked = 0;
    d->was_enabled = 0;
    d->enable_ns = 0;
    *d->fault_reason = WHY_NONE;
  } else if (!d->blocked) {
    if (!d->bus_ok) {
      block(d, WHY_BUS);
    } else if (d->watchdog_ms != 0 && d->watchdog_ns >= (long long)d->watchdog_ms * 1000000LL) {
      block(d, WHY_WATCHDOG);
    } else if (faulted && d->reset_ns <= 0) {
      block(d, WHY_DRIVE_FAULT);
    } else if (d->was_enabled && d->state != ST_OPERATION_ENABLED) {
      block(d, faulted ? WHY_DRIVE_FAULT : WHY_LEFT_ENABLED);
    } else if (d->state != ST_OPERATION_ENABLED) {
      d->enable_ns += period;
      if (d->enable_timeout_ms != 0 && d->enable_ns > (long long)d->enable_timeout_ms * 1000000LL) {
        block(d, mode_ok ? WHY_ENABLE_TIMEOUT : WHY_WRONG_MODE);
      }
    }
  }
  run = enable && !d->blocked;

  // control word
  switch (d->state) {
    case ST_FAULT:
      // the drive resets on a rising edge of bit 7
      cw = (d->reset_ns > 0 && !d->reset_start) ? CW_FAULT_RESET : CW_DISABLE_VOLTAGE;
      d->reset_start = 0;
      break;
    case ST_FAULT_REACTION_ACTIVE:
    case ST_NOT_READY:
    case ST_QUICK_STOP_ACTIVE:
    case ST_UNKNOWN:
      cw = CW_DISABLE_VOLTAGE;
      break;
    case ST_SWITCH_ON_DISABLED:
      cw = CW_SHUTDOWN;
      break;
    case ST_READY_TO_SWITCH_ON:
      cw = run ? CW_SWITCH_ON : CW_SHUTDOWN;
      break;
    case ST_SWITCHED_ON:
      cw = (run && mode_ok) ? CW_ENABLE_OPERATION : run ? CW_SWITCH_ON : CW_SHUTDOWN;
      break;
    case ST_OPERATION_ENABLED:
      cw = run ? CW_ENABLE_OPERATION : CW_SHUTDOWN;
      break;
    default:
      cw = CW_DISABLE_VOLTAGE;
  }
  if (d->reset_ns > 0) d->reset_ns -= period;
  if (run && d->state == ST_OPERATION_ENABLED) d->was_enabled = 1;

  *d->drv_fault = enable && d->blocked;
  *d->controlword = cw;
  EC_WRITE_U16(&pd[d->controlword_o.os], cw);
  if (d->modes_o.mapped) {
    EC_WRITE_S8(&pd[d->modes_o.os], d->mode);
    *d->opmode = d->mode;
  }

  // Targets: follow the commands only in "Operation enabled", for this
  // enable; otherwise hold the actual position or stand still.
  run = run && d->state == ST_OPERATION_ENABLED;
  if (d->target_position_o.mapped) {
    if (run && d->mode == MODE_CSP) {
      *d->drv_target_position = clamp_s32(*d->pos_cmd * scale_or_one(d->pos_scale));
    } else {
      *d->drv_target_position = *d->drv_actual_position;
    }
    EC_WRITE_S32(&pd[d->target_position_o.os], *d->drv_target_position);
  }
  if (d->target_velocity_o.mapped) {
    if (run && d->mode == MODE_CSV) {
      *d->drv_target_velocity = clamp_s32(*d->velocity_cmd * scale_or_one(d->velo_scale));
    } else {
      *d->drv_target_velocity = 0;
    }
    EC_WRITE_S32(&pd[d->target_velocity_o.os], *d->drv_target_velocity);
  }

  lcec_generic_write(slave, period);
}
