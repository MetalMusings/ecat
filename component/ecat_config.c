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
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <yaml.h>

#include "devices/ecat_cia402.h"
#include "devices/ecat_encoder.h"
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
  unsigned int pp_limits;     // settings 0x607D:01, 0x607D:02, 0x607F:00 seen (bits 0-2)
  unsigned int owned; // which of the profile's objects it has (bits of cia402_objects)
  int n_enc;          // the slave's encoder channels
  uint16_t enc_index[MAX_ENCODERS];   // their status indexes
  unsigned int enc_owned[MAX_ENCODERS];  // which of enc_objects each has
  int enc_bits[MAX_ENCODERS];            // their counters' bits
  char enc_name[MAX_ENCODERS][LCEC_CONF_STR_MAXLEN];
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
};
#define N_CIA402_OBJECTS (int)(sizeof(cia402_objects) / sizeof(cia402_objects[0]))

static int cia402_object(uint16_t index, uint8_t subindex) {
  int i;
  for (i = 0; i < N_CIA402_OBJECTS; i++)
    if (cia402_objects[i].index == index && subindex == 0) return i;
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
static void pdo_entry(ctx_t *c, buf_t *b, yaml_node_t *e, int output, unsigned int *pins) {
  LCEC_CONF_PDOENTRY_T *pe;
  yaml_node_t *split;
  const char *use = get_str(c, e, "use");
  const char *pin = get_str(c, e, "pin");
  const char *object = get_str(c, e, "object");
  const dtype_t *dt = find_dtype(get_str(c, e, "type"));
  long long bits = get_int(c, e, "bits", 0);
  uint16_t index = 0;
  uint8_t subindex = 0;
  int i, k, ch = 0;

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
  if (use != NULL && !strcmp(use, "gap")) return;
  i = cia402_object(index, subindex);
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
        c->owned |= 1u << i;
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
      error(c, "%s: \"use: profile\" needs profile cia402 or an encoder channel", object);
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
    const char *hal = get_str(c, e, "hal-type");
    copy_str(pe->halPin, pin);
    if (hal != NULL && !strcmp(hal, "bit")) {
      pe->halType = HAL_BIT;
    } else if (hal != NULL && !strcmp(hal, "s32")) {
      pe->halType = HAL_S32;
    } else if (hal != NULL && !strcmp(hal, "u32")) {
      pe->halType = HAL_U32;
    } else if (hal != NULL && !strcmp(hal, "float")) {
      pe->halType = HAL_FLOAT;
      if (dt->is_float) {
        pe->subType = dt->bytes == 8 ? lcecPdoEntTypeFloatDoubleIeee : lcecPdoEntTypeFloatIeee;
      } else {
        pe->subType = dt->is_signed ? lcecPdoEntTypeFloatSigned : lcecPdoEntTypeFloatUnsigned;
        pe->floatScale = get_float(c, e, "scale", 1.0);
        pe->floatOffset = get_float(c, e, "offset", 0.0);
      }
    } else {
      error(c, "%s: hal-type \"%s\" is not supported (bit, s32, u32, float)", object, hal ? hal : "");
      return;
    }
    (*pins)++;
  }

  split = get(c, e, "split");
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
      ce->halType = HAL_BIT;
      ce->floatScale = 1.0;
      copy_str(ce->halPin, scalar(bit));
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
        for (k = 0; k < seq_len(entries); k++) pdo_entry(c, b, seq_item(c, entries, k), d == 0, &pins);
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
  int i;
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
    if (index == 0x607D && (subindex == 1 || subindex == 2)) c->pp_limits |= 1u << (subindex - 1);
    if (index == 0x607F && subindex == 0) c->pp_limits |= 4;
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

static void slave(ctx_t *c, buf_t *b, yaml_node_t *s) {
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
  sc->configPdos = get_bool(c, s, "configure-pdos", 1);
  c->cia402 = profile != NULL && !strcmp(profile, "cia402");
  c->cia402_modes = 0;
  c->cia402_all = 0;
  c->owned = 0;
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
  }
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
    for (j = 0; j < n_need; j++) {
      i = cia402_object(need[j], 0);
      if (!(c->owned & (1u << i))) error(c, "the cia402 profile needs 0x%04X:00 with \"use: profile\"%s", need[j],
                                          j >= n_mode_objects ? " to change modes" : "");
    }
  }
  for (i = 0; i < c->n_enc; i++) {
    if (!(c->enc_owned[i] & (1u << ENC_COUNTER))) {
      error(c, "encoder %s needs 0x%04X:11 (counter value) with \"use: profile\"", c->enc_name[i], c->enc_index[i]);
    }
  }
  c->pp_limits = 0;
  settings(c, b, get(c, s, "settings"), off);
  if (c->cia402) {
    // the drive plans PP moves itself: it must know its limits
    if ((c->cia402_all & (1u << 1)) && c->pp_limits != 7) {
      error(c, "cia402 mode pp needs the settings 0x607D:01 and 0x607D:02 (software position limits) "
               "and 0x607F:00 (max profile velocity)");
    }
  }
  c->where[0] = 0;
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
