//
//    ecat: read the configuration file (ethercat.yaml).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

/// @file
/// @brief Read ethercat.yaml into lcec's configuration records. See ecat_config.h.

#include "ecat_config.h"

#include <errno.h>
#include <stdarg.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <arpa/inet.h>
#include <yaml.h>

#include "devices/ecat_cia402.h"
#include "devices/ecat_encoder.h"
#include "devices/ecat_serial.h"
#include "lcec.h"

#define ECAT_FORMAT_VERSION 1

typedef struct {
  char *data;
  size_t len, cap;
} buf_t;

#define MAX_ENCODERS 16

typedef struct {
  yaml_document_t *doc;
  const char *path;
  char where[64];  // "slave 3", for messages
  int errors;
  int cia402;         // the slave being read has the cia402 profile
  unsigned int cia402_modes;  // the other modes it may switch to (bit n: mode n)
  unsigned int cia402_all;    // mode and modes
  int n_axes;         // the drive's axes (cia402.axes), 1 without
  int ff_velocity;    // cia402.feedforward.velocity is given
  int ff_torque;      // cia402.feedforward.torque is given
  unsigned int pp_limits[ECAT_CIA402_MAX_AXES];  // per axis: settings 0x607D:01, 0x607D:02 seen (bits 0-1)
  int mode_setting[ECAT_CIA402_MAX_AXES];        // per axis: the setting 0x6060:00's value, -1000 without
  unsigned int owned[ECAT_CIA402_MAX_AXES];      // per axis: which of the profile's objects it has (bits of cia402_objects)
  int n_enc;          // the slave's encoder channels
  uint16_t enc_index[MAX_ENCODERS];   // their status indexes
  unsigned int enc_owned[MAX_ENCODERS];  // which of enc_objects each has
  int enc_bits[MAX_ENCODERS];            // their counters' bits
  char enc_name[MAX_ENCODERS][LCEC_CONF_STR_MAXLEN];
  int n_ser;          // the slave's serial channels
  uint16_t ser_pdo[ECAT_MP_SERIAL_MAX_CHANNELS][2];   // their output and input PDOs
  int ser_bits[ECAT_MP_SERIAL_MAX_CHANNELS][2];       // the PDOs' bits (0: not in the process data)
  uint16_t ser_settings[ECAT_MP_SERIAL_MAX_CHANNELS]; // their settings indexes
  uint8_t ser_baud[ECAT_MP_SERIAL_MAX_CHANNELS], ser_frame[ECAT_MP_SERIAL_MAX_CHANNELS];  // at start
  uint8_t ser_continuous[ECAT_MP_SERIAL_MAX_CHANNELS];  // send-continuous
  char ser_name[ECAT_MP_SERIAL_MAX_CHANNELS][LCEC_CONF_STR_MAXLEN];
} ctx_t;

// ---- the cia402 profile's objects ----

static const struct {
  uint16_t index;
  int bits;
  int output;
} cia402_objects[] = {
    {0x603F, 16, 0},  // error code
    {0x6040, 16, 1},  // control word
    {0x6041, 16, 0},  // status word
    {0x6060, 8, 1},   // modes of operation
    {0x6061, 8, 0},   // modes of operation display
    {0x6064, 32, 0},  // position actual value
    {0x606C, 32, 0},  // velocity actual value
    {0x607A, 32, 1},  // target position
    {0x6081, 32, 1},  // profile velocity (PP)
    {0x60FF, 32, 1},  // target velocity
    {0x60B1, 32, 1},  // velocity offset (feedforward in CSP)
    {0x60B2, 16, 1},  // torque offset (feedforward in CSP)
};
#define N_CIA402_OBJECTS (int)(sizeof(cia402_objects) / sizeof(cia402_objects[0]))

static int cia402_object(uint16_t index, uint8_t subindex) {
  int i;
  for (i = 0; i < N_CIA402_OBJECTS; i++)
    if (cia402_objects[i].index == index && subindex == 0) return i;
  return -1;
}

/// The axis of a drive's object (index + axis * 0x800), or -1.
static int cia402_axis(ctx_t *c, uint16_t index, uint16_t base) {
  int a;
  for (a = 0; a < c->n_axes; a++)
    if (index == base + a * ECAT_CIA402_AXIS_OFFSET) return a;
  return -1;
}

// ---- encoder channels' objects (MDP 511), at the status index S and
// the control index S + 0x1000 ----

static const struct {
  uint8_t subindex;
  int output;
  int bits;  // 0: 16 or 32
} enc_objects[] = {
    {0x01, 0, 1},  // latch C valid
    {0x02, 0, 1},  // latch extern valid
    {0x03, 0, 1},  // set counter done
    {0x11, 0, 0},  // counter value
    {0x12, 0, 0},  // latch value
    {0x01, 1, 1},  // enable latch C
    {0x02, 1, 1},  // enable latch extern on positive edge
    {0x03, 1, 1},  // set counter
    {0x04, 1, 1},  // enable latch extern on negative edge
    {0x11, 1, 0},  // set counter value
};
#define N_ENC_OBJECTS (int)(sizeof(enc_objects) / sizeof(enc_objects[0]))
#define ENC_COUNTER 3

/// The encoder object at index:subindex: its number in enc_objects, and
/// the channel in *ch; or -1.
static int enc_object(ctx_t *c, uint16_t index, uint8_t subindex, int *ch) {
  int i, k;
  for (i = 0; i < c->n_enc; i++) {
    int output = index == c->enc_index[i] + 0x1000;
    if (index != c->enc_index[i] && !output) continue;
    for (k = 0; k < N_ENC_OBJECTS; k++) {
      if (enc_objects[k].output == output && enc_objects[k].subindex == subindex) {
        *ch = i;
        return k;
      }
    }
  }
  return -1;
}

static void error(ctx_t *c, const char *fmt, ...) {
  char msg[256];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(msg, sizeof(msg), fmt, ap);
  va_end(ap);
  rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s: %s%s%s\n", c->path, c->where, c->where[0] ? ": " : "", msg);
  c->errors++;
}

static void *append(buf_t *b, size_t size) {
  void *p;
  if (b->len + size > b->cap) {
    size_t cap = b->cap ? b->cap * 2 : 4096;
    while (cap < b->len + size) cap *= 2;
    char *n = realloc(b->data, cap);
    if (n == NULL) return NULL;
    b->data = n;
    b->cap = cap;
  }
  p = b->data + b->len;
  memset(p, 0, size);
  b->len += size;
  return p;
}

// ---- YAML access ----

static yaml_node_t *node(ctx_t *c, int id) { return id ? yaml_document_get_node(c->doc, id) : NULL; }

static const char *scalar(yaml_node_t *n) {
  return (n != NULL && n->type == YAML_SCALAR_NODE) ? (const char *)n->data.scalar.value : NULL;
}

static int is_null(yaml_node_t *n) {
  const char *s = scalar(n);
  return n == NULL || (s != NULL && (!strcmp(s, "null") || !strcmp(s, "~") || s[0] == 0));
}

static yaml_node_t *get(ctx_t *c, yaml_node_t *map, const char *key) {
  yaml_node_pair_t *p;
  if (map == NULL || map->type != YAML_MAPPING_NODE) return NULL;
  for (p = map->data.mapping.pairs.start; p < map->data.mapping.pairs.top; p++) {
    const char *k = scalar(node(c, p->key));
    if (k != NULL && !strcmp(k, key)) return node(c, p->value);
  }
  return NULL;
}

static int seq_len(yaml_node_t *n) {
  return (n != NULL && n->type == YAML_SEQUENCE_NODE) ? (int)(n->data.sequence.items.top - n->data.sequence.items.start) : 0;
}

static yaml_node_t *seq_item(ctx_t *c, yaml_node_t *n, int i) { return node(c, n->data.sequence.items.start[i]); }

static int is_seq(yaml_node_t *n) { return n != NULL && n->type == YAML_SEQUENCE_NODE; }

