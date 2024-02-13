
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

/// @brief Pins behind `enable_opmode`:
static const lcec_pindesc_t pins_opmode[] = {
    // HAL_OUT is readable, HAL_IN is writable.
    {HAL_S32, HAL_IN, offsetof(lcec_class_cia402_channel_t, opmode), "%s.%s.%s.%s-opmode"},
    {HAL_S32, HAL_OUT, offsetof(lcec_class_cia402_channel_t, opmode_display), "%s.%s.%s.%s-opmode-display"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

/// @brief Pins behind `enable_hm` (required pins for devices that support homing mode):
static const lcec_pindesc_t pins_hm[] = {
    {HAL_S32, HAL_OUT, offsetof(lcec_class_cia402_channel_t, home_method), "%s.%s.%s.%s-home-method"},
    {HAL_U32, HAL_OUT, offsetof(lcec_class_cia402_channel_t, home_velocity_fast), "%s.%s.%s.%s-home-velocity-fast"},
    {HAL_U32, HAL_OUT, offsetof(lcec_class_cia402_channel_t, home_velocity_slow), "%s.%s.%s.%s-home-velocity-slow"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

#define OPTIONAL_PIN_READ(var_name, pin_name, type)                                                    \
  static const lcec_pindesc_t pins_##var_name[] = {                                                    \
      {HAL_##type, HAL_OUT, offsetof(lcec_class_cia402_channel_t, var_name), "%s.%s.%s.%s-" pin_name}, \
      {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},                                           \
  }

#define OPTIONAL_PIN_WRITE(var_name, pin_name, type)                                                  \
  static const lcec_pindesc_t pins_##var_name[] = {                                                   \
      {HAL_##type, HAL_IN, offsetof(lcec_class_cia402_channel_t, var_name), "%s.%s.%s.%s-" pin_name}, \
      {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},                                          \
  }

OPTIONAL_PIN_READ(actual_following_error, "actual-following-error", U32);
OPTIONAL_PIN_READ(actual_position, "actual-position", S32);
OPTIONAL_PIN_READ(actual_torque, "actual-torque", S32);
OPTIONAL_PIN_READ(actual_velocity, "actual-velocity", S32);
OPTIONAL_PIN_READ(actual_velocity_sensor, "actual-velocity-sensor", S32);

OPTIONAL_PIN_WRITE(following_error_timeout, "following-error-timeout", U32);
OPTIONAL_PIN_WRITE(following_error_window, "following-error-window", U32);
OPTIONAL_PIN_WRITE(home_accel, "home-accel", U32);
OPTIONAL_PIN_WRITE(interpolation_time_period, "interpolation-time-period", U32);
OPTIONAL_PIN_WRITE(motion_profile, "motion-profile", S32);
OPTIONAL_PIN_WRITE(motor_rated_torque, "motor-rated-torque", U32);
OPTIONAL_PIN_WRITE(profile_accel, "profile-accel", U32);
OPTIONAL_PIN_WRITE(profile_decel, "profile-decel", U32);
OPTIONAL_PIN_WRITE(profile_end_velocity, "profile-end-velocity", U32);
OPTIONAL_PIN_WRITE(profile_max_velocity, "profile-max-velocity", U32);
OPTIONAL_PIN_WRITE(profile_velocity, "profile-velocity", U32);
OPTIONAL_PIN_WRITE(target_position, "target-position", S32);
OPTIONAL_PIN_WRITE(target_velocity, "target-velocity", S32);
OPTIONAL_PIN_WRITE(torque_maximum, "torque-maximum", U32);

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
  ENABLE_OPT(actual_following_error);
  ENABLE_OPT(actual_torque);
  ENABLE_OPT(actual_velocity_sensor);
  ENABLE_OPT(digital_input);
  ENABLE_OPT(digital_output);
  ENABLE_OPT(following_error_timeout);
  ENABLE_OPT(following_error_window);
  ENABLE_OPT(home_accel);
  ENABLE_OPT(interpolation_time_period);
  ENABLE_OPT(motion_profile);
  ENABLE_OPT(motor_rated_torque);
  ENABLE_OPT(profile_accel);
  ENABLE_OPT(profile_decel);
  ENABLE_OPT(profile_end_velocity);
  ENABLE_OPT(profile_max_velocity);
  ENABLE_OPT(profile_velocity);
  ENABLE_OPT(torque_maximum);

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
#define MAP_OPTIONAL_PDO(name, index, subindex, bits)       \
  if (enabled->enable_##name) {                             \
    lcec_syncs_add_pdo_entry(syncs, index, subindex, bits); \
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
  MAP_OPTIONAL_PDO(opmode, 0x6060, 0x00, 8);          // Operating mode
  MAP_OPTIONAL_PDO(target_position, 0x607a, 0x00, 32);
  MAP_OPTIONAL_PDO(target_velocity, 0x60ff, 0x00, 32);
  MAP_OPTIONAL_PDO(digital_output, 0x60fe, 0x01, 32);
  MAP_OPTIONAL_PDO(profile_max_velocity, 0x607f, 0x0, 32);
  MAP_OPTIONAL_PDO(profile_velocity, 0x6081, 0x0, 32);
  MAP_OPTIONAL_PDO(profile_end_velocity, 0x6082, 0x0, 32);
  MAP_OPTIONAL_PDO(profile_accel, 0x6083, 0x0, 32);
  MAP_OPTIONAL_PDO(profile_decel, 0x6084, 0x0, 32);
  MAP_OPTIONAL_PDO(home_method, 0x6098, 0x0, 32);
  MAP_OPTIONAL_PDO(home_velocity_fast, 0x6099, 0x1, 32);
  MAP_OPTIONAL_PDO(home_velocity_slow, 0x6099, 0x2, 32);
  MAP_OPTIONAL_PDO(home_accel, 0x609a, 0x0, 32);
  MAP_OPTIONAL_PDO(following_error_timeout, 0x6066, 0, 16);
  MAP_OPTIONAL_PDO(following_error_window, 0x6065, 0, 32);
  MAP_OPTIONAL_PDO(interpolation_time_period, 0x60c2, 1, 8);
  MAP_OPTIONAL_PDO(motion_profile, 0x6086, 0, 16);
  MAP_OPTIONAL_PDO(motor_rated_torque, 0x6076, 0, 32);
  MAP_OPTIONAL_PDO(torque_maximum, 0x6072, 0, 16);

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
  MAP_OPTIONAL_PDO(opmode_display, 0x6061, 0x00, 8);
  MAP_OPTIONAL_PDO(actual_position, 0x6064, 0x00, 32);
  MAP_OPTIONAL_PDO(actual_velocity, 0x606c, 0x00, 32);
  MAP_OPTIONAL_PDO(actual_torque, 0x6077, 0x00, 32);
  MAP_OPTIONAL_PDO(digital_input, 0x60fd, 0x00, 32);
  MAP_OPTIONAL_PDO(actual_following_error, 0x60f4, 0, 32);
  MAP_OPTIONAL_PDO(actual_velocity_sensor, 0x6069, 0, 32);

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

#define INIT_OPTIONAL_PDO(pin_name, idx, sdx) \
  if (enabled->enable_##pin_name) lcec_pdo_init(slave, idx, sdx, &data->pin_name##_os, NULL)

  INIT_OPTIONAL_PDO(actual_following_error, base_idx + 0xf4, 0);
  INIT_OPTIONAL_PDO(actual_position, base_idx + 0x64, 0);
  INIT_OPTIONAL_PDO(actual_torque, base_idx + 0x77, 0);
  INIT_OPTIONAL_PDO(actual_velocity, base_idx + 0x6c, 0);
  INIT_OPTIONAL_PDO(actual_velocity_sensor, base_idx + 0x69, 0);
  INIT_OPTIONAL_PDO(following_error_timeout, base_idx + 0x66, 0);
  INIT_OPTIONAL_PDO(following_error_window, base_idx + 0x65, 0);
  INIT_OPTIONAL_PDO(home_accel, base_idx + 0x9a, 0);
  INIT_OPTIONAL_PDO(home_method, base_idx + 0x98, 0);
  INIT_OPTIONAL_PDO(home_velocity_fast, base_idx + 0x99, 1);
  INIT_OPTIONAL_PDO(home_velocity_slow, base_idx + 0x99, 2);
  INIT_OPTIONAL_PDO(interpolation_time_period, base_idx + 0xc2, 1);
  INIT_OPTIONAL_PDO(motion_profile, base_idx + 0x86, 0);
  INIT_OPTIONAL_PDO(motor_rated_torque, base_idx + 0x76, 0);
  INIT_OPTIONAL_PDO(opmode, base_idx + 0x60, 0);
  INIT_OPTIONAL_PDO(opmode_display, base_idx + 0x61, 0);
  INIT_OPTIONAL_PDO(profile_accel, base_idx + 0x83, 0);
  INIT_OPTIONAL_PDO(profile_decel, base_idx + 0x84, 0);
  INIT_OPTIONAL_PDO(profile_end_velocity, base_idx + 0x82, 0);
  INIT_OPTIONAL_PDO(profile_max_velocity, base_idx + 0x7f, 0);
  INIT_OPTIONAL_PDO(profile_velocity, base_idx + 0x81, 0);
  INIT_OPTIONAL_PDO(target_position, base_idx + 0x7a, 0);
  INIT_OPTIONAL_PDO(target_velocity, base_idx + 0xff, 0);
  INIT_OPTIONAL_PDO(torque_maximum, base_idx + 0x72, 0);

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

  REGISTER_OPTIONAL_PINS(opmode);
  REGISTER_OPTIONAL_PINS(hm);

  REGISTER_OPTIONAL_PINS(actual_following_error);
  REGISTER_OPTIONAL_PINS(actual_position);
  REGISTER_OPTIONAL_PINS(actual_torque);
  REGISTER_OPTIONAL_PINS(actual_velocity);
  REGISTER_OPTIONAL_PINS(actual_velocity_sensor);
  REGISTER_OPTIONAL_PINS(following_error_timeout);
  REGISTER_OPTIONAL_PINS(following_error_window);
  REGISTER_OPTIONAL_PINS(home_accel);
  REGISTER_OPTIONAL_PINS(interpolation_time_period);
  REGISTER_OPTIONAL_PINS(motion_profile);
  REGISTER_OPTIONAL_PINS(motor_rated_torque);
  REGISTER_OPTIONAL_PINS(profile_accel);
  REGISTER_OPTIONAL_PINS(profile_decel);
  REGISTER_OPTIONAL_PINS(profile_end_velocity);
  REGISTER_OPTIONAL_PINS(profile_max_velocity);
  REGISTER_OPTIONAL_PINS(profile_velocity);
  REGISTER_OPTIONAL_PINS(target_position);
  REGISTER_OPTIONAL_PINS(target_velocity);
  REGISTER_OPTIONAL_PINS(torque_maximum);

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

#define SET_OPTIONAL_DEFAULTS8(pin_name, idx, sidx) \
  if (enabled->enable_##pin_name) lcec_read_sdo8_pin(slave, idx, sidx, data->pin_name)
#define SET_OPTIONAL_DEFAULTS8S(pin_name, idx, sidx) \
  if (enabled->enable_##pin_name) lcec_read_sdo8_pin_signed(slave, idx, sidx, data->pin_name)
#define SET_OPTIONAL_DEFAULTS16(pin_name, idx, sidx) \
  if (enabled->enable_##pin_name) lcec_read_sdo16_pin(slave, idx, sidx, data->pin_name)
#define SET_OPTIONAL_DEFAULTS16S(pin_name, idx, sidx) \
  if (enabled->enable_##pin_name) lcec_read_sdo16_pin_signed(slave, idx, sidx, data->pin_name)
#define SET_OPTIONAL_DEFAULTS32(pin_name, idx, sidx) \
  if (enabled->enable_##pin_name) lcec_read_sdo32_pin(slave, idx, sidx, data->pin_name)

  SET_OPTIONAL_DEFAULTS16(following_error_timeout, base_idx + 0x66, 0);
  SET_OPTIONAL_DEFAULTS16S(motion_profile, base_idx + 0x86, 0);
  SET_OPTIONAL_DEFAULTS16(torque_maximum, base_idx + 0x72, 0);
  SET_OPTIONAL_DEFAULTS32(following_error_window, base_idx + 0x65, 0);
  SET_OPTIONAL_DEFAULTS32(home_accel, base_idx + 0x9a, 0);
  SET_OPTIONAL_DEFAULTS32(home_velocity_fast, base_idx + 0x99, 1);
  SET_OPTIONAL_DEFAULTS32(home_velocity_slow, base_idx + 0x99, 2);
  SET_OPTIONAL_DEFAULTS32(motor_rated_torque, base_idx + 0x76, 0);
  SET_OPTIONAL_DEFAULTS32(profile_accel, base_idx + 0x83, 0);
  SET_OPTIONAL_DEFAULTS32(profile_decel, base_idx + 0x84, 0);
  SET_OPTIONAL_DEFAULTS32(profile_end_velocity, base_idx + 0x82, 0);
  SET_OPTIONAL_DEFAULTS32(profile_max_velocity, base_idx + 0x7f, 0);
  SET_OPTIONAL_DEFAULTS32(profile_velocity, base_idx + 0x81, 0);
  SET_OPTIONAL_DEFAULTS8(interpolation_time_period, base_idx + 0xc2, 1);
  SET_OPTIONAL_DEFAULTS8S(home_method, base_idx + 0x98, 0);

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

#define READ_OPT(pin_name, size) \
  if (data->enabled->enable_##pin_name) *(data->pin_name) = EC_READ_##size(&pd[data->pin_name##_os])

  *(data->statusword) = EC_READ_U16(&pd[data->statusword_os]);
  READ_OPT(actual_following_error, U32);
  READ_OPT(actual_position, S32);
  READ_OPT(actual_torque, S32);
  READ_OPT(actual_velocity, S32);
  READ_OPT(actual_velocity_sensor, S32);
  READ_OPT(opmode_display, S8);
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

#define WRITE_OPT(name, size) \
  if (data->enabled->enable_##name) EC_WRITE_##size(&pd[data->name##_os], *(data->name))

  EC_WRITE_U16(&pd[data->controlword_os], (uint16_t)(*(data->controlword)));
  WRITE_OPT(following_error_timeout, U16);
  WRITE_OPT(following_error_window, U32);
  WRITE_OPT(home_accel, U32);
  WRITE_OPT(home_method, S32);
  WRITE_OPT(home_velocity_fast, U32);
  WRITE_OPT(home_velocity_slow, U32);
  WRITE_OPT(interpolation_time_period, U8);
  WRITE_OPT(motion_profile, S16);
  WRITE_OPT(motor_rated_torque, U32);
  WRITE_OPT(opmode, S8);
  WRITE_OPT(profile_accel, U32);
  WRITE_OPT(profile_decel, U32);
  WRITE_OPT(profile_end_velocity, U32);
  WRITE_OPT(profile_max_velocity, U32);
  WRITE_OPT(profile_velocity, U32);
  WRITE_OPT(target_position, S32);
  WRITE_OPT(target_velocity, S32);
  WRITE_OPT(torque_maximum, U16);
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
int lcec_cia402_handle_modparam(struct lcec_slave *slave, const lcec_slave_modparam_t *p) {
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
    default:
      return 1;
  }
}
