//
//    Copyright (C) 2024 Scott Laird <scott@sigkill.org>
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
//    You should have received a copy of the GNU General Public License
//    along with this program; if not, write to the Free Software
//    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
//

/// @file
/// @brief Library for CiA 402 servo/stepper controllers

#include "../lcec.h"

#define LCEC_CIA402_PDOS           9
#define LCEC_CIA402_BASIC_IN1_LEN  6
#define LCEC_CIA402_BASIC_OUT1_LEN 5

extern ec_pdo_entry_info_t lcec_cia402_basic_in1[];
extern ec_pdo_entry_info_t lcec_cia402_basic_out1[];

/// @brief This is the option list for CiA 402 devices.
///
/// This provides the naming prefix for devices, and also controls
/// which optional features are enabled.  Fortunately or
/// unfortunately, the vast bulk of CiA 402 CoE objects are optional,
/// and the exact object implemented by each device may vary widely.
/// This is intended to *only* require the objects that the standard
/// lists as mandatory.  Then, each device driver can enable optional
/// features to fit what the hardware provides.
///
/// In general, these options are broken into 3 categories:
///
/// 1. Technically optional but practically required.  This includes
///    `opmode`, which the standard doesn't require but nearly all
///    devices would be expected to support.  These default to on, but
///    can be disabled.
/// 2. Mode-required objects.  For instance, `pp` mode requires that
///    the `-actual-position` and `-target-position` objects be
///    available.
///
/// 3. Individually optional objects.  Other objects, like
///    `-actual-torque` are optional; some devices will implement them
///    and others will not.  These will need to be flagged on on a
///    per-device, per-object basis.
///
/// At the moment, only pp/pv/csp/csv are even slightly implemented.
/// More will follow as hardware support grows.
typedef struct {
  char *name_prefix;          ///< Prefix for device naming, defaults to "srv".
  int enable_opmode;          ///< Enable opmode and opmode-display.  They're technically optional in the spec.
  int enable_pp;              ///< If true, enable required PP-mode pins: `-actual-position` and `-target-position`.
  int enable_pv;              ///< If true, enable required PV-mode pins: `-actual-velocity` and `-target-velocity`.
  int enable_csp;             ///< If true, enable required CSP-mode pins: `-actual-position` and `-target-position`, plus others.
  int enable_csv;             ///< If true, enable required PV-mode pins: `-actual-velocity` and `-target-velocity`, plus others.
  int enable_hm;              ///< If true, enable required homing-mode pins.  TBD
  int enable_ip;              ///< If true, enable required interpolation-mode pins.  TBD.
  int enable_vl;              ///< If true, enable required velocity-mode pins.  TBD.
  int enable_cst;             ///< If true, enable required Cyclic Synchronous Torque mode pins.  TBD
  int enable_actual_torque;   ///< If true, enable `-actual-torque`.
  int enable_digital_input;   ///< If true, enable digital input PDO.
  int enable_digital_output;  ///< If true, enable digital output PDO.
} lcec_class_cia402_options_t;

/// This is the internal version of `lcec_class_cia402_options_t`.  It
/// lists each specific pin (or atomic set of pins, in the case of
/// `opmode`), and decisions about mapping/etc can be based on this.
/// This is constructed from an options structure by
/// `lcec_cia402_enabled()`.
typedef struct {
  int enable_opmode;
  int enable_actual_position;
  int enable_actual_velocity;
  int enable_actual_torque;
  int enable_target_position;
  int enable_target_velocity;
  int enable_digital_input;
  int enable_digital_output;
} lcec_class_cia402_enabled_t;

typedef struct {
  // Out
  hal_u32_t *controlword;
  hal_s32_t *opmode;
  hal_s32_t *target_position;
  hal_s32_t *target_velocity;

  // In
  hal_u32_t *statusword;
  hal_s32_t *opmode_display;
  hal_s32_t *actual_position;
  hal_s32_t *actual_velocity;
  hal_s32_t *actual_torque;

  unsigned int controlword_os;  ///< The controlword's offset in the master's PDO data structure.
  unsigned int opmode_os;       ///< The opmode's offset in the master's PDO data structure.
  unsigned int targetpos_os;    ///< The target position's offset in the master's PDO data structure.
  unsigned int targetvel_os;    ///< The target velocity's offset in the master's PDO data structure.

  unsigned int statusword_os;   ///< The statusword's offset in the master's PDO data structure.
  unsigned int opmode_disp_os;  ///< The opmode display's offset in the master's PDO data structure.
  unsigned int actpos_os;       ///< The actual position's offset in the master's PDO data structure.
  unsigned int actvel_os;       ///< The actual velocity's offset in the master's PDO data structure.
  unsigned int acttorq_os;      ///< The actual torque's offset in the master's PDO data structure.

  lcec_class_cia402_options_t *options;  ///< The options used to create this device.
  lcec_class_cia402_enabled_t *enabled;
} lcec_class_cia402_channel_t;

typedef struct {
  int count;                               ///< The number of channels described by this structure.
  lcec_class_cia402_channel_t **channels;  ///< a dynamic array of `lcec_class_cia402_channel_t` channels.  There should be 1 per axis.
} lcec_class_cia402_channels_t;

lcec_class_cia402_channels_t *lcec_cia402_allocate_channels(int count);
lcec_class_cia402_channel_t *lcec_cia402_register_channel(
    ec_pdo_entry_reg_t **pdo_entry_regs, struct lcec_slave *slave, uint16_t base_idx, lcec_class_cia402_options_t *opt);