/// Whether `name` is in the list `invert` (a sequence of names, or NULL).
static int inverted(ctx_t *c, yaml_node_t *invert, const char *name) {
  int i;
  for (i = 0; name != NULL && i < seq_len(invert); i++) {
    const char *n = scalar(seq_item(c, invert, i));
    if (n != NULL && !strcmp(n, name)) return 1;
  }
  return 0;
}

static const char *get_str(ctx_t *c, yaml_node_t *map, const char *key) { return scalar(get(c, map, key)); }

/// An integer ("12", "0x0300"); def if missing, an error if not a number.
static long long get_int(ctx_t *c, yaml_node_t *map, const char *key, long long def) {
  const char *s = get_str(c, map, key);
  char *end;
  long long v;
  if (s == NULL) return def;
  errno = 0;
  v = strtoll(s, &end, 0);
  if (errno || *end || end == s) {
    error(c, "%s: \"%s\" is not a number", key, s);
    return def;
  }
  return v;
}

static double get_float(ctx_t *c, yaml_node_t *map, const char *key, double def) {
  const char *s = get_str(c, map, key);
  char *end;
  double v;
  if (s == NULL) return def;
  v = strtod(s, &end);
  if (*end || end == s) {
    error(c, "%s: \"%s\" is not a number", key, s);
    return def;
  }
  return v;
}

static int get_bool(ctx_t *c, yaml_node_t *map, const char *key, int def) {
  const char *s = get_str(c, map, key);
  if (s == NULL) return def;
  if (!strcmp(s, "true") || !strcmp(s, "True") || !strcmp(s, "yes")) return 1;
  if (!strcmp(s, "false") || !strcmp(s, "False") || !strcmp(s, "no")) return 0;
  error(c, "%s: \"%s\" is not true or false", key, s);
  return def;
}

/// "0x6040:00" -> index and subindex.
static int parse_object(ctx_t *c, const char *s, uint16_t *index, uint8_t *subindex) {
  unsigned int i, sub;
  char extra;
  if (s == NULL || sscanf(s, "0x%x:%x%c", &i, &sub, &extra) != 2 || i > 0xffff || sub > 0xff) {
    error(c, "\"%s\" is not an object such as \"0x6040:00\"", s ? s : "");
    return -1;
  }
  *index = i;
  *subindex = sub;
  return 0;
}

static void copy_str(char *dst, const char *src) {
  strncpy(dst, src ? src : "", LCEC_CONF_STR_MAXLEN - 1);
  dst[LCEC_CONF_STR_MAXLEN - 1] = 0;
}

/// A cia402 mode's name -> its number, or 0.
static int cia402_mode_number(const char *name) {
  static const struct {
    const char *name;
    int number;
  } modes[] = {{"pp", 1}, {"pv", 3}, {"hm", 6}, {"csp", 8}, {"csv", 9}};
  unsigned int i;
  for (i = 0; name != NULL && i < sizeof(modes) / sizeof(modes[0]); i++)
    if (!strcmp(modes[i].name, name)) return modes[i].number;
  return 0;
}

/// cia402.modes: [hm, pv] -> a bit mask, bit n for mode n.
static unsigned int cia402_modes(ctx_t *c, yaml_node_t *list) {
  unsigned int m = 0;
  int i;
  if (list == NULL || is_null(list)) return 0;
  if (list->type != YAML_SEQUENCE_NODE) {
    error(c, "cia402.modes must be a list, such as [hm]");
    return 0;
  }
  for (i = 0; i < seq_len(list); i++) {
    const char *name = scalar(seq_item(c, list, i));
    int n = cia402_mode_number(name);
    if (n) {
      m |= 1u << n;
    } else {
      error(c, "cia402.modes: \"%s\" is not supported (csp, csv, pv, pp, hm)", name ? name : "");
    }
  }
  return m;
}

// ---- data types ----

typedef struct {
  const char *name;
  int bytes;
  int is_signed;
  int is_float;
} dtype_t;

static const dtype_t dtypes[] = {
    {"BOOL", 1, 0, 0},
    {"SINT", 1, 1, 0},
    {"USINT", 1, 0, 0},
    {"INT", 2, 1, 0},
    {"UINT", 2, 0, 0},
    {"DINT", 4, 1, 0},
    {"UDINT", 4, 0, 0},
    {"LINT", 8, 1, 0},
    {"ULINT", 8, 0, 0},
    {"REAL", 4, 1, 1},
    {"LREAL", 8, 1, 1},
    {NULL},
};

static const dtype_t *find_dtype(const char *name) {
  const dtype_t *t;
  for (t = dtypes; name != NULL && t->name != NULL; t++)
    if (!strcmp(t->name, name)) return t;
  return NULL;
}

// ---- process data ----

