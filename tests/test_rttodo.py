"""Tests of the real-time check's list of lasting changes (rttodo.py): the
groups, the GRUB line, the start-up service, and the rules' keep forms.

Run with: python3 -m unittest discover -s tests
"""

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("ECATSETUP_LINUXCNC_SERIES", "2.10")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = os.environ["TMPDIR"] = \
        __import__("tempfile").tempdir = __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
    __import__("atexit").register(__import__("shutil").rmtree, os.environ["ECATSETUP_TEST_STATE"], True)

from ecatsetup import rtcheck, rtflow, rtrules, rttodo  # noqa: E402


def finding(id_, kind, title=None, commands=(), undo=(), kernel_param=None, keep=None, action=None, item=None):
    return types.SimpleNamespace(id=id_, kind=kind, title=title or id_, commands=list(commands),
                                 undo_commands=list(undo), kernel_param=kernel_param, keep=keep,
                                 action=action, item=item, check=None)


class FakeFlow:
    def __init__(self, found=(), changes=()):
        self.found = list(found)
        self.log = types.SimpleNamespace(changes=list(changes))
        self.log.active_for = lambda rule, item: next(
            (c for c in self.log.changes if c.in_force and c.rule == rule and (c.item or "") == item), None)
        self.inventory = None
        self.latest = None

    def findings(self):
        return self.found, {}


class TestParams(unittest.TestCase):
    def test_merge(self):
        line = "quiet isolcpus=3 nohz_full=3 splash"
        self.assertEqual(rttodo.merge_params(line, ["isolcpus=managed_irq,domain,3", "i915.enable_dc=0", "nosmt"]),
                         "quiet isolcpus=managed_irq,domain,3 nohz_full=3 splash i915.enable_dc=0 nosmt")
        # twice the same name: one left, in the first place
        self.assertEqual(rttodo.merge_params("a=1 b a=2", ["a=3"]), "a=3 b")
        self.assertEqual(rttodo.merge_params("", ["x=1"]), "x=1")

    def test_grub_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "grub")
            with open(p, "w") as f:
                f.write('GRUB_DEFAULT=0\n#GRUB_CMDLINE_LINUX_DEFAULT="old"\n'
                        'GRUB_CMDLINE_LINUX_DEFAULT="quiet isolcpus=3 irqaffinity=0-2"\nGRUB_CMDLINE_LINUX=""\n')
            self.assertEqual(rttodo.grub_cmdline(p), ("quiet isolcpus=3 irqaffinity=0-2", True))
            self.assertEqual(rttodo.grub_cmdline(os.path.join(d, "none")), (None, False))


class Change(rtflow.Change):
    pass


def change(rule, state, **kw):
    return rtflow.Change(id="c-" + rule, rule=rule, title=kw.pop("title", rule), state=state, **kw)


