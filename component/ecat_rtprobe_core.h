//
//    ecat_rtprobe: the time of every run of a HAL thread, for EtherCAT
//    Setup's real-time check (ecatsetup/rtcheck.py, docs/development.md
//    "Real-time check").
//
//    The module (ecat_rtprobe.c) gives one function per name; added first
//    in a thread, it writes rtapi_get_time() (CLOCK_MONOTONIC in uspace)
//    into a ring in a file in /dev/shm each time the thread runs. EtherCAT
//    Setup reads the file while it measures. Nothing else in HAL is used,
//    so it works beside a configuration that has sampler or timedelta of
//    its own (HAL loads a component once).
//
//    The file (little endian, as the computer):
//      0   header, 64 bytes: "ECATRTP" + NUL, u32 version (1), u32 n
//          (instances), u32 slots (a power of two), u32 instance size (128),
//          s64 created (ns, CLOCK_MONOTONIC), the rest zero;
//      64  n instances, 128 bytes each: char name[64] (NUL-ended), s64
//          period (ns, the thread's, 0 until the first run), u64 count (runs
//          recorded; the ring slot of run k is k % slots), the rest zero;
//      64 + 128 n: n rings of slots s64 time stamps (ns), one after another.
//    The writer puts the time in the slot first and then publishes the new
//    count (release); a reader takes the count (acquire), reads the slots
//    after the ones it had, and counts as lost what the ring overwrote.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#ifndef _ECAT_RTPROBE_CORE_H_
#define _ECAT_RTPROBE_CORE_H_

#include <stddef.h>
#include <stdint.h>

#define ECAT_RTPROBE_MAGIC "ECATRTP"
#define ECAT_RTPROBE_VERSION 1
#define ECAT_RTPROBE_MAX 16
#define ECAT_RTPROBE_SLOTS 65536  // 65 s at 1 ms, 1.6 s at 25 us: read every 50 ms
#define ECAT_RTPROBE_NAME_LEN 64
#define ECAT_RTPROBE_PATH "/dev/shm/ecat-rtprobe"

typedef struct {
  char magic[8];
  uint32_t version;
  uint32_t n;
  uint32_t slots;
  uint32_t inst_size;
  int64_t created;
  char pad[32];
} ecat_rtprobe_header_t;

typedef struct {
  char name[ECAT_RTPROBE_NAME_LEN];
  int64_t period;
  uint64_t count;
  char pad[48];
} ecat_rtprobe_inst_t;

_Static_assert(sizeof(ecat_rtprobe_header_t) == 64, "header layout");
_Static_assert(sizeof(ecat_rtprobe_inst_t) == 128, "instance layout");
_Static_assert(offsetof(ecat_rtprobe_inst_t, period) == 64, "instance layout");
_Static_assert(offsetof(ecat_rtprobe_inst_t, count) == 72, "instance layout");

typedef struct {
  ecat_rtprobe_header_t *head;  ///< the mapped file
  size_t size;
} ecat_rtprobe_t;

/// The file's size for n instances.
static inline size_t ecat_rtprobe_size(unsigned n) {
  return sizeof(ecat_rtprobe_header_t) + n * sizeof(ecat_rtprobe_inst_t) +
         (size_t)n * ECAT_RTPROBE_SLOTS * sizeof(int64_t);
}

static inline ecat_rtprobe_inst_t *ecat_rtprobe_inst(ecat_rtprobe_t *p, unsigned i) {
  return (ecat_rtprobe_inst_t *)((char *)p->head + sizeof(ecat_rtprobe_header_t)) + i;
}

static inline int64_t *ecat_rtprobe_ring(ecat_rtprobe_t *p, unsigned i) {
  return (int64_t *)((char *)p->head + sizeof(ecat_rtprobe_header_t) + p->head->n * sizeof(ecat_rtprobe_inst_t)) +
         (size_t)i * ECAT_RTPROBE_SLOTS;
}

/// One run of instance i at time now (ns) in a thread of the given period:
/// real-time safe (no system call, no lock).
static inline void ecat_rtprobe_record(ecat_rtprobe_t *p, unsigned i, int64_t now, long period) {
  ecat_rtprobe_inst_t *in = ecat_rtprobe_inst(p, i);
  uint64_t c = __atomic_load_n(&in->count, __ATOMIC_RELAXED);
  if (in->period != period) in->period = period;
  ecat_rtprobe_ring(p, i)[c & (ECAT_RTPROBE_SLOTS - 1)] = now;
  __atomic_store_n(&in->count, c + 1, __ATOMIC_RELEASE);
}

/// Make the file (replacing an old one) with the given names, mapped.
/// Returns 0, or a negative errno.
int ecat_rtprobe_open(ecat_rtprobe_t *p, const char *path, const char *const *names, unsigned n, int64_t now);

/// Unmap and remove the file.
void ecat_rtprobe_close(ecat_rtprobe_t *p, const char *path);

#endif
