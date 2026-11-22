//
//  The start check: before the slaves are configured, the component
//  compares the bus with ethercat.yaml and stops with a clear message if
//  they differ, and refuses to share the network card with the kernel
//  master.
//
//  The comparison and the reading of the kernel master's configuration are
//  plain functions without the master, so that they can be unit tested.
//

#ifndef _ECAT_STARTCHECK_H_
#define _ECAT_STARTCHECK_H_

#include <stdint.h>

#define ECAT_CHECK_INFO 0
#define ECAT_CHECK_WARN 1
#define ECAT_CHECK_ERROR 2

/// A slave as the configuration expects it.
typedef struct {
  int position;
  uint32_t vid, pid, revision;  ///< revision 0: not checked
  const char *device;           ///< the device name in the file, may be NULL
  const char *name;             ///< the pin prefix
} ecat_expect_t;

/// A slave as found on the bus.
typedef struct {
  uint32_t vid, pid, revision;
  char name[64];  ///< the slave's own name from its SII
} ecat_found_t;

typedef void (*ecat_report_t)(int level, const char *text, void *arg);

/// Compare the configuration's slaves with the bus. Each difference is
/// reported; returns the number of errors.
int ecat_startcheck_compare(const ecat_expect_t *expect, int n_expect, const ecat_found_t *found, int n_found,
    ecat_report_t report, void *arg);

/// A PDO entry and a PDO, for comparing mappings without the master's headers.
typedef struct {
  uint16_t index;
  uint8_t subindex;
  uint8_t bits;
} ecat_pdo_entry_t;

typedef struct {
  uint8_t sm;
  uint16_t index;
  int n_entries;
  const ecat_pdo_entry_t *entries;
} ecat_pdo_t;

/// A slave with configure-pdos: false runs on its own PDO mapping, which
/// ecat does not write: compare the file's PDOs (`want`) with the slave's, as
/// the master read them at its scan (`have`). For each sync manager of the
/// file, the slave must have the same PDOs in the same order with the same
/// entries (a sync manager only the slave uses does not matter). Each
/// difference is reported as an error; returns the number of errors.
int ecat_startcheck_own_pdos(int position, const char *name, const ecat_pdo_t *want, int n_want,
    const ecat_pdo_t *have, int n_have, ecat_report_t report, void *arg);

/// Whether a kernel master configuration (the text of /etc/ethercat.conf
/// or /etc/sysconfig/ethercat) gives the kernel master this interface, by
/// name or MAC address (mac may be NULL), or any interface
/// (ff:ff:ff:ff:ff:ff). Returns 1 and the value in what, or 0.
int ecat_kernel_master_uses(const char *conf_text, const char *ifname, const char *mac, char *what, int what_len);

/// The DC reference clock, after ETG.1020 (21.1): "The first device that
/// should be synchronized is used as the reference clock". Of the
/// configured slaves (positions, and whether each has `dc:` with an
/// assign-activate other than 0x0000), the one with DC that comes first on
/// the bus; slaves the file does not use (`unused`,
/// or not in it) are never chosen, where the master's own choice is the
/// first DC-capable slave on the bus, used or not. Returns the index into
/// the arrays, or -1 when no slave has DC (the master's choice stands).
int ecat_ref_clock_choice(const int *position, const int *has_dc, int n);

#endif
