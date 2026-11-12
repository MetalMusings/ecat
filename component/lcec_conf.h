//
//    Copyright (C) 2011 Sascha Ittner <sascha.ittner@modusoft.de>
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
//    You should have received a copy of the GNU General Public License
//    along with this program; if not, write to the Free Software
//    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
//

/// @file

#ifndef _LCEC_CONF_H_
#define _LCEC_CONF_H_

#include "ecrt.h"
#include "hal.h"

#define LCEC_MODULE_NAME "ecat"

#define LCEC_CONF_SHMEM_KEY   0xACB572C7
#define LCEC_CONF_SHMEM_MAGIC 0x036ED5A3

#define LCEC_CONF_STR_MAXLEN 48

#define LCEC_CONF_SDO_COMPLETE_SUBIDX -1
#define LCEC_CONF_GENERIC_MAX_SUBPINS 32
#define LCEC_CONF_GENERIC_MAX_BITLEN  255

typedef enum {
  lcecConfTypeNone = 0,
  lcecConfTypeMasters,
  lcecConfTypeMaster,
  lcecConfTypeSlave,
  lcecConfTypeDcConf,
  lcecConfTypeWatchdog,
  lcecConfTypeSyncManager,
  lcecConfTypePdo,
  lcecConfTypePdoEntry,
  lcecConfTypeSdoConfig,
  lcecConfTypeSdoDataRaw,
  lcecConfTypeIdnConfig,
  lcecConfTypeIdnDataRaw,
  lcecConfTypeInitCmds,
  lcecConfTypeComplexEntry,
  lcecConfTypeModParam
} LCEC_CONF_TYPE_T;

typedef enum {
  lcecPdoEntTypeSimple,
  lcecPdoEntTypeFloatSigned,
  lcecPdoEntTypeFloatUnsigned,
  lcecPdoEntTypeComplex,
  lcecPdoEntTypeFloatIeee,
  lcecPdoEntTypeFloatDoubleIeee,
} LCEC_PDOENT_TYPE_T;

typedef struct {
  uint32_t magic;
  size_t length;
} LCEC_CONF_HEADER_T;

typedef struct {
  LCEC_CONF_TYPE_T confType;
  int index;
  uint32_t appTimePeriod;
  int refClockSyncCycles;
  int syncToRefClock;
  uint32_t globalShiftTime;  ///< SYNC0 offset from the nominal servo thread start (ns), applied to all DC slaves.
  int autoShift;             ///< globalShiftTime="auto": lcec chooses and keeps adjusting it (see lcec_autoshift.h).
  char name[LCEC_CONF_STR_MAXLEN];
  char interface[LCEC_CONF_STR_MAXLEN];  ///< Network interface for the userspace master (lcec_uspace); empty if not given.
  char transport[LCEC_CONF_STR_MAXLEN];  ///< Transport for the userspace master (lcec_uspace), such as raw; empty if not given.
  int startCheck;  ///< master.start-check: 1 (default) stops when the bus differs, 0 only warns.
} LCEC_CONF_MASTER_T;

typedef struct {
  LCEC_CONF_TYPE_T confType;
  int index;
  char type_name[LCEC_CONF_STR_MAXLEN];
  uint32_t vid;
  uint32_t pid;
  int configPdos;
  unsigned int syncManagerCount;
  unsigned int pdoCount;
  unsigned int pdoEntryCount;
  unsigned int pdoMappingCount;
  size_t sdoConfigLength;
  size_t idnConfigLength;
  unsigned int modParamCount;
  char name[LCEC_CONF_STR_MAXLEN];
  uint32_t revision;                    ///< identity.revision, 0 if not given (for the start check).
  char device[LCEC_CONF_STR_MAXLEN];    ///< The device name in the file (for the start check).
} LCEC_CONF_SLAVE_T;

typedef struct {
  LCEC_CONF_TYPE_T confType;
  uint16_t assignActivate;
  uint32_t sync0Cycle;
  int32_t sync0Shift;
  uint32_t sync1Cycle;
  int32_t sync1Shift;
} LCEC_CONF_DC_T;

typedef struct {
  LCEC_CONF_TYPE_T confType;
  uint16_t divider;
  uint16_t intervals;
} LCEC_CONF_WATCHDOG_T;

typedef struct {
  LCEC_CONF_TYPE_T confType;
  uint8_t index;
  ec_direction_t dir;
  unsigned int pdoCount;
} LCEC_CONF_SYNCMANAGER_T;

typedef struct {
  LCEC_CONF_TYPE_T confType;
  uint16_t index;
  unsigned int pdoEntryCount;
} LCEC_CONF_PDO_T;

typedef struct {
  LCEC_CONF_TYPE_T confType;
  uint16_t index;
  uint8_t subindex;
  uint8_t bitLength;
  LCEC_PDOENT_TYPE_T subType;
  hal_type_t halType;
  rtapi_real floatScale;
  rtapi_real floatOffset;
  uint8_t invert;  // bit pins: the pin is the opposite of the bit
  uint8_t extend;  // input counters of 8..32 bits: the pin counts on past the wrap, in 64 bits
  uint8_t raw;     // uint pins: a bit pattern; a value is cut to the entry's width, not held at its top
  char halPin[LCEC_CONF_STR_MAXLEN];
} LCEC_CONF_PDOENTRY_T;

typedef struct {
  LCEC_CONF_TYPE_T confType;
  uint8_t bitOffset;
  uint8_t bitLength;
  LCEC_PDOENT_TYPE_T subType;
  hal_type_t halType;
  rtapi_real floatScale;
  rtapi_real floatOffset;
  uint8_t invert;  // the pin is the opposite of the bit
  char halPin[LCEC_CONF_STR_MAXLEN];
} LCEC_CONF_COMPLEXENTRY_T;

typedef struct {
  LCEC_CONF_TYPE_T confType;
} LCEC_CONF_NULL_T;

typedef struct {
  LCEC_CONF_TYPE_T confType;
  uint16_t index;
  int16_t subindex;
  size_t length;
  uint8_t data[];
} LCEC_CONF_SDOCONF_T;

typedef struct {
  LCEC_CONF_TYPE_T confType;
  uint8_t drive;
  uint16_t idn;
  ec_al_state_t state;
  size_t length;
  uint8_t data[];
} LCEC_CONF_IDNCONF_T;

typedef union {
  rtapi_bool bit;
  rtapi_sint s32;
  rtapi_uint u32;
  rtapi_real flt;
  char str[LCEC_CONF_STR_MAXLEN];
} LCEC_CONF_MODPARAM_VAL_T;

typedef struct {
  LCEC_CONF_TYPE_T confType;
  int id;
  char name[LCEC_CONF_STR_MAXLEN];
  LCEC_CONF_MODPARAM_VAL_T value;
} LCEC_CONF_MODPARAM_T;

#endif
