"""Which LinuxCNC EtherCAT Setup works with: a run-in-place build (its tree
named in settings.yaml as `linuxcnc:`, in $ECATSETUP_LINUXCNC, or the one the
shell is in through $EMC2_HOME) or an installed one on the PATH.

A run-in-place LinuxCNC needs its scripts/rip-environment read before its
programs run (halrun, halcmd, tclsh's HAL library path). The window and the
command line do that here, for the programs they start, so that the user
need not read it in the shell first. The installer (scripts/install-rip)
writes `linuxcnc:` into the settings."""

import os
import subprocess

from . import settings

_cache = {}


def _is_tree(p):
    return bool(p) and os.path.isfile(os.path.join(p, "scripts", "rip-environment"))


def tree():
    """The run-in-place tree to use, or None for an installed LinuxCNC."""
    for p in (os.environ.get("ECATSETUP_LINUXCNC"), settings.load().get("linuxcnc"),
              os.environ.get("EMC2_HOME")):
        if isinstance(p, str) and _is_tree(os.path.expanduser(p)):
            return os.path.realpath(os.path.expanduser(p))
    return None


def env():
    """The environment for LinuxCNC's programs: the run-in-place tree's
    rip-environment read, or None when the shell is in that already (or an
    installed LinuxCNC is used) and os.environ will do."""
    root = tree()
    if root is None:
        return None
    here = os.environ.get("EMC2_HOME")
    if here and os.path.realpath(here) == root:
        return None
    if root not in _cache:
        try:
            out = subprocess.run(
                ["bash", "-c", '. "$1/scripts/rip-environment" >/dev/null 2>&1; env -0', "x", root],
                capture_output=True, timeout=20, check=True).stdout
            _cache[root] = dict(x.split("=", 1) for x in out.decode(errors="replace").split("\0")
                                if "=" in x)
        except (OSError, subprocess.SubprocessError):
            _cache[root] = None
    return _cache[root]


def program(name):
    """The path of a LinuxCNC program: the tree's bin, else the name for the PATH."""
    root = tree()
    p = os.path.join(root, "bin", name) if root else None
    return p if p and os.path.exists(p) else name


def hallib():
    root = tree()
    if root and os.path.isdir(os.path.join(root, "lib", "hallib")):
        return os.path.join(root, "lib", "hallib")
    return "/usr/share/linuxcnc/hallib"


def describe():
    root = tree()
    return "run-in-place LinuxCNC in %s" % root if root else "installed LinuxCNC"
