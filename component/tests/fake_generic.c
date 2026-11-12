//
//    Unit tests of the ecat component: stand-ins for the generic device
//    (devices/lcec_generic.c), for the tests of the profiles, which only
//    count its calls. test_generic links the real one instead.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include "fake.h"

int lcec_generic_init(int comp_id, struct lcec_slave *slave) {
  (void)comp_id, (void)slave;
  return 0;
}

void lcec_generic_read(struct lcec_slave *slave, long period) {
  (void)slave, (void)period;
  fake_generic_reads++;
}

void lcec_generic_write(struct lcec_slave *slave, long period) {
  (void)slave, (void)period;
  fake_generic_writes++;
}

