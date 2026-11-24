//
//    Unit tests of reading ethercat.yaml (ecat_config.c).
//
//    This program is free software; you can redistribute it and/or modify
//    it under the terms of the GNU General Public License as published by
//    the Free Software Foundation; either version 2 of the License, or
//    (at your option) any later version.
//

#include <arpa/inet.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "../devices/ecat_cia402.h"
#include "../devices/ecat_encoder.h"
#include "../devices/ecat_serial.h"
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
      case lcecConfTypeStateTimeouts: off += sizeof(LCEC_CONF_STATE_TIMEOUTS_T); break;
      case lcecConfTypeSdoCycleTime: off += sizeof(LCEC_CONF_SDO_CYCLE_T); break;
      case lcecConfTypeMaster: off += sizeof(LCEC_CONF_MASTER_T); break;
      case lcecConfTypeSlave: off += sizeof(LCEC_CONF_SLAVE_T); break;
      case lcecConfTypeDcConf: off += sizeof(LCEC_CONF_DC_T); break;
      case lcecConfTypeEoe: off += sizeof(LCEC_CONF_EOE_T); break;
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
#define EOE(n) ((LCEC_CONF_EOE_T *)nth(lcecConfTypeEoe, n))
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
  CHECK_EQ(ENTRY(1)->halType, HAL_BOOL);
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

static void test_hal_type_spellings(void) {
  // 2.10 names, and the 2.9 spellings of the same types
#define SPELLING(type, bits, hal, want) \
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: " #bits ", type: " type ", pin: p, hal-type: " hal "}")), 0); \
  CHECK_EQ(ENTRY(0)->halType, want)
  SPELLING("BOOL", 1, "bool", HAL_BOOL);
  SPELLING("BOOL", 1, "bit", HAL_BOOL);
  SPELLING("DINT", 32, "sint", HAL_SINT);
  SPELLING("DINT", 32, "s32", HAL_SINT);
  SPELLING("UDINT", 32, "uint", HAL_UINT);
  SPELLING("UDINT", 32, "u32", HAL_UINT);
  SPELLING("DINT", 32, "real", HAL_REAL);
  SPELLING("DINT", 32, "float", HAL_REAL);
#undef SPELLING
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: DINT, pin: p, hal-type: s64}"), "is not supported (bool, sint, uint, real)");
}

static void test_extend(void) {
  // an input counter that keeps counting past its wrap, in 64 bits (2.9: 32)
#if ECAT_HAL_64
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: UDINT, pin: p, hal-type: sint, extend: true}")), 0);
  CHECK_EQ(ENTRY(0)->extend, 1);
#else
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: UDINT, pin: p, hal-type: sint, extend: true}"),
      "LinuxCNC 2.9's pins have 32 bits: extend is for an entry of 8 to 16 bits there");
#endif
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: UINT, pin: p, hal-type: s32, extend: true}")), 0);
  CHECK_EQ(ENTRY(0)->extend, 1);
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: UDINT, pin: p, hal-type: sint}")), 0);
  CHECK_EQ(ENTRY(0)->extend, 0);
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: UDINT, pin: p, hal-type: sint, extend: false}")), 0);
  CHECK_EQ(ENTRY(0)->extend, 0);
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: UDINT, pin: p, hal-type: uint, extend: true}"),
      "extend is for an input entry of 8 to 32 bits with hal-type sint");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: DINT, pin: p, hal-type: real, extend: true}"),
      "extend is for an input entry");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: LINT, pin: p, hal-type: sint, extend: true}"),
      ECAT_HAL_64 ? "extend is for an input entry" : "a LINT entry takes hal-type real");
  CHECK_FAILS(HEAD
      "- position: 0\n"
      "  name: s\n"
      "  profile: generic\n"
      "  process-data:\n"
      "    outputs:\n"
      "    - {pdo: '0x1600', sm: 2, entries: [{object: '0x7000:01', bits: 32, type: DINT, pin: a, hal-type: sint, extend: true}]}\n",
      "extend is for an input entry");
}

static void test_raw(void) {
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: UDINT, pin: p, hal-type: uint, raw: true}")), 0);
  CHECK_EQ(ENTRY(0)->raw, 1);
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: UDINT, pin: p, hal-type: uint}")), 0);
  CHECK_EQ(ENTRY(0)->raw, 0);
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: DINT, pin: p, hal-type: sint, raw: true}"),
      "raw is for an entry of up to 32 bits with hal-type uint");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: ULINT, pin: p, hal-type: uint, raw: true}"),
      ECAT_HAL_64 ? "raw is for an entry of up to 32 bits" : "a ULINT entry takes hal-type real");
}

static void test_hal_types(void) {
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 32, type: DINT, pin: p, hal-type: s32}")), 0);
  CHECK_EQ(ENTRY(0)->halType, HAL_SINT);
  CHECK_EQ(ENTRY(0)->subType, lcecPdoEntTypeSimple);
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: UINT, pin: p, hal-type: u32}")), 0);
  CHECK_EQ(ENTRY(0)->halType, HAL_UINT);
  // float from an integer: scaled
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: INT, pin: p, hal-type: float, scale: 0.5, offset: -3}")), 0);
  CHECK_EQ(ENTRY(0)->halType, HAL_REAL);
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
  // 64-bit integers: as 64-bit integer pins (2.10 only), or as float (up to 2^53)
#if ECAT_HAL_64
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: LINT, pin: p, hal-type: sint}")), 0);
  CHECK_EQ(ENTRY(0)->halType, HAL_SINT);
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: ULINT, pin: p, hal-type: uint}")), 0);
  CHECK_EQ(ENTRY(0)->halType, HAL_UINT);
#else
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: LINT, pin: p, hal-type: sint}"),
      "LinuxCNC 2.9 has no 64-bit pins: a LINT entry takes hal-type real");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: ULINT, pin: p, hal-type: uint}"),
      "LinuxCNC 2.9 has no 64-bit pins: a ULINT entry takes hal-type real");
#endif
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: LINT, pin: p, hal-type: float}")), 0);
  CHECK_EQ(ENTRY(0)->subType, lcecPdoEntTypeFloatSigned);
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: ULINT, pin: p, hal-type: float}")), 0);
  CHECK_EQ(ENTRY(0)->subType, lcecPdoEntTypeFloatUnsigned);
}

