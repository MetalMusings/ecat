//
//    Unit tests of the ecat component: a fake of the parts of HAL, the
//    master and lcec that a device or profile uses, so that its read and
//    write functions can be run cycle by cycle without a bus.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#ifndef _ECAT_TEST_FAKE_H_
#define _ECAT_TEST_FAKE_H_

#include <stdint.h>
#include <stdio.h>

#include "../lcec.h"

/// A process data entry of the fake slave.
typedef struct {
  uint16_t index;
  uint8_t subindex;
  uint8_t bits;
} fake_entry_t;

#define FAKE_END {0, 0, 0}

/// A slave with these entries (ending with FAKE_END), laid out one after
/// the other in the process data, and these modparams (ending with id -1;
/// may be NULL). The bus is working. Forgets the pins of the slave before.
lcec_slave_t *fake_slave(const char *name, const fake_entry_t *entries, const lcec_slave_modparam_t *modparams);

/// The working counter is complete and the slave is in OP, or not.
void fake_bus(lcec_slave_t *slave, int ok);

/// Process data of an entry; it must be in the slave's entries.
uint32_t fake_get(lcec_slave_t *slave, uint16_t index, uint8_t subindex);
void fake_set(lcec_slave_t *slave, uint16_t index, uint8_t subindex, uint32_t value);

/// A pin or parameter by its full name; NULL if there is none.
void *fake_find(const char *name);

/// A pin's or parameter's value, by its full name; it must exist.
#define PIN_BIT(name) (*(hal_bit_t *)fake_pin(name))
#define PIN_S32(name) (*(hal_s32_t *)fake_pin(name))
#define PIN_U32(name) (*(hal_u32_t *)fake_pin(name))
#define PIN_FLOAT(name) (*(hal_float_t *)fake_pin(name))
void *fake_pin(const char *name);

/// Calls of lcec_generic_read and lcec_generic_write.
extern int fake_generic_reads, fake_generic_writes;

/// Events recorded with lcec_event (the flight recorder); set
/// fake_n_events to 0 to start over.
#define FAKE_EVENTS 64
typedef struct {
  int slave, type;
  uint32_t a, b;
} fake_event_t;
extern fake_event_t fake_events[FAKE_EVENTS];
extern int fake_n_events;

/// The messages printed since fake_slave(), or since it was set to "".
extern char fake_msgs[];

// A minimal test runner.
extern int fake_failures;
#define CHECK(cond)                                                       \
  do {                                                                    \
    if (!(cond)) {                                                        \
      fprintf(stderr, "%s:%d: %s: CHECK(%s) failed\n", __FILE__, __LINE__, \
          __func__, #cond);                                               \
      fake_failures++;                                                    \
    }                                                                     \
  } while (0)
#define CHECK_EQ(a, b)                                                                                     \
  do {                                                                                                     \
    long long a_ = (long long)(a), b_ = (long long)(b);                                                    \
    if (a_ != b_) {                                                                                        \
      fprintf(stderr, "%s:%d: %s: %s is %lld, not %lld\n", __FILE__, __LINE__, __func__, #a, a_, b_);      \
      fake_failures++;                                                                                     \
    }                                                                                                      \
  } while (0)
#define CHECK_NEAR(a, b, eps)                                                                              \
  do {                                                                                                     \
    double a_ = (a), b_ = (b);                                                                             \
    if (!(a_ - b_ <= (eps) && b_ - a_ <= (eps))) {                                                         \
      fprintf(stderr, "%s:%d: %s: %s is %g, not %g\n", __FILE__, __LINE__, __func__, #a, a_, b_);          \
      fake_failures++;                                                                                     \
    }                                                                                                      \
  } while (0)

typedef void (*fake_test_t)(void);
/// Run the tests (ending with NULL); the exit status for main().
int fake_run(const char *suite, const fake_test_t *tests);
#define RUN_TESTS(suite, ...)                           \
  int main(void) {                                      \
    static const fake_test_t t[] = {__VA_ARGS__, NULL}; \
    return fake_run(suite, t);                          \
  }

#endif
