# ecat HAL component (ecat.so): lcec's code built against the userspace IgH
# EtherCAT master, configured from ethercat.yaml.
#
#   make                 build ecat.so
#   sudo make install    install it in LinuxCNC's module directory
#   make check           build and run the unit tests in tests/
#
# Needs LinuxCNC's halcompile and headers (linuxcnc-uspace-dev), libyaml-dev,
# and the userspace master installed in USPACE_ETHERCAT: the package
# linuxcnc-ethercat-uspace 0.3 or newer, or a master built from source.

.DEFAULT_GOAL := all

USPACE_ETHERCAT ?= /opt/linuxcnc-ethercat-uspace
# A package build can take the master's headers and libraries from a staging
# directory, while the run-time path (rpath) stays USPACE_ETHERCAT.
USPACE_SYSROOT ?=
USPACE_BUILD := $(USPACE_SYSROOT)$(USPACE_ETHERCAT)

COMP ?= $(shell which halcompile)
ifeq (, $(COMP))
  $(error halcompile not found: install linuxcnc-uspace-dev)
endif
MODINC := $(shell $(COMP) --print-modinc)
include $(MODINC)

ifeq (, $(wildcard $(USPACE_BUILD)/include/ecrt.h))
  $(error the userspace EtherCAT master is not in $(USPACE_BUILD); set USPACE_ETHERCAT)
endif

CFLAGS_ECAT := $(EXTRA_CFLAGS) -Wall -I. -I$(USPACE_BUILD)/include -MMD -MP

objs := lcec_main.o lcec_ethercat.o lcec_pins.o lcec_malloc.o lcec_lookup.o \
	lcec_modparam.o lcec_devicelist.o lcec_autoshift.o ecat_config.o ecat_startcheck.o devices/lcec_generic.o devices/ecat_cia402.o devices/ecat_encoder.o

.PHONY: all install clean check
all: ecat.so

%.o: %.c
	@echo Compiling $<
	@$(CC) -o $@ $(CFLAGS_ECAT) -c $<

-include $(objs:.o=.d)

ecat.so: $(objs)
	@echo Linking $@
	@ld -d -r -o $@.tmp $(objs)
	@objcopy -j .rtapi_export -O binary $@.tmp $@.sym
	@(echo '{ global : '; tr -s '\0' < $@.sym | xargs -r0 printf '%s;\n' | grep .; echo 'local : * ; };') > $@.ver
	@$(CC) -shared -Bsymbolic -Wl,--version-script,$@.ver -o $@ $(objs) -lm \
	  -L$(LIBDIR) -Wl,-rpath,$(LIBDIR) -llinuxcnchal \
	  -L$(USPACE_BUILD)/lib -Wl,-rpath,$(USPACE_ETHERCAT)/lib -l:libethercat.so.2 \
	  -lyaml -lpthread -lrt
	@chmod -x $@
	@rm -f $@.tmp $@.sym $@.ver

# Unit tests: a device's code with a fake of HAL, the master and lcec
# (tests/fake.c), so that its read and write functions run without a bus.
tests := tests/test_cia402 tests/test_encoder tests/test_config tests/test_autoshift tests/test_startcheck
tests/test_cia402: tests/test_cia402.o tests/fake.o devices/ecat_cia402.o
tests/test_encoder: tests/test_encoder.o tests/fake.o devices/ecat_encoder.o
tests/test_config: tests/test_config.o tests/fake.o ecat_config.o
tests/test_autoshift: tests/test_autoshift.o tests/fake.o lcec_autoshift.o
tests/test_startcheck: tests/test_startcheck.o tests/fake.o ecat_startcheck.o
-include tests/*.d

$(tests):
	@echo Linking $@
	@$(CC) -o $@ $^ -lm -lyaml

check: $(tests)
	@for t in $(tests); do $$t || exit 1; done

# Makefile.modinc has an install target of its own; this one replaces it.
install: ecat.so
	install -d $(DESTDIR)$(RTLIBDIR)
	install -m 644 ecat.so $(DESTDIR)$(RTLIBDIR)/

clean:
	rm -f ecat.so *.o *.d devices/*.o devices/*.d tests/*.o tests/*.d $(tests) *.tmp *.sym *.ver
