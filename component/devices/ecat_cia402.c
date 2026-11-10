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
///   get there within `enable-timeout-ms`, the bus stops exchanging data,
///   the master's DC sync check for the drive timed out (its SYNC0 runs on
///   an unsynchronized clock), the `watchdog` pin stops changing, or the
///   command is not a finite
///   number (NaN or infinite), the drive is shut down and
///   `drv-fault` is set. It stays shut down until `enable` goes false;
///   `fault-reason` says why.
///
/// Profile velocity (PV), for a drive that is not a joint, such as a
/// spindle: the drive ramps to `velocity-cmd` itself. When the drive is to
/// stop running (enable false, or a shutdown), it is first halted
/// (control word bit 8) with its own deceleration, and switched off when
/// it reports zero speed or after `stop-timeout-ms`, instead of coasting.
///
/// Profile position (PP), for positioning that is not a joint: the drive
/// moves to `pp-target` on a rising edge of `pp-start`, with the set-point
/// handshake done here.
///
/// Changing modes (`opmode-cmd`, among the file's `mode` and `modes`): a
/// running drive is stopped first (halted in PV and PP), then 0x6060 is
/// written and 0x6061 must follow within a second, and the new mode starts
/// from the actual values: CSP follows `pos-cmd` only once it is within
/// SYNC_COUNTS of the actual position, CSV only once `velocity-cmd` is 0.
///
/// Homing by the drive's own method (HM), when the file allows mode `hm`:
/// a rising edge of `index-enable` (from LinuxCNC's motion, homing on an
/// index pulse) or of `home` switches the drive to HM, starts it, waits for
/// "homing attained" and "target reached", and switches it back with the
/// target at the actual position. In PV and PP the drive is halted first,
/// as for a mode change, and after homing CSP follows `pos-cmd` again only
/// once it is within SYNC_COUNTS of the new position. Started by `index-enable`, `pos-fb`
/// follows `pos-cmd` while the drive homes, so that motion sees no
/// following error, and `index-enable` is cleared at the end: `pos-fb`
/// then jumps to the drive's new position, as an encoder's count does at
/// its index. A homing error or timeout shuts the drive down.
///
/// Zero at start (`cia402.zero-at-start`): positions count from where the
/// drive stands when the bus first reaches OP, for drives that keep their
/// position from an earlier run. The drive's position at that moment is
/// kept in `pos-zero` (counts) and taken off `pos-fb` and added to the
/// drive's target, so `pos-fb` starts at 0; nothing is written to the
/// drive. `drv-actual-position` and `drv-target-position` stay the drive's
/// own counts. Homing by the drive sets `pos-zero` back to 0, since the
/// drive's position is then the homed one.

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
#define OBJ_PROFILE_VELOCITY 0x6081

#define MODE_PP 1
#define MODE_PV 3
#define MODE_CSP 8
#define MODE_CSV 9
#define MODE_HOMING 6
#define MODE_BIT(m) (1u << (m))

// Control words (ETG.6010 table 3)
#define CW_DISABLE_VOLTAGE 0x0000
#define CW_SHUTDOWN 0x0006
#define CW_SWITCH_ON 0x0007
#define CW_ENABLE_OPERATION 0x000F
#define CW_FAULT_RESET 0x0080
#define CW_HALT 0x0100
#define SW_SPEED_ZERO 0x1000  // in PV
// The status word answers a new control word or target this many cycles
// later, so a status bit is not taken as the answer before. Two cycles when
// SYNC0 falls after the frame has reached the drive (the frame of cycle n
// is used at SYNC0 in cycle n, and the answer comes with the frame of cycle
// n + 1, read in cycle n + 2); master.dc.sync0-shift-ns: auto places SYNC0
// so (ecat.0.turnaround-cycles = 2). Three cycles means a configuration
// error, such as SYNC0 before the frame: then status bits can be taken
// one cycle too early.
//
// To be confirmed in use (2026-11-09): two cycles is a choice, measured
// only with 0x6061 on ECT60 drives. A drive may need longer to update
// status bits such as "target reached" or "speed zero" (a slower internal
// cycle). If use shows that, allow or use more: a per-drive parameter, or
// a larger value here.
#define STATUS_CYCLES 2
#define CW_NEW_SET_POINT 0x0010       // PP
#define CW_CHANGE_IMMEDIATELY 0x0020  // PP
#define CW_RELATIVE 0x0040            // PP
#define SW_SET_POINT_ACK 0x1000       // in PP
// After a mode change into CSP, pos-cmd is followed once it is this close
// to the actual position, in counts
#define SYNC_COUNTS 10

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
  WHY_WRONG_MODE,        // the drive does not report the mode, or a mode change failed
  WHY_BAD_COMMAND,       // pos-cmd or velocity-cmd is not a finite number
  WHY_HOMING_ERROR,      // the drive reports a homing error
  WHY_HOMING_TIMEOUT,    // homing not done within home-timeout-ms
  WHY_PP_TIMEOUT,        // a PP move not done within pp-timeout-ms
  WHY_DC_SYNC,           // the master's DC sync check timed out: SYNC0 on an unsynchronized clock
};

