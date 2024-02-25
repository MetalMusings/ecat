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

#include "lcec_class_cia402.h"

#include <stdio.h>

#include "../lcec.h"

// The CiA 402 spec includes a *lot* of optional features; see
// `documentation/cia402.md` for a partial list.  Practically
// speaking, this means that nearly every single thing that we want to
// access is optional, and attempting to access them on hardware that
// doesn't support them will lead to, at best, a poor user experience.
// So, `class_cia402` goes to great lengths to make every single
// access to a non-required object optional.  Outside of this file,
// options are controlled via `lcec_class_cia402_options_t`.  The
// options struct includes a flag for each supported CiA 402 operating
// mode, which toggles on all required objects for that mode, plus
// individual flags for each other object that may be useful.  This is
// then them condensed down to `lcec_class_cia402_enabled_t`
// internally, with one entry per optional object.
//
// Few things make code as unwieldy as page after page of almost but
// not *quite* identical `if (enabled->enabled_actual_following_error)
// ...` checks, so I've tried to package each set of checks up into a
// preprocessor macro.  As much as possible, I've tried to keep these
// macro definitions right next to where they're used.  They make
// certain assumptions about the structure of things:
//
// 1. The exact same name is used everywhere for things related to
//    optional objects.  If we're going to call a feature
//    `target_position`, then we'll have `enabled_target_position`, a
//    pin called `target_position` and an offset called
//    `target_position_os` in `hal_data`, so so on.
//
// 2. Pin values aren't converted at all before sending to hardware.
//    Since most of the CiA 402 movement values are
//    implementation-defined, that means that there's no point in
//    doing unit conversions, unlike cases where the hardware wants
//    milliAmps but we'd rather let the user talk about floating point
//    Amps.  We could deal with this if we had to without too much
//    work, but so far it hasn't really come up.
// 3. Each `enabled` entry only controls a single CoE object.
//
// This table helps keep things simple further down.  First, we're
// going to define `PDO_IDX_OFFSET`s for each optional feature.  This
// is the offset from the base address for each axis/channel (0x6000
// for the first axis, 0x6800 for the second, and so on).  It *also*
// needs to match the naming pattern described above, because it's
// only accessed from inside of `#define`s below.

#define PDO_IDX_OFFSET_actual_current 0x78
#define PDO_IDX_OFFSET_actual_following_error  0xf4
#define PDO_IDX_OFFSET_actual_position 0x64
#define PDO_IDX_OFFSET_actual_torque 0x77
#define PDO_IDX_OFFSET_actual_velocity 0x6c
#define PDO_IDX_OFFSET_actual_velocity_sensor 0x69
#define PDO_IDX_OFFSET_actual_voltage 0x79
#define PDO_IDX_OFFSET_digital_input 0xfd
#define PDO_IDX_OFFSET_digital_output 0xfe
#define PDO_IDX_OFFSET_following_error_timeout 0x66
#define PDO_IDX_OFFSET_following_error_window 0x65
#define PDO_IDX_OFFSET_home_accel 0x9a
#define PDO_IDX_OFFSET_home_method 0x98
#define PDO_IDX_OFFSET_home_velocity_fast 0x99
#define PDO_IDX_OFFSET_home_velocity_slow 0x99
#define PDO_IDX_OFFSET_interpolation_time_period 0xc2
#define PDO_IDX_OFFSET_maximum_acceleration 0xc5
#define PDO_IDX_OFFSET_maximum_current 0x73
#define PDO_IDX_OFFSET_maximum_deceleration 0xc6
#define PDO_IDX_OFFSET_maximum_motor_rpm 0x80
#define PDO_IDX_OFFSET_maximum_torque 0x72
#define PDO_IDX_OFFSET_motion_profile 0x86
#define PDO_IDX_OFFSET_motor_rated_current 0x75
#define PDO_IDX_OFFSET_motor_rated_torque 0x76
#define PDO_IDX_OFFSET_opmode 0x60
#define PDO_IDX_OFFSET_opmode_display 0x61
#define PDO_IDX_OFFSET_polarity 0x73
#define PDO_IDX_OFFSET_profile_accel 0x83
#define PDO_IDX_OFFSET_profile_decel 0x84
#define PDO_IDX_OFFSET_profile_end_velocity 0x82
#define PDO_IDX_OFFSET_profile_max_velocity 0x7f
#define PDO_IDX_OFFSET_profile_velocity 0x81
#define PDO_IDX_OFFSET_target_position 0x7a
#define PDO_IDX_OFFSET_target_torque 0x71
#define PDO_IDX_OFFSET_target_velocity 0xff
#define PDO_IDX_OFFSET_torque_demand 0x74
#define PDO_IDX_OFFSET_torque_profile_type 0x88
#define PDO_IDX_OFFSET_torque_slope 0x87
#define PDO_IDX_OFFSET_velocity_demand 0x6b
#define PDO_IDX_OFFSET_velocity_error_time 0x6e
#define PDO_IDX_OFFSET_velocity_error_window 0x6d
#define PDO_IDX_OFFSET_velocity_sensor_selector 0x61
#define PDO_IDX_OFFSET_velocity_threshold_time 0x60
#define PDO_IDX_OFFSET_velocity_threshold_window 0x6f

// Next, we'll do the same thing for sub-index addresses.  With CiA
// 402, these are mostly (but not entirely) 0.
#define PDO_SIDX_actual_current 0
#define PDO_SIDX_actual_following_error 0
#define PDO_SIDX_actual_position  0
#define PDO_SIDX_actual_torque  0
#define PDO_SIDX_actual_velocity  0
#define PDO_SIDX_actual_velocity_sensor  0
#define PDO_SIDX_actual_voltage 0
#define PDO_SIDX_digital_input 0
#define PDO_SIDX_digital_output 1
#define PDO_SIDX_following_error_timeout  0
#define PDO_SIDX_following_error_window  0
#define PDO_SIDX_home_accel  0
#define PDO_SIDX_home_method  0
#define PDO_SIDX_home_velocity_fast  1
#define PDO_SIDX_home_velocity_slow  2
#define PDO_SIDX_interpolation_time_period  1
#define PDO_SIDX_maximum_acceleration 0
#define PDO_SIDX_maximum_current 0
#define PDO_SIDX_maximum_deceleration 0
#define PDO_SIDX_maximum_motor_rpm 0
#define PDO_SIDX_maximum_torque  0
#define PDO_SIDX_motion_profile  0
#define PDO_SIDX_motor_rated_current 0
#define PDO_SIDX_motor_rated_torque  0
#define PDO_SIDX_opmode  0
#define PDO_SIDX_opmode_display  0
#define PDO_SIDX_polarity 0
#define PDO_SIDX_profile_accel  0
#define PDO_SIDX_profile_decel  0
#define PDO_SIDX_profile_end_velocity  0
#define PDO_SIDX_profile_max_velocity  0
#define PDO_SIDX_profile_velocity  0
#define PDO_SIDX_target_position  0
#define PDO_SIDX_target_torque 0
#define PDO_SIDX_target_velocity  0
#define PDO_SIDX_torque_demand 0
#define PDO_SIDX_torque_profile_type 0
#define PDO_SIDX_torque_slope 0
#define PDO_SIDX_velocity_demand 0
#define PDO_SIDX_velocity_error_time 0
#define PDO_SIDX_velocity_error_window 0
#define PDO_SIDX_velocity_sensor_selector 0
#define PDO_SIDX_velocity_threshold_time 0
#define PDO_SIDX_velocity_threshold_window 0

