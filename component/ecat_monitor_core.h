//
//  Long-term monitoring of the bus while LinuxCNC runs: what the master does
//  not watch by itself, read at a low rate and recorded in the flight
//  recorder (ecat_events.h) so that it can be looked at after hours:
//
//  - a DC slave's system time difference (0x092C): how well its clock
//    follows the reference clock (the master checks it once, before SYNC0
//    starts);
//  - the port error counters (0x0300-0x0313: rx, phy, lost link) and the
//    chip's processing and PDI error counters (0x030C, 0x030D);
//  - the watchdog counters (0x0442 process data, 0x0443 PDI);
//  - the slave's own synchronisation counters (ETG.1020 0x1C32:0B SM events
//    missed, 0x1C32:0C cycle time too small);
//  - the cycles whose working counter was not complete (the frames the
//    application lost).
//
//  This file is the logic only: it takes the values read and the time, and
//  writes events. Reading them (register and SDO requests) is in
//  lcec_main.c. Everything here is a few arithmetic operations and stores:
//  safe in the servo thread.
//
//  What is recorded, per slave (codes in ecat_mon_code()):
//  - ECAT_EV_MON_ONSET when a counter grows after ECAT_MON_QUIET_S without
//    growth: the time a fault started (to compare with the torch, the
//    spindle, ...);
//  - ECAT_EV_MON_SUM every ECAT_MON_PERIOD_S for each counter that grew in
//    the period, with the total;
//  - ECAT_EV_MON_DC every period for each monitored DC slave: the largest
//    |difference| and the number of samples;
//  - ECAT_EV_MON_DC_OVER when the difference is beyond the limit, at most
//    once a minute per slave;
//  - ECAT_EV_MON_WC (master) every period with lost cycles.
//

#ifndef _ECAT_MONITOR_CORE_H_
#define _ECAT_MONITOR_CORE_H_

#include <stdint.h>

#include "ecat_events.h"

#define ECAT_MON_PERIOD_S 600         // summaries every 10 minutes
#define ECAT_MON_QUIET_S 60           // a counter quiet this long, then growing: an onset
#define ECAT_MON_DC_LIMIT_NS 1000     // |system time difference| worth an event at once
#define ECAT_MON_DC_OVER_EVERY_S 60   // at most one such event a minute per slave

// What a counter counts; the code in the events is kind << 8 | port.
enum {
  ECAT_MON_RX = 1,           // 0x0300 + 2 * port: invalid frames received
  ECAT_MON_PHY = 2,          // 0x0301 + 2 * port: physical layer (RX error) errors
  ECAT_MON_LOST = 3,         // 0x0310 + port: link lost
  ECAT_MON_PROCESSING = 4,   // 0x030C: frames the chip could not process
  ECAT_MON_PDI = 5,          // 0x030D: errors on the PDI (the bus to the slave's processor)
  ECAT_MON_PD_WATCHDOG = 6,  // 0x0442: process data watchdog expired (counted in OP only)
  ECAT_MON_PDI_WATCHDOG = 7, // 0x0443: PDI watchdog expired (the processor stopped answering)
  ECAT_MON_SM_MISSED = 8,    // 0x1C32:0B: SM events missed
  ECAT_MON_TOO_SMALL = 9,    // 0x1C32:0C: cycle time too small
};
#define ECAT_MON_COUNTERS 18   // rx, phy, lost on 4 ports, and the six others

typedef struct {
  int position;
  int dc;                      // read 0x092C (a DC slave that is not the reference clock)
  // the values last read, for the growth (8-bit counters wrap at 255: they stop there)
  int have[ECAT_MON_COUNTERS];
  uint32_t last[ECAT_MON_COUNTERS];
  uint64_t grew_ns[ECAT_MON_COUNTERS];   // when it last grew; 0: never
  uint32_t sum[ECAT_MON_COUNTERS];       // growth in this period
  uint32_t total;              // all growth since loading (the error-count pin)
  // DC
  int32_t dc_diff;             // last difference in ns (signed)
  uint32_t dc_max;             // largest |difference| in this period
  uint32_t dc_max_all;         // ... since loading
  uint32_t dc_samples;         // samples in this period
  uint64_t dc_over_ns;         // last ECAT_EV_MON_DC_OVER
  uint64_t period_ns;          // start of this period
} ecat_mon_slave_t;

typedef struct {
  uint32_t wc_zero, wc_incomplete;          // cycles in this period
  uint32_t wc_bad_all;                      // ... not complete since activation
  uint64_t period_ns;
} ecat_mon_master_t;

/// The code of a counter: kind << 8 | port (port 0 for those of the chip).
static inline uint32_t ecat_mon_code(int kind, int port) { return ((uint32_t)kind << 8) | (uint32_t)port; }

void ecat_mon_slave_init(ecat_mon_slave_t *s, int position, int dc, uint64_t now_ns);
void ecat_mon_master_init(ecat_mon_master_t *m, uint64_t now_ns);

/// 0x0300-0x0313 as read (20 bytes).
void ecat_mon_errors(ecat_events_t *log, ecat_mon_slave_t *s, uint64_t now_ns, const uint8_t *r);
/// 0x0440-0x0443 as read (4 bytes); the process data watchdog counts only in OP.
void ecat_mon_watchdogs(ecat_events_t *log, ecat_mon_slave_t *s, uint64_t now_ns, const uint8_t *r, int op);
/// 0x092C as read (sign and magnitude: bit 31 set when the local copy is behind).
void ecat_mon_dc(ecat_events_t *log, ecat_mon_slave_t *s, uint64_t now_ns, uint32_t raw);
/// 0x1C32:0B (kind ECAT_MON_SM_MISSED) or :0C (ECAT_MON_TOO_SMALL), 16 bits.
void ecat_mon_sync(ecat_events_t *log, ecat_mon_slave_t *s, uint64_t now_ns, int kind, uint16_t value);
/// End a period that is over: the summaries. Call it now and then (once a second).
void ecat_mon_slave_period(ecat_events_t *log, ecat_mon_slave_t *s, uint64_t now_ns);

/// Every cycle: the domain's working counter state (0 zero, 1 incomplete, 2 complete).
void ecat_mon_wc(ecat_mon_master_t *m, unsigned wc_state);
void ecat_mon_master_period(ecat_events_t *log, ecat_mon_master_t *m, uint64_t now_ns);

#endif