// Profile position (PP): the set-point handshake.
enum {
  PP_IDLE = 0,  // no move since enable
  PP_SET,       // new set-point (bit 4) given, waiting for acknowledge (bit 12)
  PP_MOVING,    // acknowledged; waiting for target reached
  PP_DONE,      // at the target
};

// Changing modes (opmode-cmd).
enum {
  MC_RUN = 0,  // in the active mode
  MC_STOP,     // stopping the drive in the old mode
  MC_SWITCH,   // the new mode written, waiting for 0x6061 to follow
};

// Homing, by the drive's method (HM).
enum {
  HS_IDLE = 0,
  HS_STOP,     // PV, PP: halted, waiting for the drive to stand still
  HS_TO_HM,    // 0x6060 = 6 written, waiting for 0x6061 to follow
  HS_HOMING,   // started (control word bit 4), waiting for the result
  HS_BACK,     // back to the cyclic mode, waiting for 0x6061 to follow
};

#define MODE_CHANGE_NS 1000000000LL  // a mode change must be done within this
#define HOMING_SETTLE_NS 20000000LL  // see the homing result no earlier, unless homing was seen
#define SW_TARGET_REACHED 0x0400
#define SW_HOMING_ATTAINED 0x1000
#define SW_HOMING_ERROR 0x2000
#define CW_HOMING_START 0x0010

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
  // homing, only with mode hm
  hal_bit_t *index_enable;
  hal_bit_t *home;
  hal_bit_t *stat_homing;
  hal_bit_t *stat_homed;
  hal_bit_t *home_error;
  // PV only
  hal_bit_t *at_speed;
  hal_bit_t *zero_speed;
  // PP only
  hal_float_t *pp_target;
  hal_float_t *pp_velocity;
  hal_bit_t *pp_start;
  hal_bit_t *pp_relative;
  hal_bit_t *pp_halt;
  hal_bit_t *pp_done;
  // with more than one mode
  hal_s32_t *opmode_cmd;
  // with zero-at-start
  hal_s32_t *pos_zero;
  // parameters
  hal_float_t pos_scale;
  hal_float_t velo_scale;
  hal_bit_t auto_fault_reset;
  hal_u32_t enable_timeout_ms;
  hal_u32_t watchdog_ms;
  hal_s32_t mode;  // read only: 8 CSP, 9 CSV
  hal_u32_t home_timeout_ms;
  hal_u32_t stop_timeout_ms;
  hal_u32_t pp_timeout_ms;

  // process data
  obj_t error_code_o, controlword_o, statusword_o, modes_o, modes_display_o;
  obj_t actual_position_o, actual_velocity_o, target_position_o, target_velocity_o, profile_velocity_o;

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
  unsigned int allowed; // the modes the file allows, bit n for mode n
  int active;           // the mode in use
  int mc;               // MC_*
  int mc_to;            // the mode being changed to
  long long mc_ns;
  int mc_cycles;
  int sync;             // after a mode change: not following the command yet
  int can_home;         // mode hm is allowed
  int hs;               // HS_*
  long long hs_ns;      // time in this homing step
  int hs_seen;          // the drive was seen homing (not yet attained and reached)
  int hs_index;         // started by index-enable
  int hs_cycles;        // cycles in HS_STOP
  int index_old, home_old;
  long long stop_ns;    // PV, PP: time halting before switching off
  int stop_cycles;      // PV, PP: cycles halting
  int pp;               // PP_*
  long long pp_ns;      // PP: time of this move
  int pp_start_old;
  int pp_pending;       // PP: a start not yet given to the drive
  int32_t pp_set;       // PP: the set-point given, counts
  uint32_t pp_vel;      // PP: its profile velocity
  int pp_rel;           // PP: relative
  int pp_halt_wait;     // PP: cycles until target reached answers the end of a halt
  int32_t pv_target;    // PV: the target velocity written last
  int zero_at_start;    // cia402.zero-at-start
  int zeroed;           // pos-zero has been taken
  int32_t zero;         // counts taken off the drive's position
  int pv_wait;          // PV: cycles until "target reached" answers it
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