// Next, the type of pin that each uses.  *Generally* either HAL_U32
// or HAL_S32.  We could probably generate this by concatenating
// PDO_SIGN_foo (below) and `32`, but it'd probably bite us
// eventually.
#define PDO_PIN_TYPE_actual_current HAL_S32
#define PDO_PIN_TYPE_actual_following_error HAL_U32
#define PDO_PIN_TYPE_actual_position HAL_S32
#define PDO_PIN_TYPE_actual_torque HAL_S32
#define PDO_PIN_TYPE_actual_velocity HAL_S32
#define PDO_PIN_TYPE_actual_velocity_sensor HAL_S32
#define PDO_PIN_TYPE_actual_voltage HAL_U32
#define PDO_PIN_TYPE_following_error_timeout HAL_U32
#define PDO_PIN_TYPE_following_error_window HAL_U32
#define PDO_PIN_TYPE_home_accel HAL_U32
#define PDO_PIN_TYPE_home_method HAL_S32
#define PDO_PIN_TYPE_home_velocity_fast HAL_U32
#define PDO_PIN_TYPE_home_velocity_slow HAL_U32
#define PDO_PIN_TYPE_interpolation_time_period HAL_U32
#define PDO_PIN_TYPE_maximum_acceleration HAL_U32
#define PDO_PIN_TYPE_maximum_current HAL_U32
#define PDO_PIN_TYPE_maximum_deceleration HAL_U32
#define PDO_PIN_TYPE_maximum_motor_rpm HAL_U32
#define PDO_PIN_TYPE_maximum_torque HAL_U32
#define PDO_PIN_TYPE_motion_profile HAL_S32
#define PDO_PIN_TYPE_motor_rated_current HAL_U32
#define PDO_PIN_TYPE_motor_rated_torque HAL_U32
#define PDO_PIN_TYPE_opmode HAL_S32
#define PDO_PIN_TYPE_opmode_display HAL_S32
#define PDO_PIN_TYPE_polarity HAL_U32
#define PDO_PIN_TYPE_profile_accel HAL_U32
#define PDO_PIN_TYPE_profile_decel HAL_U32
#define PDO_PIN_TYPE_profile_end_velocity HAL_U32
#define PDO_PIN_TYPE_profile_max_velocity HAL_U32
#define PDO_PIN_TYPE_profile_velocity HAL_U32
#define PDO_PIN_TYPE_target_position HAL_S32
#define PDO_PIN_TYPE_target_torque HAL_S32
#define PDO_PIN_TYPE_target_velocity HAL_S32
#define PDO_PIN_TYPE_torque_demand HAL_S32
#define PDO_PIN_TYPE_torque_profile_type HAL_S32
#define PDO_PIN_TYPE_torque_slope HAL_U32
#define PDO_PIN_TYPE_velocity_demand HAL_S32
#define PDO_PIN_TYPE_velocity_error_time HAL_U32
#define PDO_PIN_TYPE_velocity_error_window HAL_U32
#define PDO_PIN_TYPE_velocity_sensor_selector HAL_S32
#define PDO_PIN_TYPE_velocity_threshold_time HAL_U32
#define PDO_PIN_TYPE_velocity_threshold_window HAL_U32


// Next, the length of the object for each.   Usually 8, 16, or 32.
#define PDO_BITS_actual_current 16
#define PDO_BITS_actual_following_error 32
#define PDO_BITS_actual_position 32
#define PDO_BITS_actual_torque 32
#define PDO_BITS_actual_velocity 32
#define PDO_BITS_actual_velocity_sensor 32
#define PDO_BITS_actual_voltage 32
#define PDO_BITS_digital_input 32
#define PDO_BITS_digital_output 32
#define PDO_BITS_following_error_timeout 16
#define PDO_BITS_following_error_window 32
#define PDO_BITS_home_accel 32
#define PDO_BITS_home_method 8
#define PDO_BITS_home_velocity_fast 32
#define PDO_BITS_home_velocity_slow 32
#define PDO_BITS_interpolation_time_period 8
#define PDO_BITS_maximum_acceleration 32
#define PDO_BITS_maximum_current 16
#define PDO_BITS_maximum_deceleration 32
#define PDO_BITS_maximum_motor_rpm 32
#define PDO_BITS_maximum_torque 16
#define PDO_BITS_motion_profile 16
#define PDO_BITS_motor_rated_current 32
#define PDO_BITS_motor_rated_torque 32
#define PDO_BITS_opmode 8
#define PDO_BITS_opmode_display 8
#define PDO_BITS_polarity 8
#define PDO_BITS_profile_accel 32
#define PDO_BITS_profile_decel 32
#define PDO_BITS_profile_end_velocity 32
#define PDO_BITS_profile_max_velocity 32
#define PDO_BITS_profile_velocity 32
#define PDO_BITS_target_position 32
#define PDO_BITS_target_torque 16
#define PDO_BITS_target_velocity 32
#define PDO_BITS_torque_demand 16
#define PDO_BITS_torque_profile_type 16
#define PDO_BITS_torque_slope 32
#define PDO_BITS_velocity_demand 32
#define PDO_BITS_velocity_error_time 16
#define PDO_BITS_velocity_error_window 16
#define PDO_BITS_velocity_sensor_selector 16
#define PDO_BITS_velocity_threshold_time 16
#define PDO_BITS_velocity_threshold_window 16

// Finally (?), the signeded-ness of each object.  This should match
// PDO_PIN_TYPE_foo, but we don't *necessarily* have PDO_PIN_TYPE_foo
// for all objects.
#define PDO_SIGN_actual_current S
#define PDO_SIGN_actual_following_error U
#define PDO_SIGN_actual_position S
#define PDO_SIGN_actual_torque S
#define PDO_SIGN_actual_velocity S
#define PDO_SIGN_actual_velocity_sensor S
#define PDO_SIGN_actual_voltage U
#define PDO_SIGN_following_error_timeout U
#define PDO_SIGN_following_error_window U
#define PDO_SIGN_home_accel U
#define PDO_SIGN_home_method S
#define PDO_SIGN_home_velocity_fast U
#define PDO_SIGN_home_velocity_slow U
#define PDO_SIGN_interpolation_time_period U
#define PDO_SIGN_maximum_acceleration U
#define PDO_SIGN_maximum_current U
#define PDO_SIGN_maximum_deceleration U
#define PDO_SIGN_maximum_motor_rpm U
#define PDO_SIGN_maximum_torque U
#define PDO_SIGN_motion_profile S
#define PDO_SIGN_motor_rated_current U
#define PDO_SIGN_motor_rated_torque U
#define PDO_SIGN_opmode S
#define PDO_SIGN_opmode_display S
#define PDO_SIGN_polarity U
#define PDO_SIGN_profile_accel U
#define PDO_SIGN_profile_decel U
#define PDO_SIGN_profile_end_velocity U
#define PDO_SIGN_profile_max_velocity U
#define PDO_SIGN_profile_velocity U
#define PDO_SIGN_target_position S
#define PDO_SIGN_target_torque S
#define PDO_SIGN_target_velocity S
#define PDO_SIGN_torque_demand S
#define PDO_SIGN_torque_profile_type S
#define PDO_SIGN_torque_slope U
#define PDO_SIGN_velocity_demand S
#define PDO_SIGN_velocity_error_time U
#define PDO_SIGN_velocity_error_window U
#define PDO_SIGN_velocity_sensor_selector S
#define PDO_SIGN_velocity_threshold_time U
#define PDO_SIGN_velocity_threshold_window U

// These work around a couple C pre-processor shortcomings.  In a few
// places, I want to concatenate FOO and BAR into FOOBAR, and then
// evaluate FOOBAR for macro substitution before then taking the
// result of that and concatenating it into something else.  For
// example, I'd like to take `(PDO_SIGN_##name)##(PDO_BITS##name)` and
// get `U32` for name=`home_accel`.  Unfortunately, `##` doesn't work
// like that, or use parenthesis for grouping.  The workaround is to
// put it inside of yet another macro.  Except *that* won't result in
// macro expansion happening, so you need to nest that inside *yet
// another* macro.  Go read the GCC preprocessor manual, it's in there
// and this is how they recommend handling it.
#define JOIN3(a, b, c) a##b##c
#define JOIN5(a, b, c, d, e) a##b##c##d##e
#define SUBSTJOIN3(a, b, c) JOIN3(a, b, c)
#define SUBSTJOIN5(a, b, c, d, e) JOIN5(a, b, c, d, e)

