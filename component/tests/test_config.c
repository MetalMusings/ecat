//
//    Unit tests of reading ethercat.yaml (ecat_config.c).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "../devices/ecat_cia402.h"
#include "../devices/ecat_encoder.h"
#include "../ecat_config.h"
#include "fake.h"

// ---- loading and walking the records ---------------------------------

#define MAX_RECS 256

static char *buf;
static size_t len;
static void *rec[MAX_RECS];
static int n_rec;

/// Load a configuration from text; the records are in rec[], ending with
/// the null record. 0, or -1 if ecat_config_load failed.
static int load(const char *yaml) {
  char path[] = "/tmp/ecat-test-XXXXXX";
  int fd = mkstemp(path);
  size_t off = 0;

  if (fd < 0 || write(fd, yaml, strlen(yaml)) != (ssize_t)strlen(yaml)) abort();
  close(fd);
  free(buf);
  fake_msgs[0] = 0;
  n_rec = 0;
  buf = ecat_config_load(path, &len);
  unlink(path);
  if (buf == NULL) return -1;

  // as lcec_main walks them
  for (;;) {
    LCEC_CONF_TYPE_T t = ((LCEC_CONF_NULL_T *)(buf + off))->confType;
    if (n_rec == MAX_RECS) abort();
    rec[n_rec++] = buf + off;
    switch (t) {
      case lcecConfTypeNone: off += sizeof(LCEC_CONF_NULL_T); break;
      case lcecConfTypeMaster: off += sizeof(LCEC_CONF_MASTER_T); break;
      case lcecConfTypeSlave: off += sizeof(LCEC_CONF_SLAVE_T); break;
      case lcecConfTypeDcConf: off += sizeof(LCEC_CONF_DC_T); break;
      case lcecConfTypeSyncManager: off += sizeof(LCEC_CONF_SYNCMANAGER_T); break;
      case lcecConfTypePdo: off += sizeof(LCEC_CONF_PDO_T); break;
      case lcecConfTypePdoEntry: off += sizeof(LCEC_CONF_PDOENTRY_T); break;
      case lcecConfTypeComplexEntry: off += sizeof(LCEC_CONF_COMPLEXENTRY_T); break;
      case lcecConfTypeModParam: off += sizeof(LCEC_CONF_MODPARAM_T); break;
      case lcecConfTypeSdoConfig: off += sizeof(LCEC_CONF_SDOCONF_T) + ((LCEC_CONF_SDOCONF_T *)(buf + off))->length; break;
      default:
        fprintf(stderr, "unexpected record type %d\n", t);
        abort();
    }
    if (t == lcecConfTypeNone) break;
    if (off > len) abort();
  }
  CHECK_EQ(off, len);
  return 0;
}

static LCEC_CONF_TYPE_T type_of(int i) { return ((LCEC_CONF_NULL_T *)rec[i])->confType; }

/// The n-th record (from 0) of a type, or NULL.
static void *nth(LCEC_CONF_TYPE_T t, int n) {
  for (int i = 0; i < n_rec; i++)
    if (type_of(i) == t && n-- == 0) return rec[i];
  return NULL;
}

static int count(LCEC_CONF_TYPE_T t) {
  int n = 0;
  for (int i = 0; i < n_rec; i++) n += type_of(i) == t;
  return n;
}

#define MASTER ((LCEC_CONF_MASTER_T *)nth(lcecConfTypeMaster, 0))
#define SLAVE(n) ((LCEC_CONF_SLAVE_T *)nth(lcecConfTypeSlave, n))
#define SM(n) ((LCEC_CONF_SYNCMANAGER_T *)nth(lcecConfTypeSyncManager, n))
#define PDO(n) ((LCEC_CONF_PDO_T *)nth(lcecConfTypePdo, n))
#define ENTRY(n) ((LCEC_CONF_PDOENTRY_T *)nth(lcecConfTypePdoEntry, n))
#define COMPLEX(n) ((LCEC_CONF_COMPLEXENTRY_T *)nth(lcecConfTypeComplexEntry, n))
#define MODPARAM(n) ((LCEC_CONF_MODPARAM_T *)nth(lcecConfTypeModParam, n))
#define SDO(n) ((LCEC_CONF_SDOCONF_T *)nth(lcecConfTypeSdoConfig, n))
#define DC(n) ((LCEC_CONF_DC_T *)nth(lcecConfTypeDcConf, n))

