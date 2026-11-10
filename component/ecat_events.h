//
//  The flight recorder: time-stamped events of the bus (link, AL states,
//  working counter, drive faults, DC sync, the stop guard, ...) in a ring
//  buffer, kept in a memory-mapped file in /dev/shm so that EtherCAT
//  Setup can read it while LinuxCNC runs and after it has stopped (or
//  crashed). The previous run's file is kept as <file>.prev.
//
//  Writing an event is a few stores into memory locked at load time: safe
//  in the servo thread. The file layout is read by ecatsetup/events.py;
//  keep the two in step (ECAT_EVENTS_VERSION).
//

#ifndef _ECAT_EVENTS_H_
#define _ECAT_EVENTS_H_

#include <stdint.h>

#define ECAT_EVENTS_PATH "/dev/shm/ecat-events"
#define ECAT_EVENTS_MAGIC 0x56454345u  // "ECEV"
#define ECAT_EVENTS_VERSION 1
#define ECAT_EVENTS_N 4096             // records in the ring

// Event types; a and b as noted. slave is the slave's position, -1 for
// the master.
enum {
  ECAT_EV_START = 1,        // the component loaded; a: slaves in the file
  ECAT_EV_ACTIVATED = 2,    // the master was activated (cyclic operation starts)
  ECAT_EV_STOP = 3,         // the component unloads
  ECAT_EV_LINK = 10,        // a: link up (1) or down (0)
  ECAT_EV_MASTER_AL = 11,   // a: AL states of all slaves (bits 1 INIT, 2 PREOP, 4 SAFEOP, 8 OP), b: before
  ECAT_EV_RESPONDING = 12,  // a: slaves responding, b: before
  ECAT_EV_WC = 13,          // a: working counter state (0 zero, 1 incomplete, 2 complete), b: before
  ECAT_EV_SLAVE_AL = 20,    // a: the slave's AL state, b: before
  ECAT_EV_SLAVE_ONLINE = 21,  // a: online
  ECAT_EV_SLAVE_OP = 22,    // a: operational
  ECAT_EV_DC_SYNC = 23,     // a: DC sync check failed (1) or not (0)
  ECAT_EV_DRIVE_FAULT = 30,  // a: error code 0x603F, b: status word
  ECAT_EV_DRIVE_BLOCK = 31,  // the profile shut the drive down; a: fault-reason, b: axis
  ECAT_EV_DRIVE_CLEAR = 32,  // the drive's fault cleared; b: axis
  ECAT_EV_GUARD = 40,       // a: stop guard state (0 idle, 1 taking down, 2 done), b: before
  ECAT_EV_LATE = 41,        // the guard sent a cycle for a late servo thread; a: lateness us, b: suppressed since
  ECAT_EV_SHIFT = 42,       // the automatic SYNC0 shift changed; a: shift ns, b: turn-around cycles
};

typedef struct {
  uint64_t seq;      // index + 1 once written; 0 while being written
  uint64_t time_ns;  // wall clock (CLOCK_REALTIME)
  uint16_t type;
  int16_t slave;
  uint32_t a;
  uint32_t b;
  uint32_t pad;
} ecat_event_t;

typedef struct {
  uint32_t magic;
  uint32_t version;
  uint32_t n;            // ECAT_EVENTS_N
  uint32_t record_size;  // sizeof(ecat_event_t)
  uint64_t start_ns;     // when the component loaded
  uint64_t next;         // index of the next record (atomic)
  uint32_t period_ns;    // the servo period, once known
  uint32_t pid;
  char config[256];      // the configuration file
  ecat_event_t ev[ECAT_EVENTS_N];
} ecat_events_t;

/// Rate limit for frequent events: at most one per second, counting the
/// ones left out.
typedef struct {
  uint64_t last_ns;
  uint32_t suppressed;
} ecat_events_limit_t;

/// Open the recorder file at path (the old one becomes path.prev).
/// Returns NULL (with errno) if it cannot; the component runs without.
ecat_events_t *ecat_events_open(const char *path, const char *config);
void ecat_events_close(ecat_events_t *log);

/// Set up a recorder in memory the caller owns (for tests).
void ecat_events_init(ecat_events_t *log, const char *config);

/// Add an event. log may be NULL (no recorder).
void ecat_events_add(ecat_events_t *log, int type, int slave, uint32_t a, uint32_t b);

/// Add an event at most once a second; b becomes the number left out since.
void ecat_events_add_limited(ecat_events_t *log, ecat_events_limit_t *lim, int type, int slave, uint32_t a);

/// What was last recorded of the master and of a slave, to record changes.
typedef struct {
  int known;
  unsigned link_up, al_states, responding, wc_state;
} ecat_events_master_t;

typedef struct {
  int known;
  unsigned al_state, online, operational, dc_sync_failed;
} ecat_events_slave_t;

/// Record what changed in the master's state since last time.
void ecat_events_master(ecat_events_t *log, ecat_events_master_t *last, unsigned link_up, unsigned al_states,
    unsigned responding);
/// Record a change of the domain's working counter state.
void ecat_events_wc(ecat_events_t *log, ecat_events_master_t *last, unsigned wc_state);
/// Record what changed in a slave's state since last time.
void ecat_events_slave(ecat_events_t *log, ecat_events_slave_t *last, int position, unsigned al_state, unsigned online,
    unsigned operational, unsigned dc_sync_failed);

/// Wall clock in ns.
uint64_t ecat_events_now(void);

#endif