/// @brief Pins common to all CiA 402 devices
static const lcec_pindesc_t pins_required[] = {
    // HAL_OUT is readable, HAL_IN is writable.
    {HAL_U32, HAL_IN, offsetof(lcec_class_cia402_channel_t, controlword), "%s.%s.%s.%s-cia-controlword"},
    {HAL_U32, HAL_OUT, offsetof(lcec_class_cia402_channel_t, statusword), "%s.%s.%s.%s-cia-statusword"},
    {HAL_U32, HAL_OUT, offsetof(lcec_class_cia402_channel_t, supported_modes), "%s.%s.%s.%s-supported-modes"},
    {HAL_BIT, HAL_OUT, offsetof(lcec_class_cia402_channel_t, supports_mode_pp), "%s.%s.%s.%s-supports-mode-pp"},
    {HAL_BIT, HAL_OUT, offsetof(lcec_class_cia402_channel_t, supports_mode_vl), "%s.%s.%s.%s-supports-mode-vl"},
    {HAL_BIT, HAL_OUT, offsetof(lcec_class_cia402_channel_t, supports_mode_pv), "%s.%s.%s.%s-supports-mode-pv"},
    {HAL_BIT, HAL_OUT, offsetof(lcec_class_cia402_channel_t, supports_mode_tq), "%s.%s.%s.%s-supports-mode-tq"},
    {HAL_BIT, HAL_OUT, offsetof(lcec_class_cia402_channel_t, supports_mode_hm), "%s.%s.%s.%s-supports-mode-hm"},
    {HAL_BIT, HAL_OUT, offsetof(lcec_class_cia402_channel_t, supports_mode_ip), "%s.%s.%s.%s-supports-mode-ip"},
    {HAL_BIT, HAL_OUT, offsetof(lcec_class_cia402_channel_t, supports_mode_csp), "%s.%s.%s.%s-supports-mode-csp"},
    {HAL_BIT, HAL_OUT, offsetof(lcec_class_cia402_channel_t, supports_mode_csv), "%s.%s.%s.%s-supports-mode-csv"},
    {HAL_BIT, HAL_OUT, offsetof(lcec_class_cia402_channel_t, supports_mode_cst), "%s.%s.%s.%s-supports-mode-cst"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

/// @brief Create a new, optional pin for reading, using standardized names.
#define OPTIONAL_PIN_READ(var_name, pin_name)                                                    \
  static const lcec_pindesc_t pins_##var_name[] = {                                                    \
      {PDO_PIN_TYPE_##var_name, HAL_OUT, offsetof(lcec_class_cia402_channel_t, var_name), "%s.%s.%s.%s-" pin_name}, \
      {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},                                           \
  }

/// @brief Create a new, optional pin for writing, using standardized names.
#define OPTIONAL_PIN_WRITE(var_name, pin_name)                                                  \
  static const lcec_pindesc_t pins_##var_name[] = {                                                   \
      {PDO_PIN_TYPE_##var_name, HAL_IN, offsetof(lcec_class_cia402_channel_t, var_name), "%s.%s.%s.%s-" pin_name}, \
      {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},                                          \
  }

// I really wish I could get rid of the quoted part here, but I want
// to keep pin names using dashes while obviously we can't use dashes
// in C variable names.  I guess I could post-process these when
// they're applied to turn `_` into `-`, but that feels like a bit
// much at the moment.
OPTIONAL_PIN_READ(actual_current, "actual-current");
OPTIONAL_PIN_READ(actual_following_error, "actual-following-error");
OPTIONAL_PIN_READ(actual_position, "actual-position");
OPTIONAL_PIN_READ(actual_torque, "actual-torque");
OPTIONAL_PIN_READ(actual_velocity, "actual-velocity");
OPTIONAL_PIN_READ(actual_velocity_sensor, "actual-velocity-sensor");
OPTIONAL_PIN_READ(actual_voltage, "actual-voltage");
OPTIONAL_PIN_READ(opmode_display, "opmode-display");
OPTIONAL_PIN_READ(torque_demand, "torque-demand");
OPTIONAL_PIN_READ(velocity_demand, "velocity-demand");

OPTIONAL_PIN_WRITE(following_error_timeout, "following-error-timeout");
OPTIONAL_PIN_WRITE(following_error_window, "following-error-window");
OPTIONAL_PIN_WRITE(home_accel, "home-accel");
OPTIONAL_PIN_WRITE(home_method, "home-method");
OPTIONAL_PIN_WRITE(home_velocity_fast, "home-velocity-fast");
OPTIONAL_PIN_WRITE(home_velocity_slow, "home-velocity-slow");
OPTIONAL_PIN_WRITE(interpolation_time_period, "interpolation-time-period");
OPTIONAL_PIN_WRITE(maximum_acceleration, "maximum-acceleration");
OPTIONAL_PIN_WRITE(maximum_current, "maximum-current");
OPTIONAL_PIN_WRITE(maximum_deceleration, "maximum-deceleration");
OPTIONAL_PIN_WRITE(maximum_motor_rpm, "maximum-motor-rpm");
OPTIONAL_PIN_WRITE(maximum_torque, "torque-maximum");
OPTIONAL_PIN_WRITE(motion_profile, "motion-profile");
OPTIONAL_PIN_WRITE(motor_rated_current, "motor-rated-current");
OPTIONAL_PIN_WRITE(motor_rated_torque, "motor-rated-torque");
OPTIONAL_PIN_WRITE(opmode, "opmode");
OPTIONAL_PIN_WRITE(polarity, "polarity");
OPTIONAL_PIN_WRITE(profile_accel, "profile-accel");
OPTIONAL_PIN_WRITE(profile_decel, "profile-decel");
OPTIONAL_PIN_WRITE(profile_end_velocity, "profile-end-velocity");
OPTIONAL_PIN_WRITE(profile_max_velocity, "profile-max-velocity");
OPTIONAL_PIN_WRITE(profile_velocity, "profile-velocity");
OPTIONAL_PIN_WRITE(target_position, "target-position");
OPTIONAL_PIN_WRITE(target_torque, "target-torque");
OPTIONAL_PIN_WRITE(target_velocity, "target-velocity");
OPTIONAL_PIN_WRITE(torque_profile_type, "torque-profile-type");
OPTIONAL_PIN_WRITE(torque_slope, "torque-slope");
OPTIONAL_PIN_WRITE(velocity_error_time, "velocity-error-time");
OPTIONAL_PIN_WRITE(velocity_error_window, "velocity-error-window");
OPTIONAL_PIN_WRITE(velocity_sensor_selector, "velocity-sensor-selector"); 
OPTIONAL_PIN_WRITE(velocity_threshold_time, "velocity-threshold-time");
OPTIONAL_PIN_WRITE(velocity_threshold_window, "velocity-threshold-window");

/// @brief Create a `lcec_class_cia402_enabled_t` from a
/// `lcec_class_cia402_options_t`.
static lcec_class_cia402_enabled_t *lcec_cia402_enabled(lcec_class_cia402_options_t *opt) {
  lcec_class_cia402_enabled_t *enabled;
  enabled = hal_malloc(sizeof(lcec_class_cia402_enabled_t));
  if (enabled == NULL) return NULL;

  memset(enabled, 0, sizeof(lcec_class_cia402_enabled_t));

  if (opt->enable_opmode) {
    enabled->enable_opmode = 1;
    enabled->enable_opmode_display = 1;
  }
  if (opt->enable_pp) {
    enabled->enable_actual_position = 1;
    enabled->enable_target_position = 1;
  }
  if (opt->enable_pv) {
    enabled->enable_actual_velocity = 1;
    enabled->enable_target_velocity = 1;
  }
  if (opt->enable_csp) {
    enabled->enable_actual_position = 1;
    enabled->enable_target_position = 1;
    // TODO: add interpolation pins once they're added.
  }
  if (opt->enable_csv) {
    enabled->enable_actual_velocity = 1;
    enabled->enable_target_velocity = 1;
    // TODO: add interpolation pins once they're added.
  }
  if (opt->enable_hm) {
    enabled->enable_hm = 1;
    enabled->enable_home_method = 1;
    enabled->enable_home_velocity_fast = 1;
    enabled->enable_home_velocity_slow = 1;
  }
  if (opt->enable_ip) {
    // TODO: add interpolation pins once they're added.
  }
  if (opt->enable_vl) {
    // TODO: add velocity pins once they're added.
  }
  if (opt->enable_cst) {
    // TODO: add cyclic synchronous torque pins once they're added.
  }

  // Set individual pins in `enabled` using values from `opt`.
#define ENABLE_OPT(pin_name) \
  if (opt->enable_##pin_name) enabled->enable_##pin_name = 1
  ENABLE_OPT(actual_current);
  ENABLE_OPT(actual_following_error);
  ENABLE_OPT(actual_torque);
  ENABLE_OPT(actual_velocity_sensor);
  ENABLE_OPT(actual_voltage);
  ENABLE_OPT(digital_input);
  ENABLE_OPT(digital_output);
  ENABLE_OPT(following_error_timeout);
  ENABLE_OPT(following_error_window);
  ENABLE_OPT(home_accel);
  ENABLE_OPT(interpolation_time_period);
  ENABLE_OPT(maximum_acceleration);
  ENABLE_OPT(maximum_current);
  ENABLE_OPT(maximum_deceleration);
  ENABLE_OPT(maximum_motor_rpm);
  ENABLE_OPT(maximum_torque);
  ENABLE_OPT(motion_profile);
  ENABLE_OPT(motor_rated_current);
  ENABLE_OPT(motor_rated_torque);
  ENABLE_OPT(polarity);
  ENABLE_OPT(profile_accel);
  ENABLE_OPT(profile_decel);
  ENABLE_OPT(profile_end_velocity);
  ENABLE_OPT(profile_max_velocity);
  ENABLE_OPT(profile_velocity);
  ENABLE_OPT(target_torque);
  ENABLE_OPT(torque_demand);
  ENABLE_OPT(torque_profile_type);
  ENABLE_OPT(torque_slope);
  ENABLE_OPT(velocity_demand);
  ENABLE_OPT(velocity_error_time);
  ENABLE_OPT(velocity_error_window);
  ENABLE_OPT(velocity_sensor_selector); 
  ENABLE_OPT(velocity_threshold_time);
  ENABLE_OPT(velocity_threshold_window);

  return enabled;
}

/// @brief Allocate a block of memory for holding the results from
/// `count` calls to `lcec_cia402_register_device() and friends.
///
/// It is the caller's responsibility to verify that the result is not NULL.
///
/// @param count The number of input channels (axes) to allocate
///              memory for.
/// @return A `lcec_class_cia402_channels_t` for holding
///         future results from `lcec_cia402_register_channel()`.
lcec_class_cia402_channels_t *lcec_cia402_allocate_channels(int count) {
  lcec_class_cia402_channels_t *channels;

  channels = hal_malloc(sizeof(lcec_class_cia402_channels_t));
  if (channels == NULL) {
    return NULL;
  }
  channels->count = count;
  channels->channels = hal_malloc(sizeof(lcec_class_cia402_channel_t *) * count);
  if (channels->channels == NULL) {
    return NULL;
  }
  return channels;
}

/// @brief Allocates a `lcec_class_cia402_options_t` and initializes it.
///
/// Most of the defaults are actually coded in
/// `lcec_cia402_register_channel`, so we can safely just memset
/// everything to 0 here.
lcec_class_cia402_options_t *lcec_cia402_options_single_axis(void) {
  lcec_class_cia402_options_t *opts = hal_malloc(sizeof(lcec_class_cia402_options_t));
  if (opts == NULL) {
    return NULL;
  }
  memset(opts, 0, sizeof(lcec_class_cia402_options_t));
  opts->enable_opmode = 1;  // Should almost always be enabled.

  return opts;
}

/// @brief Allocates a `lcec_class_cia402_options_t` and initializes it.
///
/// This case is used for multi-axis devices, and sets the name prefix to `srv-X` instead of just `srv`.
///
/// @param axis The axis number; will be used to set naming prefixes
/// to `srv-<axis>` instead of just `srv`.
lcec_class_cia402_options_t *lcec_cia402_options_multi_axes(int axis) {
  lcec_class_cia402_options_t *opts = lcec_cia402_options_single_axis();

  if (opts == NULL) {
    return NULL;
  }

  char *prefix = hal_malloc(sizeof(char[10]));
  if (prefix == NULL) {
    return NULL;
  }

  snprintf(prefix, 10, "srv-%d", axis);
  opts->name_prefix = prefix;

  return opts;
}

/// @brief Allocates a `lcec_syncs_t` and fills in the CiA 402 portion of it using data from `options`.
///
/// @param opt A `lcec_class_cia402_options_t` structure that describes the options in use.
lcec_syncs_t *lcec_cia402_init_sync(lcec_class_cia402_options_t *options) {
  lcec_syncs_t *syncs;

  syncs = hal_malloc(sizeof(lcec_syncs_t));
  if (syncs == NULL) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "hal_malloc() for lcec_cia402_init_sync failed\n");
    return NULL;
  }
  lcec_syncs_init(syncs);
  lcec_syncs_add_sync(syncs, EC_DIR_OUTPUT, EC_WD_DEFAULT);
  lcec_syncs_add_sync(syncs, EC_DIR_INPUT, EC_WD_DEFAULT);

  return syncs;
}

/// @brief Add an optional PDO, if it's enabled in `enabled`.
///
/// This simply removes a bunch of boilerplate code and makes optional
/// PDOs more readable.
#define MAP_OPTIONAL_PDO(name)       \
  if (enabled->enable_##name) {                             \
    lcec_syncs_add_pdo_entry(syncs, 0x6000 + PDO_IDX_OFFSET_##name, PDO_SIDX_##name, PDO_BITS_##name); \
  }

/// @brief Sets up the first batch of output PDOs for syncing.
///
/// This should be called after `lcec_cia402_init_sync()`, but before
/// registering any device-specific PDOs.  Once this returns, then
/// call `lcec_syncs_add_pdo_info(syncs, 0x1601)` and whichever
/// `lcec_syncs_add_pdo_entry()` calls you need.
int lcec_cia402_add_output_sync(lcec_syncs_t *syncs, lcec_class_cia402_options_t *options) {
  lcec_class_cia402_enabled_t *enabled = lcec_cia402_enabled(options);
  if (enabled == NULL) return -1;

  lcec_syncs_add_sync(syncs, EC_DIR_OUTPUT, EC_WD_DEFAULT);
  lcec_syncs_add_pdo_info(syncs, 0x1600);
  lcec_syncs_add_pdo_entry(syncs, 0x6040, 0x00, 16);  // Control word
  MAP_OPTIONAL_PDO(digital_output);
  MAP_OPTIONAL_PDO(following_error_timeout);
  MAP_OPTIONAL_PDO(following_error_window);
  MAP_OPTIONAL_PDO(home_accel);
  MAP_OPTIONAL_PDO(home_method);
  MAP_OPTIONAL_PDO(home_velocity_fast);
  MAP_OPTIONAL_PDO(home_velocity_slow);
  MAP_OPTIONAL_PDO(interpolation_time_period);
  MAP_OPTIONAL_PDO(maximum_acceleration);
  MAP_OPTIONAL_PDO(maximum_current);
  MAP_OPTIONAL_PDO(maximum_deceleration);
  MAP_OPTIONAL_PDO(maximum_motor_rpm);
  MAP_OPTIONAL_PDO(maximum_torque);
  MAP_OPTIONAL_PDO(motion_profile);
  MAP_OPTIONAL_PDO(motor_rated_torque);
  MAP_OPTIONAL_PDO(opmode);          // Operating mode
  MAP_OPTIONAL_PDO(polarity);
  MAP_OPTIONAL_PDO(profile_accel);
  MAP_OPTIONAL_PDO(profile_decel);
  MAP_OPTIONAL_PDO(profile_end_velocity);
  MAP_OPTIONAL_PDO(profile_max_velocity);
  MAP_OPTIONAL_PDO(profile_velocity);
  MAP_OPTIONAL_PDO(target_position);
  MAP_OPTIONAL_PDO(target_torque);
  MAP_OPTIONAL_PDO(target_velocity);
  MAP_OPTIONAL_PDO(torque_profile_type);
  MAP_OPTIONAL_PDO(torque_slope);
  MAP_OPTIONAL_PDO(velocity_error_time);
  MAP_OPTIONAL_PDO(velocity_error_window);
  MAP_OPTIONAL_PDO(velocity_sensor_selector); 
  MAP_OPTIONAL_PDO(velocity_threshold_time);
  MAP_OPTIONAL_PDO(velocity_threshold_window);

  return 0;
};

/// @brief Sets up the first batch of input PDOs for syncing.
///
/// This should be called after you're done with all output PDOs.
/// Once this returns, then call `lcec_syncs_add_pdo_info(syncs,
/// 0x1a01)` and whichever `lcec_syncs_add_pdo_entry()` calls you
/// need.
int lcec_cia402_add_input_sync(lcec_syncs_t *syncs, lcec_class_cia402_options_t *options) {
  lcec_class_cia402_enabled_t *enabled = lcec_cia402_enabled(options);
  if (enabled == NULL) return -1;

  lcec_syncs_add_sync(syncs, EC_DIR_INPUT, EC_WD_DEFAULT);
  lcec_syncs_add_pdo_info(syncs, 0x1a00);
  lcec_syncs_add_pdo_entry(syncs, 0x6041, 0x00, 16);  // Status word
  MAP_OPTIONAL_PDO(actual_current);
  MAP_OPTIONAL_PDO(actual_following_error);
  MAP_OPTIONAL_PDO(actual_position);
  MAP_OPTIONAL_PDO(actual_torque);
  MAP_OPTIONAL_PDO(actual_velocity);
  MAP_OPTIONAL_PDO(actual_velocity_sensor);
  MAP_OPTIONAL_PDO(actual_voltage);
  MAP_OPTIONAL_PDO(digital_input);
  MAP_OPTIONAL_PDO(opmode_display);
  MAP_OPTIONAL_PDO(torque_demand);
  MAP_OPTIONAL_PDO(velocity_demand);

  return 0;
};

/// @brief Register a new CiA 402 channel.
///
/// This creates a new CiA 402 channel, which is basically a single
/// axis for a CiA 402-compatible stepper or servo controller.
///
/// @param slave The `slave` passed into `_init`.
/// @param base_idx The base index for PDOs for this channel.  For
///   single-axis devices, this should be 0x6000.  For multi-axis
///   devices, it should be 0x6000 for the first, then 0x6800, 0x7000,
///   0x7800, etc.  See the documentation for your servo driver for
///   details.
/// @param opt Optional settings.  `NULL` for defaults, or a
///   `lcec_class_cia402_options_t` from
///   `lcec_cia402_options_single_axis()` or
///   `lcec_cia402_options_multi_axes()`.
lcec_class_cia402_channel_t *lcec_cia402_register_channel(struct lcec_slave *slave, uint16_t base_idx, lcec_class_cia402_options_t *opt) {
  lcec_class_cia402_channel_t *data;
  int err;
  lcec_class_cia402_enabled_t *enabled;

  // The default name depends on the port type.
  char *name_prefix = "srv";
  if (opt && opt->name_prefix) name_prefix = opt->name_prefix;

  // If we were passed a NULL opt, then create a new
  // `lcec_class_cia402_options_t` and write the defaults back into it,
  // so we don't need to repeat the above default code downstream.
  if (!opt) {
    opt = lcec_cia402_options_single_axis();
    if (opt == NULL) {
      rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "hal_malloc() for slave %s.%s failed\n", slave->master->name, slave->name);
      return NULL;
    }
  }

  // Allocate memory for per-channel data.
  data = hal_malloc(sizeof(lcec_class_cia402_channel_t));
  if (data == NULL) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "hal_malloc() for slave %s.%s failed\n", slave->master->name, slave->name);
    return NULL;
  }
  memset(data, 0, sizeof(lcec_class_cia402_channel_t));

  // Save important options for later use.  None of the _idx/_sidx will be needed outside of this function.
  data->options = opt;

  // Set the `enabled` struct from `opt`.
  enabled = lcec_cia402_enabled(opt);
  if (enabled == NULL) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "hal_malloc() for slave %s.%s failed\n", slave->master->name, slave->name);
    return NULL;
  }
  data->enabled = enabled;

  // Register PDOs
  lcec_pdo_init(slave, base_idx + 0x40, 0, &data->controlword_os, NULL);
  lcec_pdo_init(slave, base_idx + 0x41, 0, &data->statusword_os, NULL);