/// The configuration fails with a message containing msg.
#define CHECK_FAILS(yaml, msg)                                                                      \
  do {                                                                                              \
    CHECK_EQ(load(yaml), -1);                                                                       \
    if (strstr(fake_msgs, msg) == NULL) {                                                           \
      fprintf(stderr, "%s:%d: %s: no \"%s\" in:\n%s", __FILE__, __LINE__, __func__, msg, fake_msgs); \
      fake_failures++;                                                                              \
    }                                                                                               \
  } while (0)

// ---- configurations --------------------------------------------------

#define HEAD "ecat: 1\nmaster:\n  interface: enp1s0\n  transport: xdp-native\nslaves:\n"

// An EL1008 and an EL2008 as EtherCAT Setup writes them, shortened.
static const char io[] = HEAD
    "- position: 1\n"
    "  name: in\n"
    "  identity: {vendor: '0x00000002', product: '0x03f03052'}\n"
    "  profile: mdp\n"
    "  process-data:\n"
    "    inputs:\n"
    "    - pdo: '0x1A00'\n"
    "      sm: 0\n"
    "      entries:\n"
    "      - {object: '0x6000:01', bits: 1, type: BOOL, pin: din-0, hal-type: bit}\n"
    "      - {object: '0x6010:01', bits: 1, type: BOOL, pin: din-1, hal-type: bit}\n"
    "- position: 2\n"
    "  name: out\n"
    "  identity: {vendor: 2, product: 0x07d83052}\n"
    "  profile: generic\n"
    "  configure-pdos: false\n"
    "  process-data:\n"
    "    outputs:\n"
    "    - pdo: '0x1600'\n"
    "      sm: 0\n"
    "      entries:\n"
    "      - {object: '0x7000:01', bits: 1, type: BOOL, pin: dout-0, hal-type: bit}\n"
    "    - pdo: '0x1601'\n"
    "      sm: 0\n"
    "      entries:\n"
    "      - {object: '0x7010:01', bits: 1, type: BOOL, pin: dout-1, hal-type: bit}\n"
    "      - {object: '0x0000:00', bits: 6, use: gap}\n";

static void test_io_slaves(void) {
  CHECK_EQ(load(io), 0);
  CHECK_EQ(fake_msgs[0], 0);
  CHECK_EQ(type_of(0), lcecConfTypeMaster);
  CHECK_EQ(type_of(n_rec - 1), lcecConfTypeNone);
  CHECK(!strcmp(MASTER->name, "0"));
  CHECK(!strcmp(MASTER->interface, "enp1s0"));
  CHECK(!strcmp(MASTER->transport, "xdp-native"));
  CHECK_EQ(MASTER->appTimePeriod, 0);  // the servo thread's period
  CHECK_EQ(count(lcecConfTypeSlave), 2);

  CHECK_EQ(SLAVE(0)->index, 1);
  CHECK(!strcmp(SLAVE(0)->name, "in"));
  CHECK(!strcmp(SLAVE(0)->type_name, "generic"));
  CHECK_EQ(SLAVE(0)->vid, 2);
  CHECK_EQ(SLAVE(0)->pid, 0x03f03052);
  CHECK_EQ(SLAVE(0)->configPdos, 1);
  CHECK_EQ(SLAVE(0)->syncManagerCount, 1);
  CHECK_EQ(SLAVE(0)->pdoCount, 1);
  CHECK_EQ(SLAVE(0)->pdoEntryCount, 2);
  CHECK_EQ(SLAVE(0)->pdoMappingCount, 2);
  CHECK_EQ(SM(0)->dir, EC_DIR_INPUT);
  CHECK_EQ(ENTRY(1)->index, 0x6010);
  CHECK_EQ(ENTRY(1)->subindex, 1);
  CHECK_EQ(ENTRY(1)->bitLength, 1);
  CHECK_EQ(ENTRY(1)->halType, HAL_BIT);
  CHECK(!strcmp(ENTRY(1)->halPin, "din-1"));

  // two PDOs on one sync manager, and a gap without a pin
  CHECK_EQ(SLAVE(1)->configPdos, 0);
  CHECK_EQ(SLAVE(1)->pid, 0x07d83052);
  CHECK_EQ(SLAVE(1)->syncManagerCount, 1);
  CHECK_EQ(SLAVE(1)->pdoCount, 2);
  CHECK_EQ(SLAVE(1)->pdoEntryCount, 3);
  CHECK_EQ(SLAVE(1)->pdoMappingCount, 2);
  CHECK_EQ(SM(1)->dir, EC_DIR_OUTPUT);
  CHECK_EQ(SM(1)->pdoCount, 2);
  CHECK_EQ(PDO(2)->index, 0x1601);
  CHECK_EQ(PDO(2)->pdoEntryCount, 2);
  CHECK_EQ(ENTRY(4)->bitLength, 6);
  CHECK_EQ(ENTRY(4)->halPin[0], 0);
}

