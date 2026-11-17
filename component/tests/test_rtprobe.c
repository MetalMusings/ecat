//
//    ecat_rtprobe's file: the layout EtherCAT Setup reads (rtcheck.py
//    ProbeReader), the ring and the names.
//

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "../ecat_rtprobe_core.h"
#include "fake.h"

static void path_for(char *buf, size_t n) {
  snprintf(buf, n, "%s/ecat-rtprobe-test-%d", getenv("TMPDIR") ? getenv("TMPDIR") : "/tmp", (int)getpid());
}

static void test_rtprobe_layout(void) {
  char path[256];
  const char *names[] = {"probe-base", "probe-servo"};
  ecat_rtprobe_t p;
  struct stat st;
  path_for(path, sizeof(path));
  CHECK_EQ(ecat_rtprobe_open(&p, path, names, 2, 123456789), 0);
  CHECK_EQ(stat(path, &st), 0);
  CHECK_EQ((long)st.st_size, (long)(64 + 2 * 128 + 2 * ECAT_RTPROBE_SLOTS * 8));
  CHECK(!memcmp(p.head->magic, "ECATRTP", 8));
  CHECK_EQ((int)p.head->version, 1);
  CHECK_EQ((int)p.head->n, 2);
  CHECK_EQ((int)p.head->slots, ECAT_RTPROBE_SLOTS);
  CHECK_EQ((int)p.head->inst_size, 128);
  CHECK_EQ((long)p.head->created, 123456789L);
  CHECK(!strcmp((char *)p.head + 64, "probe-base"));
  CHECK(!strcmp((char *)p.head + 64 + 128, "probe-servo"));
  // the ring of instance 1 starts after instance 0's
  CHECK((char *)ecat_rtprobe_ring(&p, 1) == (char *)p.head + 64 + 2 * 128 + ECAT_RTPROBE_SLOTS * 8);

  ecat_rtprobe_record(&p, 1, 1000, 1000000);
  ecat_rtprobe_record(&p, 1, 1001000, 1000000);
  ecat_rtprobe_record(&p, 0, 5, 25000);
  CHECK_EQ((long)ecat_rtprobe_inst(&p, 1)->count, 2L);
  CHECK_EQ((long)ecat_rtprobe_inst(&p, 1)->period, 1000000L);
  CHECK_EQ((long)ecat_rtprobe_inst(&p, 0)->period, 25000L);
  CHECK_EQ((long)ecat_rtprobe_ring(&p, 1)[1], 1001000L);
  CHECK_EQ((long)ecat_rtprobe_ring(&p, 0)[0], 5L);
  CHECK_EQ((long)*(int64_t *)((char *)p.head + 64 + 128 + 64), 1000000L);   // period at 64
  CHECK_EQ((long)*(uint64_t *)((char *)p.head + 64 + 128 + 72), 2L);        // count at 72

  // the ring wraps: run k is in slot k % slots
  for (long k = 2; k < ECAT_RTPROBE_SLOTS + 3; k++) ecat_rtprobe_record(&p, 1, k * 1000, 1000000);
  CHECK_EQ((long)ecat_rtprobe_inst(&p, 1)->count, (long)ECAT_RTPROBE_SLOTS + 3);
  CHECK_EQ((long)ecat_rtprobe_ring(&p, 1)[2], (long)(ECAT_RTPROBE_SLOTS + 2) * 1000);
  CHECK_EQ((long)ecat_rtprobe_ring(&p, 1)[3], 3000L);

  ecat_rtprobe_close(&p, path);
  CHECK(p.head == NULL);
  CHECK(stat(path, &st) < 0 && errno == ENOENT);
}

static void test_rtprobe_names(void) {
  char path[256];
  char longname[80];
  const char *bad[] = {""};
  const char *toolong[] = {longname};
  ecat_rtprobe_t p;
  path_for(path, sizeof(path));
  memset(longname, 'a', 64);
  longname[64] = 0;
  CHECK_EQ(ecat_rtprobe_open(&p, path, bad, 1, 0), -EINVAL);
  CHECK_EQ(ecat_rtprobe_open(&p, path, toolong, 1, 0), -EINVAL);
  CHECK_EQ(ecat_rtprobe_open(&p, path, bad, 0, 0), -EINVAL);
  longname[63] = 0;     // 63 characters fit
  CHECK_EQ(ecat_rtprobe_open(&p, path, toolong, 1, 0), 0);
  // a second load replaces the file; the first mapping stays valid for its reader
  {
    ecat_rtprobe_t q;
    const char *one[] = {"x"};
    CHECK_EQ(ecat_rtprobe_open(&q, path, one, 1, 0), 0);
    CHECK(!strcmp(ecat_rtprobe_inst(&p, 0)->name, longname));
    CHECK(!strcmp(ecat_rtprobe_inst(&q, 0)->name, "x"));
    ecat_rtprobe_close(&q, NULL);
  }
  ecat_rtprobe_close(&p, path);
}

RUN_TESTS("rtprobe", test_rtprobe_layout, test_rtprobe_names)