#define INIT_OPTIONAL_PDO(pin_name) \
  if (enabled->enable_##pin_name) lcec_pdo_init(slave, base_idx + PDO_IDX_OFFSET_##pin_name, PDO_SIDX_##pin_name, &data->pin_name##_os, NULL)

  INIT_OPTIONAL_PDO(actual_current);
  INIT_OPTIONAL_PDO(actual_following_error);
  INIT_OPTIONAL_PDO(actual_position);
  INIT_OPTIONAL_PDO(actual_torque);
  INIT_OPTIONAL_PDO(actual_velocity);
  INIT_OPTIONAL_PDO(actual_velocity_sensor);
  INIT_OPTIONAL_PDO(actual_voltage);
  INIT_OPTIONAL_PDO(following_error_timeout);
  INIT_OPTIONAL_PDO(following_error_window);
  INIT_OPTIONAL_PDO(home_accel);
  INIT_OPTIONAL_PDO(home_method);
  INIT_OPTIONAL_PDO(home_velocity_fast);
  INIT_OPTIONAL_PDO(home_velocity_slow);
  INIT_OPTIONAL_PDO(interpolation_time_period);
  INIT_OPTIONAL_PDO(maximum_acceleration);
  INIT_OPTIONAL_PDO(maximum_current);
  INIT_OPTIONAL_PDO(maximum_deceleration);
  INIT_OPTIONAL_PDO(maximum_motor_rpm);
  INIT_OPTIONAL_PDO(maximum_torque);
  INIT_OPTIONAL_PDO(motion_profile);
  INIT_OPTIONAL_PDO(motor_rated_current);
  INIT_OPTIONAL_PDO(motor_rated_torque);
  INIT_OPTIONAL_PDO(opmode);
  INIT_OPTIONAL_PDO(opmode_display);
  INIT_OPTIONAL_PDO(polarity);
  INIT_OPTIONAL_PDO(profile_accel);
  INIT_OPTIONAL_PDO(profile_decel);
  INIT_OPTIONAL_PDO(profile_end_velocity);
  INIT_OPTIONAL_PDO(profile_max_velocity);
  INIT_OPTIONAL_PDO(profile_velocity);
  INIT_OPTIONAL_PDO(target_position);
  INIT_OPTIONAL_PDO(target_torque);
  INIT_OPTIONAL_PDO(target_velocity);
  INIT_OPTIONAL_PDO(torque_demand);
  INIT_OPTIONAL_PDO(torque_profile_type);
  INIT_OPTIONAL_PDO(torque_slope);
  INIT_OPTIONAL_PDO(velocity_demand); 
  INIT_OPTIONAL_PDO(velocity_error_time);
  INIT_OPTIONAL_PDO(velocity_error_window);
  INIT_OPTIONAL_PDO(velocity_sensor_selector); 
  INIT_OPTIONAL_PDO(velocity_threshold_time);
  INIT_OPTIONAL_PDO(velocity_threshold_window);

  // Register pins
  err = lcec_pin_newf_list(data, pins_required, LCEC_MODULE_NAME, slave->master->name, slave->name, name_prefix);
  if (err != 0) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "lcec_pin_newf_list for slave %s.%s failed\n", slave->master->name, slave->name);
    return NULL;
  }