/// Fill one PDO entry record (and complex records for "split"); count pins.
static void pdo_entry(ctx_t *c, buf_t *b, yaml_node_t *e, int output, unsigned int *pins, int ser) {
  LCEC_CONF_PDOENTRY_T *pe;
  yaml_node_t *split, *invert;
  const char *hal = get_str(c, e, "hal-type");
  const char *use = get_str(c, e, "use");
  const char *pin = get_str(c, e, "pin");
  const char *object = get_str(c, e, "object");
  const dtype_t *dt = find_dtype(get_str(c, e, "type"));
  long long bits = get_int(c, e, "bits", 0);
  uint16_t index = 0;
  uint8_t subindex = 0;
  int i, k, ch = 0, a, ax;

  if ((pe = append(b, sizeof(*pe))) == NULL) {
    error(c, "out of memory");
    return;
  }
  pe->confType = lcecConfTypePdoEntry;
  if (parse_object(c, object, &index, &subindex) < 0) return;
  if (bits < 1 || bits > 64) {
    error(c, "%s: bits must be 1 to 64", object);
    return;
  }
  pe->index = index;
  pe->subindex = subindex;
  pe->bitLength = bits;
  pe->subType = lcecPdoEntTypeSimple;
  pe->floatScale = 1.0;
  pe->floatOffset = 0.0;
  if (use != NULL && !strcmp(use, "gap")) {
    if (ser >= 0) c->ser_bits[ser][!output] += bits;
    return;
  }
  // a serial channel's PDO: all of it is the function's
  if (ser >= 0) {
    if (use == NULL || strcmp(use, "profile") || pin != NULL || get(c, e, "split") != NULL) {
      error(c, "%s: serial channel %s handles this PDO; give its entries \"use: profile\"", object, c->ser_name[ser]);
    }
    c->ser_bits[ser][!output] += bits;
    return;
  }
  // a drive's object, of axis ax (index + ax * 0x800)
  i = -1;
  ax = 0;
  for (a = 0; a < (c->cia402 ? c->n_axes : 1) && i < 0; a++) {
    if ((i = cia402_object((uint16_t)(index - a * ECAT_CIA402_AXIS_OFFSET), subindex)) >= 0) ax = a;
  }
  k = enc_object(c, index, subindex, &ch);
  if (use != NULL && !strcmp(use, "profile")) {
    if (pin != NULL || get(c, e, "split") != NULL) {
      error(c, "%s: an entry with \"use: profile\" has no pin", object);
    } else if (c->cia402 && i >= 0) {
      if (cia402_objects[i].output != output) {
        error(c, "%s: belongs in the %s", object, cia402_objects[i].output ? "outputs" : "inputs");
      } else if (cia402_objects[i].bits != bits) {
        error(c, "%s: the cia402 profile needs %d bits", object, cia402_objects[i].bits);
      } else {
        c->owned[ax] |= 1u << i;
      }
    } else if (k >= 0) {
      if (enc_objects[k].output != output) {
        error(c, "%s: belongs in the %s", object, enc_objects[k].output ? "outputs" : "inputs");
      } else if (enc_objects[k].bits ? bits != enc_objects[k].bits : bits != 16 && bits != 32) {
        error(c, "%s: the encoder needs %s bits", object, enc_objects[k].bits ? "1" : "16 or 32");
      } else {
        c->enc_owned[ch] |= 1u << k;
        if (enc_objects[k].bits == 0) {
          if (c->enc_bits[ch] && c->enc_bits[ch] != bits) error(c, "%s: the encoder's counter, latch and set value need the same bits", object);
          c->enc_bits[ch] = bits;
        }
      }
    } else if (c->cia402) {
      error(c, "%s: the cia402 profile does not handle this object; give it a pin", object);
    } else if (c->n_enc) {
      error(c, "%s: no encoder channel handles this object; give it a pin", object);
    } else {
      error(c, "%s: \"use: profile\" needs profile cia402, an encoder channel or a serial channel's PDO", object);
    }
    return;
  } else if (use != NULL) {
    error(c, "%s: unknown use \"%s\" (profile, gap)", object, use);
    return;
  }
  if (c->cia402 && i >= 0) {
    error(c, "%s: the cia402 profile handles this object; give it \"use: profile\" instead of a pin", object);
    return;
  }
  if (k >= 0) {
    error(c, "%s: encoder channel %s handles this object; give it \"use: profile\" instead of a pin", object, c->enc_name[ch]);
    return;
  }
  if (dt == NULL) {
    error(c, "%s: unknown type \"%s\"", object, get_str(c, e, "type") ? get_str(c, e, "type") : "");
    return;
  }

  if (pin != NULL) {
    copy_str(pe->halPin, pin);
    // 2.10 names: bool, real, sint, uint. The 2.9 spellings (bit, float, s32,
    // u32) are the same types: files written for 2.9 keep working.
    const char *hal_as_written = hal;
    if (hal != NULL) {
      if (!strcmp(hal, "bit")) hal = "bool";
      else if (!strcmp(hal, "float")) hal = "real";
      else if (!strcmp(hal, "s32")) hal = "sint";
      else if (!strcmp(hal, "u32")) hal = "uint";
    }
    if (dt->bytes == 8 && !dt->is_float) {
#if ECAT_HAL_64
      // LINT, ULINT: an integer pin of the same sign has all 64 bits; a real
      // pin carries them up to 2^53
      // (s32 and u32 meant 32-bit pins: not for these)
      if (hal == NULL || !strcmp(hal_as_written, "s32") || !strcmp(hal_as_written, "u32") ||
          (strcmp(hal, "real") && strcmp(hal, dt->is_signed ? "sint" : "uint"))) {
        error(c, "%s: a %s entry takes hal-type %s (or real)", object, dt->name, dt->is_signed ? "sint" : "uint");
        return;
      }
#else
      // LinuxCNC 2.9: only a real pin (up to 2^53) carries them
      if (hal == NULL || strcmp(hal, "real")) {
        error(c, "%s: LinuxCNC 2.9 has no 64-bit pins: a %s entry takes hal-type real", object, dt->name);
        return;
      }
#endif
    }
    if (hal != NULL && !strcmp(hal, "bool")) {
      pe->halType = HAL_BOOL;
      pe->invert = get_bool(c, e, "invert", 0);
    } else if (hal != NULL && !strcmp(hal, "sint")) {
      pe->halType = HAL_SINT;
    } else if (hal != NULL && !strcmp(hal, "uint")) {
      pe->halType = HAL_UINT;
    } else if (hal != NULL && !strcmp(hal, "real")) {
      pe->halType = HAL_REAL;
      if (dt->is_float) {
        pe->subType = dt->bytes == 8 ? lcecPdoEntTypeFloatDoubleIeee : lcecPdoEntTypeFloatIeee;
      } else {
        pe->subType = dt->is_signed ? lcecPdoEntTypeFloatSigned : lcecPdoEntTypeFloatUnsigned;
        pe->floatScale = get_float(c, e, "scale", 1.0);
        pe->floatOffset = get_float(c, e, "offset", 0.0);
      }
    } else {
      error(c, "%s: hal-type \"%s\" is not supported (bool, sint, uint, real)", object, hal ? hal : "");
      return;
    }
    if (get_bool(c, e, "raw", 0)) {
      // a bit pattern (digital outputs): too wide a value is cut, not held at the top
      if (strcmp(hal, "uint") || bits > 32) {
        error(c, "%s: raw is for an entry of up to 32 bits with hal-type uint", object);
        return;
      }
      pe->raw = 1;
    }
    if (get_bool(c, e, "extend", 0)) {
      // a counter that wraps at its width: the pin keeps counting, in 64 bits
      if (output || strcmp(hal, "sint") || bits < 8 || bits > 32) {
        error(c, "%s: extend is for an input entry of 8 to 32 bits with hal-type sint", object);
        return;
      }
#if !ECAT_HAL_64
      if (bits == 32) {
        // the pin wraps at 32 bits as the entry does: nothing to extend
        error(c, "%s: LinuxCNC 2.9's pins have 32 bits: extend is for an entry of 8 to 16 bits there", object);
        return;
      }
#endif
      pe->extend = 1;
    }
    (*pins)++;
  }

  split = get(c, e, "split");
  invert = get(c, e, "invert");
  if (pin == NULL && invert != NULL) {
    // a list of the split's pin names to invert
    if (split == NULL || !is_seq(invert)) {
      error(c, "%s: invert is true or false for a bit pin, a list of names for a split", object);
      return;
    }
    for (i = 0; i < seq_len(invert); i++) {
      const char *name = scalar(seq_item(c, invert, i));
      int found = 0, j;
      for (j = 0; name != NULL && j < seq_len(split); j++) {
        const char *sn = scalar(seq_item(c, split, j));
        if (sn != NULL && !strcmp(sn, name)) found = 1;
      }
      if (!found) error(c, "%s: invert lists %s, which is not in split", object, name ? name : "");
    }
  } else if (pin != NULL && invert != NULL && (hal == NULL || (strcmp(hal, "bit") && strcmp(hal, "bool")))) {
    error(c, "%s: invert is for bit pins", object);
  }
  if (split != NULL) {
    if (pin != NULL) {
      error(c, "%s: give pin or split, not both", object);
      return;
    }
    if (seq_len(split) > bits) {
      error(c, "%s: split has more pins than bits", object);
      return;
    }
    pe->subType = lcecPdoEntTypeComplex;
    for (i = 0; i < seq_len(split); i++) {
      yaml_node_t *bit = seq_item(c, split, i);
      LCEC_CONF_COMPLEXENTRY_T *ce;
      if (is_null(bit)) continue;
      if ((ce = append(b, sizeof(*ce))) == NULL) {
        error(c, "out of memory");
        return;
      }
      ce->confType = lcecConfTypeComplexEntry;
      ce->bitOffset = i;
      ce->bitLength = 1;
      ce->subType = lcecPdoEntTypeSimple;
      ce->halType = HAL_BOOL;
      ce->floatScale = 1.0;
      copy_str(ce->halPin, scalar(bit));
      ce->invert = inverted(c, invert, scalar(bit));
      (*pins)++;
    }
  }
}

