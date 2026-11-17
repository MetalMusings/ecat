//
//    Unit tests of ecat_hal.h's layer for LinuxCNC 2.9: 2.10's HAL API on
//    2.9's functions, which are faked here. Built against 2.10's headers
//    there is no layer: the suite says so and passes.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../ecat_hal.h"
#include "fake.h"

#if !ECAT_HAL_64

// ---- 2.9's functions, faked ------------------------------------------
// (the names in parentheses: ecat_hal.h maps 2.10's calls onto these)

static struct {
  char name[HAL_NAME_LEN + 1];
  int kind;  // HAL_BIT, ... (pin) or -HAL_BIT, ... (param)
  int dir, comp_id;
  void *where;
} made[16];
static int n_made, fail_next;
static int task_uses_fp = -1, funct_uses_fp = -1;
static unsigned long task_stack;

static int record(int kind, int dir, void *where, int comp_id, const char *fmt, va_list ap) {
  if (fail_next) return -EINVAL;
  if (n_made == 16) abort();
  vsnprintf(made[n_made].name, sizeof(made[n_made].name), fmt, ap);
  made[n_made].kind = kind;
  made[n_made].dir = dir;
  made[n_made].comp_id = comp_id;
  made[n_made].where = where;
  n_made++;
  return 0;
}

static rtapi_u64 store[16];  // the pins' storage, as 2.9's shared memory would hold it
static int n_store;

#define FAKE_PIN(t, T)                                                                        \
  int hal_pin_##t##_newf(hal_pin_dir_t dir, hal_##t##_t **ptr, int comp_id, const char *fmt, ...) { \
    va_list ap;                                                                               \
    int err;                                                                                  \
    va_start(ap, fmt);                                                                        \
    err = record(T, dir, &store[n_store], comp_id, fmt, ap);                                  \
    va_end(ap);                                                                               \
    if (err == 0) *ptr = (hal_##t##_t *)&store[n_store++];                                    \
    return err;                                                                               \
  }                                                                                           \
  int hal_param_##t##_newf(hal_param_dir_t dir, hal_##t##_t *data, int comp_id, const char *fmt, ...) { \
    va_list ap;                                                                               \
    int err;                                                                                  \
    va_start(ap, fmt);                                                                        \
    err = record(-T, dir, (void *)data, comp_id, fmt, ap);                                    \
    va_end(ap);                                                                               \
    return err;                                                                               \
  }
FAKE_PIN(bit, HAL_BIT)
FAKE_PIN(s32, HAL_S32)
FAKE_PIN(u32, HAL_U32)
FAKE_PIN(float, HAL_FLOAT)

void *hal_malloc(long size) { return calloc(1, size); }

int(rtapi_task_new)(void (*taskcode)(void *), void *arg, int prio, int owner, unsigned long int stacksize, int uses_fp) {
  (void)taskcode, (void)arg, (void)prio, (void)owner;
  task_stack = stacksize;
  task_uses_fp = uses_fp;
  return 3;
}

int(hal_export_funct)(const char *name, void (*funct)(void *, long), void *arg, int uses_fp, int reentrant, int comp_id) {
  (void)name, (void)funct, (void)arg, (void)reentrant, (void)comp_id;
  funct_uses_fp = uses_fp;
  return 0;
}

static void start(void) {
  memset(made, 0, sizeof(made));
  memset(store, 0, sizeof(store));
  n_made = n_store = fail_next = 0;
}

// ---- tests -------------------------------------------------------------

static void test_pins_are_named_and_given_their_default(void) {
  hal_sint_t s = NULL;
  hal_uint_t u = NULL;
  hal_bool_t b = NULL;
  hal_real_t r = NULL;
  start();
  CHECK_EQ(hal_pin_new_sint(7, HAL_OUT, &s, -5, "%s.%s.%d", "ecat", "x", 3), 0);
  CHECK_EQ(hal_pin_new_uint(7, HAL_IN, &u, 9, "ecat.u"), 0);
  CHECK_EQ(hal_pin_new_bool(7, HAL_IO, &b, 1, "ecat.b"), 0);
  CHECK_EQ(hal_pin_new_real(7, HAL_OUT, &r, 2.5, "ecat.r"), 0);
  CHECK_EQ(n_made, 4);
  CHECK(!strcmp(made[0].name, "ecat.x.3"));
  CHECK_EQ(made[0].kind, HAL_S32);
  CHECK_EQ(made[0].dir, HAL_OUT);
  CHECK_EQ(made[0].comp_id, 7);
  CHECK(made[0].where == (void *)s);
  CHECK_EQ(made[1].kind, HAL_U32);
  CHECK_EQ(made[2].kind, HAL_BIT);
  CHECK_EQ(made[2].dir, HAL_IO);
  CHECK_EQ(made[3].kind, HAL_FLOAT);
  CHECK_EQ(hal_get_sint(s), -5);
  CHECK_EQ(*s, -5);
  CHECK_EQ(hal_get_uint(u), 9);
  CHECK_EQ(hal_get_bool(b), 1);
  CHECK_NEAR(hal_get_real(r), 2.5, 0);
}