#define REGISTER_OPTIONAL_PINS(pin_name)                                                                                              \
  do {                                                                                                                                \
    if (enabled->enable_##pin_name) {                                                                                                 \
      err = lcec_pin_newf_list(data, pins_##pin_name, LCEC_MODULE_NAME, slave->master->name, slave->name, name_prefix);               \
      if (err != 0) {                                                                                                                 \
        rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "lcec_pin_newf_list for slave %s.%s failed\n", slave->master->name, slave->name); \
        return NULL;                                                                                                                  \
      }                                                                                                                               \
    }                                                                                                                                 \
  } while (0)
  //

  REGISTER_OPTIONAL_PINS(actual_current);
  REGISTER_OPTIONAL_PINS(actual_following_error);
  REGISTER_OPTIONAL_PINS(actual_position);
  REGISTER_OPTIONAL_PINS(actual_torque);
  REGISTER_OPTIONAL_PINS(actual_velocity);
  REGISTER_OPTIONAL_PINS(actual_velocity_sensor);
  REGISTER_OPTIONAL_PINS(actual_voltage);
  REGISTER_OPTIONAL_PINS(following_error_timeout);
  REGISTER_OPTIONAL_PINS(following_error_window);
  REGISTER_OPTIONAL_PINS(home_accel);
  REGISTER_OPTIONAL_PINS(home_method);
  REGISTER_OPTIONAL_PINS(home_velocity_fast);
  REGISTER_OPTIONAL_PINS(home_velocity_slow);
  REGISTER_OPTIONAL_PINS(interpolation_time_period);
  REGISTER_OPTIONAL_PINS(maximum_acceleration);
  REGISTER_OPTIONAL_PINS(maximum_current);
  REGISTER_OPTIONAL_PINS(maximum_deceleration);
  REGISTER_OPTIONAL_PINS(maximum_motor_rpm);
  REGISTER_OPTIONAL_PINS(maximum_torque);
  REGISTER_OPTIONAL_PINS(motion_profile);
  REGISTER_OPTIONAL_PINS(motor_rated_current);
  REGISTER_OPTIONAL_PINS(motor_rated_torque);
  REGISTER_OPTIONAL_PINS(opmode);
  REGISTER_OPTIONAL_PINS(opmode_display);
  REGISTER_OPTIONAL_PINS(polarity);
  REGISTER_OPTIONAL_PINS(profile_accel);
  REGISTER_OPTIONAL_PINS(profile_decel);
  REGISTER_OPTIONAL_PINS(profile_end_velocity);
  REGISTER_OPTIONAL_PINS(profile_max_velocity);
  REGISTER_OPTIONAL_PINS(profile_velocity);
  REGISTER_OPTIONAL_PINS(target_position);
  REGISTER_OPTIONAL_PINS(target_torque);
  REGISTER_OPTIONAL_PINS(target_velocity);
  REGISTER_OPTIONAL_PINS(torque_demand);
  REGISTER_OPTIONAL_PINS(torque_profile_type);
  REGISTER_OPTIONAL_PINS(torque_slope);
  REGISTER_OPTIONAL_PINS(velocity_demand);
  REGISTER_OPTIONAL_PINS(velocity_error_time);
  REGISTER_OPTIONAL_PINS(velocity_error_window);
  REGISTER_OPTIONAL_PINS(velocity_sensor_selector); 
  REGISTER_OPTIONAL_PINS(velocity_threshold_time);
  REGISTER_OPTIONAL_PINS(velocity_threshold_window);

  // Set default values for pins here.
  uint32_t modes;
  lcec_read_sdo32(slave, base_idx + 0x502, 0, &modes);

  *(data->supported_modes) = modes;
  *(data->supports_mode_pp) = modes & 1 << 0;
  *(data->supports_mode_vl) = modes & 1 << 1;
  *(data->supports_mode_pv) = modes & 1 << 2;
  *(data->supports_mode_tq) = modes & 1 << 3;
  *(data->supports_mode_hm) = modes & 1 << 5;
  *(data->supports_mode_ip) = modes & 1 << 6;
  *(data->supports_mode_csp) = modes & 1 << 7;
  *(data->supports_mode_csv) = modes & 1 << 8;
  *(data->supports_mode_cst) = modes & 1 << 9;

  /// @brief Initializes a pin's defaults using the current value of the backing SDO.
  ///
  /// Calling `SET_OPTIONAL_DEFAULTS_FOO` checks to see if
  /// `enabled->enable_FOO` is true, and if so it sets the pin data
  /// stores in `data->FOO` to the current value of the SDO behind it.
  ///
  /// It does this by looking at PDO_IDX_OFFSET_FOO and PDO_SIDX_FOO
  /// to find the SDO port.  Then it reads the correct number of bits
  /// and the correct signedness using `lcec_read_sdo_XX_pin_YY32`,
  /// where XX is the value of PDO_BITS_FOO and YY is value of
  /// PDO_SIGN_FOO.
  ///
  /// The upshot?  Call SET_OPTIONAL_DEFAULTS(FOO), and the right
  /// thing happens.