/// Sync managers, PDOs and entries of one slave, grouped by sync manager in
/// the order they first appear.
static void process_data(ctx_t *c, buf_t *b, yaml_node_t *pd, LCEC_CONF_SLAVE_T *s, size_t slave_off) {
  static const char *dirs[] = {"outputs", "inputs"};
  int seen[256] = {0};
  int d, i, j, k;

  for (d = 0; d < 2; d++) {
    yaml_node_t *pdos = get(c, pd, dirs[d]);
    for (i = 0; i < seq_len(pdos); i++) {
      long long sm = get_int(c, seq_item(c, pdos, i), "sm", -1);
      size_t sm_off;
      LCEC_CONF_SYNCMANAGER_T *smc;
      if (sm < 0 || sm > 255) {
        error(c, "%s PDO %d: sm must be 0 to 255", dirs[d], i);
        continue;
      }
      if (seen[sm]) {
        // the other direction's PDOs on it would be left out
        if (seen[sm] != d + 1 && seen[sm] != 3) {
          error(c, "sm %lld is used for outputs and inputs", sm);
          seen[sm] = 3;
        }
        continue;
      }
      seen[sm] = d + 1;
      sm_off = b->len;
      if ((smc = append(b, sizeof(*smc))) == NULL) {
        error(c, "out of memory");
        return;
      }
      smc->confType = lcecConfTypeSyncManager;
      smc->index = sm;
      smc->dir = d == 0 ? EC_DIR_OUTPUT : EC_DIR_INPUT;
      ((LCEC_CONF_SLAVE_T *)(b->data + slave_off))->syncManagerCount++;

      // every PDO of this direction with this sync manager
      for (j = i; j < seq_len(pdos); j++) {
        yaml_node_t *pdo = seq_item(c, pdos, j);
        yaml_node_t *entries = get(c, pdo, "entries");
        size_t pdo_off;
        LCEC_CONF_PDO_T *pc;
        unsigned int pins = 0;
        if (get_int(c, pdo, "sm", -1) != sm) continue;
        pdo_off = b->len;
        if ((pc = append(b, sizeof(*pc))) == NULL) {
          error(c, "out of memory");
          return;
        }
        pc->confType = lcecConfTypePdo;
        pc->index = get_int(c, pdo, "pdo", 0);
        pc->pdoEntryCount = seq_len(entries);
        ((LCEC_CONF_SYNCMANAGER_T *)(b->data + sm_off))->pdoCount++;
        {
          int ser = -1, q;
          for (q = 0; q < c->n_ser; q++) {
            if (c->ser_pdo[q][0] == pc->index || c->ser_pdo[q][1] == pc->index) ser = q;
          }
          if (ser >= 0 && c->ser_pdo[ser][d] != pc->index) {
            error(c, "serial channel %s: PDO 0x%04X is in the %s", c->ser_name[ser], pc->index, dirs[d]);
            ser = -1;
          }
          for (k = 0; k < seq_len(entries); k++) pdo_entry(c, b, seq_item(c, entries, k), d == 0, &pins, ser);
        }
        s = (LCEC_CONF_SLAVE_T *)(b->data + slave_off);  // the buffer may have moved
        s->pdoCount++;
        s->pdoEntryCount += ((LCEC_CONF_PDO_T *)(b->data + pdo_off))->pdoEntryCount;
        s->pdoMappingCount += pins;
      }
    }
  }
}

// ---- settings ----

static void settings(ctx_t *c, buf_t *b, yaml_node_t *list, size_t slave_off) {
  int i, ax;
  for (i = 0; i < seq_len(list); i++) {
    yaml_node_t *st = seq_item(c, list, i);
    const char *object = get_str(c, st, "object");
    const char *hex = get_str(c, st, "data");
    uint8_t data[64];
    size_t len = 0;
    uint16_t index;
    uint8_t subindex;
    LCEC_CONF_SDOCONF_T *sc;

    if (parse_object(c, object, &index, &subindex) < 0) continue;
    {
      int q;
      for (q = 0; q < c->n_ser; q++) {
        if (index == c->ser_settings[q] &&
            (subindex == ECAT_SER_SUB_BAUD || subindex == ECAT_SER_SUB_FRAME || subindex == ECAT_SER_SUB_CONTINUOUS)) {
          error(c, "setting %s: the baud rate, data frame and send continuous of serial channel %s are its baud, frame "
                   "and send-continuous", object, c->ser_name[q]);
        }
      }
    }
    if ((ax = cia402_axis(c, index, 0x607D)) >= 0 && (subindex == 1 || subindex == 2)) c->pp_limits[ax] |= 1u << (subindex - 1);
    if ((ax = cia402_axis(c, index, 0x6060)) >= 0 && subindex == 0 && get(c, st, "value") != NULL)
      c->mode_setting[ax] = (int)get_int(c, st, "value", 0);
    if (get(c, st, "value") != NULL) {
      const dtype_t *dt = find_dtype(get_str(c, st, "type"));
      if (dt == NULL) {
        error(c, "setting %s: unknown type", object);
        continue;
      }
      len = dt->bytes;
      if (dt->is_float) {
        double v = get_float(c, st, "value", 0);
        if (len == 4) {
          float f = v;
          memcpy(data, &f, 4);  // little endian on the machines LinuxCNC runs on
        } else {
          memcpy(data, &v, 8);
        }
      } else {
        unsigned long long v = (unsigned long long)get_int(c, st, "value", 0);
        size_t n;
        for (n = 0; n < len; n++) data[n] = (v >> (8 * n)) & 0xff;
      }
    } else if (hex != NULL) {
      const char *p = hex;
      unsigned int byte;
      int used;
      while (*p) {
        if (*p == ' ') {
          p++;
          continue;
        }
        if (len >= sizeof(data) || sscanf(p, "%2x%n", &byte, &used) != 1 || used != 2) {
          error(c, "setting %s: data \"%s\" is not hex bytes", object, hex);
          len = 0;
          break;
        }
        data[len++] = byte;
        p += 2;
      }
      if (len == 0) continue;
    } else {
      error(c, "setting %s: give value and type, or data", object);
      continue;
    }
    if ((sc = append(b, sizeof(*sc) + len)) == NULL) {
      error(c, "out of memory");
      return;
    }
    sc->confType = lcecConfTypeSdoConfig;
    sc->index = index;
    sc->subindex = subindex;
    sc->length = len;
    memcpy(sc->data, data, len);
    ((LCEC_CONF_SLAVE_T *)(b->data + slave_off))->sdoConfigLength += sizeof(*sc) + len;
  }
}

// ---- slaves and master ----

/// The slave's encoder channels: [{name: enc, index: '0x6010'}, ...]; a
/// modParam record each.
static void encoders(ctx_t *c, buf_t *b, yaml_node_t *list, size_t slave_off) {
  int i, j, n;

  c->n_enc = 0;
  if (list == NULL || is_null(list)) return;
  if (list->type != YAML_SEQUENCE_NODE) {
    error(c, "encoders must be a list");
    return;
  }
  n = seq_len(list);
  for (i = 0; i < n; i++) {
    yaml_node_t *e = seq_item(c, list, i);
    const char *name = get_str(c, e, "name");
    long long index = get_int(c, e, "index", -1);
    LCEC_CONF_MODPARAM_T *mp;

    if (c->n_enc == MAX_ENCODERS) {
      error(c, "more than %d encoder channels", MAX_ENCODERS);
      return;
    }
    if (name == NULL || !name[0] || strlen(name) >= LCEC_CONF_STR_MAXLEN) {
      error(c, "encoder %d: name missing or too long", i + 1);
      continue;
    }
    if (index < 0x6000 || index > 0x6FF0 || (index & 0xF) != 0) {
      error(c, "encoder %s: index must be the channel's status index, 0x6000 to 0x6FF0 in steps of 0x10", name);
      continue;
    }
    for (j = 0; j < c->n_enc; j++) {
      if (c->enc_index[j] == index) error(c, "encoder %s: index 0x%04llX is used twice", name, index);
      if (!strcmp(c->enc_name[j], name)) error(c, "encoder %s: the name is used twice", name);
    }
    if ((mp = append(b, sizeof(*mp))) == NULL) {
      error(c, "out of memory");
      return;
    }
    mp->confType = lcecConfTypeModParam;
    mp->id = ECAT_MP_ENCODER + (int)((index - 0x6000) / 0x10);
    copy_str(mp->name, "encoder");
    copy_str(mp->value.str, name);
    ((LCEC_CONF_SLAVE_T *)(b->data + slave_off))->modParamCount++;

    c->enc_index[c->n_enc] = index;
    c->enc_owned[c->n_enc] = 0;
    c->enc_bits[c->n_enc] = 0;
    copy_str(c->enc_name[c->n_enc], name);
    c->n_enc++;
  }
}

