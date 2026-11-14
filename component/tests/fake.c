//
//    Unit tests of the ecat component: the fake HAL, master and lcec.
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include "fake.h"

#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

#define MAX_ENTRIES 64
#define MAX_PINS 512
#define PD_SIZE 256

typedef struct {
  fake_entry_t e;
  unsigned int os, bp;
} placed_t;

typedef struct {
  char name[HAL_NAME_LEN + 1];
  void *addr;
} named_t;

static placed_t placed[MAX_ENTRIES];
static int n_placed;
static named_t names[MAX_PINS];
static int n_names;

int fake_generic_reads, fake_generic_writes;
char fake_msgs[8192];
int fake_failures;

static void *zalloc(size_t size) {
  void *p = calloc(1, size);
  if (p == NULL) abort();
  return p;
}

lcec_slave_t *fake_slave(const char *name, const fake_entry_t *entries, const lcec_slave_modparam_t *modparams) {
  lcec_master_t *m = zalloc(sizeof(*m));
  lcec_slave_t *s = zalloc(sizeof(*s));
  ec_pdo_entry_info_t *pe;
  ec_pdo_info_t *pdo;
  ec_sync_info_t *sm;
  unsigned int bit = 0;
  int n, i;

  for (n = 0; entries[n].index != 0; n++) {
  }
  if (n > MAX_ENTRIES) abort();

  // All entries in one PDO of one sync manager: the devices look only for
  // the entries, not for where they are.
  pe = zalloc(sizeof(*pe) * (n + 1));
  pdo = zalloc(sizeof(*pdo) * 2);
  sm = zalloc(sizeof(*sm) * 2);
  n_placed = 0;
  for (i = 0; i < n; i++) {
    pe[i].index = entries[i].index;
    pe[i].subindex = entries[i].subindex;
    pe[i].bit_length = entries[i].bits;
    if (entries[i].bits != 1) bit = (bit + 7) & ~7u;
    placed[n_placed].e = entries[i];
    placed[n_placed].os = bit / 8;
    placed[n_placed].bp = bit % 8;
    n_placed++;
    bit += entries[i].bits;
  }
  if ((bit + 7) / 8 > PD_SIZE) abort();
  pdo[0].index = 0x1600;
  pdo[0].n_entries = n;
  pdo[0].entries = pe;
  sm[0].index = 2;
  sm[0].dir = EC_DIR_OUTPUT;
  sm[0].n_pdos = 1;
  sm[0].pdos = pdo;
  sm[1].index = 0xff;

  m->process_data = zalloc(PD_SIZE);
  m->process_data_len = PD_SIZE;
  strncpy(s->name, name, sizeof(s->name) - 1);
  s->master = m;
  s->generic_sync_managers = sm;
  if (modparams != NULL) {
    for (n = 0; modparams[n].id != -1; n++) {
    }
    s->modparams = zalloc(sizeof(*s->modparams) * (n + 1));
    memcpy(s->modparams, modparams, sizeof(*s->modparams) * (n + 1));
  }

  n_names = 0;
  fake_generic_reads = fake_generic_writes = 0;
  fake_msgs[0] = 0;
  fake_bus(s, 1);
  return s;
}

lcec_slave_t *fake_capture_slave(const char *name, const fake_capture_slave_t *c, const lcec_slave_modparam_t *modparams) {
  return fake_slave(name, c->entries, modparams);
}

int fake_capture_inputs(lcec_slave_t *slave, const fake_capture_slave_t *c, int n) {
  const uint32_t *row;
  int i;
  if (c->n_cycles == 0) return 0;
  row = c->values + (size_t)(n % c->n_cycles) * c->n_entries;
  for (i = c->n_outputs; i < c->n_entries; i++) fake_set(slave, c->entries[i].index, c->entries[i].subindex, row[i]);
  return 1;
}

long long fake_capture_output(const fake_capture_slave_t *c, int n, uint16_t index, uint8_t subindex) {
  int i;
  if (c->n_cycles == 0) return -1;
  for (i = 0; i < c->n_outputs; i++) {
    if (c->entries[i].index == index && c->entries[i].subindex == subindex)
      return c->values[(size_t)(n % c->n_cycles) * c->n_entries + i];
  }
  return -1;
}

void fake_bus(lcec_slave_t *slave, int ok) {
  slave->master->domain_state.wc_state = ok ? EC_WC_COMPLETE : EC_WC_INCOMPLETE;
  slave->state.operational = ok ? 1 : 0;
}

static placed_t *lookup(uint16_t index, uint8_t subindex) {
  int i;
  for (i = 0; i < n_placed; i++) {
    if (placed[i].e.index == index && placed[i].e.subindex == subindex) return &placed[i];
  }
  fprintf(stderr, "fake: no entry 0x%04X:%02X\n", index, subindex);
  abort();
}

