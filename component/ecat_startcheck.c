//
//  The start check, see ecat_startcheck.h.
//

#include "ecat_startcheck.h"

#include <ctype.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>

static void say(ecat_report_t report, void *arg, int level, const char *fmt, ...) {
  char text[384];
  va_list ap;

  va_start(ap, fmt);
  vsnprintf(text, sizeof(text), fmt, ap);
  va_end(ap);
  report(level, text, arg);
}

static const char *or_unknown(const char *s) {
  return (s != NULL && s[0]) ? s : "a slave";
}

int ecat_startcheck_compare(const ecat_expect_t *expect, int n_expect, const ecat_found_t *found, int n_found,
    ecat_report_t report, void *arg) {
  int errors = 0, highest = -1, i;

  for (i = 0; i < n_expect; i++) {
    const ecat_expect_t *e = &expect[i];
    if (e->position > highest) highest = e->position;
    if (e->position < 0 || e->position >= n_found) {
      say(report, arg, ECAT_CHECK_ERROR,
          "slave %d (%s, %s) is not on the bus: the bus has %d slave%s", e->position, e->name,
          or_unknown(e->device), n_found, n_found == 1 ? "" : "s");
      errors++;
      continue;
    }
    const ecat_found_t *f = &found[e->position];
    if (f->vid != e->vid || f->pid != e->pid) {
      say(report, arg, ECAT_CHECK_ERROR,
          "slave %d is %s (vendor 0x%08X, product 0x%08X); the configuration expects %s there "
          "(vendor 0x%08X, product 0x%08X) as %s",
          e->position, or_unknown(f->name), f->vid, f->pid, or_unknown(e->device), e->vid, e->pid, e->name);
      errors++;
    } else if (e->revision != 0 && f->revision != e->revision) {
      say(report, arg, ECAT_CHECK_WARN,
          "slave %d (%s) has revision 0x%08X, the configuration was made with 0x%08X; "
          "check it with EtherCAT Setup if the slave behaves differently",
          e->position, e->name, f->revision, e->revision);
    }
  }
  if (n_found > highest + 1) {
    say(report, arg, ECAT_CHECK_INFO, "the bus has %d slaves; slave %d%s is not in the configuration and stays unused",
        n_found, highest + 1, n_found > highest + 2 ? " and later" : "");
  }
  return errors;
}

// The value of a shell-style assignment, without quotes and blanks.
static void value_of(const char *p, char *out, int len) {
  int n = 0;

  while (*p == ' ' || *p == '\t') p++;
  while (*p && *p != '\n' && *p != '#' && n < len - 1) {
    if (*p != '"' && *p != '\'' && *p != ' ' && *p != '\t' && *p != '\r') out[n++] = *p;
    p++;
  }
  out[n] = 0;
}

int ecat_kernel_master_uses(const char *conf_text, const char *ifname, const char *mac, char *what, int what_len) {
  const char *line = conf_text;

  while (line != NULL && *line) {
    const char *p = line, *eq;
    char key[64], value[128];
    int n = 0;

    while (*p == ' ' || *p == '\t') p++;
    eq = strchr(p, '=');
    if (*p != '#' && eq != NULL && (strchr(p, '\n') == NULL || eq < strchr(p, '\n'))) {
      while (p < eq && n < (int)sizeof(key) - 1 && !isspace((unsigned char)*p)) key[n++] = *p++;
      key[n] = 0;
      // MASTER0_DEVICE, MASTER1_DEVICE, ... and their _BACKUP
      if (!strncmp(key, "MASTER", 6) && isdigit((unsigned char)key[6]) &&
          (strstr(key, "_DEVICE") != NULL || strstr(key, "_BACKUP") != NULL)) {
        value_of(eq + 1, value, sizeof(value));
        if (value[0] && (!strcmp(value, ifname) || (mac != NULL && !strcasecmp(value, mac)) ||
                            !strcasecmp(value, "ff:ff:ff:ff:ff:ff"))) {
          if (what != NULL && what_len > 0) snprintf(what, what_len, "%s=%s", key, value);
          return 1;
        }
      }
    }
    line = strchr(line, '\n');
    if (line != NULL) line++;
  }
  return 0;
}