#define SET_OPTIONAL_DEFAULTS(pin_name) \
  if (enabled->enable_##pin_name) SUBSTJOIN5(lcec_read_sdo, PDO_BITS_##pin_name, _pin_, PDO_SIGN_##pin_name, 32)(slave, base_idx + PDO_IDX_OFFSET_##pin_name, PDO_SIDX_##pin_name, data->pin_name)

  SET_OPTIONAL_DEFAULTS(following_error_timeout);
  SET_OPTIONAL_DEFAULTS(following_error_window);
  SET_OPTIONAL_DEFAULTS(home_accel);
  SET_OPTIONAL_DEFAULTS(home_method);
  SET_OPTIONAL_DEFAULTS(home_velocity_fast);
  SET_OPTIONAL_DEFAULTS(home_velocity_slow);
  SET_OPTIONAL_DEFAULTS(interpolation_time_period);
  SET_OPTIONAL_DEFAULTS(maximum_acceleration);
  SET_OPTIONAL_DEFAULTS(maximum_current);
  SET_OPTIONAL_DEFAULTS(maximum_deceleration);
  SET_OPTIONAL_DEFAULTS(maximum_motor_rpm);
  SET_OPTIONAL_DEFAULTS(maximum_torque);
  SET_OPTIONAL_DEFAULTS(motion_profile);
  SET_OPTIONAL_DEFAULTS(motor_rated_current);
  SET_OPTIONAL_DEFAULTS(motor_rated_torque);
  SET_OPTIONAL_DEFAULTS(polarity);
  SET_OPTIONAL_DEFAULTS(profile_accel);
  SET_OPTIONAL_DEFAULTS(profile_decel);
  SET_OPTIONAL_DEFAULTS(profile_end_velocity);
  SET_OPTIONAL_DEFAULTS(profile_max_velocity);
  SET_OPTIONAL_DEFAULTS(profile_velocity);
  SET_OPTIONAL_DEFAULTS(target_torque);
  SET_OPTIONAL_DEFAULTS(torque_profile_type);
  SET_OPTIONAL_DEFAULTS(torque_slope);
  SET_OPTIONAL_DEFAULTS(velocity_error_time);
  SET_OPTIONAL_DEFAULTS(velocity_error_window);
  SET_OPTIONAL_DEFAULTS(velocity_sensor_selector); 
  SET_OPTIONAL_DEFAULTS(velocity_threshold_time);
  SET_OPTIONAL_DEFAULTS(velocity_threshold_window);

  return data;
}

