//
//    ecat: the serial function (see ecat_serial.h).
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
/// @brief Serial channels on a generic slave.
///
/// A channel is two PDOs, both owned by the function (`use: profile`):
/// the output PDO starts with the control byte and the output length,
/// the input PDO with the status byte and the input length; the rest of
/// each are the data bytes (22 on an EL6021 with PDO 0x1602/0x1A02 or
/// 0x1604/0x1A04). The baud rate and data frame of the file are written
/// at start (as settings); the program on the port may change the baud
/// rate (and the stop bits) while running, through SDO requests.

#include "ecat_serial.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

ecat_serial_channel_t *ecat_serial_channels = NULL;

typedef struct {
  ecat_serial_channel_t *ch;  // not in HAL memory: the bridge thread uses it
  unsigned int out_os, in_os;
  ec_sdo_request_t *req_baud, *req_frame, *active;
  // pins
  hal_uint_t state;
  hal_uint_t tx_bytes, rx_bytes;
  hal_uint_t parity_errors, framing_errors, overrun_errors, buffer_full;
  hal_uint_t baud;
  hal_bool_t settings_error;
  hal_bool_t port_open;
} chan_t;

typedef struct {
  lcec_slave_rw_t read, write;  // the slave's own
  int n;
  chan_t *ch;
} serial_t;

static const lcec_pindesc_t pins[] = {
    {HAL_UINT, HAL_OUT, offsetof(chan_t, state), "%s.%s.state"},
    {HAL_UINT, HAL_OUT, offsetof(chan_t, tx_bytes), "%s.%s.tx-bytes"},
    {HAL_UINT, HAL_OUT, offsetof(chan_t, rx_bytes), "%s.%s.rx-bytes"},
    {HAL_UINT, HAL_OUT, offsetof(chan_t, parity_errors), "%s.%s.parity-errors"},
    {HAL_UINT, HAL_OUT, offsetof(chan_t, framing_errors), "%s.%s.framing-errors"},
    {HAL_UINT, HAL_OUT, offsetof(chan_t, overrun_errors), "%s.%s.overrun-errors"},
    {HAL_UINT, HAL_OUT, offsetof(chan_t, buffer_full), "%s.%s.buffer-full"},
    {HAL_UINT, HAL_OUT, offsetof(chan_t, baud), "%s.%s.baud"},
    {HAL_BOOL, HAL_OUT, offsetof(chan_t, settings_error), "%s.%s.settings-error"},
    {HAL_BOOL, HAL_OUT, offsetof(chan_t, port_open), "%s.%s.port-open"},
    {HAL_TYPE_UNSPECIFIED, HAL_DIR_UNSPECIFIED, -1, NULL},
};

static void ser_read(lcec_slave_t *slave, long period);
static void ser_write(lcec_slave_t *slave, long period);

// ---- settings while running, through SDO requests ----

static int sdo_start(void *ctx, uint16_t index, uint8_t subindex, uint8_t value) {
  chan_t *c = ctx;
  (void)index;
  c->active = subindex == ECAT_SER_SUB_BAUD ? c->req_baud : c->req_frame;
  if (c->active == NULL) return -1;
  EC_WRITE_U8(ecrt_sdo_request_data(c->active), value);
  ecrt_sdo_request_write(c->active);
  return 0;
}

static int sdo_poll(void *ctx) {
  chan_t *c = ctx;
  if (c->active == NULL) return -1;
  switch (ecrt_sdo_request_state(c->active)) {
    case EC_REQUEST_SUCCESS: return 1;
    case EC_REQUEST_ERROR: return -1;
    default: return 0;
  }
}

// ---- set up ----

/// The PDO's first entry's place in the process data and its size in bits.
static int find_pdo(lcec_slave_t *slave, uint16_t pdo, unsigned int *os, int *bits) {
  const ec_sync_info_t *sm;
  unsigned int i, j, bp;
  for (sm = slave->generic_sync_managers; sm != NULL && sm->index != 0xff; sm++) {
    for (i = 0; i < sm->n_pdos; i++) {
      const ec_pdo_info_t *p = &sm->pdos[i];
      if (p->index != pdo || p->n_entries == 0) continue;
      *bits = 0;
      for (j = 0; j < p->n_entries; j++) *bits += p->entries[j].bit_length;
      // every entry is registered, so that the domain has the whole PDO
      for (j = 1; j < p->n_entries; j++) {
        unsigned int o2, b2;
        if (p->entries[j].index == 0) continue;  // a gap
        if (lcec_pdo_init(slave, p->entries[j].index, p->entries[j].subindex, &o2, &b2)) return -EIO;
      }
      if (lcec_pdo_init(slave, p->entries[0].index, p->entries[0].subindex, os, &bp)) return -EIO;
      return bp == 0 ? 0 : -EINVAL;
    }
  }
  return -ENOENT;
}

static uint32_t mp_u32(lcec_slave_t *slave, int id) {
  lcec_slave_modparam_t *p;
  for (p = slave->modparams; p != NULL && p->id != -1; p++)
    if (p->id == id) return p->value.u32;
  return 0;
}

static const char *mp_str(lcec_slave_t *slave, int id) {
  lcec_slave_modparam_t *p;
  for (p = slave->modparams; p != NULL && p->id != -1; p++)
    if (p->id == id) return p->value.str;
  return NULL;
}

