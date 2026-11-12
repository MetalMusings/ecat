//
//    ecat: the encoder function, for incremental encoder channels laid out
//    as in ETG.5001 MDP 511 (Beckhoff EL5101, EL5151, EL5152, EL7041, ...).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//
//    This program is distributed in the hope that it will be useful,
//    but WITHOUT ANY WARRANTY; without even the implied warranty of
//    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
//    GNU General Public License for more details.
//

/// @file
/// @brief Encoder channels: an extended count, scaled position and velocity,
/// index and external latch, like LinuxCNC's encoder component.
///
/// An MDP 511 encoder channel has its inputs at a status index S
/// (0x6000 + 0x10 * n) and its outputs at S + 0x1000:
///
///   S:01 latch C valid         S+0x1000:01 enable latch C
///   S:02 latch extern valid    S+0x1000:02 enable latch extern on positive edge
///   S:03 set counter done      S+0x1000:03 set counter
///   S:11 counter value         S+0x1000:04 enable latch extern on negative edge
///   S:12 latch value           S+0x1000:11 set counter value
///
/// The function owns these entries (`use: profile`); the channel's other
/// entries, such as the input states or frequency, keep ordinary pins.
///
/// The counter, 16 or 32 bits, is extended to 64 bits, so it does not wrap
/// as long as it moves less than half its range in one cycle. The counter
/// is never set: position is kept by moving a reference instead, so that
/// position feedback never jumps except at an index or reset.

#include "ecat_encoder.h"

#include <errno.h>
#include <stdint.h>
#include <string.h>

#define HIST 256  // velocity window, cycles

typedef struct {
  unsigned int os, bp;
  int bits;  // 0: not mapped
} obj_t;

typedef struct {
  // in
  hal_bool_t reset;
  hal_bool_t index_enable;
  hal_bool_t latch_enable_pos;
  hal_bool_t latch_enable_neg;
  // out
  hal_sint_t counts;
  hal_sint_t raw_count;
  hal_real_t pos;
  hal_real_t velocity;
  hal_real_t latch_pos;
  hal_bool_t ok;
  // parameters
  hal_real_t pos_scale;
  hal_uint_t velocity_window_ms;

  // process data
  obj_t latch_c_valid, latch_ext_valid, count, latch;
  obj_t en_latch_c, en_latch_ext_pos, set_counter, en_latch_ext_neg, set_value;

  // state
  int have;        // last_raw is valid
  uint32_t last_raw;
  int64_t ext;     // extended count
  int64_t ref;     // ext at position 0
  int armed_c;     // latch C valid was seen false since index-enable
  int armed_ext;   // the same for the external latch
  int64_t hist[HIST];
  int head, filled;
} channel_t;

typedef struct {
  lcec_slave_rw_t read, write;  // the slave's own
  int n;
  channel_t *ch;
} encoders_t;