static void test_a_name_too_long_is_refused(void) {
  hal_sint_t s = NULL;
  char long_name[HAL_NAME_LEN + 2];
  start();
  memset(long_name, 'a', sizeof(long_name) - 1);
  long_name[sizeof(long_name) - 1] = 0;
  CHECK_EQ(hal_pin_new_sint(1, HAL_OUT, &s, 0, "%s", long_name), -EINVAL);
  CHECK_EQ(hal_param_new_sint(1, HAL_RW, &s, 0, "%s", long_name), -EINVAL);
  CHECK_EQ(n_made, 0);
  long_name[HAL_NAME_LEN] = 0;  // exactly HAL_NAME_LEN: fits
  CHECK_EQ(hal_pin_new_sint(1, HAL_OUT, &s, 0, "%s", long_name), 0);
  CHECK_EQ(n_made, 1);
}

static void test_a_failed_pin_gets_no_default(void) {
  hal_sint_t s = NULL;
  start();
  fail_next = 1;
  CHECK(hal_pin_new_sint(1, HAL_OUT, &s, 4, "p") != 0);
  CHECK(s == NULL);
}

static void test_params_are_slots_of_their_own(void) {
  hal_uint_t u = NULL, u2 = NULL;
  hal_real_t r = NULL;
  hal_bool_t b = NULL;
  start();
  CHECK_EQ(hal_param_new_uint(4, HAL_RW, &u, 250, "ecat.0.p"), 0);
  CHECK_EQ(hal_param_new_uint(4, HAL_RO, &u2, 1, "ecat.0.q"), 0);
  CHECK_EQ(hal_param_new_real(4, HAL_RW, &r, 0.25, "ecat.0.r"), 0);
  CHECK_EQ(hal_param_new_bool(4, HAL_RW, &b, 1, "ecat.0.b"), 0);
  CHECK_EQ(n_made, 4);
  CHECK(u != NULL && u2 != NULL && u != u2);
  CHECK(made[0].where == (void *)u);  // registered with the slot the reference points to
  CHECK_EQ(made[0].kind, -HAL_U32);
  CHECK_EQ(made[0].dir, HAL_RW);
  CHECK_EQ(made[1].dir, HAL_RO);
  CHECK(!strcmp(made[1].name, "ecat.0.q"));
  CHECK_EQ(made[2].kind, -HAL_FLOAT);
  CHECK_EQ(made[3].kind, -HAL_BIT);
  CHECK_EQ(*u, 250);  // the default is in the slot before halcmd can read it
  CHECK_EQ(hal_get_uint(u2), 1);
  CHECK_NEAR(*r, 0.25, 0);
  CHECK_EQ(*b, 1);
  *u = 500;  // halcmd setp
  CHECK_EQ(hal_get_uint(u), 500);
}

static void test_integers_keep_the_low_32_bits(void) {
  hal_sint_t s = NULL;
  hal_uint_t u = NULL;
  start();
  hal_pin_new_sint(1, HAL_OUT, &s, 0, "s");
  hal_pin_new_uint(1, HAL_OUT, &u, 0, "u");
  // the value given back is the one asked for, as 2.10's
  CHECK_EQ(hal_set_sint(s, (1LL << 32) + 5), (1LL << 32) + 5);
  CHECK_EQ(hal_get_sint(s), 5);
  hal_set_sint(s, 0x80000000LL);  // wraps as a counter does
  CHECK_EQ(hal_get_sint(s), -2147483647LL - 1);
  hal_set_sint(s, -1);
  CHECK_EQ(hal_get_sint(s), -1);
  hal_set_uint(u, (1ULL << 32) + 7);
  CHECK_EQ(hal_get_uint(u), 7);
  hal_set_uint(u, 0xFFFFFFFFULL);
  CHECK_EQ((long long)hal_get_uint(u), 0xFFFFFFFFLL);  // not sign-extended
  CHECK_EQ(store[1] >> 32, 0);                          // and nothing written past the pin
}

static void test_extend_counter_as_in_2_10(void) {
  // the examples of 2.10's hal.h, and both directions over the wrap
  CHECK_EQ(hal_extend_counter(7, 0, 3), 8);
  CHECK_EQ(hal_extend_counter(8, 7, 3), 7);
  CHECK_EQ(hal_extend_counter(65535, 2, 16), 65538);
  CHECK_EQ(hal_extend_counter(65538, 65534, 16), 65534);
  CHECK_EQ(hal_extend_counter(0, 65535, 16), -1);
  CHECK_EQ(hal_extend_counter(-1, 3, 16), 3);
  CHECK_EQ(hal_extend_counter(0xFFFFFFFFLL, 1, 32), 0x100000001LL);
}

static void test_tasks_and_functions_use_floating_point(void) {
  start();
  CHECK_EQ(rtapi_task_new(NULL, NULL, 1, 2, 65536), 3);
  CHECK_EQ(task_uses_fp, 1);
  CHECK_EQ((long long)task_stack, 65536);
  CHECK_EQ(hal_export_funct("ecat.read", NULL, NULL, 0, 1), 0);
  CHECK_EQ(funct_uses_fp, 1);
}

RUN_TESTS("hal 2.9 layer", test_pins_are_named_and_given_their_default, test_a_name_too_long_is_refused,
    test_a_failed_pin_gets_no_default, test_params_are_slots_of_their_own, test_integers_keep_the_low_32_bits,
    test_extend_counter_as_in_2_10, test_tasks_and_functions_use_floating_point)

#else

int main(void) {
  printf("hal 2.9 layer: not built against 2.9 (LinuxCNC 2.10 headers): nothing to test\n");
  return 0;
}

#endif