static void test_entry_errors(void) {
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: UINT, pin: p, hal-type: s64}"), "hal-type \"s64\" is not supported");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 16, type: UINT, pin: p}"), "hal-type \"\" is not supported");
#if ECAT_HAL_64
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: LINT, pin: p, hal-type: s32}"), "a LINT entry takes hal-type sint (or real)");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: ULINT, pin: p, hal-type: u32}"), "a ULINT entry takes hal-type uint (or real)");
  // an integer pin of the other sign is no better
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: LINT, pin: p, hal-type: uint}"), "a LINT entry takes hal-type sint");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: ULINT, pin: p, hal-type: sint}"), "a ULINT entry takes hal-type uint");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: LINT, pin: p}"), "a LINT entry takes hal-type sint");
#else
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: LINT, pin: p, hal-type: s32}"), "a LINT entry takes hal-type real");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: ULINT, pin: p, hal-type: uint}"), "a ULINT entry takes hal-type real");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:11', bits: 64, type: LINT, pin: p}"), "a LINT entry takes hal-type real");
#endif
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
  CHECK_FAILS(ONE_ENTRY("{object: '0x6041:00', bits: 16, type: UINT, use: profile}"), "needs profile cia402, an encoder channel or a serial channel's PDO");
}

static void test_split(void) {
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:01', bits: 8, type: USINT, split: [a, ~, c]}")), 0);
  CHECK_EQ(ENTRY(0)->subType, lcecPdoEntTypeComplex);
  CHECK_EQ(count(lcecConfTypeComplexEntry), 2);
  CHECK_EQ(COMPLEX(0)->bitOffset, 0);
  CHECK(!strcmp(COMPLEX(0)->halPin, "a"));
  CHECK_EQ(COMPLEX(1)->bitOffset, 2);
  CHECK(!strcmp(COMPLEX(1)->halPin, "c"));
  CHECK_EQ(COMPLEX(1)->halType, HAL_BOOL);
  CHECK_EQ(SLAVE(0)->pdoEntryCount, 1);
  CHECK_EQ(SLAVE(0)->pdoMappingCount, 2);
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:01', bits: 2, type: USINT, split: [a, b, c]}"), "split has more pins than bits");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:01', bits: 8, type: USINT, pin: p, hal-type: u32, split: [a]}"), "give pin or split, not both");
}

static void test_invert(void) {
  // a bit pin: true or false
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:01', bits: 1, type: BOOL, pin: p, hal-type: bit, invert: true}")), 0);
  CHECK_EQ(ENTRY(0)->invert, 1);
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:01', bits: 1, type: BOOL, pin: p, hal-type: bit}")), 0);
  CHECK_EQ(ENTRY(0)->invert, 0);
  // a split: the names to invert
  CHECK_EQ(load(ONE_ENTRY("{object: '0x6000:01', bits: 8, type: USINT, split: [a, ~, c], invert: [c]}")), 0);
  CHECK_EQ(COMPLEX(0)->invert, 0);
  CHECK_EQ(COMPLEX(1)->invert, 1);
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:01', bits: 8, type: USINT, split: [a, b], invert: [z]}"), "invert lists z, which is not in split");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:01', bits: 8, type: USINT, split: [a, b], invert: true}"), "a list of names for a split");
  CHECK_FAILS(ONE_ENTRY("{object: '0x6000:01', bits: 8, type: USINT, pin: p, hal-type: u32, invert: true}"), "invert is for bit pins");
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

#define EOE_SLAVE(block) ("ecat: 1\nslaves:\n- position: 0\n  name: d\n  profile: generic\n  eoe: " block "\n")

static void test_eoe(void) {
  CHECK_EQ(load(EOE_SLAVE("{ip: 192.168.10.21/24, gateway: 192.168.10.1, dns: 8.8.8.8, hostname: drive-x, mac: '02:00:00:00:10:21'}")), 0);
  CHECK_EQ(count(lcecConfTypeEoe), 1);
  CHECK_EQ(EOE(0)->given, LCEC_EOE_IP | LCEC_EOE_MASK | LCEC_EOE_GATEWAY | LCEC_EOE_DNS | LCEC_EOE_HOSTNAME | LCEC_EOE_MAC);
  CHECK_EQ(EOE(0)->ip, htonl(0xC0A80A15));
  CHECK_EQ(EOE(0)->mask, htonl(0xFFFFFF00));
  CHECK_EQ(EOE(0)->gateway, htonl(0xC0A80A01));
  CHECK_EQ(EOE(0)->dns, htonl(0x08080808));
  CHECK_EQ(EOE(0)->mac[0], 0x02);
  CHECK_EQ(EOE(0)->mac[5], 0x21);
  CHECK_EQ(strcmp(EOE(0)->hostname, "drive-x"), 0);
  // the mask as an address, and only an address
  CHECK_EQ(load(EOE_SLAVE("{ip: 10.0.0.5, mask: 255.255.0.0}")), 0);
  CHECK_EQ(EOE(0)->given, LCEC_EOE_IP | LCEC_EOE_MASK);
  CHECK_EQ(EOE(0)->mask, htonl(0xFFFF0000));
  CHECK_EQ(load(EOE_SLAVE("{ip: 10.0.0.5}")), 0);
  CHECK_EQ(EOE(0)->given, LCEC_EOE_IP);
  // /0 and /32
  CHECK_EQ(load(EOE_SLAVE("{ip: 10.0.0.5/32}")), 0);
  CHECK_EQ(EOE(0)->mask, htonl(0xFFFFFFFFu));
  // the PC's own address on the slave's network device
  CHECK_EQ(load(EOE_SLAVE("{ip: 192.168.10.21/24, host-ip: 192.168.10.1}")), 0);
  CHECK_EQ(EOE(0)->given, LCEC_EOE_IP | LCEC_EOE_MASK | LCEC_EOE_HOST_IP);
  CHECK_EQ(EOE(0)->host_ip, htonl(0xC0A80A01));
  CHECK_EQ(load(EOE_SLAVE("{ip: 10.0.0.5, mask: 255.255.0.0, host-ip: 10.0.200.1}")), 0);
  CHECK_EQ(EOE(0)->host_ip, htonl(0x0A00C801));
  CHECK_FAILS(EOE_SLAVE("{ip: 10.0.0.5, host-ip: 10.0.0.1}"), "eoe.host-ip: needs the subnet");
  CHECK_FAILS(EOE_SLAVE("{ip: 10.0.0.5/24, host-ip: 10.0.0.5}"), "is the slave's own address");
  CHECK_FAILS(EOE_SLAVE("{ip: 10.0.0.5/24, host-ip: 10.0.1.1}"), "is not in the slave's subnet");
  CHECK_FAILS(EOE_SLAVE("{ip: 10.0.0.5/24, host-ip: 10.0.0.1/24}"), "eoe.host-ip");
  CHECK_FAILS(EOE_SLAVE("{ip: 10.0.0.5/32, host-ip: 10.0.0.1}"), "room for one address only");
  CHECK_FAILS(EOE_SLAVE("{ip: 10.0.0.5/24, host-ip: nonsense}"), "eoe.host-ip: \"nonsense\" is not an IPv4 address");
  // without eoe: no record
  CHECK_EQ(load(io), 0);
  CHECK_EQ(count(lcecConfTypeEoe), 0);
  CHECK_FAILS(EOE_SLAVE("{gateway: 10.0.0.1}"), "eoe: needs ip");
  CHECK_FAILS(EOE_SLAVE("{ip: 10.0.0.300}"), "eoe.ip: \"10.0.0.300\" is not an IPv4 address");
  CHECK_FAILS(EOE_SLAVE("{ip: 10.0.0.5/33}"), "prefix length");
  CHECK_FAILS(EOE_SLAVE("{ip: 10.0.0.5/24, mask: 255.255.255.0}"), "not both");
  CHECK_FAILS(EOE_SLAVE("{ip: 10.0.0.5, mask: 255.0.255.0}"), "is not a subnet mask");
  CHECK_FAILS(EOE_SLAVE("{ip: 10.0.0.5, mac: '02:00:00:00:10'}"), "eoe.mac");
  CHECK_FAILS(EOE_SLAVE("{ip: 10.0.0.5, hostname: ''}"), "eoe.hostname");
}

