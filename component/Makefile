# ecat HAL component (ecat.so): lcec's code built against the userspace IgH
# EtherCAT master, configured from ethercat.yaml.
#
#   make                 build ecat.so
#   sudo make install    install it in LinuxCNC's module directory
#
# Needs LinuxCNC's halcompile and headers (linuxcnc-uspace-dev), libyaml-dev,
# and the userspace master installed in USPACE_ETHERCAT.

.DEFAULT_GOAL := all

USPACE_ETHERCAT ?= /opt/ethercat-uspace
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
	lcec_modparam.o lcec_devicelist.o ecat_config.o devices/lcec_generic.o devices/ecat_cia402.o

.PHONY: all install clean
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

# Makefile.modinc has an install target of its own; this one replaces it.
install: ecat.so
	install -d $(DESTDIR)$(RTLIBDIR)
	install -m 644 ecat.so $(DESTDIR)$(RTLIBDIR)/

clean:
	rm -f ecat.so *.o *.d devices/*.o devices/*.d *.tmp *.sym *.ver
