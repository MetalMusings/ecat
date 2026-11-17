"""The real-time check's list of lasting changes for the user to make
(the Timing tab's "To do yourself", ``ecatsetup --rt-todo``).

EtherCAT Setup changes nothing that lasts after a restart: Try it makes a
run-time change, and Keep it keeps it until the computer restarts. This
module turns what was kept, and what the findings propose, into the forms
that last, grouped so that each group is one edit: kernel parameters (one
new GRUB line), sysctl settings, kernel module options, commands run at each
start (a systemd service), commands run once, files, other commands, and
what is for the BIOS or only advice. Each with how to undo it. It only reads
(/etc/default/grub for the present kernel command line)."""

import os
import shlex
import time

from . import rtflow

GRUB = "/etc/default/grub"
SYSCTL_FILE = "/etc/sysctl.d/90-ecatsetup-rt.conf"
MODPROBE_FILE = "/etc/modprobe.d/ecatsetup-rt.conf"
SERVICE = "ecatsetup-rt.service"
SERVICE_FILE = "/etc/systemd/system/" + SERVICE


class Item:
    """One change in its lasting form. origin: "kept" (tried and kept after
    measuring), "found" (a finding, not tried)."""

    def __init__(self, title, origin, verdict=None, kind=None, commands=(), undo_commands=(),
                 kernel_param=None, keep=None, action=None, rule=None):
        self.title = title
        self.origin = origin
        self.verdict = verdict
        self.kind = kind
        self.commands = list(commands or [])
        self.undo_commands = list(undo_commands or [])
        self.kernel_param = kernel_param
        self.keep = keep or {}
        self.action = action
        self.rule = rule

    def why(self):
        if self.origin == "kept":
            return "tried and kept%s" % (" (measured: %s)" % self.verdict if self.verdict else "")
        if self.kind == "runtime":
            return "found, not tried yet: try it first (Try it in Improve mode), and keep it only if it helps"
        if self.kind == "permanent":
            return "found: it can only be tried with a restart"
        return "found"


def items(flow):
    """What goes on the list: the changes kept (this boot only), then the
    findings that are not in force."""
    out = []
    seen = set()
    for c in flow.log.changes:
        if c.state != "kept":
            continue
        out.append(Item(c.title, "kept", c.verdict, "runtime", c.commands, c.undo_commands, c.kernel_param,
                        c.keep if c.keep is not None else ({"boot": True} if not c.kernel_param else None),
                        rule=c.rule))
        seen.add((c.rule, c.item or ""))
    found, _ = flow.findings()
    for f in found:
        if f.kind == "measure" or (f.id, rtflow.item_key(f)) in seen:
            continue
        if flow.log.active_for(f.id, rtflow.item_key(f)):
            continue          # being tried: kept or undone first
        out.append(Item(f.title, "found", None, f.kind, f.commands, f.undo_commands, f.kernel_param,
                        getattr(f, "keep", None), f.action, f.id))
    return out


def grub_cmdline(path=GRUB):
    """(the value of GRUB_CMDLINE_LINUX_DEFAULT, whether the file was read)."""
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return None, False
    value = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("GRUB_CMDLINE_LINUX_DEFAULT="):
            try:
                parts = shlex.split(s.split("=", 1)[1])
                value = parts[0] if parts else ""
            except ValueError:
                value = s.split("=", 1)[1].strip().strip('"')
    return value, True


def param_key(p):
    return p.split("=", 1)[0]


def merge_params(line, params):
    """The command line with params put in: one of the same name replaced,
    else added at the end."""
    toks = (line or "").split()
    for p in params:
        k = param_key(p)
        hit = [i for i, t in enumerate(toks) if param_key(t) == k]
        if hit:
            toks[hit[0]] = p
            for i in reversed(hit[1:]):
                del toks[i]
        else:
            toks.append(p)
    return " ".join(toks)