void lcec_cia402_read(struct lcec_slave *slave, lcec_class_cia402_channel_t *data);
void lcec_cia402_read_all(struct lcec_slave *slave, lcec_class_cia402_channels_t *channels);
void lcec_cia402_write(struct lcec_slave *slave, lcec_class_cia402_channel_t *data);
void lcec_cia402_write_all(struct lcec_slave *slave, lcec_class_cia402_channels_t *channels);
lcec_class_cia402_options_t *lcec_cia402_options_single_axis(void);
lcec_class_cia402_options_t *lcec_cia402_options_multi_axis(void);
int lcec_cia402_handle_modparam(struct lcec_slave *slave, const lcec_slave_modparam_t *p);
lcec_modparam_desc_t *lcec_cia402_channelized_modparams(lcec_modparam_desc_t const *orig);
lcec_modparam_desc_t *lcec_cia402_modparams(lcec_modparam_desc_t const *device_mps);
lcec_syncs_t *lcec_cia402_init_sync(lcec_class_cia402_options_t *options);
int lcec_cia402_add_output_sync(lcec_syncs_t *syncs, lcec_class_cia402_options_t *options);
int lcec_cia402_add_input_sync(lcec_syncs_t *syncs, lcec_class_cia402_options_t *options);

#define ADD_TYPES_WITH_CIA402_MODPARAMS(types, mps)        \
  static void AddTypes(void) __attribute__((constructor)); \
  static void AddTypes(void) {                             \
    const lcec_modparam_desc_t *all_modparams;             \
    int i;                                                 \
    all_modparams = lcec_cia402_modparams(mps);            \
    for (i = 0; types[i].name != NULL; i++) {              \
      types[i].modparams = all_modparams;                  \
    }                                                      \
    lcec_addtypes(types, __FILE__);                        \
  }

// modParam IDs
//
// These need to:
//   (a) be >= CIA402_MP_BASE and
//   (b) be a multiple of 4, with 3 unused IDs between each.
//       That is, the hex version should end in 0, 4, 8, or c.
//
// These are run through `lcec_cia402_channelized_modparams()` which
// creates additional versions of these for 4 different channels (or
// axes).

#define CIA402_MP_BASE              0x1000
#define CIA402_MP_POSLIMIT_MIN      0x1000  // 0x607b:01 "Minimum position range limit" S32
#define CIA402_MP_POSLIMIT_MAX      0x1004  // 0x607b:02 "Maximum position range limit" S32
#define CIA402_MP_SWPOSLIMIT_MIN    0x1008  // 0x607d:01 "Minimum software position limit" S32
#define CIA402_MP_SWPOSLIMIT_MAX    0x100c  // 0x607d:02 "Maximum software position limit" S32
#define CIA402_MP_HOME_OFFSET       0x1010  // 0x607c:00 "home offset" S32
#define CIA402_MP_MAXPROFILEVEL     0x1020  // 0x607f:00 "max profile velocity" U32
#define CIA402_MP_MAXMOTORSPEED     0x1024  // 0x6080:00 "max motor speed" U32
#define CIA402_MP_PROFILEVEL        0x1028  // 0x6081:00 "profile velocity" U32
#define CIA402_MP_ENDVEL            0x102c  // 0x6082:00 "end velocity" U32
#define CIA402_MP_PROFACCEL         0x1030  // 0x6083:00 "profile acceleration" U32
#define CIA402_MP_PROFDECEL         0x1034  // 0x6084:00 "profile deceleration" U32
#define CIA402_MP_QUICKDECEL        0x1038  // 0x6085:00 "quick stop deceleration" U32
#define CIA402_MP_OPTCODE_QUICKSTOP 0x1040  // 0x605a:00 "quick stop option code" S16
#define CIA402_MP_OPTCODE_SHUTDOWN  0x1044  // 0x605b:00 "shutdown option code" S16
#define CIA402_MP_OPTCODE_DISABLE   0x1048  // 0x605c:00 "disable operation option code" S16
#define CIA402_MP_OPTCODE_HALT      0x104c  // 0x605d:00 "halt option code" S16
#define CIA402_MP_OPTCODE_FAULT     0x1050  // 0x605e:00 "fault option code" S16
#define CIA402_MP_HOME_METHOD       0x1060  // 0x6098:00 "homing method" S8
#define CIA402_MP_HOME_VEL_FAST     0x1064  // 0x6099:01 "homing velocity fast" U32
#define CIA402_MP_HOME_VEL_SLOW     0x1068  // 0x6099:02 "homing velocity slow" U32
#define CIA402_MP_HOME_ACCEL        0x106c  // 0x609a:00 "homing acceleration" S32
#define CIA402_MP_PROBE_FUNCTION    0x1070  // 0x60b8:00 "probe function" U16
#define CIA402_MP_PROBE1_POS        0x1080  // 0x60ba:00 "touch probe 1 positive value" S32
#define CIA402_MP_PROBE1_NEG        0x1084  // 0x60bb:00 "touch probe 1 negative value" S32
#define CIA402_MP_PROBE2_POS        0x1088  // 0x60bc:00 "touch probe 2 positive value" S32
#define CIA402_MP_PROBE2_NEG        0x108c  // 0x60bad:00 "touch probe 2 negative value" S32