static LCEC_CONF_MODPARAM_T *modparam(ctx_t *c, buf_t *b, size_t slave_off, int id, const char *name) {
  LCEC_CONF_MODPARAM_T *mp = append(b, sizeof(*mp));
  if (mp == NULL) {
    error(c, "out of memory");
    return NULL;
  }
  mp->confType = lcecConfTypeModParam;
  mp->id = id;
  copy_str(mp->name, name);
  ((LCEC_CONF_SLAVE_T *)(b->data + slave_off))->modParamCount++;
  return mp;
}

/// The slave's serial channels: [{name: com, output-pdo: '0x1604',
/// input-pdo: '0x1A04', port: /dev/ttyECAT0, baud: 19200, frame: 8E1}, ...];
/// five modParam records each (ecat_serial.h).
static void serial_channels(ctx_t *c, buf_t *b, yaml_node_t *list, size_t slave_off) {
  int i, j, n;
  c->n_ser = 0;
  if (list == NULL || is_null(list)) return;
  if (list->type != YAML_SEQUENCE_NODE) {
    error(c, "serial must be a list");
    return;
  }
  n = seq_len(list);
  for (i = 0; i < n; i++) {
    yaml_node_t *e = seq_item(c, list, i);
    const char *name = get_str(c, e, "name");
    const char *port = get_str(c, e, "port");
    const char *frame = get_str(c, e, "frame");
    long long out = get_int(c, e, "output-pdo", -1), in = get_int(c, e, "input-pdo", -1);
    long long settings_index = get_int(c, e, "settings-index", 0x8000 + 0x10 * i);
    long long baud = get_int(c, e, "baud", 9600);
    uint8_t baud_value, frame_value = 0;
    LCEC_CONF_MODPARAM_T *mp;
    int base = ECAT_MP_SERIAL + 8 * c->n_ser;
    if (c->n_ser == ECAT_MP_SERIAL_MAX_CHANNELS) {
      error(c, "more than %d serial channels", ECAT_MP_SERIAL_MAX_CHANNELS);
      return;
    }
    if (name == NULL || !name[0] || strlen(name) >= LCEC_CONF_STR_MAXLEN) {
      error(c, "serial channel %d: name missing or too long", i + 1);
      continue;
    }
    if (out < 0x1600 || out > 0x17FF || in < 0x1A00 || in > 0x1BFF) {
      error(c, "serial channel %s: output-pdo (0x1600-0x17FF) and input-pdo (0x1A00-0x1BFF) are needed", name);
      continue;
    }
    if (port == NULL || port[0] != '/' || strlen(port) >= LCEC_CONF_STR_MAXLEN) {
      error(c, "serial channel %s: port must be a full path shorter than %d characters, such as /dev/ttyECAT0", name,
          LCEC_CONF_STR_MAXLEN);
      continue;
    }
    if (settings_index < 0x8000 || settings_index > 0x8FF0) {
      error(c, "serial channel %s: settings-index must be 0x8000 to 0x8FF0", name);
      continue;
    }
    if ((baud_value = ecat_ser_baud_value((unsigned long)baud)) == 0) {
      error(c, "serial channel %s: baud %lld is not one of 300, 600, 1200, 2400, 4800, 9600, 12000, 14400, 19200, 38400, 57600, 115200",
          name, baud);
      continue;
    }
    {
      // 8N1, 8E1, 7O2, ...
      const char *f = frame != NULL ? frame : "8N1";
      if (strlen(f) == 3 && (f[0] == '7' || f[0] == '8') && (f[2] == '1' || f[2] == '2'))
        frame_value = ecat_ser_frame_value(f[0] - '0', f[1], f[2] - '0');
      if (frame_value == 0) {
        error(c, "serial channel %s: frame \"%s\" is not one of 7E1, 7O1, 8N1, 8E1, 8O1, 7N2, 7E2, 7O2, 8N2, 8E2, 8O2", name, f);
        continue;
      }
    }
    for (j = 0; j < c->n_ser; j++) {
      if (!strcmp(c->ser_name[j], name)) error(c, "serial channel %s: the name is used twice", name);
      if (c->ser_pdo[j][0] == out || c->ser_pdo[j][1] == in) error(c, "serial channel %s: its PDOs are another channel's", name);
      if (c->ser_settings[j] == settings_index) error(c, "serial channel %s: settings-index is another channel's", name);
    }
    if ((mp = modparam(c, b, slave_off, base + ECAT_MP_SERIAL_NAME, "serial.name")) == NULL) return;
    copy_str(mp->value.str, name);
    if ((mp = modparam(c, b, slave_off, base + ECAT_MP_SERIAL_PDOS, "serial.pdos")) == NULL) return;
    mp->value.u32 = (uint32_t)out << 16 | (uint32_t)in;
    if ((mp = modparam(c, b, slave_off, base + ECAT_MP_SERIAL_PORT, "serial.port")) == NULL) return;
    copy_str(mp->value.str, port);
    if ((mp = modparam(c, b, slave_off, base + ECAT_MP_SERIAL_SETTINGS, "serial.settings")) == NULL) return;
    mp->value.u32 = (uint32_t)settings_index | (uint32_t)baud_value << 16 | (uint32_t)frame_value << 24;
    if ((mp = modparam(c, b, slave_off, base + ECAT_MP_SERIAL_FLAGS, "serial.flags")) == NULL) return;
    mp->value.u32 = get_bool(c, e, "send-continuous", 0) ? 1 : 0;
    c->ser_continuous[c->n_ser] = mp->value.u32 & 1;
    c->ser_pdo[c->n_ser][0] = out;
    c->ser_pdo[c->n_ser][1] = in;
    c->ser_bits[c->n_ser][0] = c->ser_bits[c->n_ser][1] = 0;
    c->ser_settings[c->n_ser] = settings_index;
    c->ser_baud[c->n_ser] = baud_value;
    c->ser_frame[c->n_ser] = frame_value;
    copy_str(c->ser_name[c->n_ser], name);
    c->n_ser++;
  }
}

/// The serial channels' baud rate and data frame, written at start (and
/// by the master again after a power cycle of the slave).
static void serial_settings(ctx_t *c, buf_t *b, size_t slave_off) {
  int k, j;
  for (k = 0; k < c->n_ser; k++) {
    for (j = 0; j < 3; j++) {
      LCEC_CONF_SDOCONF_T *sc = append(b, sizeof(*sc) + 1);
      if (sc == NULL) {
        error(c, "out of memory");
        return;
      }
      sc->confType = lcecConfTypeSdoConfig;
      sc->index = c->ser_settings[k];
      sc->subindex = j == 0 ? ECAT_SER_SUB_BAUD : j == 1 ? ECAT_SER_SUB_FRAME : ECAT_SER_SUB_CONTINUOUS;
      sc->length = 1;
      sc->data[0] = j == 0 ? c->ser_baud[k] : j == 1 ? c->ser_frame[k] : c->ser_continuous[k];
      ((LCEC_CONF_SLAVE_T *)(b->data + slave_off))->sdoConfigLength += sizeof(*sc) + 1;
    }
  }
}

/// An IPv4 address "a.b.c.d" or "a.b.c.d/n" (prefix length into *prefix, -1
/// without), in network byte order into *out. Errors name the key.
static int parse_ipv4(ctx_t *c, const char *key, const char *text, uint32_t *out, int *prefix) {
  char buf[64], *slash;
  struct in_addr a;
  if (prefix) *prefix = -1;
  if (text == NULL || strlen(text) >= sizeof(buf)) {
    error(c, "eoe.%s: \"%s\" is not an IPv4 address", key, text ? text : "");
    return -1;
  }
  strcpy(buf, text);
  slash = strchr(buf, '/');
  if (slash != NULL) {
    char *end;
    long n;
    *slash++ = 0;
    n = strtol(slash, &end, 10);
    if (prefix == NULL || *end || end == slash || n < 0 || n > 32) {
      error(c, "eoe.%s: \"%s\" is not an address with a prefix length such as 192.168.10.21/24", key, text);
      return -1;
    }
    *prefix = (int)n;
  }
  if (inet_pton(AF_INET, buf, &a) != 1) {
    error(c, "eoe.%s: \"%s\" is not an IPv4 address", key, text);
    return -1;
  }
  *out = a.s_addr;
  return 0;
}