static void test_sync_managers_in_order_of_first_use(void) {
  CHECK_EQ(load(HEAD
               "- position: 0\n"
               "  name: s\n"
               "  profile: generic\n"
               "  process-data:\n"
               "    outputs:\n"
               "    - {pdo: '0x1600', sm: 2, entries: [{object: '0x7000:01', bits: 16, type: UINT, pin: a, hal-type: u32}]}\n"
               "    - {pdo: '0x1601', sm: 4, entries: [{object: '0x7010:01', bits: 16, type: UINT, pin: b, hal-type: u32}]}\n"
               "    - {pdo: '0x1602', sm: 2, entries: [{object: '0x7020:01', bits: 16, type: UINT, pin: c, hal-type: u32}]}\n"
               "    inputs:\n"
               "    - {pdo: '0x1A00', sm: 3, entries: [{object: '0x6000:01', bits: 16, type: UINT, pin: d, hal-type: u32}]}\n"),
      0);
  CHECK_EQ(SLAVE(0)->syncManagerCount, 3);
  CHECK_EQ(SM(0)->index, 2);
  CHECK_EQ(SM(0)->pdoCount, 2);
  CHECK_EQ(SM(1)->index, 4);
  CHECK_EQ(SM(2)->index, 3);
  CHECK_EQ(SM(2)->dir, EC_DIR_INPUT);
  CHECK_EQ(PDO(0)->index, 0x1600);
  CHECK_EQ(PDO(1)->index, 0x1602);
  CHECK_EQ(PDO(2)->index, 0x1601);
}

static void test_sync_manager_for_outputs_and_inputs(void) {
  CHECK_FAILS(HEAD
      "- position: 0\n"
      "  name: s\n"
      "  profile: generic\n"
      "  process-data:\n"
      "    outputs:\n"
      "    - {pdo: '0x1600', sm: 2, entries: [{object: '0x7000:01', bits: 16, type: UINT, pin: a, hal-type: u32}]}\n"
      "    inputs:\n"
      "    - {pdo: '0x1A00', sm: 2, entries: [{object: '0x6000:01', bits: 16, type: UINT, pin: d, hal-type: u32}]}\n",
      "sm 2 is used for outputs and inputs");
}

#define ONE_ENTRY(entry)        \
  HEAD                          \
  "- position: 0\n"             \
  "  name: s\n"                 \
  "  profile: generic\n"        \
  "  process-data:\n"           \
  "    inputs:\n"               \
  "    - pdo: '0x1A00'\n"       \
  "      sm: 3\n"               \
  "      entries:\n"            \
  "      - " entry "\n"

static void test_hal_types(void) {
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: DINT, pin: p, hal-type: s32}")), 0);
  CHECK_EQ(ENTRY(0)->halType, HAL_S32);
  CHECK_EQ(ENTRY(0)->subType, lcecPdoEntTypeSimple);
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: UINT, pin: p, hal-type: u32}")), 0);
  CHECK_EQ(ENTRY(0)->halType, HAL_U32);
  // float from an integer: scaled
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: INT, pin: p, hal-type: float, scale: 0.5, offset: -3}")), 0);
  CHECK_EQ(ENTRY(0)->halType, HAL_FLOAT);
  CHECK_EQ(ENTRY(0)->subType, lcecPdoEntTypeFloatSigned);
  CHECK_NEAR(ENTRY(0)->floatScale, 0.5, 0);
  CHECK_NEAR(ENTRY(0)->floatOffset, -3, 0);
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: UINT, pin: p, hal-type: float}")), 0);
  CHECK_EQ(ENTRY(0)->subType, lcecPdoEntTypeFloatUnsigned);
  CHECK_NEAR(ENTRY(0)->floatScale, 1, 0);
  // IEEE floats
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: REAL, pin: p, hal-type: float}")), 0);
  CHECK_EQ(ENTRY(0)->subType, lcecPdoEntTypeFloatIeee);
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: LREAL, pin: p, hal-type: float}")), 0);
  CHECK_EQ(ENTRY(0)->subType, lcecPdoEntTypeFloatDoubleIeee);
}

