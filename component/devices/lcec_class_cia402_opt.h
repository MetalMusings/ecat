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

// The CiA 402 spec includes a *lot* of optional features; see
// `documentation/cia402.md` for a partial list.  Practically
// speaking, this means that nearly every single thing that we want to
// access is optional, and attempting to access them on hardware that
// doesn't support them will lead to, at best, a poor user experience.
// So, `class_cia402` goes to great lengths to make every single
// access to a non-required object optional.  Outside of this file,
// options are controlled via `lcec_class_cia402_channel_options_t`.  The
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

#define PDO_IDX_OFFSET_actual_current            0x78
#define PDO_IDX_OFFSET_actual_following_error    0xf4
#define PDO_IDX_OFFSET_actual_position           0x64
#define PDO_IDX_OFFSET_actual_torque             0x77
#define PDO_IDX_OFFSET_actual_velocity           0x6c
#define PDO_IDX_OFFSET_actual_velocity_sensor    0x69
#define PDO_IDX_OFFSET_actual_vl                 0x44
#define PDO_IDX_OFFSET_actual_voltage            0x79
#define PDO_IDX_OFFSET_demand_vl                 0x43
#define PDO_IDX_OFFSET_digital_input             0xfd
#define PDO_IDX_OFFSET_digital_output            0xfe
#define PDO_IDX_OFFSET_error_code                0x3f
#define PDO_IDX_OFFSET_following_error_timeout   0x66
#define PDO_IDX_OFFSET_following_error_window    0x65
#define PDO_IDX_OFFSET_home_accel                0x9a
#define PDO_IDX_OFFSET_home_method               0x98
#define PDO_IDX_OFFSET_home_velocity_fast        0x99
#define PDO_IDX_OFFSET_home_velocity_slow        0x99
#define PDO_IDX_OFFSET_interpolation_time_period 0xc2
#define PDO_IDX_OFFSET_maximum_acceleration      0xc5
#define PDO_IDX_OFFSET_maximum_current           0x73
#define PDO_IDX_OFFSET_maximum_deceleration      0xc6
#define PDO_IDX_OFFSET_maximum_motor_rpm         0x80
#define PDO_IDX_OFFSET_maximum_torque            0x72
#define PDO_IDX_OFFSET_motion_profile            0x86
#define PDO_IDX_OFFSET_motor_rated_current       0x75
#define PDO_IDX_OFFSET_motor_rated_torque        0x76
#define PDO_IDX_OFFSET_opmode                    0x60
#define PDO_IDX_OFFSET_opmode_display            0x61
#define PDO_IDX_OFFSET_polarity                  0x7e
#define PDO_IDX_OFFSET_profile_accel             0x83
#define PDO_IDX_OFFSET_profile_decel             0x84
#define PDO_IDX_OFFSET_profile_end_velocity      0x82
#define PDO_IDX_OFFSET_profile_max_velocity      0x7f
#define PDO_IDX_OFFSET_profile_velocity          0x81
#define PDO_IDX_OFFSET_target_position           0x7a
#define PDO_IDX_OFFSET_target_torque             0x71
#define PDO_IDX_OFFSET_target_velocity           0xff
#define PDO_IDX_OFFSET_target_vl                 0x42
#define PDO_IDX_OFFSET_torque_demand             0x74
#define PDO_IDX_OFFSET_torque_profile_type       0x88
#define PDO_IDX_OFFSET_torque_slope              0x87
#define PDO_IDX_OFFSET_velocity_demand           0x6b
#define PDO_IDX_OFFSET_velocity_error_time       0x6e
#define PDO_IDX_OFFSET_velocity_error_window     0x6d
#define PDO_IDX_OFFSET_velocity_sensor_selector  0x6a
#define PDO_IDX_OFFSET_velocity_threshold_time   0x70
#define PDO_IDX_OFFSET_velocity_threshold_window 0x6f
#define PDO_IDX_OFFSET_vl_accel                  0x48
#define PDO_IDX_OFFSET_vl_decel                  0x49
#define PDO_IDX_OFFSET_vl_maximum                0x46
#define PDO_IDX_OFFSET_vl_minimum                0x46

