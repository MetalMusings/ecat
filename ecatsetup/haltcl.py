"""HAL files written in Tcl (HALFILE = x.tcl, run by LinuxCNC's haltcl),
turned into the plain HAL they would give, without running HAL.

The file runs in tclsh as haltcl runs it: the INI file's sections as
arrays (::KINS(JOINTS), from the file with its #INCLUDEs in place, read as
LinuxCNC's parse_ini reads it), every halcmd command that is not already
a Tcl command as a command of its own, setp and sets taking an expression.
Here those commands only write the HAL line they were given, with the
line of the Tcl file it came from; nothing is loaded or connected.

What cannot be known without HAL: a query (getp, gets, ptype, stype,
show) gives an empty answer, and is named in the problems, as is any Tcl
error (the lines before it are kept).
"""

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from . import lcnc_env

TCLSH = os.environ.get("ECAT_TCLSH") or shutil.which("tclsh") or "tclsh"

# halcmd's commands (halcmd -h); those that are Tcl commands already
# (list, source) stay Tcl's, as in haltcl
COMMANDS = ["loadrt", "loadusr", "waitusr", "unload", "unloadrt", "unloadusr", "lock", "unlock",
            "net", "linkps", "linksp", "linkpp", "unlinkp", "newsig", "delsig", "getp", "gets",
            "setp", "sets", "ptype", "stype", "addf", "delf", "show", "save", "start", "stop",
            "alias", "unalias", "status", "newinst", "echo", "unecho", "print_comp",
            "newthread", "delthread", "debug"]
QUERIES = {"getp", "gets", "ptype", "stype", "show", "status", "save"}

PRELUDE = r"""
set _out [open [lindex $argv 0] w]
set _err [open [lindex $argv 1] w]
set _file [file normalize [lindex $argv 3]]

# LinuxCNC's parse_ini (lib/tcltk/linuxcnc.tcl)
proc parse_ini {filename} {
    set f [open $filename]
    while {[gets $f line] >= 0} {
        set line [string trim $line]
        if {[regexp {^\[(.*)\]\s*$} $line _ section]} {
        } elseif {[regexp {^([^#]+?)\s*=\s*(.*?)\s*$} $line _  k v]} {
            upvar $section s
            lappend s([string trim $k]) $v
        }
    }
    close $f
}
if {[lindex $argv 2] ne ""} { parse_ini [lindex $argv 2] }

# the line of the Tcl file a command came from
proc _line {} {
    for {set i [info frame]} {$i > 0} {incr i -1} {
        set f [info frame $i]
        if {[dict exists $f file] && [file normalize [dict get $f file]] eq $::_file} {
            return [dict get $f line]
        }
    }
    return 0
}

proc hal {c args} {
    # an INI value is a list (parse_ini lappends): its braces are not HAL's
    set words [list $c]
    foreach a $args { lappend words [join $a { }] }
    puts $::_out "[_line]\t[join $words { }]"
    if {[lsearch -exact {%(queries)s} $c] >= 0} {
        puts $::_err "[_line]\tquery\t$c $args"
    }
    return ""
}
proc setp {p v} {
    if {[catch {set v [uplevel [list expr $v]]}]} {}
    hal setp $p $v
}
proc sets {p v} {
    if {[catch {set v [uplevel [list expr $v]]}]} {}
    hal sets $p $v
}
foreach c {%(commands)s} {
    if {[info commands $c] == {}} {
        proc $c args "eval hal $c \$args"
    }
}
proc show args { hal show {*}$args }
package provide Linuxcnc 1.0
package provide Hal 1.0

set argv {}
if {[catch {source $_file} msg]} {
    puts $::_err "0\terror\t$msg"
}
close $_out
close $_err
"""


@dataclass
class Expanded:
    text: str = ""                                 # plain HAL, one statement a line
    lines: list = field(default_factory=list)      # the Tcl file's line of each text line
    problems: list = field(default_factory=list)   # readable


_cache = {}


def _stamp(*paths):
    out = []
    for p in paths:
        try:
            out.append(os.stat(p).st_mtime_ns)
        except OSError:
            out.append(None)
    return tuple(out)


def expand(path, ini_path=None, timeout=10):
    """The HAL a .tcl file gives with the INI file's values (see the module
    docstring). Raises OSError when tclsh is missing or the file cannot be
    read."""
    from . import integrate
    path = os.path.abspath(path)
    sources = [path] + ([ini_path] + integrate.ini_includes(ini_path) if ini_path else [])
    key = (path, os.path.abspath(ini_path) if ini_path else None)
    stamp = _stamp(*sources)
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with tempfile.TemporaryDirectory(prefix="ecatsetup-haltcl-") as d:
        out, err, ini, prelude = (os.path.join(d, n) for n in ("out", "err", "ini", "prelude.tcl"))
        ini_arg = ""
        if ini_path:
            with open(ini, "w") as f:
                f.write("".join(x for _, x in integrate.ini_lines(ini_path)))
            ini_arg = ini
        with open(prelude, "w") as f:
            f.write(PRELUDE % {"commands": " ".join(COMMANDS), "queries": " ".join(sorted(QUERIES))})
        env = dict(lcnc_env.env() or os.environ)
        if ini_path:
            env["INI_FILE_NAME"] = os.path.abspath(ini_path)
        env.setdefault("HALLIB_DIR", integrate.HALLIB)
        try:
            proc = subprocess.run([TCLSH, prelude, out, err, ini_arg, path],
                                  cwd=os.path.dirname(path), env=env, capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise OSError("%s did not finish in %d s in tclsh" % (os.path.basename(path), timeout))
        result = Expanded()
        name = os.path.basename(path)
        try:
            with open(out, errors="replace") as f:
                for raw in f.read().splitlines():
                    line, _, hal = raw.partition("\t")
                    result.lines.append(int(line) if line.isdigit() else 0)
                    result.text += hal + "\n"
            with open(err, errors="replace") as f:
                for raw in f.read().splitlines():
                    line, _, rest = raw.partition("\t")
                    kind, _, what = rest.partition("\t")
                    if kind == "query":
                        result.problems.append("%s:%s asks HAL (%s); the answer is not known "
                                               "here" % (name, line, what.strip()))
                    elif kind == "error":
                        result.problems.append("%s: Tcl error, the rest of the file is not read: %s"
                                               % (name, what))
        except OSError:
            result.problems.append("%s could not be run in tclsh: %s" % (
                name, (proc.stderr or proc.stdout).strip()))
        if proc.returncode and not result.problems:
            result.problems.append("%s: tclsh: %s" % (name, (proc.stderr or proc.stdout).strip()))
    _cache[key] = (stamp, result)
    return result
