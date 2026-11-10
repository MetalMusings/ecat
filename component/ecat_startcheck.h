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

/// Whether a kernel master configuration (the text of /etc/ethercat.conf
/// or /etc/sysconfig/ethercat) gives the kernel master this interface, by
/// name or MAC address (mac may be NULL), or any interface
/// (ff:ff:ff:ff:ff:ff). Returns 1 and the value in what, or 0.
int ecat_kernel_master_uses(const char *conf_text, const char *ifname, const char *mac, char *what, int what_len);

#endif