static void test_entry_errors(void) {
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: UINT, pin: p, hal-type: s64}"), "hal-type \"s64\" is not supported");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: UINT, pin: p}"), "hal-type \"\" is not supported");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: WORD, pin: p, hal-type: u32}"), "unknown type \"WORD\"");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 0, type: UINT, pin: p, hal-type: u32}"), "bits must be 1 to 64");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 65, type: UINT, pin: p, hal-type: u32}"), "bits must be 1 to 64");
  CHECK_FAILS(ONE_ENTRY("{object: '6000:11', bits: 16, type: UINT, pin: p, hal-type: u32}"), "\"6000:11\" is not an object");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11x', bits: 16, type: UINT, pin: p, hal-type: u32}"), "is not an object");
  CHECK_FAILS(ONE_ENTRY("{object: '0x16000:11', bits: 16, type: UINT, pin: p, hal-type: u32}"), "is not an object");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: UINT, use: pin}"), "unknown use \"pin\"");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: sixteen, type: UINT, pin: p, hal-type: u32}"), "bits: \"sixteen\" is not a number");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: UINT, pin: p, hal-type: float, scale: x}"), "scale: \"x\" is not a number");
  // "use: profile" without a profile or encoder
  CHECK_FAILS(ONE_ENTRY("{object: '0x6041:00', bits: 16, type: UINT, use: profile}"), "needs profile cia402 or an encoder channel");
}

static void test_split(void) {
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:01', bits: 8, type: USINT, split: [a, ~, c]}")), 0);
  CHECK_EQ(ENTRY(0)->subType, lcecPdoEntTypeComplex);
  CHECK_EQ(count(lcecConfTypeComplexEntry), 2);
  CHECK_EQ(COMPLEX(0)->bitOffset, 0);
  CHECK(!strcmp(COMPLEX(0)->halPin, "a"));
  CHECK_EQ(COMPLEX(1)->bitOffset, 2);
  CHECK(!strcmp(COMPLEX(1)->halPin, "c"));
  CHECK_EQ(COMPLEX(1)->halType, HAL_BIT);
  CHECK_EQ(SLAVE(0)->pdoEntryCount, 1);
  CHECK_EQ(SLAVE(0)->pdoMappingCount, 2);
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:01', bits: 2, type: USINT, split: [a, b, c]}"), "split has more pins than bits");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:01', bits: 8, type: USINT, pin: p, hal-type: u32, split: [a]}"), "give pin or split, not both");
}

static void test_file_errors(void) {
  size_t l;
  fake_msgs[0] = 0;
  CHECK(ecat_config_load("", &l) == NULL);
  CHECK(strstr(fake_msgs, "loadrt ecat config=") != NULL);
  fake_msgs[0] = 0;
  CHECK(ecat_config_load("/nonexistent/ethercat.yaml", &l) == NULL);
  CHECK(strstr(fake_msgs, "cannot open /nonexistent/ethercat.yaml") != NULL);
  CHECK_FAILS("ecat: 1\nslaves: [\n- a\n", "line ");
  CHECK_FAILS("master: {}\nslaves: []\n", "not an ecat configuration of format version 1");
  CHECK_FAILS("ecat: 2\nslaves: []\n", "format version 1");
  CHECK_FAILS("ecat: 1\nmaster: {}\nslaves: []\n", "no slaves");
}

static void test_slave_errors(void) {
  CHECK_FAILS(HEAD "- {name: s, profile: generic}\n", "slave -1: position missing");
  CHECK_FAILS(HEAD "- {position: 3, profile: generic}\n", "slave 3: name missing");
  CHECK_FAILS(HEAD "- {position: 3, name: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, profile: generic}\n", "is too long");
  CHECK_FAILS(HEAD "- {position: 3, name: s, profile: ds402}\n", "profile \"ds402\" is not supported");
  CHECK_FAILS(HEAD "- {position: 3, name: s}\n", "profile \"\" is not supported");
  CHECK_FAILS(HEAD "- {position: 3, name: s, profile: generic, configure-pdos: maybe}\n", "\"maybe\" is not true or false");
  // every error is reported, not only the first
  CHECK_FAILS(HEAD "- {position: 3, profile: ds402}\n- {position: x, name: t, profile: generic}\n", "name missing");
  CHECK(strstr(fake_msgs, "ds402") != NULL);
  CHECK(strstr(fake_msgs, "position: \"x\" is not a number") != NULL);
}