def _unit_command(cmd):
    """A command as an ExecStart line: through sh (pipes, globs, >), with
    systemd's % specifiers escaped."""
    return "ExecStart=/bin/sh -c %s" % shlex.quote(cmd).replace("%", "%%")


def _heredoc(path, text):
    return ["sudo tee %s >/dev/null <<'EOF'" % path] + text.rstrip("\n").split("\n") + ["EOF"]


def _block(lines):
    return ["```"] + lines + ["```", ""]


def text(flow, grub_path=GRUB, now=None):
    """The list, as Markdown (readable as plain text)."""
    now = now or time.time()
    day = time.strftime("%Y%m%d", time.localtime(now))
    its = items(flow)
    inv = flow.inventory or (flow.run(flow.latest).inventory if flow.latest else None)
    head = ["# Real-time settings to make yourself", "",
            "Written %s by EtherCAT Setup on %s%s." % (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), os.uname().nodename,
                " (%s, kernel %s)" % (inv.data.get("cpu_model") or "?", inv.data.get("kernel") or "?")
                if inv is not None else ""), "",
            "EtherCAT Setup changes nothing that lasts after a restart. These are the real-time check's "
            "changes in the forms that do last, for you to make. Make one group at a time and measure again "
            "afterwards, the same way as before (the Timing tab, or ecatsetup --rt-check). Lines that start "
            "with sudo need an administrator. Each group says how to take it back.", ""]
    kept = [i for i in its if i.origin == "kept"]
    tried = [c for c in flow.log.changes if c.state == "tried"]
    if tried:
        head += ["Being tried, not on the list until kept or undone: "
                 + "; ".join(c.title for c in tried) + ".", ""]
    if kept:
        head += ["Kept for this boot only (they are gone after a restart unless made lasting below): "
                 + "; ".join(i.title for i in kept) + ".", ""]
    if not its:
        return "\n".join(head + ["Nothing to do: no change was kept and no finding proposes one.", ""])
    out = list(head)
    n = [0]

    def section(title):
        n[0] += 1
        out.extend(["## %d. %s" % (n[0], title), ""])

    def listing(group, what=lambda i: ""):
        for i in group:
            out.append("- %s%s (%s)" % (what(i), i.title, i.why()))
            note = i.keep.get("note") if i.keep else None
            if note:
                out.append("  Note: " + " ".join(note.split()))
        out.append("")

    # kernel parameters
    kp = [i for i in its if i.kernel_param and (i.origin == "kept" and not i.keep or i.origin == "found"
                                                 and (i.kind == "permanent" or i.kind == "runtime"
                                                      and not i.keep))]
    if kp:
        section("Kernel parameters (need a restart)")
        listing(kp, lambda i: "`%s`: " % i.kernel_param)
        params = []
        for i in kp:
            if i.kernel_param not in params:
                params.append(i.kernel_param)
        line, found = grub_cmdline(grub_path)
        if found:
            new = merge_params(line, params)
            out += ["The line in %s now:" % grub_path, ""] + _block(['GRUB_CMDLINE_LINUX_DEFAULT="%s"' % line])
            out += ["The line with them (a parameter of the same name is replaced):", ""] + \
                _block(['GRUB_CMDLINE_LINUX_DEFAULT="%s"' % new])
            backup = "%s.before-rt-%s" % (grub_path, day)
            out += ["Then:", ""] + _block([
                "sudo cp %s %s" % (grub_path, backup),
                "sudoedit %s        # put the line above in place of the old one" % grub_path,
                "sudo update-grub"])
            out += ["Restart the computer, then measure again. To take them back: `sudo cp %s %s && sudo "
                    "update-grub`, and restart. If the computer does not start: in the GRUB menu press e, "
                    "remove the parameter from the line that starts with linux, and press Ctrl-X (this "
                    "start only; then take it back as above)." % (backup, grub_path), ""]
            out += ["Files in /etc/default/grub.d/ can set the line too: if one does, change it there.", ""]
        else:
            out += ["%s was not found: add these to your boot loader's kernel command line:" % grub_path, ""]
            out += _block([" ".join(params)])

    def keep_group(form):
        return [i for i in its if i.keep and form in i.keep and i.kind == "runtime"]

    g = keep_group("sysctl")
    if g:
        section("Kernel settings (sysctl)")
        listing(g)
        out += _block(_heredoc(SYSCTL_FILE, "\n".join(i.keep["sysctl"] for i in g)) + ["sudo sysctl --system"])
        out += ["To take them back: `sudo rm %s`; the old values come back at the next restart, or at once "
                "with: %s." % (SYSCTL_FILE, "; ".join("`sudo %s`" % c for i in g for c in i.undo_commands)), ""]
    g = keep_group("modprobe")
    if g:
        section("Kernel module options")
        listing(g)
        out += _block(_heredoc(MODPROBE_FILE, "\n".join(i.keep["modprobe"] for i in g)))
        out += ["They count from the next restart (the run-time command does it for this boot: %s). To "
                "take them back: `sudo rm %s` and restart." % (
                    "; ".join("`sudo sh -c '%s'`" % c for i in g for c in i.commands), MODPROBE_FILE), ""]
    g = keep_group("boot")
    if g:
        section("Run at every start (a systemd service)")
        listing(g)
        unit = ["[Unit]", "Description=Real-time settings for LinuxCNC (EtherCAT Setup's real-time check)",
                "After=network-online.target", "Wants=network-online.target", "", "[Service]",
                "Type=oneshot", "RemainAfterExit=yes"]
        unit += [_unit_command(c) for i in g for c in i.commands]
        unit += ["", "[Install]", "WantedBy=multi-user.target"]
        out += _block(_heredoc(SERVICE_FILE, "\n".join(unit)) + [
            "sudo systemctl daemon-reload", "sudo systemctl enable --now %s" % SERVICE])
        out += ["To take it back: `sudo systemctl disable %s && sudo rm %s`; the settings go back at the next "
                "restart, or at once with: %s." % (
                    SERVICE, SERVICE_FILE, "; ".join("`sudo sh -c '%s'`" % c for i in g for c in i.undo_commands)),
                ""]
    g = keep_group("once")
    if g:
        section("Commands to run once")
        listing(g)
        out += _block(["sudo " + c for i in g for c in i.keep["once"]])
        out += ["To take them back: %s." % "; ".join("`sudo %s`" % c for i in g for c in i.keep.get("once-undo")
                                                      or []), ""]
    g = keep_group("file")
    if g:
        section("Files")
        listing(g)
        lines = []
        for i in g:
            f = i.keep["file"]
            lines += _heredoc(f["path"], f["text"]) + ["sudo " + c for c in f.get("then") or []]
        out += _block(lines)
        out += ["To take them back: %s." % "; ".join(
            "`sudo rm %s`%s" % (i.keep["file"]["path"], "".join(" and `sudo %s`" % c for c in
                                                                 i.keep["file"].get("then") or []))
            for i in g), ""]
    g = [i for i in its if i.kind == "permanent" and not i.kernel_param and i.commands]
    if g:
        section("Other changes")
        for i in g:
            out.append("- %s (%s): %s" % (i.title, i.why(), " ".join((i.action or "").split())))
        out.append("")
        out += _block(["sudo " + c for i in g for c in i.commands])
    g = [i for i in its if i.kind in ("bios", "advice")]
    if g:
        section("In the BIOS, and advice")
        for i in g:
            out.append("- **%s**: %s" % (i.title, " ".join((i.action or "").split())))
        out.append("")
    return "\n".join(out)


def default_name(now=None):
    return "ecatsetup-rt-todo-%s-%s.md" % (os.uname().nodename,
                                            time.strftime("%Y%m%d-%H%M", time.localtime(now or time.time())))
