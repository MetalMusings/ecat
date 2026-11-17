//
//    ecat: HAL for LinuxCNC 2.9 and 2.10.
//
//    ecat is written against LinuxCNC 2.10's HAL API: pins and params are
//    references (hal_bool_t, hal_sint_t, hal_uint_t, hal_real_t) read and
//    written with hal_get_*() and hal_set_*(), made with hal_pin_new_*() and
//    hal_param_new_*(). This header is the only place that knows about 2.9:
//    built against 2.9's hal.h it gives that API on 2.9's pointers.
//
//    On 2.9 (ECAT_HAL_64 is 0):
//    - integer pins and params have 32 bits; hal_set_sint() and
//      hal_set_uint() keep the low 32 bits (a counter wraps as in lcec,
//      like 2.10's hal_set_si32()); arithmetic is still done in 64 bits;
//    - a param is a slot taken with hal_malloc() and registered with
//      2.9's hal_param_*_newf(); its reference points to that slot;
//    - rtapi_task_new() and hal_export_funct() have 2.9's uses_fp argument
//      (always set).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#ifndef _ECAT_HAL_H_
#define _ECAT_HAL_H_

#include "hal.h"

#if defined(HAL_API_VERSION) && HAL_API_VERSION >= 1

/// 1: LinuxCNC 2.10 or later, integer pins have 64 bits.
#define ECAT_HAL_64 1

#else  // LinuxCNC 2.9

#define ECAT_HAL_64 0

#include <stdarg.h>
#include <stdio.h>

// The value types of 2.10's API. Integers are worked with in 64 bits; only
// the pins are narrower.
typedef bool rtapi_bool;
typedef rtapi_s64 rtapi_sint;
typedef rtapi_u64 rtapi_uint;
typedef double rtapi_real;

// References: a pointer to where the value is (2.9's pin pointer, or a
// param's slot).
typedef hal_bit_t *hal_bool_t;
typedef hal_s32_t *hal_sint_t;
typedef hal_u32_t *hal_uint_t;
typedef hal_float_t *hal_real_t;

// One direction type for pins and params, as in 2.10.
typedef int hal_pdir_t;

#define HAL_BOOL HAL_BIT
#define HAL_REAL HAL_FLOAT
#define HAL_SINT HAL_S32
#define HAL_UINT HAL_U32

static inline rtapi_bool hal_get_bool(const hal_bool_t ref) { return *ref; }
static inline rtapi_bool hal_set_bool(hal_bool_t ref, rtapi_bool val) { *ref = val; return val; }
static inline rtapi_sint hal_get_sint(const hal_sint_t ref) { return *ref; }
static inline rtapi_sint hal_set_sint(hal_sint_t ref, rtapi_sint val) { *ref = (rtapi_s32)(rtapi_u32)val; return val; }
static inline rtapi_uint hal_get_uint(const hal_uint_t ref) { return *ref; }
static inline rtapi_uint hal_set_uint(hal_uint_t ref, rtapi_uint val) { *ref = (rtapi_u32)val; return val; }
static inline rtapi_real hal_get_real(const hal_real_t ref) { return *ref; }
static inline rtapi_real hal_set_real(hal_real_t ref, rtapi_real val) { *ref = val; return val; }

// The name is formatted here, so that 2.9's functions get it as "%s".
#define ECAT_HAL_NAME(name, fmt)                                  \
  char name[HAL_NAME_LEN + 1];                                    \
  do {                                                            \
    va_list ap_;                                                  \
    int n_;                                                       \
    va_start(ap_, fmt);                                           \
    n_ = vsnprintf(name, sizeof(name), fmt, ap_);                 \
    va_end(ap_);                                                  \
    if (n_ < 0 || n_ > HAL_NAME_LEN) return -EINVAL;              \
  } while (0)

#define ECAT_HAL_PIN_NEW(type, t29, ctype)                                                                  \
  static inline __attribute__((unused, format(printf, 5, 6))) int hal_pin_new_##type(                     \
      int compid, hal_pdir_t dir, hal_##type##_t *ref, ctype def, const char *fmt, ...) {                  \
    int err;                                                                                               \
    ECAT_HAL_NAME(name, fmt);                                                                              \
    err = hal_pin_##t29##_newf((hal_pin_dir_t)dir, (hal_##t29##_t **)ref, compid, "%s", name);             \
    if (err == 0) hal_set_##type(*ref, def);                                                               \
    return err;                                                                                            \
  }                                                                                                        \
  static inline __attribute__((unused, format(printf, 5, 6))) int hal_param_new_##type(                   \
      int compid, hal_pdir_t dir, hal_##type##_t *ref, ctype def, const char *fmt, ...) {                  \
    hal_##t29##_t *slot;                                                                                   \
    int err;                                                                                               \
    ECAT_HAL_NAME(name, fmt);                                                                              \
    slot = hal_malloc(sizeof(*slot));                                                                      \
    if (slot == NULL) return -ENOMEM;                                                                      \
    *ref = slot;                                                                                           \
    hal_set_##type(slot, def);                                                                             \
    err = hal_param_##t29##_newf((hal_param_dir_t)dir, slot, compid, "%s", name);                          \
    return err;                                                                                            \
  }

ECAT_HAL_PIN_NEW(bool, bit, rtapi_bool)
ECAT_HAL_PIN_NEW(sint, s32, rtapi_sint)
ECAT_HAL_PIN_NEW(uint, u32, rtapi_uint)
ECAT_HAL_PIN_NEW(real, float, rtapi_real)

#undef ECAT_HAL_PIN_NEW
#undef ECAT_HAL_NAME

/// 2.10's hal_extend_counter(): a counter of nbits bits (old: the value
/// so far, newlow: the counter's new reading) taken on in 64 bits.
/// The same code as 2.10's (Jeff Epler's; needs arithmetic right shift).
static inline rtapi_s64 hal_extend_counter(rtapi_s64 old, rtapi_s64 newlow, int nbits) {
  int nshift = 64 - nbits;
  rtapi_u64 oldlow_shifted = ((rtapi_u64)old << nshift);
  rtapi_u64 newlow_shifted = ((rtapi_u64)newlow << nshift);
  rtapi_s64 diff_shifted = newlow_shifted - oldlow_shifted;
  return (rtapi_u64)old + (diff_shifted >> nshift);
}

// 2.9's rtapi_task_new() and hal_export_funct() take uses_fp; ecat's
// tasks and functions use floating point.
#define rtapi_task_new(code, arg, prio, owner, stacksize) rtapi_task_new(code, arg, prio, owner, stacksize, RTAPI_USES_FP)
#define hal_export_funct(name, funct, arg, reentrant, comp_id) hal_export_funct(name, funct, arg, 1, reentrant, comp_id)

#endif

#endif