static void test_dc(void) {
  CHECK_EQ(load("ecat: 1\n"
                "master:\n"
                "  dc: {ref-clock-sync-cycles: -1, sync-to-ref-clock: true, sync0-shift-ns: 100000}\n"
                "slaves:\n"
                "- position: 0\n"
                "  name: d\n"
                "  profile: generic\n"
                "  dc: {assign-activate: '0x300', sync0-shift-ns: -5000}\n"
                "- position: 1\n"
                "  name: e\n"
                "  profile: generic\n"
                "  dc: {assign-activate: '0x300', sync0-cycles: 2}\n"),
      0);
  CHECK_EQ(MASTER->refClockSyncCycles, -1);
  CHECK_EQ(MASTER->syncToRefClock, 1);
  CHECK_EQ(MASTER->globalShiftTime, 100000);
  CHECK_EQ(DC(0)->assignActivate, 0x300);
  CHECK_EQ(DC(0)->sync0Cycle, 1);  // servo periods
  CHECK_EQ(DC(0)->sync0Shift, -5000);
  CHECK_EQ(DC(1)->sync0Cycle, 2);
  // without dc: none
  CHECK_EQ(load(io), 0);
  CHECK_EQ(count(lcecConfTypeDcConf), 0);
  CHECK_EQ(MASTER->syncToRefClock, 0);
}

#define SETTINGS(list) HEAD "- position: 0\n  name: s\n  profile: generic\n  settings:\n" list

static void test_settings(void) {
  CHECK_EQ(load(SETTINGS("  - {object: '0x8000:01', type: UINT, value: '0x1234'}\n"
                         "  - {object: '0x8000:02', type: DINT, value: -2}\n"
                         "  - {object: '0x8000:03', type: REAL, value: 1.5}\n"
                         "  - {object: '0x8000:04', data: '01 02 ab'}\n"
                         "  - {object: '0x8000:05', type: BOOL, value: 1}\n")),
      0);
  CHECK_EQ(count(lcecConfTypeSdoConfig), 5);
  CHECK_EQ(SDO(0)->index, 0x8000);
  CHECK_EQ(SDO(0)->subindex, 1);
  CHECK_EQ(SDO(0)->length, 2);
  CHECK(!memcmp(SDO(0)->data, "\x34\x12", 2));
  CHECK_EQ(SDO(1)->length, 4);
  CHECK(!memcmp(SDO(1)->data, "\xfe\xff\xff\xff", 4));
  {
    float f = 1.5f;
    CHECK_EQ(SDO(2)->length, 4);
    CHECK(!memcmp(SDO(2)->data, &f, 4));
  }
  CHECK_EQ(SDO(3)->length, 3);
  CHECK(!memcmp(SDO(3)->data, "\x01\x02\xab", 3));
  CHECK_EQ(SDO(4)->length, 1);
  CHECK_EQ(SLAVE(0)->sdoConfigLength, 5 * sizeof(LCEC_CONF_SDOCONF_T) + 2 + 4 + 4 + 3 + 1);

  CHECK_FAILS(SETTINGS("  - {object: '0x8000:01', data: '0102x'}\n"), "is not hex bytes");
  CHECK_FAILS(SETTINGS("  - {object: '0x8000:01', data: '012'}\n"), "is not hex bytes");
  CHECK_FAILS(SETTINGS("  - {object: '0x8000:01', value: 3}\n"), "setting 0x8000:01: unknown type");
  CHECK_FAILS(SETTINGS("  - {object: '0x8000:01', type: UINT}\n"), "give value and type, or data");
  CHECK_FAILS(SETTINGS("  - {object: '0x8000:01', type: UINT, value: 1.5}\n"), "value: \"1.5\" is not a number");
}

// ---- the cia402 profile ----------------------------------------------

#define CIA402(mode, outputs, inputs)                                                 \
  HEAD                                                                                \
  "- position: 0\n"                                                                   \
  "  name: x\n"                                                                       \
  "  profile: cia402\n"                                                               \
  "  cia402: {mode: " mode "}\n"                                                      \
  "  process-data:\n"                                                                 \
  "    outputs:\n"                                                                    \
  "    - pdo: '0x1600'\n"                                                             \
  "      sm: 2\n"                                                                     \
  "      entries:\n" outputs                                                          \
  "    inputs:\n"                                                                     \
  "    - pdo: '0x1A00'\n"                                                             \
  "      sm: 3\n"                                                                     \
  "      entries:\n" inputs

