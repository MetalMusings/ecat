//
//    ecat_rtprobe: a HAL module with one function per name that writes the
//    time of each of its thread's runs into a file in /dev/shm, for
//    EtherCAT Setup's real-time check (ecat_rtprobe_core.h has the file).
//
//      loadrt ecat_rtprobe names=probe-servo,probe-base
//      addf probe-servo servo-thread 1
//      addf probe-base base-thread 1
//
//    The function takes no pins and needs no floating point. It is put
//    first in its thread, so that its time is the thread's wake-up.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include "rtapi.h"
#include "rtapi_app.h"
#include "ecat_hal.h"
#include "ecat_rtprobe_core.h"

MODULE_LICENSE("GPL");
MODULE_AUTHOR("MetalMusings");
MODULE_DESCRIPTION("Time of every run of HAL threads, for EtherCAT Setup's real-time check");

static char *names[ECAT_RTPROBE_MAX] = {0};
RTAPI_MP_ARRAY_STRING(names, ECAT_RTPROBE_MAX, "names of the functions, one per thread");
static char *file = ECAT_RTPROBE_PATH;
RTAPI_MP_STRING(file, "the file the times go to");

typedef struct {
  ecat_rtprobe_t *probe;
  unsigned index;
} probe_arg_t;

static int comp_id = -1;
static ecat_rtprobe_t probe;
static probe_arg_t args[ECAT_RTPROBE_MAX];

static void probe_funct(void *arg, long period) {
  probe_arg_t *a = arg;
  ecat_rtprobe_record(a->probe, a->index, rtapi_get_time(), period);
}

int rtapi_app_main(void) {
  unsigned n = 0, i;
  int err;

  while (n < ECAT_RTPROBE_MAX && names[n] != NULL && names[n][0]) n++;
  if (n == 0) {
    rtapi_print_msg(RTAPI_MSG_ERR, "ecat_rtprobe: give names=<function>[,...]\n");
    return -EINVAL;
  }
  comp_id = hal_init("ecat_rtprobe");
  if (comp_id < 0) return comp_id;
  err = ecat_rtprobe_open(&probe, file, (const char *const *)names, n, rtapi_get_time());
  if (err < 0) {
    rtapi_print_msg(RTAPI_MSG_ERR, "ecat_rtprobe: %s: cannot make the file (%d)\n", file, err);
    hal_exit(comp_id);
    return err;
  }
  for (i = 0; i < n; i++) {
    args[i].probe = &probe;
    args[i].index = i;
    err = hal_export_funct(names[i], probe_funct, &args[i], 0, comp_id);
    if (err < 0) {
      rtapi_print_msg(RTAPI_MSG_ERR, "ecat_rtprobe: function %s: %d\n", names[i], err);
      ecat_rtprobe_close(&probe, file);
      hal_exit(comp_id);
      return err;
    }
  }
  hal_ready(comp_id);
  return 0;
}

void rtapi_app_exit(void) {
  // the functions are out of the threads (delf, or the threads stopped)
  // before a module is unloaded
  ecat_rtprobe_close(&probe, file);
  hal_exit(comp_id);
}