// Next, we'll do the same thing for sub-index addresses.  With CiA
// 402, these are mostly (but not entirely) 0.
#define PDO_SIDX_actual_current            0
#define PDO_SIDX_actual_following_error    0
#define PDO_SIDX_actual_position           0
#define PDO_SIDX_actual_torque             0
#define PDO_SIDX_actual_velocity           0
#define PDO_SIDX_actual_velocity_sensor    0
#define PDO_SIDX_actual_vl                 0
#define PDO_SIDX_actual_voltage            0
#define PDO_SIDX_demand_vl                 0
#define PDO_SIDX_digital_input             0
#define PDO_SIDX_digital_output            1
#define PDO_SIDX_error_code                0
#define PDO_SIDX_following_error_timeout   0
#define PDO_SIDX_following_error_window    0
#define PDO_SIDX_home_accel                0
#define PDO_SIDX_home_method               0
#define PDO_SIDX_home_velocity_fast        1
#define PDO_SIDX_home_velocity_slow        2
#define PDO_SIDX_interpolation_time_period 1
#define PDO_SIDX_maximum_acceleration      0
#define PDO_SIDX_maximum_current           0
#define PDO_SIDX_maximum_deceleration      0
#define PDO_SIDX_maximum_motor_rpm         0
#define PDO_SIDX_maximum_torque            0
#define PDO_SIDX_motion_profile            0
#define PDO_SIDX_motor_rated_current       0
#define PDO_SIDX_motor_rated_torque        0
#define PDO_SIDX_opmode                    0
#define PDO_SIDX_opmode_display            0
#define PDO_SIDX_polarity                  0
#define PDO_SIDX_profile_accel             0
#define PDO_SIDX_profile_decel             0
#define PDO_SIDX_profile_end_velocity      0
#define PDO_SIDX_profile_max_velocity      0
#define PDO_SIDX_profile_velocity          0
#define PDO_SIDX_target_position           0
#define PDO_SIDX_target_torque             0
#define PDO_SIDX_target_velocity           0
#define PDO_SIDX_target_vl                 0
#define PDO_SIDX_torque_demand             0
#define PDO_SIDX_torque_profile_type       0
#define PDO_SIDX_torque_slope              0
#define PDO_SIDX_velocity_demand           0
#define PDO_SIDX_velocity_error_time       0
#define PDO_SIDX_velocity_error_window     0
#define PDO_SIDX_velocity_sensor_selector  0
#define PDO_SIDX_velocity_threshold_time   0
#define PDO_SIDX_velocity_threshold_window 0
#define PDO_SIDX_vl_accel                  0
#define PDO_SIDX_vl_decel                  0
#define PDO_SIDX_vl_maximum                2
#define PDO_SIDX_vl_minimum                1