#define E(obj, bits, type, rest) "      - {object: '" obj "', bits: " #bits ", type: " type ", " rest "}\n"
#define P(obj, bits, type) E(obj, bits, type, "use: profile")
#define CSP_OUT P("0x6040:00", 16, "UINT") P("0x6060:00", 8, "SINT") P("0x607A:00", 32, "DINT")
#define CSP_IN P("0x6041:00", 16, "UINT") P("0x6061:00", 8, "SINT") P("0x6064:00", 32, "DINT")

static void test_cia402_csp(void) {
  CHECK_EQ(load(CIA402("csp", CSP_OUT, CSP_IN E("0x6077:00", 16, "INT", "pin: torque, hal-type: s32"))), 0);
  CHECK_EQ(fake_msgs[0], 0);
  CHECK(!strcmp(SLAVE(0)->type_name, "cia402"));
  CHECK_EQ(SLAVE(0)->modParamCount, 1);
  CHECK_EQ(MODPARAM(0)->id, ECAT_MP_CIA402_MODE);
  CHECK_EQ(MODPARAM(0)->value.u32, 8);
  // the profile's entries are in the process data, without pins
  CHECK_EQ(SLAVE(0)->pdoEntryCount, 7);
  CHECK_EQ(SLAVE(0)->pdoMappingCount, 1);
  CHECK_EQ(ENTRY(0)->index, 0x6040);
  CHECK_EQ(ENTRY(0)->halPin[0], 0);
}

static void test_cia402_csv(void) {
  CHECK_EQ(load(CIA402("csv", P("0x6040:00", 16, "UINT") P("0x60FF:00", 32, "DINT"),
               P("0x6041:00", 16, "UINT") P("0x606C:00", 32, "DINT"))),
      0);
  CHECK_EQ(MODPARAM(0)->value.u32, 9);
  // CSV does not need the position objects, but CSP does
  CHECK_FAILS(CIA402("csp", P("0x6040:00", 16, "UINT") P("0x60FF:00", 32, "DINT"),
                  P("0x6041:00", 16, "UINT") P("0x606C:00", 32, "DINT")),
      "needs 0x607A:00");
  CHECK(strstr(fake_msgs, "needs 0x6064:00") != NULL);
}

static void test_cia402_errors(void) {
  CHECK_FAILS(CIA402("pp", CSP_OUT, CSP_IN), "cia402.mode must be csp or csv");
  CHECK_FAILS(CIA402("csp", P("0x6060:00", 8, "SINT") P("0x607A:00", 32, "DINT"), CSP_IN), "needs 0x6040:00");
  // a profile object with a pin
  CHECK_FAILS(CIA402("csp", CSP_OUT, P("0x6041:00", 16, "UINT") P("0x6064:00", 32, "DINT")
                                         E("0x6061:00", 8, "SINT", "pin: mode, hal-type: s32")),
      "0x6061:00: the cia402 profile handles this object");
  // in the wrong direction, with the wrong bits
  CHECK_FAILS(CIA402("csp", CSP_OUT P("0x6064:00", 32, "DINT"), P("0x6041:00", 16, "UINT")), "0x6064:00: belongs in the inputs");
  CHECK_FAILS(CIA402("csp", CSP_OUT, P("0x6041:00", 16, "UINT") P("0x6064:00", 16, "INT")), "needs 32 bits");
  // a profile entry that the profile does not handle, or with a pin
  CHECK_FAILS(CIA402("csp", CSP_OUT, CSP_IN P("0x6077:00", 16, "INT")), "0x6077:00: the cia402 profile does not handle this object");
  CHECK_FAILS(CIA402("csp", CSP_OUT, CSP_IN E("0x603F:00", 16, "UINT", "use: profile, pin: e")), "has no pin");
  // a subindex other than 0 is not the profile's
  CHECK_FAILS(CIA402("csp", CSP_OUT, CSP_IN P("0x6041:01", 16, "UINT")), "does not handle this object");
}

