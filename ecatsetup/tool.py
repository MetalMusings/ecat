"""Read from the bus with the userspace master's ethercat tool.

Everything here only reads: slave list, SII, the CoE dictionary and single
SDO uploads. The tool talks to a running master over its IPC socket, which
can be a free-running ec_master ("ethercat-uspace start") or the master
inside a running LinuxCNC.
"""

import os
import re
import subprocess

TOOL = os.environ.get("ECAT_TOOL", "ethercat-uspace")


class ToolError(Exception):
    pass


def run(*args, binary=False):
    proc = subprocess.run([TOOL] + [str(a) for a in args], capture_output=True)
    if proc.returncode != 0:
        raise ToolError("%s %s: %s" % (TOOL, " ".join(map(str, args)),
                                       proc.stderr.decode(errors="replace").strip()))
    return proc.stdout if binary else proc.stdout.decode(errors="replace")


def slave_positions():
    """Positions of the slaves on the bus."""
    return [int(line.split()[0]) for line in run("slaves").splitlines() if line.strip()]


def slave_list():
    """Position -> (name, master's error flag), from the slave list."""
    slaves = {}
    for line in run("slaves").splitlines():
        f = line.split(None, 4)
        if len(f) >= 4:
            slaves[int(f[0])] = (f[4] if len(f) > 4 else "", f[3] == "E")
    return slaves


def slave_info(pos):
    """Identity and state from "slaves -v" for one slave."""
    text = run("slaves", "-v", "-p", pos)
    info = {"position": pos, "dc": "Distributed clocks: yes" in text}
    for key, pattern in (("vendor_id", r"Vendor Id:\s+(0x[0-9a-f]+)"),
                         ("product_code", r"Product code:\s+(0x[0-9a-f]+)"),
                         ("revision", r"Revision number:\s+(0x[0-9a-f]+)"),
                         ("serial", r"Serial number:\s+(0x[0-9a-f]+)")):
        m = re.search(pattern, text)
        info[key] = int(m.group(1), 16) if m else None
    m = re.search(r"^State:\s+(\S+)", text, re.M)
    info["state"] = m.group(1) if m else "?"
    # kept by the master since the slave's own code is cleared when the
    # error is acknowledged (MetalMusings/ethercat)
    m = re.search(r"^Last AL status code:\s+(0x[0-9a-f]+)", text, re.M)
    info["al_status_code"] = int(m.group(1), 16) if m else None
    return info


def sii(pos):
    return run("sii_read", "-p", pos, binary=True)


def sdos(pos):
    return run("sdos", "-p", pos)


def upload(pos, index, subindex, dtype):
    """One SDO value as an int (or string), or None if the slave refuses."""
    try:
        out = run("upload", "-p", pos, "-t", dtype, "0x%04x" % index, subindex).strip()
    except ToolError:
        return None
    if dtype == "string":
        return out
    return int(out.split()[0], 0)