// Next, the type of pin that each uses.  *Generally* either HAL_U32
// or HAL_S32.  We could probably generate this by concatenating
// PDO_SIGN_foo (below) and `32`, but it'd probably bite us
// eventually.
#define PDO_PIN_TYPE_actual_current            HAL_S32
#define PDO_PIN_TYPE_actual_following_error    HAL_U32
#define PDO_PIN_TYPE_actual_position           HAL_S32
#define PDO_PIN_TYPE_actual_torque             HAL_S32
#define PDO_PIN_TYPE_actual_velocity           HAL_S32
#define PDO_PIN_TYPE_actual_velocity_sensor    HAL_S32
#define PDO_PIN_TYPE_actual_vl                 HAL_S32
#define PDO_PIN_TYPE_actual_voltage            HAL_U32
#define PDO_PIN_TYPE_demand_vl                 HAL_S32
#define PDO_PIN_TYPE_error_code                HAL_U32
#define PDO_PIN_TYPE_following_error_timeout   HAL_U32
#define PDO_PIN_TYPE_following_error_window    HAL_U32
#define PDO_PIN_TYPE_home_accel                HAL_U32
#define PDO_PIN_TYPE_home_method               HAL_S32
#define PDO_PIN_TYPE_home_velocity_fast        HAL_U32
#define PDO_PIN_TYPE_home_velocity_slow        HAL_U32
#define PDO_PIN_TYPE_interpolation_time_period HAL_U32
#define PDO_PIN_TYPE_maximum_acceleration      HAL_U32
#define PDO_PIN_TYPE_maximum_current           HAL_U32
#define PDO_PIN_TYPE_maximum_deceleration      HAL_U32
#define PDO_PIN_TYPE_maximum_motor_rpm         HAL_U32
#define PDO_PIN_TYPE_maximum_torque            HAL_U32
#define PDO_PIN_TYPE_motion_profile            HAL_S32
#define PDO_PIN_TYPE_motor_rated_current       HAL_U32
#define PDO_PIN_TYPE_motor_rated_torque        HAL_U32
#define PDO_PIN_TYPE_opmode                    HAL_S32
#define PDO_PIN_TYPE_opmode_display            HAL_S32
#define PDO_PIN_TYPE_polarity                  HAL_U32
#define PDO_PIN_TYPE_profile_accel             HAL_U32
#define PDO_PIN_TYPE_profile_decel             HAL_U32
#define PDO_PIN_TYPE_profile_end_velocity      HAL_U32
#define PDO_PIN_TYPE_profile_max_velocity      HAL_U32
#define PDO_PIN_TYPE_profile_velocity          HAL_U32
#define PDO_PIN_TYPE_target_position           HAL_S32
#define PDO_PIN_TYPE_target_torque             HAL_S32
#define PDO_PIN_TYPE_target_velocity           HAL_S32
#define PDO_PIN_TYPE_target_vl                 HAL_S32
#define PDO_PIN_TYPE_torque_demand             HAL_S32
#define PDO_PIN_TYPE_torque_profile_type       HAL_S32
#define PDO_PIN_TYPE_torque_slope              HAL_U32
#define PDO_PIN_TYPE_velocity_demand           HAL_S32
#define PDO_PIN_TYPE_velocity_error_time       HAL_U32
#define PDO_PIN_TYPE_velocity_error_window     HAL_U32
#define PDO_PIN_TYPE_velocity_sensor_selector  HAL_S32
#define PDO_PIN_TYPE_velocity_threshold_time   HAL_U32
#define PDO_PIN_TYPE_velocity_threshold_window HAL_U32
#define PDO_PIN_TYPE_vl_accel                  HAL_U32
#define PDO_PIN_TYPE_vl_decel                  HAL_U32
#define PDO_PIN_TYPE_vl_maximum                HAL_S32
#define PDO_PIN_TYPE_vl_minimum                HAL_S32

// Next, the length of the object for each.   Usually 8, 16, or 32.
#define PDO_BITS_actual_current            16
#define PDO_BITS_actual_following_error    32
#define PDO_BITS_actual_position           32
#define PDO_BITS_actual_torque             32
#define PDO_BITS_actual_velocity           32
#define PDO_BITS_actual_velocity_sensor    32
#define PDO_BITS_actual_vl                 16
#define PDO_BITS_actual_voltage            32
#define PDO_BITS_demand_vl                 16
#define PDO_BITS_digital_input             32
#define PDO_BITS_digital_output            32
#define PDO_BITS_error_code                16
#define PDO_BITS_following_error_timeout   16
#define PDO_BITS_following_error_window    32
#define PDO_BITS_home_accel                32
#define PDO_BITS_home_method               8
#define PDO_BITS_home_velocity_fast        32
#define PDO_BITS_home_velocity_slow        32
#define PDO_BITS_interpolation_time_period 8
#define PDO_BITS_maximum_acceleration      32
#define PDO_BITS_maximum_current           16
#define PDO_BITS_maximum_deceleration      32
#define PDO_BITS_maximum_motor_rpm         32
#define PDO_BITS_maximum_torque            16
#define PDO_BITS_motion_profile            16
#define PDO_BITS_motor_rated_current       32
#define PDO_BITS_motor_rated_torque        32
#define PDO_BITS_opmode                    8
#define PDO_BITS_opmode_display            8
#define PDO_BITS_polarity                  8
#define PDO_BITS_profile_accel             32
#define PDO_BITS_profile_decel             32
#define PDO_BITS_profile_end_velocity      32
#define PDO_BITS_profile_max_velocity      32
#define PDO_BITS_profile_velocity          32
#define PDO_BITS_target_position           32
#define PDO_BITS_target_torque             16
#define PDO_BITS_target_velocity           32
#define PDO_BITS_target_vl                 16
#define PDO_BITS_torque_demand             16
#define PDO_BITS_torque_profile_type       16
#define PDO_BITS_torque_slope              32
#define PDO_BITS_velocity_demand           32
#define PDO_BITS_velocity_error_time       16
#define PDO_BITS_velocity_error_window     16
#define PDO_BITS_velocity_sensor_selector  16
#define PDO_BITS_velocity_threshold_time   16
#define PDO_BITS_velocity_threshold_window 16
#define PDO_BITS_vl_accel                  32
#define PDO_BITS_vl_decel                  32
#define PDO_BITS_vl_maximum                16
#define PDO_BITS_vl_minimum                16