static void test_cia402_modes(void) {
  CHECK_EQ(load(CIA402("csp, modes: [hm]", CSP_OUT, CSP_IN)), 0);
  CHECK_EQ(SLAVE(0)->modParamCount, 2);
  CHECK_EQ(MODPARAM(0)->id, ECAT_MP_CIA402_MODE);
  CHECK_EQ(MODPARAM(1)->id, ECAT_MP_CIA402_MODES);
  CHECK_EQ(MODPARAM(1)->value.u32, 1u << 6);
  // without modes: no record
  CHECK_EQ(load(CIA402("csp, modes: []", CSP_OUT, CSP_IN)), 0);
  CHECK_EQ(SLAVE(0)->modParamCount, 1);
  CHECK_FAILS(CIA402("csp, modes: [pp]", CSP_OUT, CSP_IN), "cia402.modes: \"pp\" is not supported (hm)");
  CHECK_FAILS(CIA402("csp, modes: hm", CSP_OUT, CSP_IN), "cia402.modes must be a list");
  // changing modes needs the mode and its display
  CHECK_FAILS(CIA402("csp, modes: [hm]", P("0x6040:00", 16, "UINT") P("0x607A:00", 32, "DINT"),
                  P("0x6041:00", 16, "UINT") P("0x6064:00", 32, "DINT")),
      "needs 0x6060:00 with \"use: profile\" to change modes");
  CHECK(strstr(fake_msgs, "needs 0x6061:00") != NULL);
}

// ---- encoder channels -------------------------------------------------

#define ENCODER(encoders, outputs, inputs) \
  HEAD                                     \
  "- position: 0\n"                        \
  "  name: e\n"                            \
  "  profile: mdp\n"                       \
  "  encoders: " encoders "\n"             \
  "  process-data:\n"                      \
  "    outputs:\n"                         \
  "    - pdo: '0x1600'\n"                  \
  "      sm: 2\n"                          \
  "      entries:\n" outputs               \
  "    inputs:\n"                          \
  "    - pdo: '0x1A00'\n"                  \
  "      sm: 3\n"                          \
  "      entries:\n" inputs

#define EL5101_OUT                                                                                   \
  P("0x7000:01", 1, "BOOL") P("0x7000:02", 1, "BOOL") P("0x7000:03", 1, "BOOL") P("0x7000:04", 1, "BOOL") \
  E("0x0000:00", 4, "BOOL", "use: gap") P("0x7000:11", 32, "UDINT")
#define EL5101_IN                                                                                    \
  P("0x6000:01", 1, "BOOL") P("0x6000:02", 1, "BOOL") P("0x6000:03", 1, "BOOL")                     \
  E("0x6000:04", 1, "BOOL", "pin: overflow, hal-type: bit") E("0x0000:00", 4, "BOOL", "use: gap")    \
  P("0x6000:11", 32, "UDINT") P("0x6000:12", 32, "UDINT")

static void test_encoder(void) {
  CHECK_EQ(load(ENCODER("[{name: enc, index: '0x6000'}]", EL5101_OUT, EL5101_IN)), 0);
  CHECK_EQ(fake_msgs[0], 0);
  CHECK(!strcmp(SLAVE(0)->type_name, "generic"));
  CHECK_EQ(SLAVE(0)->modParamCount, 1);
  CHECK_EQ(MODPARAM(0)->id, ECAT_MP_ENCODER);
  CHECK(!strcmp(MODPARAM(0)->value.str, "enc"));
  CHECK_EQ(SLAVE(0)->pdoMappingCount, 1);  // overflow
}

static void test_encoder_channels(void) {
  // the second channel of an EL5152, 16 bits, only the counter
  CHECK_EQ(load(ENCODER("[{name: a, index: '0x6000'}, {name: b, index: '0x6010'}]", P("0x7000:01", 1, "BOOL"),
               P("0x6000:11", 32, "UDINT") P("0x6010:11", 16, "UINT"))),
      0);
  CHECK_EQ(SLAVE(0)->modParamCount, 2);
  CHECK_EQ(MODPARAM(1)->id, ECAT_MP_ENCODER + 1);
  CHECK(!strcmp(MODPARAM(1)->value.str, "b"));
  // with the cia402 profile (EL7041 has an encoder and a drive)
  CHECK_EQ(load(HEAD
               "- position: 0\n"
               "  name: x\n"
               "  profile: cia402\n"
               "  cia402: {mode: csv}\n"
               "  encoders: [{name: enc, index: '0x6000'}]\n"
               "  process-data:\n"
               "    outputs:\n"
               "    - pdo: '0x1600'\n"
               "      sm: 2\n"
               "      entries:\n" P("0x6040:00", 16, "UINT") P("0x60FF:00", 32, "DINT")
               "    inputs:\n"
               "    - pdo: '0x1A00'\n"
               "      sm: 3\n"
               "      entries:\n" P("0x6041:00", 16, "UINT") P("0x606C:00", 32, "DINT") P("0x6000:11", 32, "UDINT")),
      0);
  CHECK_EQ(SLAVE(0)->modParamCount, 2);
  CHECK_EQ(MODPARAM(0)->id, ECAT_MP_CIA402_MODE);
  CHECK_EQ(MODPARAM(1)->id, ECAT_MP_ENCODER);
}