static void test_dc(void) {
  CHECK_EQ(load("ecat: 1\n"
                "master:\n"
                "  dc: {ref-clock-sync-cycles: -1, servo-thread-follows-ref-clock: true, sync0-shift-ns: 100000}\n"
                "slaves:\n"
                "- position: 0\n"
                "  name: d\n"
                "  profile: generic\n"
                "  dc: {assign-activate: '0x300', sync0-shift-ns: -5000}\n"
                "- position: 1\n"
                "  name: e\n"
                "  profile: generic\n"
                "  dc: {assign-activate: '0x300', sync0-cycles: 2}\n"
                "- position: 2\n"
                "  name: f\n"
                "  profile: generic\n"
                "  dc: {assign-activate: '0x700', sync1-cycles: 4, sync1-shift-ns: 10000}\n"),
      0);
  CHECK_EQ(MASTER->refClockSyncCycles, -1);
  CHECK_EQ(MASTER->syncToRefClock, 1);
  CHECK_EQ(MASTER->globalShiftTime, 100000);
  CHECK_EQ(DC(0)->assignActivate, 0x300);
  CHECK_EQ(DC(0)->sync0Cycle, 1);  // servo periods
  CHECK_EQ(DC(0)->sync0Shift, -5000);
  CHECK_EQ(DC(1)->sync0Cycle, 2);
  CHECK_EQ(DC(1)->sync1Cycle, 0);       // no SYNC1
  CHECK_EQ(DC(2)->sync1Cycle, 4);       // SYNC0 cycles, until the SYNC0 cycle is known
  CHECK_EQ(DC(2)->sync1Shift, 10000);
  CHECK_FAILS("ecat: 1\nmaster: {}\nslaves:\n- {position: 0, name: d, profile: generic, "
              "dc: {assign-activate: '0x700', sync1-cycles: -1}}\n", "dc sync1-cycles must be 0 to 65535");
  CHECK_FAILS("ecat: 1\nmaster: {}\nslaves:\n- {position: 0, name: d, profile: generic, "
              "dc: {assign-activate: '0x700', sync1-shift-ns: 5}}\n", "dc sync1-shift-ns needs sync1-cycles");
  CHECK_FAILS("ecat: 1\nmaster: {}\nslaves:\n- {position: 0, name: d, profile: generic, "
              "dc: {assign-activate: '0x700', sync1-cycles: 1, sync1-shift-ns: -1000}}\n",
              "dc sync1-shift-ns must be 0 or more");
  CHECK_EQ(load("ecat: 1\nmaster: {}\nslaves:\n- {position: 0, name: d, profile: generic, "
                "dc: {assign-activate: '0x300', sync1-shift-ns: 0}}\n"), 0);   // 0 needs no cycle
  // without dc: none
  CHECK_EQ(load(io), 0);
  CHECK_EQ(count(lcecConfTypeDcConf), 0);
  CHECK_EQ(MASTER->syncToRefClock, 0);
}