static const lcec_pindesc_t pins[] = {
    {HAL_BOOL, HAL_IN, offsetof(channel_t, reset), "%s.%s.reset"},
    {HAL_SINT, HAL_OUT, offsetof(channel_t, counts), "%s.%s.counts"},
    {HAL_SINT, HAL_OUT, offsetof(channel_t, raw_count), "%s.%s.raw-count"},
    {HAL_REAL, HAL_OUT, offsetof(channel_t, pos), "%s.%s.pos"},
    {HAL_REAL, HAL_OUT, offsetof(channel_t, velocity), "%s.%s.velocity"},
    {HAL_BOOL, HAL_OUT, offsetof(channel_t, ok), "%s.%s.ok"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

// only when latch C is mapped: a HAL file must not wait for an index that
// can never come
static const lcec_pindesc_t index_pins[] = {
    {HAL_BOOL, HAL_IO, offsetof(channel_t, index_enable), "%s.%s.index-enable"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static const lcec_pindesc_t latch_pins[] = {
    {HAL_BOOL, HAL_IO, offsetof(channel_t, latch_enable_pos), "%s.%s.latch-enable-pos"},
    {HAL_BOOL, HAL_IO, offsetof(channel_t, latch_enable_neg), "%s.%s.latch-enable-neg"},
    {HAL_REAL, HAL_OUT, offsetof(channel_t, latch_pos), "%s.%s.latch-pos"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static const lcec_paramdesc_t params[] = {
    {HAL_REAL, HAL_RW, offsetof(channel_t, pos_scale), "%s.%s.pos-scale"},
    {HAL_UINT, HAL_RW, offsetof(channel_t, velocity_window_ms), "%s.%s.velocity-window-ms"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static void enc_read(lcec_slave_t *slave, long period);
static void enc_write(lcec_slave_t *slave, long period);

/// Find an entry in the slave's process data and register it.
static int find(lcec_slave_t *slave, uint16_t index, uint8_t subindex, obj_t *o) {
  const ec_sync_info_t *sm;
  unsigned int i, j;

  for (sm = slave->generic_sync_managers; sm != NULL && sm->index != 0xff; sm++) {
    for (i = 0; i < sm->n_pdos; i++) {
      for (j = 0; j < sm->pdos[i].n_entries; j++) {
        const ec_pdo_entry_info_t *e = &sm->pdos[i].entries[j];
        if (e->index == index && e->subindex == subindex) {
          o->bits = e->bit_length;
          return lcec_pdo_init(slave, index, subindex, &o->os, &o->bp);
        }
      }
    }
  }
  o->bits = 0;
  return 0;
}

static int channel_init(lcec_slave_t *slave, channel_t *c, uint16_t s, const char *name) {
  uint16_t o = s + 0x1000;
  int err;

  if (find(slave, s, 0x01, &c->latch_c_valid) || find(slave, s, 0x02, &c->latch_ext_valid) ||
      find(slave, s, 0x11, &c->count) || find(slave, s, 0x12, &c->latch) ||
      find(slave, o, 0x01, &c->en_latch_c) || find(slave, o, 0x02, &c->en_latch_ext_pos) ||
      find(slave, o, 0x03, &c->set_counter) || find(slave, o, 0x04, &c->en_latch_ext_neg) ||
      find(slave, o, 0x11, &c->set_value)) {
    return -EIO;
  }
  // ecat_config.c has checked these; a safety net
  if (c->count.bits != 16 && c->count.bits != 32) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s.%s: the encoder needs 0x%04X:11, 16 or 32 bits\n", slave->name, name, s);
    return -EINVAL;
  }
  if (c->latch.bits && c->latch.bits != c->count.bits) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s.%s: 0x%04X:12 must have the bits of 0x%04X:11\n", slave->name, name, s, s);
    return -EINVAL;
  }

  if ((err = lcec_pin_newf_list(c, pins, slave->name, name)) != 0) return err;
  if (c->latch_c_valid.bits && c->latch.bits && c->en_latch_c.bits) {
    if ((err = lcec_pin_newf_list(c, index_pins, slave->name, name)) != 0) return err;
  }
  if (c->latch_ext_valid.bits && c->latch.bits && (c->en_latch_ext_pos.bits || c->en_latch_ext_neg.bits)) {
    if ((err = lcec_pin_newf_list(c, latch_pins, slave->name, name)) != 0) return err;
  }
  if ((err = lcec_param_newf_list(c, params, slave->name, name)) != 0) return err;
  // after the parameters are made, which zeroes them
  hal_set_real(c->pos_scale, 1.0);
  hal_set_uint(c->velocity_window_ms, 10);
  return 0;
}

int ecat_encoder_init(int comp_id, lcec_slave_t *slave) {
  lcec_slave_modparam_t *p;
  encoders_t *d;
  int n = 0, i = 0, err;

  for (p = slave->modparams; p != NULL && p->id != -1; p++) {
    if (p->id >= ECAT_MP_ENCODER && p->id <= ECAT_MP_ENCODER_LAST) n++;
  }
  if (n == 0) return 0;

  d = LCEC_HAL_ALLOCATE(encoders_t);
  d->ch = LCEC_HAL_ALLOCATE_ARRAY(channel_t, n);
  d->n = n;
  for (p = slave->modparams; p != NULL && p->id != -1; p++) {
    if (p->id >= ECAT_MP_ENCODER && p->id <= ECAT_MP_ENCODER_LAST) {
      uint16_t s = 0x6000 + 0x10 * (p->id - ECAT_MP_ENCODER);
      if ((err = channel_init(slave, &d->ch[i++], s, p->value.str)) != 0) return err;
    }
  }

  d->read = slave->proc_read;
  d->write = slave->proc_write;
  slave->encoder_data = d;
  slave->proc_read = enc_read;
  slave->proc_write = enc_write;
  return 0;
}

static uint32_t get(uint8_t *pd, const obj_t *o) {
  switch (o->bits) {
    case 0: return 0;
    case 1: return EC_READ_BIT(&pd[o->os], o->bp);
    case 16: return EC_READ_U16(&pd[o->os]);
    default: return EC_READ_U32(&pd[o->os]);
  }
}

static void put(uint8_t *pd, const obj_t *o, uint32_t v) {
  switch (o->bits) {
    case 0: break;
    case 1: EC_WRITE_BIT(&pd[o->os], o->bp, v != 0); break;
    case 16: EC_WRITE_U16(&pd[o->os], v); break;
    default: EC_WRITE_U32(&pd[o->os], v);
  }
}

/// a - b for a counter of the given bits, which wraps
static int32_t diff(uint32_t a, uint32_t b, int bits) {
  return bits == 16 ? (int16_t)(uint16_t)(a - b) : (int32_t)(a - b);
}

static double scale_or_one(double s) { return (s > -1e-20 && s < 1e-20) ? 1.0 : s; }

static void channel_read(lcec_slave_t *slave, channel_t *c, long period) {
  uint8_t *pd = slave->master->process_data;
  int bus_ok = slave->master->domain_state.wc_state == EC_WC_COMPLETE && slave->state.operational;
  double scale = scale_or_one(hal_get_real(c->pos_scale));
  uint32_t raw;
  int n, back;

  hal_set_bool(c->ok, bus_ok);
  if (!bus_ok) {
    // hold the position; continue from it when data comes back
    c->have = 0;
    c->armed_c = c->armed_ext = 0;
    c->filled = 0;
    hal_set_real(c->velocity, 0.0);
    return;
  }

  raw = get(pd, &c->count);
  if (!c->have) {
    c->last_raw = raw;
    c->have = 1;
  }
  c->ext += diff(raw, c->last_raw, c->count.bits);
  c->last_raw = raw;
  hal_set_sint(c->raw_count, c->count.bits == 16 ? (int16_t)raw : (int32_t)raw);

  if (hal_get_bool(c->reset)) c->ref = c->ext;

  // index: latch C is armed while index-enable; at the index, position 0
  if (c->index_enable != NULL && hal_get_bool(c->index_enable)) {
    if (!get(pd, &c->latch_c_valid)) {
      c->armed_c = 1;
    } else if (c->armed_c) {
      c->ref = c->ext + diff(get(pd, &c->latch), raw, c->count.bits);
      hal_set_bool(c->index_enable, 0);
      c->armed_c = 0;
    }
  } else {
    c->armed_c = 0;
  }

  // external latch: the position at the edge
  if (c->latch_enable_pos != NULL && (hal_get_bool(c->latch_enable_pos) || hal_get_bool(c->latch_enable_neg))) {
    if (!get(pd, &c->latch_ext_valid)) {
      c->armed_ext = 1;
    } else if (c->armed_ext) {
      int64_t at = c->ext + diff(get(pd, &c->latch), raw, c->count.bits);
      hal_set_real(c->latch_pos, (double)(at - c->ref) / scale);
      hal_set_bool(c->latch_enable_pos, 0);
      hal_set_bool(c->latch_enable_neg, 0);
      c->armed_ext = 0;
    }
  } else {
    c->armed_ext = 0;
  }

  hal_set_sint(c->counts, (int32_t)(c->ext - c->ref));
  hal_set_real(c->pos, (double)(c->ext - c->ref) / scale);

  // velocity over the window, from the samples before this one
  {
    // the parameter has 64 bits: cap it before it is made into a count of samples
    rtapi_uint ms = hal_get_uint(c->velocity_window_ms);
    long long samples = period > 0 ? (long long)(ms > 1000000000ULL ? 1000000000ULL : ms) * 1000000LL / period : 0;
    n = samples > HIST ? HIST : (int)samples;
  }
  if (n < 1) n = 1;
  if (n > c->filled) n = c->filled;
  if (n > 0) {
    back = (c->head - (n - 1) + HIST) % HIST;
    hal_set_real(c->velocity, (double)(c->ext - c->hist[back]) / scale / (n * (double)period * 1e-9));
  } else {
    hal_set_real(c->velocity, 0.0);
  }
  c->head = (c->head + 1) % HIST;
  c->hist[c->head] = c->ext;
  if (c->filled < HIST) c->filled++;
}

static void channel_write(lcec_slave_t *slave, channel_t *c) {
  uint8_t *pd = slave->master->process_data;

  put(pd, &c->en_latch_c, c->index_enable != NULL && hal_get_bool(c->index_enable));
  put(pd, &c->en_latch_ext_pos, c->latch_enable_pos != NULL && hal_get_bool(c->latch_enable_pos));
  put(pd, &c->en_latch_ext_neg, c->latch_enable_neg != NULL && hal_get_bool(c->latch_enable_neg));
  put(pd, &c->set_counter, 0);
  put(pd, &c->set_value, 0);
}

static void enc_read(lcec_slave_t *slave, long period) {
  encoders_t *d = slave->encoder_data;
  int i;

  if (d->read != NULL) d->read(slave, period);
  for (i = 0; i < d->n; i++) channel_read(slave, &d->ch[i], period);
}

static void enc_write(lcec_slave_t *slave, long period) {
  encoders_t *d = slave->encoder_data;
  int i;

  for (i = 0; i < d->n; i++) channel_write(slave, &d->ch[i]);
  if (d->write != NULL) d->write(slave, period);
}