static void test_encoder_errors(void) {
  CHECK_FAILS(ENCODER("[{name: enc, index: '0x6000'}]", EL5101_OUT, P("0x6000:01", 1, "BOOL")),
      "encoder enc needs 0x6000:11 (counter value)");
  CHECK_FAILS(ENCODER("[{name: enc, index: '0x6008'}]", EL5101_OUT, EL5101_IN), "index must be the channel's status index");
  CHECK_FAILS(ENCODER("[{name: enc, index: '0x7000'}]", EL5101_OUT, EL5101_IN), "index must be");
  CHECK_FAILS(ENCODER("[{index: '0x6000'}]", EL5101_OUT, EL5101_IN), "encoder 1: name missing");
  CHECK_FAILS(ENCODER("[{name: a, index: '0x6000'}, {name: a, index: '0x6010'}]", "", P("0x6000:11", 32, "UDINT") P("0x6010:11", 32, "UDINT")),
      "the name is used twice");
  CHECK_FAILS(ENCODER("[{name: a, index: '0x6000'}, {name: b, index: '0x6000'}]", "", P("0x6000:11", 32, "UDINT")),
      "index 0x6000 is used twice");
  CHECK_FAILS(ENCODER("{name: a, index: '0x6000'}", "", P("0x6000:11", 32, "UDINT")), "encoders must be a list");
  // its objects need "use: profile", with the right bits and direction
  CHECK_FAILS(ENCODER("[{name: enc, index: '0x6000'}]", EL5101_OUT,
                  P("0x6000:11", 32, "UDINT") E("0x6000:12", 32, "UDINT", "pin: latch, hal-type: s32")),
      "encoder channel enc handles this object");
  CHECK_FAILS(ENCODER("[{name: enc, index: '0x6000'}]", "", P("0x6000:11", 8, "USINT")), "the encoder needs 16 or 32 bits");
  CHECK_FAILS(ENCODER("[{name: enc, index: '0x6000'}]", "", P("0x6000:11", 32, "UDINT") P("0x6000:01", 8, "USINT")),
      "the encoder needs 1 bits");
  CHECK_FAILS(ENCODER("[{name: enc, index: '0x6000'}]", "", P("0x6000:11", 32, "UDINT") P("0x6000:12", 16, "UINT")),
      "need the same bits");
  CHECK_FAILS(ENCODER("[{name: enc, index: '0x6000'}]", P("0x6000:11", 32, "UDINT"), ""), "0x6000:11: belongs in the inputs");
  CHECK_FAILS(ENCODER("[{name: enc, index: '0x6000'}]", "", P("0x6000:11", 32, "UDINT") P("0x6000:13", 32, "UDINT")),
      "no encoder channel handles this object");
}

static void test_too_many_encoders(void) {
  static char list[2048], yaml[8192];
  strcpy(list, "[");
  for (int i = 0; i < 17; i++) {
    snprintf(list + strlen(list), sizeof(list) - strlen(list), "%s{name: c%d, index: '0x6%02X0'}", i ? ", " : "", i, i);
  }
  strcat(list, "]");
  snprintf(yaml, sizeof(yaml), ENCODER("%s", "", P("0x6000:11", 32, "UDINT")), list);
  CHECK_FAILS(yaml, "more than 16 encoder channels");
}

RUN_TESTS("config", test_io_slaves, test_sync_managers_in_order_of_first_use, test_sync_manager_for_outputs_and_inputs,
    test_hal_types, test_entry_errors, test_split, test_file_errors, test_slave_errors, test_dc, test_settings,
    test_cia402_csp, test_cia402_csv, test_cia402_errors, test_cia402_modes, test_encoder, test_encoder_channels, test_encoder_errors,
    test_too_many_encoders)