static void test_dc_auto_shift(void) {
  CHECK_EQ(load("ecat: 1\n"
                "master:\n"
                "  dc: {ref-clock-sync-cycles: 1000, servo-thread-follows-ref-clock: true, sync0-shift-ns: auto}\n"
                "slaves:\n"
                "- position: 0\n"
                "  name: d\n"
                "  profile: generic\n"),
      0);
  CHECK_EQ(MASTER->autoShift, 1);
  CHECK_EQ(MASTER->globalShiftTime, 0);
  CHECK_EQ(load(io), 0);
  CHECK_EQ(MASTER->autoShift, 0);
  // the servo thread must follow the DC for the shift to move
  CHECK_FAILS("ecat: 1\n"
              "master:\n"
              "  dc: {sync0-shift-ns: auto}\n"
              "slaves:\n"
              "- position: 0\n"
              "  name: d\n"
              "  profile: generic\n",
      "sync0-shift-ns: auto needs master.dc.servo-thread-follows-ref-clock: true");
  CHECK_FAILS("ecat: 1\n"
              "master:\n"
              "  dc: {servo-thread-follows-ref-clock: true, sync0-shift-ns: often}\n"
              "slaves:\n"
              "- position: 0\n"
              "  name: d\n"
              "  profile: generic\n",
      "sync0-shift-ns: \"often\" is not a number");
  // the old name is an error, not ignored
  CHECK_FAILS("ecat: 1\n"
              "master:\n"
              "  dc: {sync-to-ref-clock: true}\n"
              "slaves:\n"
              "- position: 0\n"
              "  name: d\n"
              "  profile: generic\n",
      "sync-to-ref-clock is now called servo-thread-follows-ref-clock");
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

  // long data (a complete-access block): up to ECAT_SDO_DATA_MAX bytes, then a clear error
  {
    static char yaml[8192];
    char hex[2 * 1025 + 1];
    int n;
    for (n = 0; n < 1025; n++) memcpy(hex + 2 * n, "a5", 2);
    hex[2 * 1024] = 0;
    snprintf(yaml, sizeof(yaml), SETTINGS("  - {object: '0x8000:00', data: '%s'}\n"), hex);
    CHECK_EQ(load(yaml), 0);
    CHECK_EQ(SDO(0)->length, 1024);
    hex[2 * 1024] = 'a';
    hex[2 * 1025] = 0;
    snprintf(yaml, sizeof(yaml), SETTINGS("  - {object: '0x8000:00', data: '%s'}\n"), hex);
    CHECK_FAILS(yaml, "data is longer than 1024 bytes");
  }
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

#define CSP_OUT2 P("0x6840:00", 16, "UINT") P("0x6860:00", 8, "SINT") P("0x687A:00", 32, "DINT")
#define CSP_IN2 P("0x6841:00", 16, "UINT") P("0x6861:00", 8, "SINT") P("0x6864:00", 32, "DINT")

static void test_cia402_axes(void) {
  CHECK_EQ(load(CIA402("csp, axes: [x1, x2]", CSP_OUT CSP_OUT2, CSP_IN CSP_IN2)), 0);
  CHECK_EQ(fake_msgs[0], 0);
  CHECK_EQ(SLAVE(0)->modParamCount, 3);
  CHECK_EQ(MODPARAM(1)->id, ECAT_MP_CIA402_AXIS);
  CHECK(!strcmp(MODPARAM(1)->value.str, "x1"));
  CHECK(!strcmp(MODPARAM(2)->value.str, "x2"));
  // the second axis's objects are the profile's too, and each axis needs its own
  CHECK_FAILS(CIA402("csp, axes: [x1, x2]", CSP_OUT CSP_OUT2, CSP_IN P("0x6864:00", 32, "DINT")),
      "the cia402 profile needs 0x6841:00");
  // without axes, 0x6840 is not the profile's
  CHECK_FAILS(CIA402("csp", CSP_OUT CSP_OUT2, CSP_IN CSP_IN2), "0x6840:00: the cia402 profile does not handle this object");
  CHECK_FAILS(CIA402("csp, axes: [a, b, c, d, e]", CSP_OUT, CSP_IN), "cia402.axes: a list of 1 to 4 names");
  CHECK_FAILS(CIA402("pp, axes: [x1, x2]", CSP_OUT CSP_OUT2, CSP_IN CSP_IN2
                     "  settings:\n"
                     "  - {object: '0x607D:01', type: DINT, value: -1}\n"
                     "  - {object: '0x607D:02', type: DINT, value: 1}\n"
                     "  - {object: '0x607F:00', type: UDINT, value: 1}\n"),
      "cia402 mode pp needs the settings 0x687D:01 and 0x687D:02");
}

static void test_cia402_csv(void) {
  CHECK_EQ(load(CIA402("csv", P("0x6040:00", 16, "UINT") P("0x60FF:00", 32, "DINT"),
               P("0x6041:00", 16, "UINT") P("0x6061:00", 8, "SINT") P("0x606C:00", 32, "DINT"))),
      0);
  CHECK_EQ(MODPARAM(0)->value.u32, 9);
  // CSV does not need the position objects, but CSP does
  CHECK_FAILS(CIA402("csp", P("0x6040:00", 16, "UINT") P("0x60FF:00", 32, "DINT"),
                  P("0x6041:00", 16, "UINT") P("0x606C:00", 32, "DINT")),
      "needs 0x607A:00");
  CHECK(strstr(fake_msgs, "needs 0x6064:00") != NULL);
  // PV needs what CSV needs
  CHECK_EQ(load(CIA402("pv", P("0x6040:00", 16, "UINT") P("0x60FF:00", 32, "DINT"),
               P("0x6041:00", 16, "UINT") P("0x6061:00", 8, "SINT") P("0x606C:00", 32, "DINT"))),
      0);
  CHECK_EQ(MODPARAM(0)->value.u32, 3);
  CHECK_FAILS(CIA402("pv", P("0x6040:00", 16, "UINT"), P("0x6041:00", 16, "UINT") P("0x606C:00", 32, "DINT")),
      "needs 0x60FF:00");
}

static void test_cia402_errors(void) {
  CHECK_FAILS(CIA402("cst", CSP_OUT, CSP_IN), "cia402.mode must be csp, csv, pv or pp");
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

#define PP_LIMITS                                                     \
  "  settings:\n"                                                     \
  "  - {object: '0x607D:01', type: DINT, value: -100000}\n"           \
  "  - {object: '0x607D:02', type: DINT, value: 100000}\n"            \
  "  - {object: '0x607F:00', type: UDINT, value: 50000}\n"

static void test_cia402_pp(void) {
  CHECK_EQ(load(CIA402("pp", CSP_OUT P("0x6081:00", 32, "UDINT"), CSP_IN) PP_LIMITS), 0);
  CHECK_EQ(MODPARAM(0)->value.u32, 1);
  CHECK_EQ(count(lcecConfTypeSdoConfig), 3);
  // the drive plans the move: it must have its limits
  CHECK_FAILS(CIA402("pp", CSP_OUT, CSP_IN), "cia402 mode pp needs the settings 0x607D:01 and 0x607D:02");
  CHECK(strstr(fake_msgs, "add them to the settings, type DINT") != NULL);
  CHECK_FAILS(CIA402("pp", CSP_OUT, CSP_IN) "  settings:\n  - {object: '0x607D:01', type: DINT, value: -1}\n",
      "cia402 mode pp needs the settings 0x607D:01 and 0x607D:02");
  // 0x607F where the drive has it: not required (the ECT60 has none)
  CHECK_EQ(load(CIA402("pp", CSP_OUT, CSP_IN) "  settings:\n  - {object: '0x607D:01', type: DINT, value: -1}\n"
                                              "  - {object: '0x607D:02', type: DINT, value: 1}\n"), 0);
  CHECK_EQ(count(lcecConfTypeSdoConfig), 2);
  // and the positions
  CHECK_FAILS(CIA402("pp", P("0x6040:00", 16, "UINT"), P("0x6041:00", 16, "UINT")) PP_LIMITS, "needs 0x607A:00");
  // 0x6081 is the profile's, not a pin
  CHECK_FAILS(CIA402("pp", CSP_OUT E("0x6081:00", 32, "UDINT", "pin: v, hal-type: u32"), CSP_IN) PP_LIMITS,
      "the cia402 profile handles this object");
}

// the mode: set every cycle (0x6060 mapped), once as a setting, or at least seen (0x6061)
#define NOMODE_OUT P("0x6040:00", 16, "UINT") P("0x607A:00", 32, "DINT")
#define NOMODE_IN P("0x6041:00", 16, "UINT") P("0x6064:00", 32, "DINT")

#define TIMEOUTS(n) ((LCEC_CONF_STATE_TIMEOUTS_T *)nth(lcecConfTypeStateTimeouts, n))

#define CYCLE(n) ((LCEC_CONF_SDO_CYCLE_T *)nth(lcecConfTypeSdoCycleTime, n))

static void test_init_cmds(void) {
  // before the settings; complete access from 0 or 1; the cycle time later; disabled left out
  CHECK_EQ(load("ecat: 1\nmaster: {}\nslaves:\n- position: 0\n  name: d\n  profile: generic\n"
                "  init-cmds:\n"
                "  - {at: PS, object: '0x8000:06', type: USINT, value: 1}\n"
                "  - {at: PS, object: '0x8080:01', complete: true, data: '00 00 00 03 03 00'}\n"
                "  - {at: IP, object: '0x8090:00', complete: true, data: '02 11 22'}\n"
                "  - {at: PS, object: '0x1C32:02', from: cycle-time}\n"
                "  - {at: PS, object: '0x8000:07', type: USINT, value: 5, disabled: true}\n"
                "  settings:\n  - {object: '0x8000:06', type: USINT, value: 2}\n"),
      0);
  CHECK_EQ(count(lcecConfTypeSdoConfig), 4);
  CHECK_EQ(SDO(0)->index, 0x8000);
  CHECK_EQ(SDO(0)->data[0], 1);                    // the device's first ...
  CHECK_EQ(SDO(1)->subindex, LCEC_CONF_SDO_COMPLETE_FROM1);
  CHECK_EQ(SDO(1)->length, 6);
  CHECK_EQ(SDO(2)->subindex, LCEC_CONF_SDO_COMPLETE_SUBIDX);
  CHECK_EQ(SDO(3)->data[0], 2);                    // ... the user's setting after
  CHECK_EQ(count(lcecConfTypeSdoCycleTime), 1);
  CHECK_EQ(CYCLE(0)->index, 0x1C32);
  CHECK_EQ(CYCLE(0)->subindex, 2);
  CHECK_FAILS("ecat: 1\nmaster: {}\nslaves:\n- position: 0\n  name: d\n  profile: generic\n"
              "  init-cmds:\n  - {at: SO, object: '0x8000:06', type: USINT, value: 1}\n",
              "init-cmds 0x8000:06: at SO is not supported yet");
  CHECK_FAILS("ecat: 1\nmaster: {}\nslaves:\n- position: 0\n  name: d\n  profile: generic\n"
              "  init-cmds:\n  - {at: PS, object: '0x6060:00', from: profile}\n",
              "from: profile is only for a cia402 drive's 0x6060:00");
  // the mode from the profile: set, as a setting would (no "neither set nor seen")
  CHECK_EQ(load(CIA402("pv", P("0x6040:00", 16, "UINT") P("0x60FF:00", 32, "DINT"),
                       P("0x6041:00", 16, "UINT") P("0x606C:00", 32, "DINT"))
                "  init-cmds:\n  - {at: PS, object: '0x6060:00', from: profile}\n"), 0);
  CHECK_EQ(SDO(0)->data[0], 3);
  // modes may list the mode itself: the row still gets the mode's number
  CHECK_EQ(load(CIA402("csp, modes: [csp, hm]", CSP_OUT, CSP_IN)
                "  init-cmds:\n  - {at: PS, object: '0x6060:00', from: profile}\n"), 0);
  CHECK_EQ(SDO(0)->data[0], 8);
}

static void test_state_timeouts(void) {
  CHECK_EQ(load("ecat: 1\nmaster: {}\nslaves:\n- {position: 0, name: d, profile: generic, "
                "state-timeouts-ms: {preop: 6000, safeop-op: 30000}}\n"), 0);
  CHECK_EQ(TIMEOUTS(0)->preop, 6000);
  CHECK_EQ(TIMEOUTS(0)->safeop_op, 30000);
  CHECK_EQ(TIMEOUTS(0)->back_to_init, 0);  // the master's default
  CHECK_EQ(TIMEOUTS(0)->back_to_safeop, 0);
  CHECK_EQ(load(io), 0);
  CHECK_EQ(count(lcecConfTypeStateTimeouts), 0);
  CHECK_FAILS("ecat: 1\nmaster: {}\nslaves:\n- {position: 0, name: d, profile: generic, "
              "state-timeouts-ms: {preop: 0}}\n", "state-timeouts-ms.preop must be 1 to 600000");
  CHECK_FAILS("ecat: 1\nmaster: {}\nslaves:\n- {position: 0, name: d, profile: generic, "
              "state-timeouts-ms: {pre-op: 6000}}\n", "state-timeouts-ms: unknown key \"pre-op\"");
  // the SDO response timeout, alone or with them
  CHECK_EQ(load("ecat: 1\nmaster: {}\nslaves:\n- {position: 0, name: d, profile: generic, sdo-timeout-ms: 2000}\n"), 0);
  CHECK_EQ(TIMEOUTS(0)->sdo_response, 2000);
  CHECK_EQ(TIMEOUTS(0)->preop, 0);
  CHECK_FAILS("ecat: 1\nmaster: {}\nslaves:\n- {position: 0, name: d, profile: generic, sdo-timeout-ms: 0}\n",
              "sdo-timeout-ms must be 1 to 600000");
  CHECK_FAILS("ecat: 1\nmaster: {}\nslaves:\n- {position: 0, name: d, profile: generic, sdo-timeout-ms: 2s}\n",
              "sdo-timeout-ms must be 1 to 600000");
}

static void test_sync1(void) {
  // the register value (ETG.2000 CycleTime1): (n - 1) * SYNC0 + shift
  CHECK_EQ(ecat_sync1_cycle(0, 1000000, 0), 0);
  CHECK_EQ(ecat_sync1_cycle(1, 1000000, 250000), 250000);
  CHECK_EQ(ecat_sync1_cycle(2, 1000000, 0), 1000000);
  CHECK_EQ(ecat_sync1_cycle(4, 500000, 10000), 1510000);
}

static void test_cia402_mode_setting(void) {
  CHECK_FAILS(CIA402("csp", NOMODE_OUT, NOMODE_IN), "the drive's mode is neither set nor seen");
  CHECK(strstr(fake_msgs, "add the setting 0x6060:00 = 8 (type SINT") != NULL);
  CHECK_EQ(load(CIA402("csp", NOMODE_OUT, NOMODE_IN) "  settings:\n  - {object: '0x6060:00', type: SINT, value: 8}\n"), 0);
  CHECK_EQ(count(lcecConfTypeSdoConfig), 1);
  CHECK_FAILS(CIA402("csp", NOMODE_OUT, NOMODE_IN) "  settings:\n  - {object: '0x6060:00', type: SINT, value: 3}\n",
      "the setting 0x6060:00 is 3, but cia402.mode is 8");
  CHECK_EQ(load(CIA402("pv", P("0x6040:00", 16, "UINT") P("0x60FF:00", 32, "DINT"),
                       P("0x6041:00", 16, "UINT") P("0x606C:00", 32, "DINT"))
                "  settings:\n  - {object: '0x6060:00', type: SINT, value: 3}\n"), 0);
  // seen only (0x6061 mapped): the profile enables only in the mode it shows
  CHECK_EQ(load(CIA402("csp", NOMODE_OUT, NOMODE_IN P("0x6061:00", 8, "SINT"))), 0);
  // set every cycle
  CHECK_EQ(load(CIA402("csp", NOMODE_OUT P("0x6060:00", 8, "SINT"), NOMODE_IN)), 0);
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
  CHECK_FAILS(CIA402("csp, modes: [cst]", CSP_OUT, CSP_IN), "cia402.modes: \"cst\" is not supported (csp, csv, pv, pp, hm)");
  CHECK_FAILS(CIA402("csp, modes: hm", CSP_OUT, CSP_IN), "cia402.modes must be a list");
  // changing modes needs the mode and its display
  CHECK_FAILS(CIA402("csp, modes: [hm]", P("0x6040:00", 16, "UINT") P("0x607A:00", 32, "DINT"),
                  P("0x6041:00", 16, "UINT") P("0x6064:00", 32, "DINT")),
      "needs 0x6060:00 with \"use: profile\" to change modes");
  CHECK(strstr(fake_msgs, "needs 0x6061:00") != NULL);
  CHECK(strstr(fake_msgs, "neither set nor seen") == NULL);   // said once, by the line above
  // other modes: what each needs
  CHECK_EQ(load(CIA402("csp, modes: [pv]", CSP_OUT P("0x60FF:00", 32, "DINT"), CSP_IN P("0x606C:00", 32, "DINT"))), 0);
  CHECK_EQ(MODPARAM(1)->value.u32, 1u << 3);
  CHECK_FAILS(CIA402("csp, modes: [pv]", CSP_OUT, CSP_IN), "needs 0x60FF:00");
  CHECK_FAILS(CIA402("csp, modes: [pp]", CSP_OUT, CSP_IN), "cia402 mode pp needs the settings");
  CHECK_EQ(load(CIA402("csp, modes: [pp, hm]", CSP_OUT, CSP_IN) PP_LIMITS), 0);
  CHECK_EQ(MODPARAM(1)->value.u32, (1u << 1) | (1u << 6));
  CHECK_FAILS(CIA402("hm", CSP_OUT, CSP_IN), "cia402.mode must be csp, csv, pv or pp");
}

static void test_cia402_zero_at_start(void) {
  CHECK_EQ(load(CIA402("csp, zero-at-start: true", CSP_OUT, CSP_IN)), 0);
  CHECK_EQ(SLAVE(0)->modParamCount, 2);
  CHECK_EQ(MODPARAM(1)->id, ECAT_MP_CIA402_ZERO_AT_START);
  CHECK_EQ(MODPARAM(1)->value.u32, 1);
  // with other modes too
  CHECK_EQ(load(CIA402("csp, modes: [hm], zero-at-start: true", CSP_OUT, CSP_IN)), 0);
  CHECK_EQ(SLAVE(0)->modParamCount, 3);
  CHECK_EQ(MODPARAM(1)->id, ECAT_MP_CIA402_MODES);
  CHECK_EQ(MODPARAM(2)->id, ECAT_MP_CIA402_ZERO_AT_START);
  // false or left out: no record
  CHECK_EQ(load(CIA402("csp, zero-at-start: false", CSP_OUT, CSP_IN)), 0);
  CHECK_EQ(SLAVE(0)->modParamCount, 1);
  CHECK_FAILS(CIA402("csp, zero-at-start: 1", CSP_OUT, CSP_IN), "zero-at-start: \"1\" is not true or false");
}

static void test_cia402_homing_options(void) {
  CHECK_EQ(load(CIA402("csp, modes: [hm], home-enable-again: true, home-speed: 3000", CSP_OUT, CSP_IN)), 0);
  CHECK_EQ(SLAVE(0)->modParamCount, 4);
  CHECK_EQ(MODPARAM(2)->id, ECAT_MP_CIA402_HOME_ENABLE_AGAIN);
  CHECK_EQ(MODPARAM(2)->value.u32, 1);
  CHECK_EQ(MODPARAM(3)->id, ECAT_MP_CIA402_HOME_SPEED);
  CHECK_EQ(MODPARAM(3)->value.u32, 3000);
  // false or left out: no record
  CHECK_EQ(load(CIA402("csp, modes: [hm], home-enable-again: false", CSP_OUT, CSP_IN)), 0);
  CHECK_EQ(SLAVE(0)->modParamCount, 2);
  CHECK_FAILS(CIA402("csp, home-enable-again: 1", CSP_OUT, CSP_IN), "home-enable-again: \"1\" is not true or false");
  CHECK_FAILS(CIA402("csp, home-speed: 0", CSP_OUT, CSP_IN), "cia402.home-speed: the fastest homing speed");
  CHECK_FAILS(CIA402("csp, home-speed: -5", CSP_OUT, CSP_IN), "cia402.home-speed: the fastest homing speed");
}

static void test_cia402_feedforward(void) {
#define FF_OUT CSP_OUT P("0x60B1:00", 32, "DINT") P("0x60B2:00", 16, "INT")
  CHECK_EQ(load(CIA402("csp, feedforward: {velocity: 0.9, torque: 0.012, torque-max: 50}", FF_OUT, CSP_IN)), 0);
  CHECK_EQ(fake_msgs[0], 0);
  CHECK_EQ(SLAVE(0)->modParamCount, 4);
  CHECK_EQ(MODPARAM(1)->id, ECAT_MP_CIA402_FF_VELOCITY);
  CHECK_NEAR(MODPARAM(1)->value.flt, 0.9, 0);
  CHECK_EQ(MODPARAM(2)->id, ECAT_MP_CIA402_FF_TORQUE);
  CHECK_NEAR(MODPARAM(2)->value.flt, 0.012, 0);
  CHECK_EQ(MODPARAM(3)->id, ECAT_MP_CIA402_FF_TORQUE_MAX);
  CHECK_NEAR(MODPARAM(3)->value.flt, 50, 0);
  // the offsets belong to the profile, also without feedforward (written 0)
  CHECK_EQ(load(CIA402("csp", FF_OUT, CSP_IN)), 0);
  CHECK_EQ(SLAVE(0)->modParamCount, 1);
  CHECK_FAILS(CIA402("csp", CSP_OUT E("0x60B1:00", 32, "DINT", "pin: voff, hal-type: sint"), CSP_IN),
              "the cia402 profile handles this object");
  CHECK_FAILS(CIA402("csp", CSP_OUT P("0x60B2:00", 32, "DINT"), CSP_IN), "the cia402 profile needs 16 bits");
  // each needs its object
  CHECK_FAILS(CIA402("csp, feedforward: {velocity: 1}", CSP_OUT, CSP_IN),
              "cia402.feedforward.velocity needs 0x60B1:00 (velocity offset)");
  CHECK_FAILS(CIA402("csp, feedforward: {torque: 0.01}", CSP_OUT P("0x60B1:00", 32, "DINT"), CSP_IN),
              "cia402.feedforward.torque needs 0x60B2:00 (torque offset)");
  CHECK_FAILS(CIA402("csp, feedforward: {velocity: 1, torque-max: 400}", FF_OUT, CSP_IN),
              "cia402.feedforward.torque-max: 0 to 300");
  CHECK_FAILS(CIA402("csp, feedforward: {velocity: fast}", FF_OUT, CSP_IN), "velocity: \"fast\" is not a number");
  CHECK_FAILS(CIA402("csp, feedforward: 1", FF_OUT, CSP_IN), "cia402.feedforward needs");
  // CSP only
  CHECK_FAILS(CIA402("csv, feedforward: {velocity: 1}",
                     P("0x6040:00", 16, "UINT") P("0x60FF:00", 32, "DINT") P("0x60B1:00", 32, "DINT"),
                     P("0x6041:00", 16, "UINT") P("0x606C:00", 32, "DINT")),
              "cia402.feedforward works in CSP only");
#undef FF_OUT
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
               "      entries:\n" P("0x6041:00", 16, "UINT") P("0x6061:00", 8, "SINT") P("0x606C:00", 32, "DINT") P("0x6000:11", 32, "UDINT")),
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

// ---- serial channels -------------------------------------------------

/// An EL6021 with PDOs 0x1602/0x1A02 (a 16-bit control/status word and 22
/// data bytes), its serial: block and settings from the arguments.
static const char *el6021(const char *serial, const char *settings, int in_bits_cut) {
  static char y[16384];
  char *p = y;
  int i;
  p += sprintf(p, HEAD
      "- position: 0\n"
      "  name: com\n"
      "  identity: {vendor: '0x00000002', product: '0x17853052'}\n"
      "  profile: generic\n"
      "%s"
      "  process-data:\n"
      "    outputs:\n"
      "    - pdo: '0x1602'\n"
      "      sm: 2\n"
      "      entries:\n"
      "      - {object: '0x3003:01', bits: 16, use: profile}\n", serial);
  for (i = 0; i < 22; i++) p += sprintf(p, "      - {object: '0x3003:%02x', bits: 8, use: profile}\n", i + 2);
  p += sprintf(p,
      "    inputs:\n"
      "    - pdo: '0x1A02'\n"
      "      sm: 3\n"
      "      entries:\n"
      "      - {object: '0x3103:01', bits: 16, use: profile}\n");
  for (i = 0; i < 22 - in_bits_cut; i++) p += sprintf(p, "      - {object: '0x3103:%02x', bits: 8, use: profile}\n", i + 2);
  p += sprintf(p, "%s", settings);
  return y;
}

#define SERIAL_OK                         \
  "  serial:\n"                           \
  "  - name: port\n"                      \
  "    output-pdo: '0x1602'\n"            \
  "    input-pdo: '0x1A02'\n"             \
  "    port: /dev/ttyECAT0\n"             \
  "    baud: 19200\n"                     \
  "    frame: 8E1\n"

static void test_serial(void) {
  LCEC_CONF_MODPARAM_T *mp;
  int i, found = 0;
  CHECK_EQ(load(el6021(SERIAL_OK, "", 0)), 0);
  CHECK_EQ(SLAVE(0)->modParamCount, 5);
  CHECK_EQ(SLAVE(0)->pdoMappingCount, 0);  // no pins: all the function's
  for (i = 0; (mp = MODPARAM(i)) != NULL; i++) {
    switch (mp->id - ECAT_MP_SERIAL) {
      case ECAT_MP_SERIAL_NAME: CHECK(!strcmp(mp->value.str, "port")); found |= 1; break;
      case ECAT_MP_SERIAL_PDOS: CHECK_EQ(mp->value.u32, 0x16021A02); found |= 2; break;
      case ECAT_MP_SERIAL_PORT: CHECK(!strcmp(mp->value.str, "/dev/ttyECAT0")); found |= 4; break;
      case ECAT_MP_SERIAL_SETTINGS: CHECK_EQ(mp->value.u32, 0x8000 | 7 << 16 | 4 << 24); found |= 8; break;
      case ECAT_MP_SERIAL_FLAGS: CHECK_EQ(mp->value.u32, 0); found |= 16; break;
      default: CHECK(0);
    }
  }
  CHECK_EQ(found, 31);
  // baud rate, data frame and send continuous written at start
  CHECK_EQ(count(lcecConfTypeSdoConfig), 3);
  CHECK(SDO(2)->index == 0x8000 && SDO(2)->subindex == 0x04 && SDO(2)->length == 1 && SDO(2)->data[0] == 0);
  CHECK(SDO(0)->index == 0x8000 && SDO(0)->subindex == 0x11 && SDO(0)->length == 1 && SDO(0)->data[0] == 7);
  CHECK(SDO(1)->index == 0x8000 && SDO(1)->subindex == 0x15 && SDO(1)->length == 1 && SDO(1)->data[0] == 4);
  // defaults: 9600 8N1
  CHECK_EQ(load(el6021("  serial:\n  - {name: p, output-pdo: '0x1602', input-pdo: '0x1A02', port: /dev/ttyECAT1}\n", "", 0)), 0);
  CHECK(SDO(0)->data[0] == 6 && SDO(1)->data[0] == 3);
  CHECK_EQ(load(el6021("  serial:\n  - {name: p, output-pdo: '0x1602', input-pdo: '0x1A02', port: /dev/ttyECAT1, "
                       "send-continuous: true}\n", "", 0)), 0);
  CHECK(SDO(2)->subindex == 0x04 && SDO(2)->data[0] == 1);
  for (i = 0; (mp = MODPARAM(i)) != NULL; i++) {
    if (mp->id == ECAT_MP_SERIAL + ECAT_MP_SERIAL_FLAGS) CHECK_EQ(mp->value.u32, 1);
  }
}

static void test_serial_errors(void) {
  CHECK_FAILS(el6021(SERIAL_OK, "", 1), "the output and input PDOs need the same size");
  CHECK_FAILS(el6021("  serial:\n  - {name: p, output-pdo: '0x1602', input-pdo: '0x1A02', port: /dev/ttyECAT0, baud: 28800}\n", "", 0),
      "baud 28800 is not one of");
  CHECK_FAILS(el6021("  serial:\n  - {name: p, output-pdo: '0x1602', input-pdo: '0x1A02', port: /dev/ttyECAT0, frame: 7N1}\n", "", 0),
      "frame \"7N1\" is not one of");
  CHECK_FAILS(el6021("  serial:\n  - {name: p, output-pdo: '0x1602', input-pdo: '0x1A02'}\n", "", 0), "port must be a full path");
  CHECK_FAILS(el6021("  serial:\n  - {name: p, output-pdo: '0x1602', input-pdo: '0x1A02', port: ttyECAT0}\n", "", 0),
      "port must be a full path");
  CHECK_FAILS(el6021("  serial:\n  - {name: p, output-pdo: '0x1603', input-pdo: '0x1A02', port: /dev/ttyECAT0}\n", "", 0),
      "PDO 0x1603 is not in the outputs");
  CHECK_FAILS(el6021("  serial:\n  - {name: p, output-pdo: '0x1A02', input-pdo: '0x1A02', port: /dev/ttyECAT0}\n", "", 0),
      "output-pdo (0x1600-0x17FF) and input-pdo");
  CHECK_FAILS(el6021(SERIAL_OK, "  settings:\n  - {object: '0x8000:11', value: 6, type: USINT}\n", 0),
      "are its baud, frame and send-continuous");
  CHECK_FAILS(el6021(SERIAL_OK, "  settings:\n  - {object: '0x8000:04', value: 1, type: BOOL}\n", 0),
      "are its baud, frame and send-continuous");
  // an entry of the channel's PDO with a pin
  {
    static char y[16384], rest[16384];
    char *e;
    strcpy(y, el6021(SERIAL_OK, "", 0));
    e = strstr(y, "{object: '0x3003:05', bits: 8, use: profile}");
    CHECK(e != NULL);
    if (e != NULL) {
      strcpy(rest, e + strlen("{object: '0x3003:05', bits: 8, use: profile}"));
      sprintf(e, "{object: '0x3003:05', bits: 8, pin: xx, type: USINT, hal-type: uint}%s", rest);
    }
    CHECK_FAILS(y, "serial channel port handles this PDO");
  }
  // use: profile without a channel
  CHECK_FAILS(el6021("", "", 0), "a serial channel's PDO");
}

static void test_master_cpus(void) {
  CHECK_EQ(load("ecat: 1\nslaves:\n- position: 0\n  name: a\n  profile: generic\n"), 0);
  CHECK_EQ(MASTER->irqCpu, -1);
  CHECK_EQ(MASTER->cpu, -1);
  CHECK_EQ(load("ecat: 1\nmaster: {irq-cpu: 3, cpu: 1}\nslaves:\n- position: 0\n  name: a\n  profile: generic\n"), 0);
  CHECK_EQ(MASTER->irqCpu, 3);
  CHECK_EQ(MASTER->cpu, 1);
  CHECK_FAILS("ecat: 1\nmaster: {irq-cpu: -2}\nslaves:\n- position: 0\n  name: a\n  profile: generic\n", "master.irq-cpu");
  CHECK_FAILS("ecat: 1\nmaster: {cpu: 5000}\nslaves:\n- position: 0\n  name: a\n  profile: generic\n", "master.cpu");
}

// ---- slaves that are only on the bus --------------------------------

static void test_unused(void) {
  // an unused slave is one record: identity for the start check, nothing else
  CHECK_EQ(load("ecat: 1\nslaves:\n"
                "- position: 0\n  name: a\n  device: EK1100\n  identity: {vendor: '0x2', product: '0x44c2c52', revision: '0x110000'}\n  profile: generic\n  unused: true\n"
                "  process-data:\n    inputs:\n    - {pdo: '0x1a00', sm: 3, entries: [{object: '0x6000:01', bits: 8, type: USINT, pin: x}]}\n  settings:\n  - {object: '0x2000:00', type: UDINT, value: 1}\n"
                "- position: 1\n  name: b\n  profile: generic\n"),
           0);
  CHECK_EQ(count(lcecConfTypeSlave), 2);
  CHECK_EQ(SLAVE(0)->unused, 1);
  CHECK_EQ(SLAVE(1)->unused, 0);
  CHECK_EQ(SLAVE(0)->vid, 2);
  CHECK_EQ(SLAVE(0)->pid, 0x44c2c52);
  CHECK_EQ(SLAVE(0)->revision, 0x110000);
  CHECK_EQ(SLAVE(0)->syncManagerCount, 0);
  CHECK_EQ(SLAVE(0)->pdoMappingCount, 0);
  CHECK_EQ(SLAVE(0)->sdoConfigLength, 0);
  CHECK_EQ(count(lcecConfTypePdoEntry), 0);
  CHECK_EQ(count(lcecConfTypeSdoConfig), 0);
  // the default: used
  CHECK_EQ(load("ecat: 1\nslaves:\n- position: 0\n  name: a\n  profile: generic\n  unused: false\n"), 0);
  CHECK_EQ(SLAVE(0)->unused, 0);
  // a slave with a network address is used
  CHECK_FAILS("ecat: 1\nslaves:\n- position: 0\n  name: a\n  profile: generic\n  unused: true\n  eoe: {ip: 10.0.0.2/24}\n", "unused: true and eoe");
}

RUN_TESTS("config", test_cia402_feedforward, test_cia402_zero_at_start, test_cia402_homing_options, test_dc_auto_shift, test_io_slaves, test_sync_managers_in_order_of_first_use, test_sync_manager_for_outputs_and_inputs,
    test_hal_type_spellings, test_extend, test_raw, test_hal_types, test_entry_errors, test_split, test_invert, test_file_errors, test_slave_errors, test_dc, test_eoe, test_settings,
    test_cia402_csp, test_cia402_axes, test_cia402_csv, test_cia402_errors, test_cia402_pp, test_cia402_mode_setting, test_sync1, test_state_timeouts, test_init_cmds, test_cia402_modes, test_encoder, test_encoder_channels, test_encoder_errors,
    test_too_many_encoders, test_serial, test_serial_errors, test_unused, test_master_cpus)