/// @brief Reads data from a single CiA 402 channel (one axis).
///
/// @param slave The `slave`, passed from the per-device `_read`.
/// @param data  Which channel to read; a `lcec_class_cia402_channel_t *`, as returned by lcec_cia402_register_channel.
///
/// Call this once per channel registered, from inside of your device's
/// read function.  Use `lcec_cia402_read_all` to read all channels.
void lcec_cia402_read(struct lcec_slave *slave, lcec_class_cia402_channel_t *data) {
  uint8_t *pd = slave->master->process_data;

#define READ_OPT(pin_name) \
  if (data->enabled->enable_##pin_name) *(data->pin_name) = (SUBSTJOIN3(EC_READ_, PDO_SIGN_##pin_name, PDO_BITS_##pin_name) (&pd[data->pin_name##_os]))

  *(data->statusword) = EC_READ_U16(&pd[data->statusword_os]);
  READ_OPT(actual_current);
  READ_OPT(actual_following_error);
  READ_OPT(actual_position);
  READ_OPT(actual_torque);
  READ_OPT(actual_velocity);
  READ_OPT(actual_velocity_sensor);
  READ_OPT(actual_voltage);
  READ_OPT(opmode_display);
  READ_OPT(torque_demand);
  READ_OPT(velocity_demand);
}

/// @brief Reads data from all CiA 402 input ports.
///
/// @param slave The `slave`, passed from the per-device `_read`.
/// @param channels An `lcec_class_cia402_channel_t *`, as returned by lcec_cia402_register_channel.
void lcec_cia402_read_all(struct lcec_slave *slave, lcec_class_cia402_channels_t *channels) {
  for (int i = 0; i < channels->count; i++) {
    lcec_cia402_read(slave, channels->channels[i]);
  }
}

void lcec_cia402_write(struct lcec_slave *slave, lcec_class_cia402_channel_t *data) {
  uint8_t *pd = slave->master->process_data;

#define WRITE_OPT(name) \
  if (data->enabled->enable_##name) SUBSTJOIN3(EC_WRITE_, PDO_SIGN_##name, PDO_BITS_##name) (&pd[data->name##_os], *(data->name))

  EC_WRITE_U16(&pd[data->controlword_os], (uint16_t)(*(data->controlword)));
  WRITE_OPT(following_error_timeout);
  WRITE_OPT(following_error_window);
  WRITE_OPT(home_accel);
  WRITE_OPT(home_method);
  WRITE_OPT(home_velocity_fast);
  WRITE_OPT(home_velocity_slow);
  WRITE_OPT(interpolation_time_period);
  WRITE_OPT(maximum_acceleration);
  WRITE_OPT(maximum_current);
  WRITE_OPT(maximum_deceleration);
  WRITE_OPT(maximum_motor_rpm);
  WRITE_OPT(maximum_torque);
  WRITE_OPT(motion_profile);
  WRITE_OPT(motor_rated_current);
  WRITE_OPT(motor_rated_torque);
  WRITE_OPT(opmode);
  WRITE_OPT(polarity);
  WRITE_OPT(profile_accel);
  WRITE_OPT(profile_decel);
  WRITE_OPT(profile_end_velocity);
  WRITE_OPT(profile_max_velocity);
  WRITE_OPT(profile_velocity);
  WRITE_OPT(target_position);
  WRITE_OPT(target_torque);
  WRITE_OPT(target_velocity);
  WRITE_OPT(torque_profile_type);
  WRITE_OPT(torque_slope);
  WRITE_OPT(velocity_demand);
  WRITE_OPT(velocity_error_time);
  WRITE_OPT(velocity_error_window);
  WRITE_OPT(velocity_sensor_selector); 
  WRITE_OPT(velocity_threshold_time);
  WRITE_OPT(velocity_threshold_window);
}

/// @brief Writess data to all CiA 402 output ports.
///
/// @param slave The `slave`, passed from the per-device `_read`.
/// @param channels An `lcec_class_cia402_channel_t *`, as returned by lcec_cia402_register_channel.
void lcec_cia402_write_all(struct lcec_slave *slave, lcec_class_cia402_channels_t *channels) {
  for (int i = 0; i < channels->count; i++) {
    lcec_class_cia402_channel_t *channel = channels->channels[i];

    lcec_cia402_write(slave, channel);
  }
}

/// @brief Modparams settings available via XML.
static const lcec_modparam_desc_t per_channel_modparams[] = {
    {"positionLimitMin", CIA402_MP_POSLIMIT_MIN, MODPARAM_TYPE_S32},
    {"positionLimitMax", CIA402_MP_POSLIMIT_MAX, MODPARAM_TYPE_S32},
    {"swPositionLimitMin", CIA402_MP_SWPOSLIMIT_MIN, MODPARAM_TYPE_S32},
    {"swPositionLimitMax", CIA402_MP_SWPOSLIMIT_MIN, MODPARAM_TYPE_S32},
    {"homeOffset", CIA402_MP_HOME_OFFSET, MODPARAM_TYPE_S32},
    {"quickDecel", CIA402_MP_QUICKDECEL, MODPARAM_TYPE_U32},
    {"quickStopOptionCode", CIA402_MP_OPTCODE_QUICKSTOP, MODPARAM_TYPE_S32},
    {"shutdownOptionCode", CIA402_MP_OPTCODE_SHUTDOWN, MODPARAM_TYPE_S32},
    {"disableOptionCode", CIA402_MP_OPTCODE_DISABLE, MODPARAM_TYPE_S32},
    {"haltOptionCode", CIA402_MP_OPTCODE_HALT, MODPARAM_TYPE_S32},
    {"faultOptionCode", CIA402_MP_OPTCODE_FAULT, MODPARAM_TYPE_S32},
    {"probeFunction", CIA402_MP_PROBE_FUNCTION, MODPARAM_TYPE_U32},
    {"probe1Positive", CIA402_MP_PROBE1_POS, MODPARAM_TYPE_S32},
    {"probe1Negative", CIA402_MP_PROBE1_NEG, MODPARAM_TYPE_S32},
    {"probe2Positive", CIA402_MP_PROBE2_POS, MODPARAM_TYPE_S32},
    {"probe2Negative", CIA402_MP_PROBE2_NEG, MODPARAM_TYPE_S32},
    {"enablePP", CIA402_MP_ENABLE_PP, MODPARAM_TYPE_BIT},
    {"enablePV", CIA402_MP_ENABLE_PV, MODPARAM_TYPE_BIT},
    {"enableCSP", CIA402_MP_ENABLE_CSP, MODPARAM_TYPE_BIT},
    {"enableCSV", CIA402_MP_ENABLE_CSV, MODPARAM_TYPE_BIT},
    {"enableHM", CIA402_MP_ENABLE_HM, MODPARAM_TYPE_BIT},
    {"enableIP", CIA402_MP_ENABLE_IP, MODPARAM_TYPE_BIT},
    {"enableVL", CIA402_MP_ENABLE_VL, MODPARAM_TYPE_BIT},
    {"enableTQ", CIA402_MP_ENABLE_TQ, MODPARAM_TYPE_BIT},
    {"enableCST", CIA402_MP_ENABLE_CST, MODPARAM_TYPE_BIT},
    {"enableActualCurrent", CIA402_MP_ENABLE_ACTUAL_CURRENT, MODPARAM_TYPE_BIT},
    {"enableActualFollowingError", CIA402_MP_ENABLE_ACTUAL_FOLLOWING_ERROR, MODPARAM_TYPE_BIT},
    {"enableActualTorque", CIA402_MP_ENABLE_ACTUAL_TORQUE, MODPARAM_TYPE_BIT},
    {"enableActualVelocitySensor", CIA402_MP_ENABLE_ACTUAL_VELOCITY_SENSOR, MODPARAM_TYPE_BIT},
    {"enableActualVoltage", CIA402_MP_ENABLE_ACTUAL_VOLTAGE, MODPARAM_TYPE_BIT},
    {"enableFollowingErrorTimeout", CIA402_MP_ENABLE_FOLLOWING_ERROR_TIMEOUT, MODPARAM_TYPE_BIT},
    {"enableFollowingErrorWindow", CIA402_MP_ENABLE_FOLLOWING_ERROR_WINDOW, MODPARAM_TYPE_BIT},
    {"enableHomeAccel", CIA402_MP_ENABLE_HOME_ACCEL, MODPARAM_TYPE_BIT},
    {"enableInterpolationTimePeriod", CIA402_MP_ENABLE_INTERPOLATION_TIME_PERIOD, MODPARAM_TYPE_BIT},
    {"enableMaximumAcceleration", CIA402_MP_ENABLE_MAXIMUM_ACCELERATION, MODPARAM_TYPE_BIT},
    {"enableMaximumCurrent", CIA402_MP_ENABLE_MAXIMUM_CURRENT, MODPARAM_TYPE_BIT},
    {"enableMaximumDeceleration", CIA402_MP_ENABLE_MAXIMUM_DECELERATION, MODPARAM_TYPE_BIT},
    {"enableMaximumMotorRPM", CIA402_MP_ENABLE_MAXIMUM_MOTOR_RPM, MODPARAM_TYPE_BIT},
    {"enableMaximumTorque", CIA402_MP_ENABLE_MAXIMUM_TORQUE, MODPARAM_TYPE_BIT},
    {"enableMotorRatedCurrent", CIA402_MP_ENABLE_MOTOR_RATED_CURRENT, MODPARAM_TYPE_BIT},
    {"enableMotorRatedTorque", CIA402_MP_ENABLE_MOTOR_RATED_TORQUE, MODPARAM_TYPE_BIT},
    {"enablePolarity", CIA402_MP_ENABLE_POLARITY, MODPARAM_TYPE_BIT},
    {"enableProfileAccel", CIA402_MP_ENABLE_PROFILE_ACCEL, MODPARAM_TYPE_BIT},
    {"enableProfileDecel", CIA402_MP_ENABLE_PROFILE_DECEL, MODPARAM_TYPE_BIT},
    {"enableProfileEndVelocity", CIA402_MP_ENABLE_PROFILE_END_VELOCITY, MODPARAM_TYPE_BIT},
    {"enableProfileMaxVelocity", CIA402_MP_ENABLE_PROFILE_MAX_VELOCITY, MODPARAM_TYPE_BIT},
    {"enableProfileVelocity", CIA402_MP_ENABLE_PROFILE_VELOCITY, MODPARAM_TYPE_BIT},
    {"enableTargetTorque", CIA402_MP_ENABLE_TARGET_TORQUE, MODPARAM_TYPE_BIT},
    {"enableTorqueDemand", CIA402_MP_ENABLE_TORQUE_DEMAND, MODPARAM_TYPE_BIT},
    {"enableTorqueProfileType", CIA402_MP_ENABLE_TORQUE_PROFILE_TYPE, MODPARAM_TYPE_BIT},
    {"enableTorqueSlope", CIA402_MP_ENABLE_TORQUE_SLOPE, MODPARAM_TYPE_BIT},
    {"enableVelocityDemand", CIA402_MP_ENABLE_VELOCITY_DEMAND, MODPARAM_TYPE_BIT},
    {"enableVelocityErrorTime", CIA402_MP_ENABLE_VELOCITY_ERROR_TIME, MODPARAM_TYPE_BIT},
    {"enableVelocityErrorWindow", CIA402_MP_ENABLE_VELOCITY_ERROR_WINDOW, MODPARAM_TYPE_BIT},
    {"enableVelocitySensorSelector", CIA402_MP_ENABLE_VELOCITY_SENSOR_SELECTOR, MODPARAM_TYPE_BIT},
    {"enableVelocityThresholdTime", CIA402_MP_ENABLE_VELOCITY_THRESHOLD_TIME, MODPARAM_TYPE_BIT},
    {"enableVelocityThresholdWindow", CIA402_MP_ENABLE_VELOCITY_THRESHOLD_WINDOW, MODPARAM_TYPE_BIT},
    {NULL},
};

/// @brief Duplicate modparams with per-channel options
///
/// This reads from `lcec_class_cia402_modparams` and produces a new
/// list of modparams that includes versions of all modparams for 8
/// distinct channels, for use with multi-axis CiA 402 devices.
///
/// Specifically, this reads from `lcec_class_cia402_modparams` and
/// returns a version where each line is duplicated 9 times.  The
/// first duplicate is just a copy of the entry from
/// `lcec_class_cia402_modparams`; the other 8 are channel-specific
/// versions, with names and ID numbers modified from the original.
/// If there's an entry for `foo` with an ID of 10, then this will
/// produce 9 outputs:
///
/// - `{"foo", 10}`
/// - `{"ch1foo", 10}`
/// - `{"ch2foo", 11}`
/// - `{"ch3foo", 12}`
/// - `{"ch4foo", 13}`
/// - `{"ch5foo", 13}`
/// - `{"ch6foo", 13}`
/// - `{"ch7foo", 13}`
///
/// Note that this makes `ch1foo` equivalant to `foo`.  In all cases,
/// `foo` should set a parameter for the first channel, not all
/// channels.
///
/// If we have device-level modParams, then we should handle them via
/// a different list.
lcec_modparam_desc_t *lcec_cia402_channelized_modparams(lcec_modparam_desc_t const *orig) {
  lcec_modparam_desc_t *mp;
  int l;

  l = lcec_modparam_desc_len(orig);

  mp = malloc(sizeof(lcec_modparam_desc_t) * (l * 9 + 1));
  if (mp == NULL) {
    return NULL;
  }

  mp[l * 9] = orig[l];  // Copy terminator.

  for (l = 0; orig[l].name != NULL; l++) {
    mp[l * 9] = orig[l];
    for (int i = 1; i < 9; i++) {
      char *name;
      mp[l * 9 + i] = orig[l];

      name = malloc(strlen(orig[l].name) + 4);
      if (name == NULL) {
        free(mp);
        return NULL;
      }
      sprintf(name, "ch%d%s", l, orig[l].name);
      mp[l * 9 + i].name = name;
      mp[l * 9 + i].id += i - 1;
    }
  }

  return mp;
}

/// @brief Merge per-device modParams and channelized generic CiA 402
/// modParams into a single list.
///
/// @param device_mps a `lcec_modparam_desc_t[]` containing all of the
/// device-specific `<modParam>`settings.
lcec_modparam_desc_t *lcec_cia402_modparams(lcec_modparam_desc_t const *device_mps) {
  const lcec_modparam_desc_t *channelized_mps = lcec_cia402_channelized_modparams(per_channel_modparams);
  if (channelized_mps == NULL) return NULL;

  return lcec_modparam_desc_concat(device_mps, channelized_mps);
}

/// @brief Handle a single modparam entry
///
/// This should be called as part of the slave's modparam handling
/// code, in a loop over `slave->modparams`.  This will set SDOs on
/// the slave device as needed.
///
/// @param slave The `lcec_slave` passed to `_init`.
/// @param p The current modparam being processed.
///
/// @return 0 if the modparam was handled, 1 if it was not handled, and <0 if an error occurred.
int lcec_cia402_handle_modparam(struct lcec_slave *slave, const lcec_slave_modparam_t *p, lcec_class_cia402_options_t *opt) {
  if (p->id < CIA402_MP_BASE) {
    return 0;
  }

  // Each of these params is available in 9 forms:
  // `foo`: set foo for channel 0.  Generally used for single-axis devices.
  // `ch0foo`: set foo for channel 0.  Identical to `foo`, above.
  // `ch1foo`: set foo for channel 1.
  // ...
  // `ch8foo`: set foo for channel 8.
  //
  // The `id` for CiA402 modparams needs to be coded so that the channel is the low-order 3 bits.

  // To keep the switch statement from getting weird, this breaks the
  // channel and ID apart so we can handle them independently.
  int channel = p->id & 7;
  int id = p->id & ~7;
  int base = 0x6000 + 0x800 * channel;

#define CASE_MP_S8(mp_name, idx, sidx) \
  case mp_name:                        \
    return lcec_write_sdo8_modparam(slave, idx, sidx, p->value.s32, p->name)
#define CASE_MP_S16(mp_name, idx, sidx) \
  case mp_name:                         \
    return lcec_write_sdo16_modparam(slave, idx, sidx, p->value.s32, p->name)
#define CASE_MP_U16(mp_name, idx, sidx) \
  case mp_name:                         \
    return lcec_write_sdo16_modparam(slave, idx, sidx, p->value.u32, p->name)
#define CASE_MP_S32(mp_name, idx, sidx) \
  case mp_name:                         \
    return lcec_write_sdo32_modparam(slave, idx, sidx, p->value.s32, p->name)
#define CASE_MP_U32(mp_name, idx, sidx) \
  case mp_name:                         \
    return lcec_write_sdo32_modparam(slave, idx, sidx, p->value.u32, p->name)
#define CASE_MP_ENABLE_BIT(mp_name, pin_name) \
  case mp_name: \
    opt->enable_##pin_name = p->value.bit; \
    return 0;
  switch (id) {
    CASE_MP_S32(CIA402_MP_POSLIMIT_MIN, base + 0x7b, 1);
    CASE_MP_S32(CIA402_MP_POSLIMIT_MAX, base + 0x7b, 2);
    CASE_MP_S32(CIA402_MP_SWPOSLIMIT_MIN, base + 0x7d, 1);
    CASE_MP_S32(CIA402_MP_SWPOSLIMIT_MAX, base + 0x7d, 2);
    CASE_MP_S32(CIA402_MP_HOME_OFFSET, base + 0x7c, 0);
    CASE_MP_U32(CIA402_MP_QUICKDECEL, base + 0x85, 0);
    CASE_MP_S16(CIA402_MP_OPTCODE_QUICKSTOP, base + 0x5a, 0);
    CASE_MP_S16(CIA402_MP_OPTCODE_SHUTDOWN, base + 0x5b, 0);
    CASE_MP_S16(CIA402_MP_OPTCODE_DISABLE, base + 0x5c, 0);
    CASE_MP_S16(CIA402_MP_OPTCODE_HALT, base + 0x5d, 0);
    CASE_MP_S16(CIA402_MP_OPTCODE_FAULT, base + 0x5e, 0);
    CASE_MP_U16(CIA402_MP_PROBE_FUNCTION, base + 0xb8, 0);
    CASE_MP_U32(CIA402_MP_PROBE1_POS, base + 0xba, 0);
    CASE_MP_U32(CIA402_MP_PROBE1_NEG, base + 0xbb, 0);
    CASE_MP_U32(CIA402_MP_PROBE2_POS, base + 0xbc, 0);
    CASE_MP_U32(CIA402_MP_PROBE2_NEG, base + 0xbd, 0);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_PP, pp);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_PV, pv);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_CSP, csp);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_CSV, csv);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_HM, hm);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_IP, ip);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_VL, vl);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_TQ, tq);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_CST, cst);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_ACTUAL_CURRENT, actual_current );
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_ACTUAL_FOLLOWING_ERROR, actual_following_error);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_ACTUAL_TORQUE, actual_torque);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_ACTUAL_VELOCITY_SENSOR, actual_velocity_sensor);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_ACTUAL_VOLTAGE, actual_voltage);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_FOLLOWING_ERROR_TIMEOUT, following_error_timeout);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_FOLLOWING_ERROR_WINDOW, following_error_window);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_HOME_ACCEL, home_accel);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_INTERPOLATION_TIME_PERIOD, interpolation_time_period);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_MAXIMUM_ACCELERATION, maximum_acceleration);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_MAXIMUM_CURRENT, maximum_current);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_MAXIMUM_DECELERATION, maximum_deceleration);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_MAXIMUM_MOTOR_RPM, maximum_motor_rpm);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_MAXIMUM_TORQUE, maximum_torque);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_MOTION_PROFILE, motion_profile);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_MOTOR_RATED_CURRENT, motor_rated_current);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_MOTOR_RATED_TORQUE, motor_rated_torque);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_POLARITY, polarity);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_PROFILE_ACCEL, profile_accel);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_PROFILE_DECEL, profile_decel);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_PROFILE_END_VELOCITY, profile_end_velocity);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_PROFILE_MAX_VELOCITY, profile_max_velocity);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_PROFILE_VELOCITY, profile_velocity);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_TARGET_TORQUE, target_torque);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_TORQUE_DEMAND, torque_demand);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_TORQUE_PROFILE_TYPE, torque_profile_type );
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_TORQUE_SLOPE,  torque_slope);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_VELOCITY_DEMAND, velocity_demand);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_VELOCITY_ERROR_TIME, velocity_error_time);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_VELOCITY_ERROR_WINDOW, velocity_error_window);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_VELOCITY_SENSOR_SELECTOR, velocity_sensor_selector);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_VELOCITY_THRESHOLD_TIME, velocity_threshold_time);
    CASE_MP_ENABLE_BIT(CIA402_MP_ENABLE_VELOCITY_THRESHOLD_WINDOW, velocity_threshold_window);
    
    default:
      return 1;
  }
}