static const lcec_pindesc_t homing_pins[] = {
    {HAL_BIT, HAL_IO, offsetof(cia402_t, index_enable), "%s.index-enable"},
    {HAL_BIT, HAL_IN, offsetof(cia402_t, home), "%s.home"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, stat_homing), "%s.stat-homing"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, stat_homed), "%s.stat-homed"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, home_error), "%s.home-error"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static const lcec_paramdesc_t homing_params[] = {
    {HAL_U32, HAL_RW, offsetof(cia402_t, home_timeout_ms), "%s.home-timeout-ms"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static const lcec_pindesc_t pv_pins[] = {
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, at_speed), "%s.at-speed"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, zero_speed), "%s.zero-speed"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static const lcec_pindesc_t pp_pins[] = {
    {HAL_FLOAT, HAL_IN, offsetof(cia402_t, pp_target), "%s.pp-target"},
    {HAL_BIT, HAL_IN, offsetof(cia402_t, pp_start), "%s.pp-start"},
    {HAL_BIT, HAL_IN, offsetof(cia402_t, pp_relative), "%s.pp-relative"},
    {HAL_BIT, HAL_IN, offsetof(cia402_t, pp_halt), "%s.pp-halt"},
    {HAL_BIT, HAL_OUT, offsetof(cia402_t, pp_done), "%s.pp-done"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

// only with 0x6081 in the process data
static const lcec_pindesc_t pp_velocity_pins[] = {
    {HAL_FLOAT, HAL_IN, offsetof(cia402_t, pp_velocity), "%s.pp-velocity"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static const lcec_pindesc_t zero_pins[] = {
    {HAL_S32, HAL_OUT, offsetof(cia402_t, pos_zero), "%s.pos-zero"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static const lcec_pindesc_t mode_pins[] = {
    {HAL_S32, HAL_IN, offsetof(cia402_t, opmode_cmd), "%s.opmode-cmd"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static const lcec_paramdesc_t stop_params[] = {
    {HAL_U32, HAL_RW, offsetof(cia402_t, stop_timeout_ms), "%s.stop-timeout-ms"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static const lcec_paramdesc_t pp_params[] = {
    {HAL_U32, HAL_RW, offsetof(cia402_t, pp_timeout_ms), "%s.pp-timeout-ms"},
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

/// A drive's axes: one, or several (cia402.axes), each with its own
/// objects (index + n * 0x800), pins and state.
typedef struct {
  int n;
  cia402_t *axis[ECAT_CIA402_MAX_AXES];
} cia402_dev_t;

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

/// Set up one axis: its objects at index + off, its pins with the prefix.
static int axis_init(lcec_slave_t *slave, cia402_t *d, const char *name, uint16_t off, int mode, unsigned int others) {
  int err;

  if (find(slave, OBJ_ERROR_CODE + off, &d->error_code_o) || find(slave, OBJ_CONTROLWORD + off, &d->controlword_o) ||
      find(slave, OBJ_STATUSWORD + off, &d->statusword_o) || find(slave, OBJ_MODES + off, &d->modes_o) ||
      find(slave, OBJ_MODES_DISPLAY + off, &d->modes_display_o) ||
      find(slave, OBJ_ACTUAL_POSITION + off, &d->actual_position_o) ||
      find(slave, OBJ_ACTUAL_VELOCITY + off, &d->actual_velocity_o) ||
      find(slave, OBJ_TARGET_POSITION + off, &d->target_position_o) ||
      find(slave, OBJ_TARGET_VELOCITY + off, &d->target_velocity_o) ||
      find(slave, OBJ_PROFILE_VELOCITY + off, &d->profile_velocity_o)) {
    return -EIO;
  }
  // ecat_config.c has checked these; a safety net
  if (!d->controlword_o.mapped || !d->statusword_o.mapped) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s: cia402 needs 0x%04X and 0x%04X in the process data\n", name,
        OBJ_CONTROLWORD + off, OBJ_STATUSWORD + off);
    return -EINVAL;
  }
  // ecat_config.c has checked these too
  if (mode != MODE_CSP && mode != MODE_CSV && mode != MODE_PV && mode != MODE_PP) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s: cia402 mode %d is not supported\n", name, mode);
    return -EINVAL;
  }
  if (others & ~(MODE_BIT(MODE_PP) | MODE_BIT(MODE_PV) | MODE_BIT(MODE_HOMING) | MODE_BIT(MODE_CSP) | MODE_BIT(MODE_CSV))) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s: cia402 modes 0x%x are not supported\n", name, others);
    return -EINVAL;
  }
  d->allowed = MODE_BIT(mode) | others;
  d->can_home = (d->allowed & MODE_BIT(MODE_HOMING)) != 0;
  if ((d->allowed & (MODE_BIT(MODE_PV) | MODE_BIT(MODE_CSV))) && !d->target_velocity_o.mapped) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s: PV and CSV need 0x60FF in the process data\n", name);
    return -EINVAL;
  }
  if ((d->allowed & (MODE_BIT(MODE_PP) | MODE_BIT(MODE_CSP))) &&
      (!d->target_position_o.mapped || !d->actual_position_o.mapped)) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s: PP and CSP need 0x607A and 0x6064 in the process data\n", name);
    return -EINVAL;
  }
  // changing modes needs the mode and its display; ecat_config.c has checked this too
  if (others && (!d->modes_o.mapped || !d->modes_display_o.mapped)) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s: changing modes needs 0x6060 and 0x6061 in the process data\n", name);
    return -EINVAL;
  }

  if ((err = lcec_pin_newf_list(d, pins, name)) != 0) return err;
  if ((err = lcec_param_newf_list(d, params, name)) != 0) return err;
  if (d->can_home) {
    if ((err = lcec_pin_newf_list(d, homing_pins, name)) != 0) return err;
    if ((err = lcec_param_newf_list(d, homing_params, name)) != 0) return err;
  }
  if (d->allowed & MODE_BIT(MODE_PV)) {
    if ((err = lcec_pin_newf_list(d, pv_pins, name)) != 0) return err;
  }
  if (d->allowed & MODE_BIT(MODE_PP)) {
    if ((err = lcec_pin_newf_list(d, pp_pins, name)) != 0) return err;
    if (d->profile_velocity_o.mapped && (err = lcec_pin_newf_list(d, pp_velocity_pins, name)) != 0) return err;
    if ((err = lcec_param_newf_list(d, pp_params, name)) != 0) return err;
  }
  if (d->allowed & (MODE_BIT(MODE_PV) | MODE_BIT(MODE_PP))) {
    if ((err = lcec_param_newf_list(d, stop_params, name)) != 0) return err;
  }
  if (d->allowed & ~MODE_BIT(MODE_HOMING) & ~MODE_BIT(mode)) {
    if ((err = lcec_pin_newf_list(d, mode_pins, name)) != 0) return err;
  }
  if (d->zero_at_start && (err = lcec_pin_newf_list(d, zero_pins, name)) != 0) return err;

  // after the parameters are made, which zeroes them
  d->mode = mode;
  d->active = mode;
  d->home_timeout_ms = 60000;
  d->stop_timeout_ms = 10000;
  d->pp_timeout_ms = 60000;
  d->pos_scale = 1.0;
  d->velo_scale = 1.0;
  d->auto_fault_reset = 1;
  d->enable_timeout_ms = 2000;
  d->watchdog_ms = 0;
  d->state = ST_UNKNOWN;
  return 0;
}

int ecat_cia402_init(int comp_id, lcec_slave_t *slave) {
  lcec_slave_modparam_t *p;
  cia402_dev_t *dev;
  const char *names[ECAT_CIA402_MAX_AXES];
  int err, a, n = 0, mode = MODE_CSP, zero = 0;
  unsigned int others = 0;

  if ((err = lcec_generic_init(comp_id, slave)) != 0) return err;

  for (p = slave->modparams; p != NULL && p->id != -1; p++) {
    if (p->id == ECAT_MP_CIA402_MODES) others = p->value.u32;
    if (p->id == ECAT_MP_CIA402_MODE) mode = p->value.u32;
    if (p->id == ECAT_MP_CIA402_ZERO_AT_START) zero = p->value.u32 != 0;
    if (p->id == ECAT_MP_CIA402_AXIS) {
      // ecat_config.c has checked the count
      if (n >= ECAT_CIA402_MAX_AXES) {
        rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s: more than %d axes\n", slave->name, ECAT_CIA402_MAX_AXES);
        return -EINVAL;
      }
      names[n++] = p->value.str;
    }
  }
  if (n == 0) names[n++] = slave->name;  // one axis, named after the slave

  dev = LCEC_HAL_ALLOCATE(cia402_dev_t);
  slave->profile_data = dev;
  slave->proc_read = cia402_read;
  slave->proc_write = cia402_write;
  for (a = 0; a < n; a++) {
    cia402_t *d = LCEC_HAL_ALLOCATE(cia402_t);
    d->zero_at_start = zero;
    if ((err = axis_init(slave, d, names[a], (uint16_t)(a * ECAT_CIA402_AXIS_OFFSET), mode, others)) != 0) return err;
    dev->axis[dev->n++] = d;
  }
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

// Positions with pos-zero: wrapping as the drive's 32-bit counter does.
static int32_t from_drive(cia402_t *d, int32_t counts) { return (int32_t)((uint32_t)counts - (uint32_t)d->zero); }
static int32_t to_drive(cia402_t *d, int32_t counts) { return (int32_t)((uint32_t)counts + (uint32_t)d->zero); }

static void axis_read(lcec_slave_t *slave, cia402_t *d) {
  uint8_t *pd = slave->master->process_data;
  uint16_t sw;
  int8_t md = 0;

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
  *d->stat_target_reached = (md == MODE_CSP || md == MODE_CSV || md == MODE_PV || md == MODE_PP) && ((sw >> 10) & 1);
  if (d->zero_speed != NULL) {
    int pv = d->state == ST_OPERATION_ENABLED && d->active == MODE_PV && (!d->modes_display_o.mapped || md == MODE_PV);
    *d->zero_speed = pv && (sw & SW_SPEED_ZERO);
    // at-speed only while running at the command, see cia402_write
    if (!pv) *d->at_speed = 0;
  }

  if (d->error_code_o.mapped) *d->error_code = EC_READ_U16(&pd[d->error_code_o.os]);
  if (d->actual_position_o.mapped) {
    *d->drv_actual_position = EC_READ_S32(&pd[d->actual_position_o.os]);
    if (d->zero_at_start && !d->zeroed && d->bus_ok) {
      d->zero = *d->drv_actual_position;
      d->zeroed = 1;
    }
    // with zero-at-start, 0 until the position is taken
    *d->pos_fb = d->zero_at_start && !d->zeroed ? 0 : from_drive(d, *d->drv_actual_position) / scale_or_one(d->pos_scale);
    if (d->pos_zero != NULL) *d->pos_zero = d->zero;
  }
  // homing for motion: no following error until index-enable is cleared
  if (d->hs != HS_IDLE && d->hs_index) *d->pos_fb = *d->pos_cmd;
  if (d->actual_velocity_o.mapped) {
    *d->drv_actual_velocity = EC_READ_S32(&pd[d->actual_velocity_o.os]);
    *d->velocity_fb = *d->drv_actual_velocity / scale_or_one(d->velo_scale);
  }
}

static void cia402_read(lcec_slave_t *slave, long period) {
  cia402_dev_t *dev = slave->profile_data;
  int a;

  lcec_generic_read(slave, period);
  for (a = 0; a < dev->n; a++) axis_read(slave, dev->axis[a]);
}

/// PV, PP: whether the drive stands still (zero speed in PV, target
/// reached, which while halted means standing still, in PP); other modes
/// hold their targets and count as still.
static int standing_still(cia402_t *d) {
  uint16_t sw = *d->statusword;
  return d->active == MODE_PV ? (sw & SW_SPEED_ZERO) != 0 : d->active == MODE_PP ? (sw & SW_TARGET_REACHED) != 0 : 1;
}

static void block(cia402_t *d, int why) {
  if (!d->blocked) {
    d->blocked = 1;
    *d->fault_reason = why;
  }
}

/// Homing: start, follow and end it; called while the drive may run.
/// Returns the control word bits to add.
static uint16_t homing(cia402_t *d, int run, uint16_t sw, long period) {
  int index = *d->index_enable, home = *d->home;
  int index_edge = index && !d->index_old;
  int home_edge = home && !d->home_old;
  uint16_t bits = 0;

  d->index_old = index;
  d->home_old = home;
  if (!run || d->state != ST_OPERATION_ENABLED) {
    // disabled or shut down: homing is abandoned, the mode goes back
    d->hs = HS_IDLE;
    *d->stat_homing = 0;
    return 0;
  }
  d->hs_ns += period;
  switch (d->hs) {
    case HS_IDLE:
      if (index_edge || home_edge) {
        // a running PV or PP drive is halted first
        d->hs = (d->active == MODE_PV || d->active == MODE_PP) ? HS_STOP : HS_TO_HM;
        d->hs_ns = 0;
        d->hs_cycles = 0;
        d->hs_index = index_edge;
        *d->stat_homed = 0;
        *d->home_error = 0;
      }
      if (d->hs != HS_STOP) break;
      // fall through
    case HS_STOP:
      // the status word answers the halt only after STATUS_CYCLES
      d->hs_cycles++;
      bits = CW_HALT;
      if (d->hs_index && !index) {
        d->hs = HS_IDLE;  // motion gave up; the mode was not changed
        bits = 0;
      } else if (d->hs_cycles > STATUS_CYCLES && standing_still(d)) {
        d->hs = HS_TO_HM;
        d->hs_ns = 0;
      } else if (d->stop_timeout_ms != 0 && d->hs_ns > (long long)d->stop_timeout_ms * 1000000LL) {
        block(d, WHY_WRONG_MODE);
      }
      break;
    case HS_TO_HM:
      if (d->hs_index && !index) {
        d->hs = HS_BACK;  // motion gave up
        d->hs_ns = 0;
      } else if (*d->opmode_display == MODE_HOMING) {
        d->hs = HS_HOMING;
        d->hs_ns = 0;
        d->hs_seen = 0;
        bits = CW_HOMING_START;
      } else if (d->hs_ns > MODE_CHANGE_NS) {
        block(d, WHY_WRONG_MODE);
      }
      break;
    case HS_HOMING:
      bits = CW_HOMING_START;
      if (d->hs_index && !index) {
        d->hs = HS_BACK;  // motion gave up; bit 4 low interrupts homing
        d->hs_ns = 0;
        bits = 0;
      } else if (sw & SW_HOMING_ERROR) {
        *d->home_error = 1;
        block(d, WHY_HOMING_ERROR);
      } else if ((sw & (SW_HOMING_ATTAINED | SW_TARGET_REACHED)) != (SW_HOMING_ATTAINED | SW_TARGET_REACHED)) {
        d->hs_seen = 1;
        if (d->home_timeout_ms != 0 && d->hs_ns > (long long)d->home_timeout_ms * 1000000LL) {
          *d->home_error = 1;
          block(d, WHY_HOMING_TIMEOUT);
        }
      } else if (d->hs_seen || d->hs_ns >= HOMING_SETTLE_NS) {
        // attained and reached: a drive that keeps these bits from an
        // earlier homing gets HOMING_SETTLE_NS to clear them
        *d->stat_homed = 1;
        d->hs = HS_BACK;
        d->hs_ns = 0;
        bits = 0;
      }
      break;
    case HS_BACK:
      if (*d->opmode_display == d->active) {
        d->hs = HS_IDLE;
        if (d->hs_index) *d->index_enable = 0;
        d->index_old = 0;
        // the drive's position is the homed one now
        d->zero = 0;
        // the position has changed: CSP must not jump to an old command
        if (d->active == MODE_CSP) d->sync = 1;
      } else if (d->hs_ns > MODE_CHANGE_NS) {
        block(d, WHY_WRONG_MODE);
      }
      break;
  }
  if (d->blocked) {
    // shut down by homing: the mode goes back in this cycle
    d->hs = HS_IDLE;
    bits = 0;
  }
  *d->stat_homing = d->hs != HS_IDLE;
  return bits;
}

/// PP: the set-point handshake. Returns the control word bits to add.
static uint16_t pp_move(cia402_t *d, int run, uint16_t sw, long period) {
  int start = *d->pp_start;
  int start_edge = start && !d->pp_start_old;
  int ack = (sw & SW_SET_POINT_ACK) != 0;
  int in_pp = !d->modes_display_o.mapped || *d->opmode_display == MODE_PP;
  uint16_t bits = 0;

  d->pp_start_old = start;
  if (!run || d->state != ST_OPERATION_ENABLED || d->hs != HS_IDLE) {
    d->pp = PP_IDLE;
    d->pp_pending = 0;
    *d->pp_done = 0;
    return 0;
  }
  if (start_edge) d->pp_pending = 1;
  if (*d->pp_halt) {
    bits |= CW_HALT;
    d->pp_halt_wait = STATUS_CYCLES;
  } else if (d->pp_halt_wait > 0) {
    d->pp_halt_wait--;
  }
  // a new move: on a rising edge of pp-start, as soon as the drive is ready
  // for a set-point (acknowledge low); it replaces a move in progress
  if (d->pp_pending && in_pp && !ack && d->pp != PP_SET) {
    d->pp_pending = 0;
    d->pp_rel = *d->pp_relative;
    d->pp_set = clamp_s32(*d->pp_target * scale_or_one(d->pos_scale));
    if (!d->pp_rel) d->pp_set = to_drive(d, d->pp_set);
    d->pp_vel = d->pp_velocity != NULL ? (uint32_t)clamp_s32(fabs(*d->pp_velocity) * scale_or_one(d->velo_scale)) : 0;
    d->pp = PP_SET;
    d->pp_ns = 0;
    *d->pp_done = 0;
  }
  switch (d->pp) {
    case PP_SET:
      if (ack) {
        d->pp = PP_MOVING;  // bit 4 low: the drive drops the acknowledge
      } else {
        bits |= CW_NEW_SET_POINT;
      }
      break;
    case PP_MOVING:
      // after the acknowledge, "target reached" answers this set-point;
      // while halted it means standing still, not at the target
      if (!ack && (sw & SW_TARGET_REACHED) && !*d->pp_halt && d->pp_halt_wait == 0) {
        d->pp = PP_DONE;
        *d->pp_done = 1;
      }
      break;
  }
  if (d->pp == PP_SET || d->pp == PP_MOVING) {
    bits |= CW_CHANGE_IMMEDIATELY | (d->pp_rel ? CW_RELATIVE : 0);
    if (!*d->pp_halt) d->pp_ns += period;
    if (d->pp_timeout_ms != 0 && d->pp_ns > (long long)d->pp_timeout_ms * 1000000LL) block(d, WHY_PP_TIMEOUT);
  }
  if (d->blocked) {
    d->pp = PP_IDLE;
    bits = 0;
  }
  return bits;
}

/// Changing modes on opmode-cmd. Returns the control word bits to add.
static uint16_t mode_change(cia402_t *d, int run, int enable, long period) {
  int want = *d->opmode_cmd == 0 ? d->mode : *d->opmode_cmd;

  if (want <= 0 || want >= 32 || want == MODE_HOMING || !(d->allowed & MODE_BIT(want))) {
    // not a mode of the file: like any other bad command
    if (enable) block(d, WHY_BAD_COMMAND);
    d->mc = MC_RUN;
    return 0;
  }
  if (d->hs != HS_IDLE) return 0;  // homing changes modes itself
  if (d->state != ST_OPERATION_ENABLED) {
    // not enabled: nothing moves, change at once (enabling waits for 0x6061)
    d->active = want;
    d->mc = MC_RUN;
    return 0;
  }
  d->mc_ns += period;
  switch (d->mc) {
    case MC_RUN:
      if (want == d->active) return 0;
      d->mc = MC_STOP;
      d->mc_to = want;
      d->mc_ns = 0;
      d->mc_cycles = 0;
      // fall through
    case MC_STOP:
      d->mc_to = want;
      if (want == d->active) {
        d->mc = MC_RUN;  // asked back before the change
        return 0;
      }
      d->mc_cycles++;
      // in CSP and CSV the targets are held from the first cycle
      if (d->mc_cycles > STATUS_CYCLES && standing_still(d)) {
        d->mc = MC_SWITCH;
        d->mc_ns = 0;
      } else if (d->stop_timeout_ms != 0 && d->mc_ns > (long long)d->stop_timeout_ms * 1000000LL) {
        block(d, WHY_WRONG_MODE);
      }
      break;
    case MC_SWITCH:
      if (*d->opmode_display == d->mc_to) {
        d->active = d->mc_to;
        d->mc = MC_RUN;
        d->sync = 1;  // PP needs a new pp-start anyway
      } else if (d->mc_ns > MODE_CHANGE_NS) {
        block(d, WHY_WRONG_MODE);
      }
      break;
  }
  if (d->blocked) {
    d->mc = MC_RUN;
    return 0;
  }
  return (d->mc == MC_STOP && (d->active == MODE_PV || d->active == MODE_PP)) ? CW_HALT : 0;
}

static void axis_write(lcec_slave_t *slave, cia402_t *d, long period) {
  uint8_t *pd = slave->master->process_data;
  int enable = *d->enable;
  int enable_edge = enable && !d->enable_old;
  int reset_edge = *d->fault_reset && !d->fault_reset_old;
  int faulted = d->state == ST_FAULT || d->state == ST_FAULT_REACTION_ACTIVE;
  int homing_now = d->hs != HS_IDLE;
#ifdef EC_HAVE_DC_SYNC_FAILED
  int dc_failed = slave->state.dc_sync_failed;
#else
  int dc_failed = 0;
#endif
  int mode_ok = !d->modes_display_o.mapped || *d->opmode_display == d->active || homing_now;
  double cmd = (d->active == MODE_CSV || d->active == MODE_PV) ? *d->velocity_cmd
                : d->active == MODE_PP                         ? *d->pp_target
                                                               : *d->pos_cmd;
  int halt = 0;
  int run;
  uint16_t cw, hbits = 0, pbits = 0, mbits = 0;

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
    } else if (dc_failed) {
      // the drive's SYNC0 does not follow the other slaves and the servo
      // thread: not run at all
      block(d, WHY_DC_SYNC);
    } else if (d->watchdog_ms != 0 && d->watchdog_ns >= (long long)d->watchdog_ms * 1000000LL) {
      block(d, WHY_WATCHDOG);
    } else if (!isfinite(cmd) || (d->pp_velocity != NULL && !isfinite(*d->pp_velocity))) {
      // never clamped into a target: that would be a jump to the end of
      // the range
      block(d, WHY_BAD_COMMAND);
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
  if (d->opmode_cmd != NULL) {
    mbits = mode_change(d, run, enable, period);
    run = enable && !d->blocked;  // a mode change may have failed
  }
  if (d->can_home) {
    uint16_t sw = *d->statusword;
    hbits = homing(d, run && d->mc == MC_RUN, sw, period);
    run = enable && !d->blocked;  // homing may have shut it down
  }

  if (d->pp_start != NULL) {
    pbits = pp_move(d, run && d->active == MODE_PP && d->mc == MC_RUN, *d->statusword, period);
    run = enable && !d->blocked;  // a move may have timed out
  }

  // PV and PP: a moving drive is halted before it is switched off, until
  // it reports zero speed (PV) or target reached, which while halted means
  // standing still (PP); the status word is taken as the answer only after
  // STATUS_CYCLES
  if ((d->active == MODE_PV || d->active == MODE_PP) && !run && d->state == ST_OPERATION_ENABLED) {
    d->stop_ns += period;
    d->stop_cycles++;
    halt = (d->stop_cycles <= STATUS_CYCLES || !standing_still(d)) && d->stop_ns <= (long long)d->stop_timeout_ms * 1000000LL;
  } else {
    d->stop_ns = 0;
    d->stop_cycles = 0;
  }

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
      cw = run ? (CW_ENABLE_OPERATION | hbits | pbits | mbits) : halt ? (CW_ENABLE_OPERATION | CW_HALT) : CW_SHUTDOWN;
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
    int8_t m = (d->hs == HS_TO_HM || d->hs == HS_HOMING) ? MODE_HOMING : d->mc == MC_SWITCH ? d->mc_to : d->active;
    EC_WRITE_S8(&pd[d->modes_o.os], m);
    *d->opmode = m;
  }

  // Targets: follow the commands only in "Operation enabled", for this
  // enable; otherwise hold the actual position or stand still.
  run = run && d->state == ST_OPERATION_ENABLED && d->hs == HS_IDLE && !homing_now && d->mc == MC_RUN;
  if (!run && !enable) d->sync = 0;
  if (run && d->sync) {
    // after a mode change: start from the actual values, without a jump
    if (d->active == MODE_CSP) {
      long long dist = (long long)clamp_s32(*d->pos_cmd * scale_or_one(d->pos_scale)) - from_drive(d, *d->drv_actual_position);
      if (dist >= -SYNC_COUNTS && dist <= SYNC_COUNTS) d->sync = 0;
    } else if (d->active == MODE_CSV) {
      if (clamp_s32(*d->velocity_cmd * scale_or_one(d->velo_scale)) == 0) d->sync = 0;
    } else {
      d->sync = 0;
    }
  }
  run = run && !d->sync;
  if (d->target_position_o.mapped) {
    if (run && d->active == MODE_CSP) {
      *d->drv_target_position = to_drive(d, clamp_s32(*d->pos_cmd * scale_or_one(d->pos_scale)));
    } else if (run && d->active == MODE_PP && d->pp != PP_IDLE) {
      *d->drv_target_position = d->pp_set;
    } else {
      *d->drv_target_position = *d->drv_actual_position;
    }
    EC_WRITE_S32(&pd[d->target_position_o.os], *d->drv_target_position);
  }
  if (d->target_velocity_o.mapped) {
    if (run && (d->active == MODE_CSV || d->active == MODE_PV)) {
      *d->drv_target_velocity = clamp_s32(*d->velocity_cmd * scale_or_one(d->velo_scale));
    } else {
      *d->drv_target_velocity = 0;
    }
    EC_WRITE_S32(&pd[d->target_velocity_o.os], *d->drv_target_velocity);
  }

  if (d->profile_velocity_o.mapped) {
    EC_WRITE_U32(&pd[d->profile_velocity_o.os], (run && d->active == MODE_PP) ? d->pp_vel : 0);
  }

  if (d->at_speed != NULL) {
    if (*d->drv_target_velocity != d->pv_target) {
      d->pv_target = *d->drv_target_velocity;
      d->pv_wait = STATUS_CYCLES;
    } else if (d->pv_wait > 0) {
      d->pv_wait--;
    }
    *d->at_speed = run && d->active == MODE_PV && d->pv_wait == 0 && (*d->statusword & SW_TARGET_REACHED) &&
                   (!d->modes_display_o.mapped || *d->opmode_display == MODE_PV);
  }
}

static void cia402_write(lcec_slave_t *slave, long period) {
  cia402_dev_t *dev = slave->profile_data;
  int a;

  for (a = 0; a < dev->n; a++) axis_write(slave, dev->axis[a], period);
  lcec_generic_write(slave, period);
}