// Finally (?), the signeded-ness of each object.  This should match
// PDO_PIN_TYPE_foo, but we don't *necessarily* have PDO_PIN_TYPE_foo
// for all objects.
#define PDO_SIGN_actual_current            S
#define PDO_SIGN_actual_following_error    U
#define PDO_SIGN_actual_position           S
#define PDO_SIGN_actual_torque             S
#define PDO_SIGN_actual_velocity           S
#define PDO_SIGN_actual_velocity_sensor    S
#define PDO_SIGN_actual_vl                 S
#define PDO_SIGN_actual_voltage            U
#define PDO_SIGN_demand_vl                 S
#define PDO_SIGN_error_code                U
#define PDO_SIGN_following_error_timeout   U
#define PDO_SIGN_following_error_window    U
#define PDO_SIGN_home_accel                U
#define PDO_SIGN_home_method               S
#define PDO_SIGN_home_velocity_fast        U
#define PDO_SIGN_home_velocity_slow        U
#define PDO_SIGN_interpolation_time_period U
#define PDO_SIGN_maximum_acceleration      U
#define PDO_SIGN_maximum_current           U
#define PDO_SIGN_maximum_deceleration      U
#define PDO_SIGN_maximum_motor_rpm         U
#define PDO_SIGN_maximum_torque            U
#define PDO_SIGN_motion_profile            S
#define PDO_SIGN_motor_rated_current       U
#define PDO_SIGN_motor_rated_torque        U
#define PDO_SIGN_opmode                    S
#define PDO_SIGN_opmode_display            S
#define PDO_SIGN_polarity                  U
#define PDO_SIGN_profile_accel             U
#define PDO_SIGN_profile_decel             U
#define PDO_SIGN_profile_end_velocity      U
#define PDO_SIGN_profile_max_velocity      U
#define PDO_SIGN_profile_velocity          U
#define PDO_SIGN_target_position           S
#define PDO_SIGN_target_torque             S
#define PDO_SIGN_target_velocity           S
#define PDO_SIGN_target_vl                 S
#define PDO_SIGN_torque_demand             S
#define PDO_SIGN_torque_profile_type       S
#define PDO_SIGN_torque_slope              U
#define PDO_SIGN_velocity_demand           S
#define PDO_SIGN_velocity_error_time       U
#define PDO_SIGN_velocity_error_window     U
#define PDO_SIGN_velocity_sensor_selector  S
#define PDO_SIGN_velocity_threshold_time   U
#define PDO_SIGN_velocity_threshold_window U
#define PDO_SIGN_vl_accel                  U
#define PDO_SIGN_vl_decel                  U
#define PDO_SIGN_vl_maximum                S
#define PDO_SIGN_vl_minimum                S

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
#define JOIN2(a, b)               a##b
#define JOIN3(a, b, c)            a##b##c
#define JOIN5(a, b, c, d, e)      a##b##c##d##e
#define SUBSTJOIN2(a, b)          JOIN2(a, b)
#define SUBSTJOIN3(a, b, c)       JOIN3(a, b, c)
#define SUBSTJOIN5(a, b, c, d, e) JOIN5(a, b, c, d, e)
