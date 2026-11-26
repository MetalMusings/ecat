"""The IgH kernel master, only as far as it is in the way of ecat.

EtherCAT Setup takes the kernel master as dealt with: it names it only when
it has (or, at the next start, will have) the network card ecat uses. The
rule is the component's start check (component/ecat_startcheck.c,
ecat_kernel_master_uses): the kernel master is loaded (/dev/EtherCAT0) and
its configuration gives it the card by name, by MAC address or as
ff:ff:ff:ff:ff:ff (any card), or its configuration cannot be read.
"""

import os
import re

DEVICE = "/dev/EtherCAT0"
CONFS = ("/etc/ethercat.conf", "/etc/sysconfig/ethercat")
SYS_NET = "/sys/class/net"
FIX = ("Stop it: sudo systemctl stop ethercat, and sudo systemctl disable ethercat so that it "
       "does not start again with the computer.")

_KEY = re.compile(r"^MASTER\d+_(DEVICE|BACKUP)$")


def uses(conf_text, ifname, mac=None):
    """The assignment ("MASTER0_DEVICE=enp2s0") of a kernel master
    configuration that gives it this card, or None."""
    for line in conf_text.splitlines():
        t = line.strip()
        if not t or t.startswith("#") or "=" not in t:
            continue
        key, value = t.split("=", 1)
        key = key.strip()
        if not _KEY.match(key):
            continue
        value = value.split("#", 1)[0].strip().replace('"', "").replace("'", "").replace(" ", "")
        if value and (value == ifname or (mac and value.lower() == mac.lower())
                      or value.lower() == "ff:ff:ff:ff:ff:ff"):
            return "%s=%s" % (key, value)
    return None


def _conf(confs):
    for path in confs:
        try:
            with open(path) as f:
                return path, f.read()
        except OSError:
            continue
    return None, None


def _mac(interface, sys_net):
    try:
        with open(os.path.join(sys_net, interface, "address")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def conflict(interface, device=DEVICE, confs=CONFS, sys_net=SYS_NET):
    """A sentence on why the loaded kernel master is in the way of ecat on
    `interface`, or None when it is not loaded or has another card. Without
    an interface it cannot be told apart: then it counts as in the way."""
    if not os.path.exists(device):
        return None
    if not interface:
        return ("The kernel EtherCAT master is loaded (%s), and the network card ecat is to use "
                "is not known: it may be the same one." % device)
    path, text = _conf(confs)
    if text is None:
        return ("The kernel EtherCAT master is loaded (%s) and its configuration cannot be read, "
                "so it may use %s too." % (device, interface))
    what = uses(text, interface, _mac(interface, sys_net))
    if what:
        return ("The kernel EtherCAT master is loaded and uses %s (%s in %s): two masters on one "
                "card do not work." % (interface, what, path))
    return None


def at_boot(interface, enabled, confs=CONFS, sys_net=SYS_NET):
    """A sentence when the kernel master is not loaded but starts with the
    computer (ethercat.service enabled) and its configuration gives it this
    card; else None."""
    if not enabled or not interface:
        return None
    path, text = _conf(confs)
    what = uses(text, interface, _mac(interface, sys_net)) if text is not None else None
    if what:
        return ("The kernel EtherCAT master starts with the computer (ethercat.service is "
                "enabled) and takes %s (%s in %s)." % (interface, what, path))
    return None