static int channel_init(lcec_slave_t *slave, chan_t *c, int k) {
  int base = ECAT_MP_SERIAL + 8 * k, out_bits = 0, in_bits = 0, err, n;
  uint32_t pdos = mp_u32(slave, base + ECAT_MP_SERIAL_PDOS);
  uint32_t set = mp_u32(slave, base + ECAT_MP_SERIAL_SETTINGS);
  const char *name = mp_str(slave, base + ECAT_MP_SERIAL_NAME);
  const char *port = mp_str(slave, base + ECAT_MP_SERIAL_PORT);
  uint16_t settings_index = set & 0xffff;
  uint8_t baud = (set >> 16) & 0xff, frame = (set >> 24) & 0xff;
  int bits = 8, stop = 1;
  char parity = 'N';
  ecat_ser_sdo_t sdo = {sdo_start, sdo_poll, c};

  if (name == NULL || port == NULL) return -EINVAL;
  if ((err = find_pdo(slave, pdos >> 16, &c->out_os, &out_bits)) != 0 ||
      (err = find_pdo(slave, pdos & 0xffff, &c->in_os, &in_bits)) != 0) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s.%s: PDOs 0x%04X and 0x%04X not found or not byte aligned\n",
        slave->name, name, pdos >> 16, pdos & 0xffff);
    return err;
  }
  // ecat_config.c has checked these; a safety net
  n = out_bits / 8 - 2;
  if (out_bits != in_bits || out_bits % 8 || n < 1 || n > ECAT_SER_MAX_DATA) {
    rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s.%s: the PDOs need 3 to 24 bytes each, the same\n", slave->name, name);
    return -EINVAL;
  }
  if ((c->ch = calloc(1, sizeof(*c->ch))) == NULL) return -ENOMEM;
  ecat_ser_init(&c->ch->core, n, settings_index, baud, frame, &sdo);
  snprintf(c->ch->name, sizeof(c->ch->name), "%s.%s", slave->name, name);
  snprintf(c->ch->port, sizeof(c->ch->port), "%s", port);
  ecat_ser_frame_of(frame, &bits, &parity, &stop);
  c->ch->data_bits = (char)bits;
  c->ch->parity = parity;

  if (slave->config != NULL) {
    c->req_baud = ecrt_slave_config_create_sdo_request(slave->config, settings_index, ECAT_SER_SUB_BAUD, 1);
    c->req_frame = ecrt_slave_config_create_sdo_request(slave->config, settings_index, ECAT_SER_SUB_FRAME, 1);
    if (c->req_baud == NULL || c->req_frame == NULL) {
      rtapi_print_msg(RTAPI_MSG_ERR, LCEC_MSG_PFX "%s.%s: cannot make the SDO requests for the settings\n", slave->name, name);
      return -ENOMEM;
    }
    ecrt_sdo_request_timeout(c->req_baud, 1000);
    ecrt_sdo_request_timeout(c->req_frame, 1000);
  }
  if ((err = lcec_pin_newf_list(c, pins, slave->name, name)) != 0) return err;
  c->ch->next = ecat_serial_channels;
  ecat_serial_channels = c->ch;
  return 0;
}

int ecat_serial_init(int comp_id, lcec_slave_t *slave) {
  serial_t *d;
  int n = 0, k;
  (void)comp_id;
  for (k = 0; k < ECAT_MP_SERIAL_MAX_CHANNELS; k++) {
    if (mp_str(slave, ECAT_MP_SERIAL + 8 * k + ECAT_MP_SERIAL_NAME) != NULL) n = k + 1;
  }
  if (n == 0) return 0;
  d = LCEC_HAL_ALLOCATE(serial_t);
  d->ch = LCEC_HAL_ALLOCATE_ARRAY(chan_t, n);
  d->n = n;
  for (k = 0; k < n; k++) {
    int err = channel_init(slave, &d->ch[k], k);
    if (err) return err;
  }
  d->read = slave->proc_read;
  d->write = slave->proc_write;
  slave->serial_data = d;
  slave->proc_read = ser_read;
  slave->proc_write = ser_write;
  return 0;
}

// ---- cycle ----

static void channel_cycle(lcec_slave_t *slave, chan_t *c, long period) {
  uint8_t *pd = slave->master->process_data;
  int bus_ok = slave->master->domain_state.wc_state == EC_WC_COMPLETE && slave->state.operational;
  ecat_ser_t *s = &c->ch->core;
  ecat_ser_cycle(s, pd + c->in_os, pd + c->out_os, bus_ok, period);
  hal_set_uint(c->state, s->state);
  hal_set_uint(c->tx_bytes, s->tx_bytes);
  hal_set_uint(c->rx_bytes, s->rx_bytes);
  hal_set_uint(c->parity_errors, s->parity_errors);
  hal_set_uint(c->framing_errors, s->framing_errors);
  hal_set_uint(c->overrun_errors, s->overrun_errors);
  hal_set_uint(c->buffer_full, s->buffer_full);
  hal_set_uint(c->baud, ecat_ser_baud_of(s->cur_baud));
  hal_set_bool(c->settings_error, atomic_load_explicit(&s->settings_failed, memory_order_relaxed));
  hal_set_bool(c->port_open, atomic_load_explicit(&c->ch->port_open, memory_order_relaxed));
}

static void ser_read(lcec_slave_t *slave, long period) {
  serial_t *d = slave->serial_data;
  int k;
  if (d->read != NULL) d->read(slave, period);
  // the handshake needs this cycle's status; its outputs go out with this
  // cycle's frame (nothing else writes them)
  for (k = 0; k < d->n; k++) channel_cycle(slave, &d->ch[k], period);
}

static void ser_write(lcec_slave_t *slave, long period) {
  serial_t *d = slave->serial_data;
  if (d->write != NULL) d->write(slave, period);
}