/// eoe: ip (with or without /prefix), mask, gateway, dns, hostname, mac, host-ip.
static void eoe(ctx_t *c, buf_t *b, yaml_node_t *e) {
  LCEC_CONF_EOE_T *ec;
  const char *v;
  int prefix = -1;
  if (e == NULL) return;
  ec = append(b, sizeof(*ec));
  if (ec == NULL) {
    error(c, "out of memory");
    return;
  }
  ec->confType = lcecConfTypeEoe;
  if ((v = get_str(c, e, "ip")) == NULL) {
    error(c, "eoe: needs ip (such as 192.168.10.21/24)");
  } else if (parse_ipv4(c, "ip", v, &ec->ip, &prefix) == 0) {
    ec->given |= LCEC_EOE_IP;
  }
  if ((v = get_str(c, e, "mask")) != NULL) {
    if (prefix >= 0) {
      error(c, "eoe: give the subnet as ip/prefix or as mask, not both");
    } else if (parse_ipv4(c, "mask", v, &ec->mask, NULL) == 0) {
      uint32_t m = ntohl(ec->mask);
      if ((~m & (~m + 1)) != 0 || m == 0) error(c, "eoe.mask: \"%s\" is not a subnet mask", v);
      ec->given |= LCEC_EOE_MASK;
    }
  } else if (prefix >= 0) {
    ec->mask = prefix == 0 ? 0 : htonl(0xffffffffu << (32 - prefix));
    ec->given |= LCEC_EOE_MASK;
  }
  if ((v = get_str(c, e, "gateway")) != NULL && parse_ipv4(c, "gateway", v, &ec->gateway, NULL) == 0) ec->given |= LCEC_EOE_GATEWAY;
  if ((v = get_str(c, e, "dns")) != NULL && parse_ipv4(c, "dns", v, &ec->dns, NULL) == 0) ec->given |= LCEC_EOE_DNS;
  if ((v = get_str(c, e, "hostname")) != NULL) {
    if (!*v || strlen(v) >= 32) error(c, "eoe.hostname: \"%s\" is empty or longer than 31 characters", v);
    copy_str(ec->hostname, v);
    ec->given |= LCEC_EOE_HOSTNAME;
  }
  if ((v = get_str(c, e, "mac")) != NULL) {
    unsigned int m[6];
    char extra;
    if (sscanf(v, "%x:%x:%x:%x:%x:%x%c", &m[0], &m[1], &m[2], &m[3], &m[4], &m[5], &extra) != 6 ||
        (m[0] | m[1] | m[2] | m[3] | m[4] | m[5]) > 0xff) {
      error(c, "eoe.mac: \"%s\" is not a MAC address such as 02:00:00:00:10:21", v);
    } else {
      int i;
      for (i = 0; i < 6; i++) ec->mac[i] = (uint8_t)m[i];
      ec->given |= LCEC_EOE_MAC;
    }
  }
  if ((v = get_str(c, e, "host-ip")) != NULL && parse_ipv4(c, "host-ip", v, &ec->host_ip, NULL) == 0) {
    // the PC's address on the slave's network device: in the slave's subnet
    if ((ec->given & (LCEC_EOE_IP | LCEC_EOE_MASK)) != (LCEC_EOE_IP | LCEC_EOE_MASK)) {
      error(c, "eoe.host-ip: needs the subnet: give ip with a prefix length (such as 192.168.10.21/24) or mask");
    } else if (ec->mask == 0 || ec->mask == 0xffffffffu) {
      error(c, "eoe.host-ip: the subnet has room for one address only");
    } else if (ec->host_ip == ec->ip) {
      error(c, "eoe.host-ip: \"%s\" is the slave's own address", v);
    } else if ((ec->host_ip & ec->mask) != (ec->ip & ec->mask)) {
      error(c, "eoe.host-ip: \"%s\" is not in the slave's subnet", v);
    } else {
      ec->given |= LCEC_EOE_HOST_IP;
    }
  }
}

