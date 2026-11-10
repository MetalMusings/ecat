//
//    Unit tests of the start check (ecat_startcheck.c).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <stdio.h>
#include <string.h>

#include "../ecat_startcheck.h"
#include "fake.h"

static char said[8][400];
static int said_level[8], n_said;

static void collect(int level, const char *text, void *arg) {
  (void)arg;
  if (n_said < 8) {
    said_level[n_said] = level;
    snprintf(said[n_said], sizeof(said[0]), "%s", text);
  }
  n_said++;
}

static const ecat_expect_t plasma[] = {
    {0, 0xA88, 0x0A880002, 0x202, "ECT60V202(COE)", "x"},
    {1, 0xA88, 0x0A880002, 0x202, "ECT60V202(COE)", "y"},
    {2, 0xD0BED1D0, 0x7000, 0, "EaserCAT 7000", "io"},
};

static ecat_found_t bus[] = {
    {0xA88, 0x0A880002, 0x202, "ECT60V202(COE)"},
    {0xA88, 0x0A880002, 0x202, "ECT60V202(COE)"},
    {0xD0BED1D0, 0x7000, 2, "EaserCAT 7000"},
    {0x2, 0x07D83052, 0x110000, "EL2008"},
};

static void test_startcheck_matching_bus(void) {
  n_said = 0;
  // revision 0 in the file: not checked
  CHECK_EQ(ecat_startcheck_compare(plasma, 3, bus, 3, collect, NULL), 0);
  CHECK_EQ(n_said, 0);
}

static void test_startcheck_other_slave(void) {
  ecat_found_t b[3];
  memcpy(b, bus, sizeof(b));
  b[2] = bus[3];
  n_said = 0;
  CHECK_EQ(ecat_startcheck_compare(plasma, 3, b, 3, collect, NULL), 1);
  CHECK_EQ(n_said, 1);
  CHECK_EQ(said_level[0], ECAT_CHECK_ERROR);
  CHECK(strcmp(said[0], "slave 2 is EL2008 (vendor 0x00000002, product 0x07D83052); the configuration expects "
                        "EaserCAT 7000 there (vendor 0xD0BED1D0, product 0x00007000) as io") == 0);
}

static void test_startcheck_same_vendor_other_product(void) {
  ecat_found_t b[3];
  memcpy(b, bus, sizeof(b));
  b[1].pid = 0x0A880003;
  n_said = 0;
  CHECK_EQ(ecat_startcheck_compare(plasma, 3, b, 3, collect, NULL), 1);
  CHECK(strstr(said[0], "slave 1 is ECT60V202(COE) (vendor 0x00000A88, product 0x0A880003)") == said[0]);
}

static void test_startcheck_missing_slaves(void) {
  n_said = 0;
  CHECK_EQ(ecat_startcheck_compare(plasma, 3, bus, 1, collect, NULL), 2);
  CHECK_EQ(n_said, 2);
  CHECK(strcmp(said[0], "slave 1 (y, ECT60V202(COE)) is not on the bus: the bus has 1 slave") == 0);
  n_said = 0;
  CHECK_EQ(ecat_startcheck_compare(plasma, 3, bus, 0, collect, NULL), 3);
}

static void test_startcheck_revision_and_extra(void) {
  ecat_found_t b[4];
  memcpy(b, bus, sizeof(b));
  b[1].revision = 0x203;
  n_said = 0;
  CHECK_EQ(ecat_startcheck_compare(plasma, 3, b, 4, collect, NULL), 0);
  CHECK_EQ(n_said, 2);
  CHECK_EQ(said_level[0], ECAT_CHECK_WARN);
  CHECK(strstr(said[0], "slave 1 (y) has revision 0x00000203, the configuration was made with 0x00000202") != NULL);
  CHECK_EQ(said_level[1], ECAT_CHECK_INFO);
  CHECK(strcmp(said[1], "the bus has 4 slaves; slave 3 is not in the configuration and stays unused") == 0);
}

static void test_kernel_master_conf(void) {
  char what[64] = "";
  const char *conf =
      "# MASTER0_DEVICE=\"enp2s0\"\n"
      "MASTER0_DEVICE=\"a8:b8:e0:01:02:03\"\n"
      "MASTER0_BACKUP=\"\"\n"
      "DEVICE_MODULES=\"generic\"\n";

  CHECK_EQ(ecat_kernel_master_uses(conf, "enp2s0", "A8:B8:E0:01:02:03", what, sizeof(what)), 1);
  CHECK(strcmp(what, "MASTER0_DEVICE=a8:b8:e0:01:02:03") == 0);
  // another card: no conflict; a commented line does not count
  CHECK_EQ(ecat_kernel_master_uses(conf, "enp2s0", "a8:b8:e0:09:09:09", what, sizeof(what)), 0);
  CHECK_EQ(ecat_kernel_master_uses(conf, "enp2s0", NULL, what, sizeof(what)), 0);
  // by name, in a second master, and "any card"
  CHECK_EQ(ecat_kernel_master_uses("MASTER1_DEVICE = enp1s0 # the bus\n", "enp1s0", NULL, what, sizeof(what)), 1);
  CHECK_EQ(ecat_kernel_master_uses("MASTER0_DEVICE='ff:ff:ff:ff:ff:ff'", "enp1s0", NULL, what, sizeof(what)), 1);
  CHECK_EQ(ecat_kernel_master_uses("MASTER0_DEVICE=\"enp1s0\"\n", "enp1s0f1", NULL, what, sizeof(what)), 0);
  CHECK_EQ(ecat_kernel_master_uses("", "enp1s0", NULL, what, sizeof(what)), 0);
  CHECK_EQ(ecat_kernel_master_uses("#MASTER0_DEVICE=enp1s0\n", "enp1s0", NULL, what, sizeof(what)), 0);
}

RUN_TESTS("startcheck", test_startcheck_matching_bus, test_startcheck_other_slave, test_startcheck_same_vendor_other_product,
    test_startcheck_missing_slaves,
    test_startcheck_revision_and_extra, test_kernel_master_conf)