class TestList(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.grub = os.path.join(self.tmp.name, "grub")
        with open(self.grub, "w") as f:
            f.write('GRUB_CMDLINE_LINUX_DEFAULT="quiet isolcpus=3"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def flow(self):
        changes = [
            change("vm-stat-interval", "kept", title="Statistics every second", verdict="better",
                   commands=["sysctl vm.stat_interval=10"], undo_commands=["sysctl vm.stat_interval=1"],
                   keep={"sysctl": "vm.stat_interval = 10"}),
            # a log written before keep: was kept: its commands at each start
            change("governor-not-performance", "kept", title="Governor", verdict="better",
                   commands=["echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
                             "printf '%s' x > /tmp/y"],
                   undo_commands=["echo powersave | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"]),
            change("turbo-on", "undone", title="Turbo", commands=["echo 1 > /x"], undo_commands=["echo 0 > /x"]),
            change("usb-irq-on-rt-cpu", "tried", title="USB IRQ", item="125", commands=["echo 0-2 > /p"],
                   undo_commands=["echo 3 > /p"], keep={"boot": True}),
        ]
        found = [
            finding("no-irqaffinity", "permanent", "No irqaffinity", kernel_param="irqaffinity=0-2"),
            finding("isolcpus-without-managed-irq", "permanent", "isolcpus without managed_irq",
                    kernel_param="isolcpus=managed_irq,domain,3"),
            finding("acpi-gpe-storm", "runtime", "GPE storm", commands=["echo disable > /g"],
                    undo=["echo enable > /g"], kernel_param="acpi_mask_gpe=0x6F"),
            finding("sound-power-save", "runtime", "Sound", commands=["echo 0 > /s"], undo=["echo 10 > /s"],
                    keep={"modprobe": "options snd_hda_intel power_save=0"}),
            finding("irqbalance-running", "runtime", "irqbalance", commands=["systemctl stop irqbalance"],
                    undo=["systemctl start irqbalance"],
                    keep={"once": ["systemctl disable --now irqbalance"],
                          "once-undo": ["systemctl enable --now irqbalance"]}),
            finding("io-nic-networkmanager", "runtime", "NM", commands=["nmcli device set enp2s0 managed no"],
                    undo=["nmcli device set enp2s0 managed yes"],
                    keep={"file": {"path": "/etc/NetworkManager/conf.d/90-ecatsetup-enp2s0.conf",
                                   "text": "[keyfile]\nunmanaged-devices=interface-name:enp2s0\n",
                                   "then": ["systemctl reload NetworkManager"]}}),
            finding("not-preempt-rt", "permanent", "Not PREEMPT_RT", commands=["apt install linux-image-rt-amd64"],
                    action="Install a PREEMPT_RT kernel."),
            finding("smi-seen", "bios", "SMIs", action="Switch off legacy USB."),
            finding("smi-not-measured", "measure", "Measure as root"),
            # in force (being tried): not on the list
            finding("usb-irq-on-rt-cpu", "runtime", "USB IRQ", commands=["echo 0-2 > /p"], undo=["echo 3 > /p"],
                    keep={"boot": True}, item={"irq": "125"}),
            # the same as a kept change: once
            finding("vm-stat-interval", "runtime", "Statistics every second", commands=["sysctl vm.stat_interval=10"],
                    undo=["sysctl vm.stat_interval=1"], keep={"sysctl": "vm.stat_interval = 10"}),
        ]
        return FakeFlow(found, changes)

    def text(self, flow=None):
        return rttodo.text(flow or self.flow(), grub_path=self.grub, now=1794900000)

    def test_items(self):
        its = rttodo.items(self.flow())
        self.assertEqual([i.rule for i in its],
                         ["vm-stat-interval", "governor-not-performance", "no-irqaffinity",
                          "isolcpus-without-managed-irq", "acpi-gpe-storm", "sound-power-save",
                          "irqbalance-running", "io-nic-networkmanager", "not-preempt-rt", "smi-seen"])
        self.assertEqual(its[1].keep, {"boot": True})          # an old log entry: its commands at each start

    def test_groups(self):
        t = self.text()
        heads = [l for l in t.splitlines() if l.startswith("## ")]
        self.assertEqual(heads, ["## 1. Kernel parameters (need a restart)", "## 2. Kernel settings (sysctl)",
                                 "## 3. Kernel module options", "## 4. Run at every start (a systemd service)",
                                 "## 5. Commands to run once", "## 6. Files", "## 7. Other changes",
                                 "## 8. In the BIOS, and advice"])
        self.assertIn("Being tried, not on the list until kept or undone: USB IRQ.", t)
        self.assertIn("Kept for this boot only (they are gone after a restart unless made lasting below): "
                      "Statistics every second; Governor.", t)
        self.assertNotIn("Measure as root", t)
        self.assertNotIn("Turbo", t)
        self.assertEqual(t.count("- Statistics every second"), 1)

    def test_grub(self):
        t = self.text()
        self.assertIn('GRUB_CMDLINE_LINUX_DEFAULT="quiet isolcpus=3"', t)
        self.assertIn('GRUB_CMDLINE_LINUX_DEFAULT="quiet isolcpus=managed_irq,domain,3 irqaffinity=0-2 '
                      'acpi_mask_gpe=0x6F"', t)
        self.assertIn("sudo cp %s %s.before-rt-" % (self.grub, self.grub), t)
        self.assertIn("sudo update-grub", t)
        self.assertIn("in the GRUB menu press e", t)
        self.assertIn("(found: it can only be tried with a restart)", t)
        self.assertIn("(found, not tried yet: try it first", t)
        # no GRUB: the parameters to add by hand
        t = rttodo.text(self.flow(), grub_path=os.path.join(self.tmp.name, "none"))
        self.assertIn("was not found: add these to your boot loader's kernel command line", t)
        self.assertIn("irqaffinity=0-2 isolcpus=managed_irq,domain,3 acpi_mask_gpe=0x6F", t)

    def test_service(self):
        t = self.text()
        self.assertIn("sudo tee /etc/systemd/system/ecatsetup-rt.service >/dev/null <<'EOF'", t)
        # through sh, systemd's % escaped, quotes kept
        self.assertIn("ExecStart=/bin/sh -c 'echo performance | tee "
                      "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor'", t)
        self.assertIn("ExecStart=/bin/sh -c 'printf '\"'\"'%%s'\"'\"' x > /tmp/y'", t)
        self.assertIn("sudo systemctl enable --now ecatsetup-rt.service", t)
        self.assertIn("sudo systemctl disable ecatsetup-rt.service && sudo rm "
                      "/etc/systemd/system/ecatsetup-rt.service", t)
        self.assertIn("`sudo sh -c 'echo powersave | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor'`", t)

    @unittest.skipIf(shutil.which("systemd-analyze") is None, "no systemd-analyze")
    def test_the_unit_is_valid(self):
        t = self.text()
        lines = t.splitlines()
        start = lines.index("sudo tee /etc/systemd/system/ecatsetup-rt.service >/dev/null <<'EOF'") + 1
        unit = "\n".join(lines[start:lines.index("EOF", start)]) + "\n"
        path = os.path.join(self.tmp.name, "ecatsetup-rt.service")
        with open(path, "w") as f:
            f.write(unit)
        p = subprocess.run(["systemd-analyze", "verify", path], capture_output=True, text=True)
        self.assertNotIn("ecatsetup-rt.service:", p.stderr, p.stderr)

    def test_the_other_forms(self):
        t = self.text()
        self.assertIn("sudo tee /etc/sysctl.d/90-ecatsetup-rt.conf >/dev/null <<'EOF'\nvm.stat_interval = 10\nEOF\n"
                      "sudo sysctl --system", t)
        self.assertIn("`sudo rm /etc/sysctl.d/90-ecatsetup-rt.conf`", t)
        self.assertIn("options snd_hda_intel power_save=0", t)
        self.assertIn("sudo systemctl disable --now irqbalance", t)
        self.assertIn("`sudo systemctl enable --now irqbalance`", t)
        self.assertIn("unmanaged-devices=interface-name:enp2s0\nEOF\nsudo systemctl reload NetworkManager", t)
        self.assertIn("sudo apt install linux-image-rt-amd64", t)
        self.assertIn("- **SMIs**: Switch off legacy USB.", t)

    def test_nothing(self):
        t = rttodo.text(FakeFlow(), grub_path=self.grub)
        self.assertIn("Nothing to do", t)
        self.assertNotIn("## ", t)


class TestRuleKeep(unittest.TestCase):
    def test_the_rules_keep_forms(self):
        loaded = rtrules.load([rtrules.HERE])
        rules = loaded if isinstance(loaded, dict) else {r.id: r for r in loaded}
        ctx = {"nonrt_text": "0-2", "item": {"irq": "125", "name": "enp2s0"}, "stat_interval": 1}
        self.assertEqual(rtrules.Finding(rules["sound-power-save"], ctx).keep,
                         {"modprobe": "options snd_hda_intel power_save=0"})
        self.assertEqual(rtrules.Finding(rules["vm-stat-interval"], ctx).keep, {"sysctl": "vm.stat_interval = 10"})
        nm = rtrules.Finding(rules["io-nic-networkmanager"], ctx).keep["file"]
        self.assertEqual(nm["path"], "/etc/NetworkManager/conf.d/90-ecatsetup-enp2s0.conf")
        self.assertIn("interface-name:enp2s0", nm["text"])
        self.assertIn("irqaffinity=0-2", rtrules.Finding(rules["usb-irq-on-rt-cpu"], ctx).keep["note"])
        # kernel-param is the lasting form: no keep then
        f = rtrules.Finding(rules["pcie-aspm"], ctx)
        self.assertEqual((f.kernel_param, f.keep), ("pcie_aspm.policy=performance", None))
        # a run-time change with no other form is kept by running it at each start
        self.assertEqual(rtrules.Finding(rules["turbo-on"], ctx).keep, {"boot": True})
        self.assertIsNone(rtrules.Finding(rules["no-irqaffinity"], ctx).keep)
        # every run-time rule with commands has a lasting form
        for r in rules.values():
            if r["action"]["kind"] == "runtime" and r["action"].get("commands"):
                f = rtrules.Finding(r, ctx)
                self.assertTrue(f.keep or f.kernel_param, r.id)

    def rule(self, action):
        d = {"id": "x", "title": "x", "when": "True", "cause": "x", "gain": "low", "source": [{"text": "t"}],
             "action": action, "undo": {"commands": ["u"], "text": "u"}}
        return rtrules.Rule(d, "test.yaml")

    def test_keep_is_checked(self):
        self.rule({"kind": "runtime", "commands": ["c"], "keep": {"sysctl": "a = 1"}})
        with self.assertRaisesRegex(rtrules.RuleError, "keep is for a run-time change"):
            self.rule({"kind": "permanent", "kernel-param": "a", "keep": {"sysctl": "a = 1"}})
        with self.assertRaisesRegex(rtrules.RuleError, "keep is for a run-time change"):
            self.rule({"kind": "runtime", "commands": ["c"], "keep": {"sysctl": "a", "modprobe": "b"}})
        with self.assertRaisesRegex(rtrules.RuleError, "keep once needs once-undo"):
            self.rule({"kind": "runtime", "commands": ["c"], "keep": {"once": ["d"]}})
        with self.assertRaisesRegex(rtrules.RuleError, "keep file needs a path and a text"):
            self.rule({"kind": "runtime", "commands": ["c"], "keep": {"file": {"path": "/x"}}})


class TestUnexplained(unittest.TestCase):
    def test_a_spike_no_rule_knows(self):
        u = rtrules.Unexplained({"label": "RES: Rescheduling interrupts", "with": 19, "of": 23,
                                 "expected": 0.4, "key": "res"}, {})
        flow = FakeFlow([u])
        self.assertEqual(rtflow.item_key(u), "")
        t = rttodo.text(flow, grub_path=os.path.join(os.path.dirname(__file__), "no-such-file"))
        self.assertIn("- **Spikes came with RES: Rescheduling interrupts, and no rule knows it**: find out what "
                      "drives this counter", t)


class TestCommandLine(unittest.TestCase):
    def test_rt_todo(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from ecatsetup import __main__ as cli
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_STATE_HOME": d}):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.rt_todo("-"), 0)
            self.assertIn("# Real-time settings to make yourself", out.getvalue())
            path = os.path.join(d, "todo.md")
            with redirect_stdout(io.StringIO()):
                cli.rt_todo(path)
            with open(path) as f:
                self.assertIn("Nothing to do", f.read())


class TestLogged(unittest.TestCase):
    def test_the_change_log_keeps_the_form(self):
        with tempfile.TemporaryDirectory() as d:
            log = rtflow.ChangeLog(os.path.join(d, "c.yaml"))
            f = finding("sound-power-save", "runtime", commands=["echo 0 > /s"], undo=["echo 10 > /s"],
                        keep={"modprobe": "options snd_hda_intel power_save=0"})
            c = log.add(f)
            log.save()
            back = rtflow.ChangeLog(os.path.join(d, "c.yaml"))
            self.assertEqual(back.changes[0].keep, {"modprobe": "options snd_hda_intel power_save=0"})
            self.assertEqual(c.keep, back.changes[0].keep)


if __name__ == "__main__":
    unittest.main()