static void slave(ctx_t *c, buf_t *b, yaml_node_t *s) {
  int a;
  yaml_node_t *ident = get(c, s, "identity");
  yaml_node_t *dc = get(c, s, "dc");
  const char *name = get_str(c, s, "name");
  const char *profile = get_str(c, s, "profile");
  size_t off = b->len;
  LCEC_CONF_SLAVE_T *sc;
  int i;

  snprintf(c->where, sizeof(c->where), "slave %lld", get_int(c, s, "position", -1));
  if ((sc = append(b, sizeof(*sc))) == NULL) {
    error(c, "out of memory");
    return;
  }
  sc->confType = lcecConfTypeSlave;
  sc->index = get_int(c, s, "position", -1);
  if (sc->index < 0) error(c, "position missing");
  if (name == NULL || !name[0]) error(c, "name missing");
  if (name != NULL && strlen(name) >= LCEC_CONF_STR_MAXLEN) error(c, "name \"%s\" is too long", name);
  copy_str(sc->name, name);
  copy_str(sc->type_name, "generic");
  sc->vid = get_int(c, ident, "vendor", 0);
  sc->pid = get_int(c, ident, "product", 0);
  sc->revision = get_int(c, ident, "revision", 0);
  copy_str(sc->device, get_str(c, s, "device"));
  sc->configPdos = get_bool(c, s, "configure-pdos", 1);
  // A slave that is only on the bus: its identity is still compared at
  // start, nothing else is read or done (what else the entry has is kept
  // for a later `unused: false`).
  sc->unused = get_bool(c, s, "unused", 0);
  if (sc->unused) {
    if (get(c, s, "eoe") != NULL) error(c, "unused: true and eoe: a slave with a network address is used (the master configures it)");
    return;
  }
  c->cia402 = profile != NULL && !strcmp(profile, "cia402");
  c->cia402_modes = 0;
  c->cia402_all = 0;
  memset(c->owned, 0, sizeof(c->owned));
  c->n_axes = 1;
  if (c->cia402) {
    const char *mode = get_str(c, get(c, s, "cia402"), "mode");
    LCEC_CONF_MODPARAM_T *mp;
    copy_str(sc->type_name, "cia402");
    if ((mp = append(b, sizeof(*mp))) == NULL) {
      error(c, "out of memory");
      return;
    }
    mp->confType = lcecConfTypeModParam;
    mp->id = ECAT_MP_CIA402_MODE;
    copy_str(mp->name, "cia402.mode");
    mp->value.u32 = cia402_mode_number(mode);
    if (mp->value.u32 == 0 || mp->value.u32 == 6) {
      error(c, "cia402.mode must be csp, csv, pv or pp");
      mp->value.u32 = 8;
    }
    c->cia402_all = 1u << mp->value.u32;
    ((LCEC_CONF_SLAVE_T *)(b->data + off))->modParamCount = 1;
    c->cia402_modes = cia402_modes(c, get(c, get(c, s, "cia402"), "modes"));
    c->cia402_all |= c->cia402_modes;
    if (c->cia402_modes) {
      if ((mp = append(b, sizeof(*mp))) == NULL) {
        error(c, "out of memory");
        return;
      }
      mp->confType = lcecConfTypeModParam;
      mp->id = ECAT_MP_CIA402_MODES;
      copy_str(mp->name, "cia402.modes");
      mp->value.u32 = c->cia402_modes;
      ((LCEC_CONF_SLAVE_T *)(b->data + off))->modParamCount++;
    }
    {
      // several axes: one modparam per axis, its pin prefix
      yaml_node_t *axes = get(c, get(c, s, "cia402"), "axes");
      int a, n = seq_len(axes);
      if (axes != NULL && (n < 1 || n > ECAT_CIA402_MAX_AXES)) {
        error(c, "cia402.axes: a list of 1 to %d names", ECAT_CIA402_MAX_AXES);
        n = 0;
      }
      for (a = 0; a < n; a++) {
        yaml_node_t *item = seq_item(c, axes, a);
        const char *ax_name = item != NULL && item->type == YAML_SCALAR_NODE ? (const char *)item->data.scalar.value : NULL;
        if (ax_name == NULL || !ax_name[0] || strlen(ax_name) >= LCEC_CONF_STR_MAXLEN) {
          error(c, "cia402.axes: axis %d needs a name (a pin prefix) shorter than %d characters", a, LCEC_CONF_STR_MAXLEN);
          continue;
        }
        if ((mp = append(b, sizeof(*mp))) == NULL) {
          error(c, "out of memory");
          return;
        }
        mp->confType = lcecConfTypeModParam;
        mp->id = ECAT_MP_CIA402_AXIS;
        copy_str(mp->name, "cia402.axes");
        copy_str(mp->value.str, ax_name);
        ((LCEC_CONF_SLAVE_T *)(b->data + off))->modParamCount++;
      }
      if (n > 0) c->n_axes = n;
    }
    c->ff_velocity = c->ff_torque = 0;
    {
      // feedforward in CSP: modparams with float values
      yaml_node_t *ff = get(c, get(c, s, "cia402"), "feedforward");
      static const struct {
        const char *key;
        int id;
      } keys[] = {{"velocity", ECAT_MP_CIA402_FF_VELOCITY}, {"torque", ECAT_MP_CIA402_FF_TORQUE},
                  {"torque-max", ECAT_MP_CIA402_FF_TORQUE_MAX}};
      unsigned int k;
      if (ff != NULL && ff->type != YAML_MAPPING_NODE) error(c, "cia402.feedforward needs velocity, torque and/or torque-max");
      for (k = 0; ff != NULL && ff->type == YAML_MAPPING_NODE && k < sizeof(keys) / sizeof(keys[0]); k++) {
        double v;
        if (get_str(c, ff, keys[k].key) == NULL) continue;
        v = get_float(c, ff, keys[k].key, 0.0);
        if (!isfinite(v) || (keys[k].id == ECAT_MP_CIA402_FF_TORQUE_MAX && (v < 0 || v > 300))) {
          error(c, "cia402.feedforward.%s: %s", keys[k].key,
                keys[k].id == ECAT_MP_CIA402_FF_TORQUE_MAX ? "0 to 300 (% of rated torque)" : "a number");
          continue;
        }
        if ((mp = append(b, sizeof(*mp))) == NULL) {
          error(c, "out of memory");
          return;
        }
        mp->confType = lcecConfTypeModParam;
        mp->id = keys[k].id;
        snprintf(mp->name, LCEC_CONF_STR_MAXLEN, "cia402.feedforward.%s", keys[k].key);
        mp->value.flt = v;
        ((LCEC_CONF_SLAVE_T *)(b->data + off))->modParamCount++;
        if (keys[k].id == ECAT_MP_CIA402_FF_VELOCITY) c->ff_velocity = 1;
        if (keys[k].id == ECAT_MP_CIA402_FF_TORQUE) c->ff_torque = 1;
      }
    }
    if (get_bool(c, get(c, s, "cia402"), "zero-at-start", 0)) {
      if ((mp = append(b, sizeof(*mp))) == NULL) {
        error(c, "out of memory");
        return;
      }
      mp->confType = lcecConfTypeModParam;
      mp->id = ECAT_MP_CIA402_ZERO_AT_START;
      copy_str(mp->name, "cia402.zero-at-start");
      mp->value.u32 = 1;
      ((LCEC_CONF_SLAVE_T *)(b->data + off))->modParamCount++;
    }
  } else if (profile == NULL || (strcmp(profile, "generic") && strcmp(profile, "mdp"))) {
    error(c, "profile \"%s\" is not supported (cia402, mdp, generic)", profile ? profile : "");
  }
  encoders(c, b, get(c, s, "encoders"), off);
  serial_channels(c, b, get(c, s, "serial"), off);

  if (dc != NULL) {
    LCEC_CONF_DC_T *dcc = append(b, sizeof(*dcc));
    if (dcc == NULL) {
      error(c, "out of memory");
      return;
    }
    dcc->confType = lcecConfTypeDcConf;
    dcc->assignActivate = get_int(c, dc, "assign-activate", 0);
    dcc->sync0Cycle = get_int(c, dc, "sync0-cycles", 1);  // servo periods, see ecat_config.h
    dcc->sync0Shift = get_int(c, dc, "sync0-shift-ns", 0);
    // SYNC1 every n SYNC0 cycles, shifted from SYNC0; the register value (CycleTime1,
    // ETG.2000) is made in lcec_main.c once the SYNC0 cycle is known (ecat_sync1_cycle)
    {
      long long n = get_int(c, dc, "sync1-cycles", 0);
      if (n < 0 || n > 0xFFFF) {
        error(c, "dc sync1-cycles must be 0 to 65535 (SYNC1 every n SYNC0 cycles)");
        n = 0;
      }
      dcc->sync1Cycle = (uint32_t)n;
      dcc->sync1Shift = (int32_t)get_int(c, dc, "sync1-shift-ns", 0);
      if (dcc->sync1Shift != 0 && n == 0) error(c, "dc sync1-shift-ns needs sync1-cycles");
    }
  }
  eoe(c, b, get(c, s, "eoe"));
  process_data(c, b, get(c, s, "process-data"), (LCEC_CONF_SLAVE_T *)(b->data + off), off);
  if (c->cia402) {
    // what each allowed mode needs, and changing modes the mode objects
    uint16_t need[8] = {0x6040, 0x6041};
    int n_need = 2, n_mode_objects;
    int j;
    if (c->cia402_all & ((1u << 8) | (1u << 1))) {  // CSP, PP
      need[n_need++] = 0x607A;
      need[n_need++] = 0x6064;
    }
    if (c->cia402_all & ((1u << 9) | (1u << 3))) {  // CSV, PV
      need[n_need++] = 0x60FF;
      need[n_need++] = 0x606C;
    }
    n_mode_objects = n_need;
    if (c->cia402_modes) {
      need[n_need++] = 0x6060;
      need[n_need++] = 0x6061;
    }
    for (a = 0; a < c->n_axes; a++) {
      for (j = 0; j < n_need; j++) {
        i = cia402_object(need[j], 0);
        if (!(c->owned[a] & (1u << i))) error(c, "the cia402 profile needs 0x%04X:00 with \"use: profile\"%s",
                                               need[j] + a * ECAT_CIA402_AXIS_OFFSET,
                                               j >= n_mode_objects ? " to change modes" : "");
      }
      // feedforward: its object, and CSP to use it in
      if (c->ff_velocity && !(c->owned[a] & (1u << cia402_object(0x60B1, 0))))
        error(c, "cia402.feedforward.velocity needs 0x%04X:00 (velocity offset) with \"use: profile\" in the outputs",
              0x60B1 + a * ECAT_CIA402_AXIS_OFFSET);
      if (c->ff_torque && !(c->owned[a] & (1u << cia402_object(0x60B2, 0))))
        error(c, "cia402.feedforward.torque needs 0x%04X:00 (torque offset) with \"use: profile\" in the outputs",
              0x60B2 + a * ECAT_CIA402_AXIS_OFFSET);
    }
    if ((c->ff_velocity || c->ff_torque) && !(c->cia402_all & (1u << 8)))
      error(c, "cia402.feedforward works in CSP only: csp must be the mode or one of the modes");
  }
  for (i = 0; i < c->n_ser; i++) {
    int d;
    for (d = 0; d < 2; d++) {
      int bits = c->ser_bits[i][d];
      if (bits == 0) {
        error(c, "serial channel %s: PDO 0x%04X is not in the %s", c->ser_name[i], c->ser_pdo[i][d], d ? "inputs" : "outputs");
      } else if (bits % 8 || bits < 24 || bits > 16 + 8 * ECAT_SER_MAX_DATA) {
        error(c, "serial channel %s: PDO 0x%04X has %d bits; it needs a control (status) byte, a length byte and 1 to %d data bytes",
            c->ser_name[i], c->ser_pdo[i][d], bits, ECAT_SER_MAX_DATA);
      }
    }
    if (c->ser_bits[i][0] && c->ser_bits[i][1] && c->ser_bits[i][0] != c->ser_bits[i][1]) {
      error(c, "serial channel %s: the output and input PDOs need the same size", c->ser_name[i]);
    }
  }
  for (i = 0; i < c->n_enc; i++) {
    if (!(c->enc_owned[i] & (1u << ENC_COUNTER))) {
      error(c, "encoder %s needs 0x%04X:11 (counter value) with \"use: profile\"", c->enc_name[i], c->enc_index[i]);
    }
  }
  memset(c->pp_limits, 0, sizeof(c->pp_limits));
  for (a = 0; a < ECAT_CIA402_MAX_AXES; a++) c->mode_setting[a] = -1000;
  serial_settings(c, b, off);
  settings(c, b, get(c, s, "settings"), off);
  if (c->cia402) {
    // the mode: set every cycle (0x6060 in the process data), or once as a setting;
    // or at least seen (0x6061: the profile enables only in the mode it shows).
    // A setting must say the mode the profile runs in.
    unsigned mode = 0;
    for (mode = 0; mode < 32 && !((c->cia402_all & ~c->cia402_modes) & (1u << mode)); mode++) {
    }
    for (a = 0; a < c->n_axes; a++) {
      unsigned o = a * ECAT_CIA402_AXIS_OFFSET;
      int set_cyclic = (c->owned[a] & (1u << cia402_object(0x6060, 0))) != 0;
      int seen = (c->owned[a] & (1u << cia402_object(0x6061, 0))) != 0;
      if (c->mode_setting[a] != -1000 && !c->cia402_modes && c->mode_setting[a] != (int)mode) {
        error(c, "the setting 0x%04X:00 is %d, but cia402.mode is %u: make them the same (the setting is the "
                 "mode the drive is put in at start)", 0x6060 + o, c->mode_setting[a], mode);
      }
      if (!c->cia402_modes && !set_cyclic && !seen && c->mode_setting[a] == -1000) {  // modes: checked above
        error(c, "the drive's mode is neither set nor seen: add the setting 0x%04X:00 = %u (type SINT, the "
                 "mode at start), or map 0x%04X:00 or 0x%04X:00",
              0x6060 + o, mode, 0x6060 + o, 0x6061 + o);
      }
    }
    // the drive plans PP moves itself: it must know its travel. 0x607F
    // (max profile velocity) is set where the drive has it; not every
    // drive does (the Rtelligent ECT60), so it is not required here.
    for (a = 0; a < c->n_axes; a++) {
      if ((c->cia402_all & (1u << 1)) && (c->pp_limits[a] & 3) != 3) {
        unsigned o = a * ECAT_CIA402_AXIS_OFFSET;
        error(c, "cia402 mode pp needs the settings 0x%04X:01 and 0x%04X:02 (software position limits): "
                 "add them to the settings, type DINT, with the lowest and highest position the drive "
                 "may move to, in counts",
              0x607D + o, 0x607D + o);
      }
    }
  }
  c->where[0] = 0;
}