uint32_t fake_get(lcec_slave_t *slave, uint16_t index, uint8_t subindex) {
  placed_t *p = lookup(index, subindex);
  uint8_t *pd = slave->master->process_data + p->os;
  switch (p->e.bits) {
    case 1: return EC_READ_BIT(pd, p->bp);
    case 8: return EC_READ_U8(pd);
    case 16: return EC_READ_U16(pd);
    default: return EC_READ_U32(pd);
  }
}

void fake_set(lcec_slave_t *slave, uint16_t index, uint8_t subindex, uint32_t value) {
  placed_t *p = lookup(index, subindex);
  uint8_t *pd = slave->master->process_data + p->os;
  switch (p->e.bits) {
    case 1: EC_WRITE_BIT(pd, p->bp, value != 0); break;
    case 8: EC_WRITE_U8(pd, value); break;
    case 16: EC_WRITE_U16(pd, value); break;
    default: EC_WRITE_U32(pd, value);
  }
}

void *fake_find(const char *name) {
  int i;
  for (i = 0; i < n_names; i++) {
    if (strcmp(names[i].name, name) == 0) return names[i].addr;
  }
  return NULL;
}

void *fake_pin(const char *name) {
  void *p = fake_find(name);
  if (p == NULL) {
    fprintf(stderr, "fake: no pin or parameter %s\n", name);
    abort();
  }
  return p;
}

static int add_name(void *addr, const char *fmt, va_list ap) {
  if (n_names == MAX_PINS) abort();
  vsnprintf(names[n_names].name, sizeof(names[n_names].name), fmt, ap);
  if (fake_find(names[n_names].name) != NULL) {
    fprintf(stderr, "fake: %s made twice\n", names[n_names].name);
    abort();
  }
  names[n_names++].addr = addr;
  return 0;
}

// ---- what the devices call -------------------------------------------

void rtapi_print_msg(msg_level_t level, const char *fmt, ...) {
  size_t n = strlen(fake_msgs);
  va_list ap;
  (void)level;
  va_start(ap, fmt);
  vsnprintf(fake_msgs + n, sizeof(fake_msgs) - n, fmt, ap);
  va_end(ap);
}

void *lcec_hal_malloc(size_t size, const char *file, const char *func, int line) {
  (void)file, (void)func, (void)line;
  return zalloc(size);
}

int lcec_pdo_init(lcec_slave_t *slave, uint16_t idx, uint16_t sidx, unsigned int *os, unsigned int *bp) {
  placed_t *p = lookup(idx, sidx);
  (void)slave;
  *os = p->os;
  if (bp != NULL) *bp = p->bp;
  return 0;
}

int lcec_pin_newf_list(void *base, const lcec_pindesc_t *list, ...) {
  const lcec_pindesc_t *p;
  va_list ap;

  for (p = list; p->type != HAL_TYPE_UNSPECIFIED; p++) {
    void *storage = zalloc(sizeof(double));
    *(void **)((char *)base + p->offset) = storage;
    va_start(ap, list);
    add_name(storage, p->fmt, ap);
    va_end(ap);
  }
  return 0;
}

int lcec_pin_newf(hal_type_t type, hal_pdir_t dir, void **data_ptr_addr, const char *fmt, ...) {
  void *storage = zalloc(sizeof(double));
  va_list ap;
  (void)type, (void)dir;
  *data_ptr_addr = storage;
  va_start(ap, fmt);
  add_name(storage, fmt, ap);
  va_end(ap);
  return 0;
}

int lcec_param_newf_list(void *base, const lcec_paramdesc_t *list, ...) {
  const lcec_paramdesc_t *p;
  va_list ap;

  for (p = list; p->type != HAL_TYPE_UNSPECIFIED; p++) {
    // a param is a reference too in 2.10, to storage that starts at zero
    void *storage = zalloc(sizeof(double));
    *(void **)((char *)base + p->offset) = storage;
    va_start(ap, list);
    add_name(storage, p->fmt, ap);
    va_end(ap);
  }
  return 0;
}

fake_event_t fake_events[FAKE_EVENTS];
int fake_n_events;

void lcec_event(lcec_slave_t *slave, int type, uint32_t a, uint32_t b) {
  if (fake_n_events < FAKE_EVENTS) {
    fake_events[fake_n_events].slave = slave->index;
    fake_events[fake_n_events].type = type;
    fake_events[fake_n_events].a = a;
    fake_events[fake_n_events].b = b;
  }
  fake_n_events++;
}

// ---- runner ----------------------------------------------------------

int fake_run(const char *suite, const fake_test_t *tests) {
  int n;
  for (n = 0; tests[n] != NULL; n++) tests[n]();
  if (fake_failures) {
    printf("%s: %d of the checks in %d tests failed\n", suite, fake_failures, n);
    return 1;
  }
  printf("%s: %d tests OK\n", suite, n);
  return 0;
}
