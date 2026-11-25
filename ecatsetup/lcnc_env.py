"""Which LinuxCNC EtherCAT Setup works with: a run-in-place build (its tree
named in settings.yaml as `linuxcnc:`, in $ECATSETUP_LINUXCNC, or the one the
shell is in through $EMC2_HOME) or an installed one on the PATH.

A run-in-place LinuxCNC needs its scripts/rip-environment read before its
programs run (halrun, halcmd, tclsh's HAL library path). The window and the
command line do that here, for the programs they start, so that the user
need not read it in the shell first. The installer (scripts/install-rip)
writes `linuxcnc:` into the settings.

The series (2.9 or 2.10) decides what the component can do: 2.9's integer
pins have 32 bits (no LINT/ULINT integer pins, `extend` only for 8- and
16-bit entries) and halcmd prints its types as bit/s32/u32/float."""

import os
import subprocess

from . import settings

_cache = {}
_versions = {}


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


def module_dir():
    """The folder of LinuxCNC's realtime modules (ECATSETUP_MODULE_DIR for tests)."""
    if os.environ.get("ECATSETUP_MODULE_DIR"):
        return os.environ["ECATSETUP_MODULE_DIR"]
    root = tree()
    return os.path.join(root, "rtlib") if root else "/usr/lib/linuxcnc/modules"


def has_module(name):
    """Whether LinuxCNC has the realtime module `name` (such as a homing module)."""
    return os.path.isfile(os.path.join(module_dir(), name + ".so"))


def describe():
    root = tree()
    v = version()
    name = "LinuxCNC %s" % v if v else "LinuxCNC"
    return "run-in-place %s in %s" % (name, root) if root else "installed %s" % name


def version():
    """LinuxCNC's version ("2.9.4", "2.10.0~pre2"), or None when it cannot be
    found: the run-in-place tree's VERSION file, else linuxcnc_var of the
    installed LinuxCNC."""
    root = tree()
    if root not in _versions:
        v = None
        if root:
            try:
                with open(os.path.join(root, "VERSION")) as f:
                    v = f.read().strip() or None
            except OSError:
                pass
        else:
            try:
                v = subprocess.run(["linuxcnc_var", "LINUXCNCVERSION"], capture_output=True,
                                   timeout=10).stdout.decode(errors="replace").strip() or None
            except (OSError, subprocess.SubprocessError):
                pass
        _versions[root] = v
    return _versions[root]


def parse_series(text):
    """"2.9.4" -> (2, 9); None when text is not a version."""
    import re
    m = re.match(r"\s*(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def series():
    """(major, minor) of the LinuxCNC in use: (2, 9), (2, 10), ... When it
    cannot be found, (2, 10), the series ecat is written for.
    $ECATSETUP_LINUXCNC_SERIES ("2.9") overrides (tests, a check for the
    other series)."""
    return (parse_series(os.environ.get("ECATSETUP_LINUXCNC_SERIES"))
            or parse_series(version()) or (2, 10))


def wide_pins(s=None):
    """True when HAL's integer pins have 64 bits (2.10 and later); s: a
    series, else the one in use."""
    return (s or series()) >= (2, 10)