uint32_t ecat_sync1_cycle(uint32_t n, uint32_t sync0_ns, int32_t shift_ns) {
  // ETG.2000 (OpMode:CycleTimeSync1): the register is CycleTimeSync1 - CycleTimeSync0
  // + ShiftTimeSync1; SYNC1 every n SYNC0 cycles: (n - 1) * SYNC0 + shift. 0: no SYNC1 cycle
  if (n == 0) return 0;
  return (uint32_t)((int64_t)(n - 1) * sync0_ns + shift_ns);
}

char *ecat_config_load(const char *path, size_t *length) {
  FILE *f;
  yaml_parser_t parser;
  yaml_document_t doc;
  yaml_node_t *root, *master, *mdc, *slaves;
  ctx_t c = {&doc, path, "", 0};
  buf_t b = {NULL, 0, 0};
  LCEC_CONF_MASTER_T *mc;
  int i;

  if (path == NULL || !path[0]) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "no configuration file: loadrt ecat config=ethercat.yaml\n");
    return NULL;
  }
  if ((f = fopen(path, "r")) == NULL) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "cannot open %s: %s\n", path, strerror(errno));
    return NULL;
  }
  yaml_parser_initialize(&parser);
  yaml_parser_set_input_file(&parser, f);
  if (!yaml_parser_load(&parser, &doc)) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s line %lu: %s\n", path, (unsigned long)parser.problem_mark.line + 1,
        parser.problem ? parser.problem : "YAML error");
    yaml_parser_delete(&parser);
    fclose(f);
    return NULL;
  }
  yaml_parser_delete(&parser);
  fclose(f);

  root = yaml_document_get_root_node(&doc);
  if (get_int(&c, root, "ecat", 0) != ECAT_FORMAT_VERSION) {
    error(&c, "not an ecat configuration of format version %d", ECAT_FORMAT_VERSION);
    goto out;
  }

  master = get(&c, root, "master");
  mdc = get(&c, master, "dc");
  if ((mc = append(&b, sizeof(*mc))) == NULL) goto out;
  mc->confType = lcecConfTypeMaster;
  mc->index = 0;
  copy_str(mc->name, "0");
  copy_str(mc->interface, get_str(&c, master, "interface"));
  copy_str(mc->transport, get_str(&c, master, "transport"));
  mc->startCheck = get_bool(&c, master, "start-check", 1);
  mc->irqCpu = get_int(&c, master, "irq-cpu", -1);
  mc->cpu = get_int(&c, master, "cpu", -1);
  if (mc->irqCpu < -1 || mc->irqCpu > 1023) error(&c, "master.irq-cpu: %d is not a CPU number", mc->irqCpu);
  if (mc->cpu < -1 || mc->cpu > 1023) error(&c, "master.cpu: %d is not a CPU number", mc->cpu);
  mc->appTimePeriod = 0;  // the servo thread's period, see ecat_config.h
  mc->refClockSyncCycles = get_int(&c, mdc, "ref-clock-sync-cycles", 0);
  // true: the servo thread follows the DC reference clock (lcec's
  // syncToRefClock); false: the slaves' clocks follow the PC's
  mc->syncToRefClock = get_bool(&c, mdc, "servo-thread-follows-ref-clock", 0);
  if (get(&c, mdc, "sync-to-ref-clock") != NULL) {
    error(&c, "master.dc.sync-to-ref-clock is now called servo-thread-follows-ref-clock");
  }
  // sync0-shift-ns: auto lets the component choose it (lcec_autoshift.h),
  // which moves the servo thread against SYNC0 and so needs
  // servo-thread-follows-ref-clock
  {
    const char *shift = get_str(&c, mdc, "sync0-shift-ns");
    if (shift != NULL && !strcmp(shift, "auto")) {
      mc->autoShift = 1;
      if (!mc->syncToRefClock) error(&c, "master.dc.sync0-shift-ns: auto needs master.dc.servo-thread-follows-ref-clock: true");
    } else {
      mc->globalShiftTime = get_int(&c, mdc, "sync0-shift-ns", 0);
    }
  }

  slaves = get(&c, root, "slaves");
  if (seq_len(slaves) == 0) error(&c, "no slaves");
  for (i = 0; i < seq_len(slaves); i++) slave(&c, &b, seq_item(&c, slaves, i));

  if (append(&b, sizeof(LCEC_CONF_NULL_T)) == NULL) error(&c, "out of memory");

out:
  yaml_document_delete(&doc);
  if (c.errors) {
    free(b.data);
    return NULL;
  }
  *length = b.len;
  return b.data;
}
