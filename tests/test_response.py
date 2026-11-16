"""Tests of the response measurement (response.py): the analysis against a
simulated drive, the recording file, the test's sampler HAL, adding the
measurement objects, and a measurement run against the fake HAL of
test_testmode with a fake halsampler.

Run with: python3 -m unittest discover -s tests
"""

import copy
import json
import math
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# quirk files: only those that come with the program
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = os.environ["TMPDIR"] = \
        __import__("tempfile").tempdir = __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
    __import__("atexit").register(__import__("shutil").rmtree, os.environ["ECATSETUP_TEST_STATE"], True)
sys.path.insert(0, os.path.dirname(__file__))

from ecatsetup import coe, drivetest, response, testmode, tool  # noqa: E402
from test_drivetest import ECT60, FakeDrive  # noqa: E402
import test_testmode  # noqa: E402
from test_testmode import PINS, cfg  # noqa: E402

P = 0.001       # the servo period of the simulations
SCALE = 1000.0  # counts per mm


def trapezoid(targets, v, a, p=P, dwell=500, settle=300):
    """A command as limit3 makes it: trapezoidal moves to the targets, each
    followed by a dwell, after a standstill at 0."""
    x0, out = 0.0, [0.0] * settle
    for t in targets:
        d = abs(t - x0)
        sign = 1 if t > x0 else -1
        ta = v / a
        if a * ta * ta > d:
            ta = math.sqrt(d / a)
        vp = a * ta
        da = 0.5 * a * ta * ta
        tc = (d - 2 * da) / vp
        total = 2 * ta + tc
        n = 1
        while n * p < total:
            tt = n * p
            if tt < ta:
                s = 0.5 * a * tt * tt
            elif tt < ta + tc:
                s = da + vp * (tt - ta)
            else:
                s = d - 0.5 * a * (total - tt) ** 2
            out.append(x0 + sign * s)
            n += 1
        out += [t] * (dwell + 1)
        x0 = t
    return out


def drive(cmd, k=2, kp=40.0, ff=0.0, p=P, zeta=None, window=1, stay=0.0):
    """A drive k cycles behind the command (the frames) with a position loop
    of gain kp and velocity feedforward ff: (pos-fb, its 0x60F4 in counts).
    zeta: a second-order loop instead, with that damping (overshoots).
    window: a sliding average over that many cycles on the command before
    the loop (a command filter, such as the ECT60's 0x2009). stay: mm the
    position stays off the target at standstill (a stepper's last counts)."""
    def at(i):
        i = min(max(i, 0), len(cmd) - 1)
        return sum(cmd[max(j, 0)] for j in range(i - window + 1, i + 1)) / window
    x, vx, fb, de = 0.0, 0.0, [], []
    for n in range(len(cmd)):
        t = at(n - k) if n >= k else cmd[0]
        tp = at(n - k - 1) if n - k - 1 >= 0 else cmd[0]
        off = stay if abs(t - tp) < 1e-12 else 0.0
        fb.append(round((x + off) * SCALE) / SCALE)
        de.append(float(round((t - x - off) * SCALE)))
        if zeta is None:
            x += (ff * (t - tp) / p + kp * (t - x)) * p
        else:
            w = kp
            vx += (w * w * (t - x) - 2 * zeta * w * vx) * p
            x += vx * p
    return fb, de


def recording(targets=(40, 0, 40, 0), v=50.0, a=500.0, k=2, kp=40.0, ff=0.0, with_de=True,
              turnaround=2, zeta=None, window=1, stay=0.0, **meta):
    cmd = trapezoid(targets, v, a)
    fb, de = drive(cmd, k, kp, ff, zeta=zeta, window=window, stay=stay)
    data = {"cmd": cmd, "fb": fb, "enabled": [1.0] * len(cmd)}
    if with_de:
        data["drive-error"] = de
    m = {"period-ns": int(P * 1e9), "scale": SCALE, "turnaround-cycles": turnaround,
         "spec": {"distance": targets[0], "velocity": v, "acceleration": a,
                  "repeats": len(targets) // 2}}
    m.update(meta)
    return response.Recording(m, data)


class TestAnalyse(unittest.TestCase):
    def test_a_soft_drive_without_feedforward(self):
        res = response.analyse(recording(kp=40.0))
        # the bus: the turn-around; the drive's target is just that much behind
        self.assertAlmostEqual(res.bus, 0.002)
        self.assertAlmostEqual(res.target_delay, 0.002)
        self.assertAlmostEqual(res.command_delay, 0.0)
        # and the rest is the drive's loop: 1/Kp
        self.assertTrue(res.drive_lag_measured)
        self.assertAlmostEqual(res.drive_lag, 1 / 40.0, delta=0.0015)
        self.assertAlmostEqual(res.lag, 0.002 + 1 / 40.0, delta=0.0015)
        self.assertAlmostEqual(res.const[1], 50 * 0.027, delta=0.08)
        self.assertAlmostEqual(res.const[-1], -res.const[1], delta=0.01)
        # the lag is taken at constant speed both ways, not from the fit
        self.assertAlmostEqual(res.lag, (res.const[1] - res.const[-1]) / (2 * res.vmax), places=9)
        self.assertEqual(len(res.stops), 4)
        self.assertEqual(max(s.overshoot for s in res.stops), 0.0)
        self.assertEqual([k for k, _t in res.findings], ["feedforward"])
        text = "\n".join(response.report(res, recording()))
        self.assertIn("an effective position gain of 4", text)
        self.assertIn("the bus: 2 cycles", text)
        self.assertIn("velocity feedforward", text)

    def test_a_command_filter(self):
        # a stiff loop behind a 25-cycle sliding average, as the ECT60 with 0x2009 = 25600 us
        res = response.analyse(recording(kp=1000.0, window=25))
        self.assertAlmostEqual(res.bus, 0.002)
        self.assertAlmostEqual(res.target_delay, 0.014, delta=0.0011)
        self.assertAlmostEqual(res.command_delay, 0.012, delta=0.0011)
        self.assertLess(res.drive_lag, 0.002)
        self.assertEqual(res.findings[0][0], "command-filter")
        self.assertIn("12.0 ms after it arrives", res.findings[0][1])
        # a bus of three cycles is not taken for a filter
        res = response.analyse(recording(k=3, kp=1000.0, turnaround=3))
        self.assertAlmostEqual(res.target_delay, 0.003)
        self.assertAlmostEqual(res.command_delay, 0.0)
        self.assertEqual([k for k, _t in res.findings], ["ok"])

    def test_the_largest_part_first(self):
        # a 12 ms filter in front of a loop that lags 10 ms, and the other way round
        res = response.analyse(recording(kp=100.0, window=25, targets=(60, 0, 60, 0)))
        self.assertEqual([k for k, _t in res.findings][:2], ["command-filter", "feedforward"])
        res = response.analyse(recording(kp=40.0, window=9, targets=(60, 0, 60, 0)))
        self.assertEqual([k for k, _t in res.findings][:2], ["feedforward", "command-filter"])

    def test_feedforward_from_linuxcnc(self):
        # the profile's velocity feedforward (0x60B1) is, for the drive, what its own
        # is: the same simulated drive with it lags only by the bus
        soft = response.analyse(recording(kp=40.0))
        self.assertIn("cia402.feedforward.velocity", soft.findings[0][1])
        fed = response.analyse(recording(kp=40.0, ff=1.0))
        self.assertAlmostEqual(fed.lag, 0.002, delta=0.0003)
        self.assertNotIn("feedforward", [k for k, _t in fed.findings])

    def test_feedforward_leaves_the_bus(self):
        res = response.analyse(recording(kp=200.0, ff=1.0))
        self.assertAlmostEqual(res.lag, 0.002, delta=0.0002)
        self.assertLess(abs(res.drive_lag), 0.0002)
        self.assertEqual(res.findings[0][0], "ok")
        self.assertIn("follows within the bus's delay", "\n".join(response.report(res)))

    def test_without_0x60f4_the_drive_is_not_split(self):
        res = response.analyse(recording(k=3, kp=80.0, with_de=False, turnaround=3))
        self.assertIsNone(res.target_delay)
        self.assertAlmostEqual(res.bus, 0.003)
        self.assertFalse(res.drive_lag_measured)
        self.assertAlmostEqual(res.drive_lag, 1 / 80.0, delta=0.001)
        self.assertIsNone(res.drive_peak)
        self.assertEqual(res.findings[0][0], "command-filter")
        self.assertIn("Add 0x60F4", res.findings[0][1])
        # no turn-around known (no automatic SYNC0 shift): two cycles
        res = response.analyse(recording(kp=80.0, with_de=False, turnaround=0))
        self.assertAlmostEqual(res.bus, 0.002)
        self.assertIn("0x60F4 would tell them apart", "\n".join(response.report(res)))

    def test_a_stepper_that_stays_a_few_counts_off_has_settled(self):
        res = response.analyse(recording(kp=1000.0, stay=0.004))
        self.assertTrue(all(st.settle is not None for st in res.stops))
        self.assertAlmostEqual(max(abs(st.final) for st in res.stops), 0.004, delta=0.0015)
        self.assertNotIn("gain-high", [k for k, _t in res.findings])

    def test_a_short_move_says_so(self):
        res = response.analyse(recording(targets=(10, 0), kp=40.0))
        self.assertTrue(any("had not settled" in n for n in res.notes))
        res = response.analyse(recording(kp=40.0))
        self.assertFalse(any("had not settled" in n for n in res.notes))

    def test_overshoot_and_settling(self):
        res = response.analyse(recording(kp=60.0, zeta=0.3, with_de=False))
        self.assertGreater(max(s.overshoot for s in res.stops), 0.05)
        self.assertIn("gain-high", [k for k, _t in res.findings])
        self.assertTrue(all(s.settle and s.settle > 0.02 for s in res.stops))
        # every stop counts from the move's own direction
        self.assertEqual([s.direction for s in res.stops], [1, -1, 1, -1])

    def test_ferror_from_the_joint(self):
        rec = recording(kp=200.0, ff=1.0)
        res = response.analyse(rec, {"MAX_VELOCITY": 100.0, "MAX_ACCELERATION": 500.0,
                                     "FERROR": 1.0, "MIN_FERROR": 0.1})
        f = res.ferror
        # 2 ms x 100 mm/s = 0.2 mm, twice that, rounded up
        self.assertAlmostEqual(f["predicted"], 0.2, delta=0.02)
        self.assertEqual(f["FERROR"], 0.5)
        self.assertTrue(f["extrapolated"])     # 100 mm/s against a test at 50
        self.assertEqual(f["now"], {"FERROR": 1.0, "MIN_FERROR": 0.1})
        text = "\n".join(response.report(res, rec))
        self.assertIn("MAX_VELOCITY 100", text)
        self.assertIn("now FERROR 1.0, MIN_FERROR 0.1", text)
        self.assertIn("extrapolated", text)
        # without a joint: the test's own velocity
        res = response.analyse(rec, {})
        self.assertFalse(res.ferror["extrapolated"])
        self.assertIn("no joint", res.ferror["source"])

    def test_torque(self):
        rec = recording(kp=200.0, ff=1.0, with_de=False,
                        limits={"max-torque": [300, 1000], "following-error": [2000, 4000]})
        cmd = rec.data["cmd"]
        v = [0.0] + [(cmd[n + 1] - cmd[n - 1]) / (2 * P) for n in range(1, len(cmd) - 1)] + [0.0]
        a = [0.0] + [(cmd[n + 1] - 2 * cmd[n] + cmd[n - 1]) / P ** 2
                     for n in range(1, len(cmd) - 1)] + [0.0]
        # 0.02 % per mm/s2 and 5 % of friction, in per mille
        rec.data["torque"] = [10 * (0.02 * an + (5.0 if vn > 1 else -5.0 if vn < -1 else 0.0))
                              for vn, an in zip(v, a)]
        res = response.analyse(rec)
        self.assertAlmostEqual(res.torque_per_accel, 0.02, delta=0.002)
        self.assertAlmostEqual(res.torque_const[1], 5.0, delta=0.1)
        self.assertAlmostEqual(res.torque_peak, 15.0, delta=0.5)
        self.assertEqual(res.torque_limit, 30.0)
        self.assertFalse(any("torque reached" in n for n in res.notes))
        self.assertEqual(res.window, {"test": 2.0, "drive": 4.0})
        text = "\n".join(response.report(res, rec))
        self.assertIn("Torque: peak 15", text)
        # what the profile's torque feedforward would need (cia402.feedforward.torque)
        self.assertIn("cia402: {feedforward: {torque: 0.02}}", text)
        self.assertIn("of the drive's own window (4.000 mm)", text)
        # at the test's limit the result is not the drive's own
        rec.meta["limits"]["max-torque"] = [150, 1000]
        res = response.analyse(rec)
        self.assertTrue(any("torque reached the test's limit" in n for n in res.notes))

    def test_only_enabled_samples(self):
        rec = recording(kp=80.0)
        n = len(rec)
        rec.data["enabled"] = [0.0] * 100 + [1.0] * (n - 100)
        res = response.analyse(rec)
        self.assertEqual(res.samples, n - 100)
        rec.data["enabled"] = [0.0] * n
        res = response.analyse(rec)
        self.assertIsNone(res.lag)
        self.assertIn("too few samples", response.report(res)[0])

    def test_lost_and_ended_early(self):
        rec = recording(kp=80.0, ended="the button was released")
        rec.lost = 3
        res = response.analyse(rec)
        self.assertTrue(any("3 samples were lost" in n for n in res.notes))
        self.assertTrue(any("ended early: the button" in n for n in res.notes))

    def test_nice(self):
        self.assertEqual([response.nice(x) for x in (0.13, 0.2, 0.21, 3.0, 7.0, 12.0)],
                         [0.2, 0.2, 0.5, 5.0, 10.0, 20.0])


class TestCompare(unittest.TestCase):
    def test_before_and_after(self):
        before = recording(kp=1000.0, window=25, trials={"0x2009:00": [25600, 25600]})
        after = recording(kp=1000.0, window=5, trials={"0x2009:00": [25600, 6400]})
        lines = response.compare(before, response.analyse(before), after, response.analyse(after))
        self.assertEqual(lines[0], "Since the last measurement: 0x2009:00 25600 -> 6400")
        text = "\n".join(lines)
        self.assertRegex(text, r"lag 1[45]\.\d\d -> [45]\.\d\d ms \(-\d\d %\)")
        self.assertIn("added before the drive's loop", text)
        # put back between them; nothing tried at all
        lines = response.compare(after, response.analyse(after), before.__class__(
            dict(before.meta, trials={}), before.data), response.analyse(before))
        self.assertEqual(lines[0], "Since the last measurement: 0x2009:00 6400 -> 25600 (put back)")
        plain = recording(kp=200.0, ff=1.0)
        self.assertEqual(response.compare(plain, response.analyse(plain), plain,
                                          response.analyse(plain))[0],
                         "Since the last measurement: no setting changed in the drive")

    def test_another_move_is_not_compared(self):
        a, b = recording(kp=80.0), recording(kp=80.0, v=40.0)
        lines = response.compare(a, response.analyse(a), b, response.analyse(b))
        self.assertIn("not comparable", lines[1])
        self.assertEqual(len(lines), 2)


class TestRecordingFile(unittest.TestCase):
    def test_parse_samples(self):
        text = "0 1.5 2.0 1\n1 1.6 2.1 1\noverrun\n4 1.9 2.2 0\nbad line\n5 2.0 2.3\n"
        data, lost = response.parse_samples(text, ["cmd", "fb", "enabled"])
        self.assertEqual(data["cmd"], [1.5, 1.6, 1.9])
        self.assertEqual(data["enabled"], [1.0, 1.0, 0.0])
        self.assertEqual(lost, 2)

    def test_save_and_load(self):
        rec = recording(targets=(5, 0), kp=80.0)
        rec.lost = 2
        with tempfile.TemporaryDirectory() as d:
            path = rec.save(os.path.join(d, "x.samples"))
            back = response.Recording.load(path)
        self.assertEqual(back.meta, rec.meta)
        self.assertEqual(back.lost, 2)
        self.assertEqual(back.data["cmd"], rec.data["cmd"])
        self.assertEqual(back.data["drive-error"], rec.data["drive-error"])


def drive_cfg(inputs_extra=(), axes=None):
    c = cfg()
    x = c["slaves"][0]
    # a drive without a quirk file (the ECT60's leaves its 0x6077 out)
    x["identity"] = {"vendor": "0x00000099", "product": "0x00000001"}
    x["process-data"]["inputs"][0]["entries"] += list(inputs_extra)
    if axes:
        x["cia402"]["axes"] = axes
    return c


FERR = {"object": "0x60F4:00", "bits": 32, "type": "DINT", "pin": "following-error",
        "hal-type": "sint"}
TORQUE = {"object": "0x6077:00", "bits": 16, "type": "INT", "pin": "torque-fb", "hal-type": "sint"}


class TestSamplerHal(unittest.TestCase):
    def test_columns_follow_the_process_data(self):
        load, addf, nets = response.sampler_hal(drive_cfg([FERR, TORQUE]))
        self.assertEqual(load, ["loadrt sampler depth=16384 cfg=ffbss"])
        self.assertEqual(addf, ["addf sampler.0 servo-thread"])
        self.assertIn("net ecatresp0-drive-error x.following-error => sampler.0.pin.3", nets)
        self.assertIn("net ecatresp0-torque x.torque-fb => sampler.0.pin.4", nets)
        self.assertEqual(nets[-1], "setp sampler.0.enable 0")
        cols = response.columns(drive_cfg([FERR, TORQUE]), 0, "x")
        self.assertEqual([c.key for c in cols], ["cmd", "fb", "enabled", "drive-error", "torque"])

    def test_no_csp_drive_no_sampler(self):
        c = cfg()
        c["slaves"] = [c["slaves"][1]]
        self.assertEqual(response.sampler_hal(c), ([], [], []))

    def test_second_axis_objects(self):
        s = drive_cfg([dict(FERR, object="0x68F4:00", pin="following-error-1")], ["a", "b"])["slaves"][0]
        self.assertEqual(response.measure_entries(s, 0), {})
        self.assertEqual(list(response.measure_entries(s, 1)), ["drive-error"])


class TestAddObjects(unittest.TestCase):
    def dictionary(self, *indexes):
        d = coe.Dictionary()
        for i in indexes:
            d.objects[i] = "x"
            d.entries[(i, 0)] = coe.CoeEntry(i, 0, "r-r-r-", "int32" if i % 0x800 == 0xF4
                                             else "int16", 32 if i % 0x800 == 0xF4 else 16, "x")
        return d

    def test_add_to_the_first_input_pdo(self):
        s = drive_cfg()["slaves"][0]
        d = self.dictionary(0x60F4, 0x6077)
        missing = response.missing_objects(s, d)
        self.assertEqual([m[0] for m in missing], [0x6077, 0x60F4])
        added = response.add_objects(s, [m[0] for m in missing], d)
        self.assertEqual(added, [dict(TORQUE), dict(FERR)])
        self.assertEqual(s["process-data"]["inputs"][0]["entries"][-2:], added)
        self.assertEqual(response.missing_objects(s, d), [])
        # not in the drive: nothing to add
        self.assertEqual(response.missing_objects(drive_cfg()["slaves"][0], self.dictionary()), [])

    def test_fixed_pdo_is_refused(self):
        s = drive_cfg()["slaves"][0]
        with self.assertRaisesRegex(ValueError, "fixed"):
            response.add_objects(s, [0x60F4], self.dictionary(0x60F4), fixed_pdos=[0x1A00])

    def test_second_axis_pin(self):
        s = drive_cfg(axes=["a", "b"])["slaves"][0]
        d = self.dictionary(0x68F4)
        self.assertEqual([m[0] for m in response.missing_objects(s, d, axis=1)], [0x68F4])
        added = response.add_objects(s, [0x68F4], d)
        self.assertEqual(added[0]["pin"], "following-error-1")


FAKE_HALSAMPLER = """\
#!/usr/bin/python3
import os, signal, sys, time
signal.signal(signal.SIGTERM, lambda *a: (sys.stdout.flush(), sys.exit(0)))
with open(os.environ["FAKE_SAMPLES"]) as f:
    sys.stdout.write(f.read())
sys.stdout.flush()
with open(os.environ["FAKE_SAMPLES"] + ".args", "w") as f:
    f.write(" ".join(sys.argv[1:]))
parent = os.getppid()
# never outlives the test that started it
for _ in range(1200):
    if os.getppid() != parent:
        break
    time.sleep(0.05)
"""


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class TestMeasurement(unittest.TestCase):
    """A measurement against TestTestMode's fake HAL and a fake halsampler."""

    def setUp(self):
        # the fake HAL of TestTestMode, without running its tests again
        helper = test_testmode.TestTestMode("test_says_so_when_ecat_does_not_load")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.dir, self.state, self.started = helper.dir, helper.state, helper.started
        pins = dict(PINS, **{"x.pos-fb": ["real", "OUT", "50"],
                             "x.drv-actual-position": ["sint", "OUT", "5000"],
                             "x.stat-op-enabled": ["bool", "OUT", "FALSE"],
                             "x.fault-reason": ["sint", "OUT", "0"],
                             "ecatjog0.out": ["real", "OUT", "50"],
                             "sampler.0.curr-depth": ["sint", "OUT", "0"],
                             "ecat.0.turnaround-cycles": ["sint", "OUT", "2"]})
        d = self.dir.name
        path = os.path.join(d, "halsampler")
        with open(path, "w") as f:
            f.write(FAKE_HALSAMPLER)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.samples = os.path.join(d, "samples.txt")
        rec = recording(targets=(10, 0), kp=200.0, ff=1.0, with_de=False)
        with open(self.samples, "w") as f:
            for n, (c, fb) in enumerate(zip(rec.data["cmd"], rec.data["fb"])):
                f.write("%d %f %f 1\n" % (n, c + 50, fb + 50))
        for p in (mock.patch.dict(os.environ, {"FAKE_HAL_PINS": json.dumps(pins),
                                               "FAKE_SAMPLES": self.samples}),
                  mock.patch.object(response, "HALSAMPLER", path),
                  mock.patch.object(response, "SAVE_DIR", os.path.join(d, "responses")),
                  mock.patch.object(drivetest, "RESTORE_FILE", os.path.join(d, "restore.json"))):
            p.start()
            self.addCleanup(p.stop)
        self.drive = FakeDrive({(0x607D, 1): -1, (0x607D, 2): -1, (0x2022, 0): 4000})
        for name in ("upload", "download"):
            p = mock.patch.object(tool, name, getattr(self.drive, name))
            p.start()
            self.addCleanup(p.stop)

    def set_fake_pin(self, name, value):
        with open(self.state) as f:
            pins = json.load(f)
        pins[name][2] = value
        with open(self.state, "w") as f:
            json.dump(pins, f)

    def setp_log(self):
        with open(self.state + ".setp") as f:
            return f.read().splitlines()

    def measurement(self, dt, spec, ini=None, clock=None):
        m = response.Measurement(dt, spec, ini, clock=clock)

        def stop():
            if m.proc is not None and m.proc.poll() is None:
                m.proc.kill()
                m.proc.wait(2)
            if m.out is not None:
                m.out.close()
        self.addCleanup(stop)
        return m

    def prepared(self):
        run = self.started()
        dt = drivetest.DriveTest(run, ECT60, None, {"scale": 100.0, "travel": 20.0})
        dt._has = lambda i, s: (i, s) in {(0x2022, 0)}
        dt.prepare()
        return dt

    def test_a_whole_measurement(self):
        dt = self.prepared()
        clock = Clock()
        # a setting being tried in this drive goes with the recording
        from ecatsetup import tuning
        p = mock.patch.object(tuning, "TRIALS_FILE", os.path.join(self.dir.name, "trials.json"))
        p.start()
        self.addCleanup(p.stop)
        tuning._save([{"position": 0, "slave": "x", "index": 0x2009, "subindex": 0,
                       "dtype": "uint16", "original": 25600, "value": 2000},
                      {"position": 5, "slave": "y", "index": 0x2009, "subindex": 0,
                       "dtype": "uint16", "original": 25600, "value": 1000}])
        m = self.measurement(dt, {"distance": 30.0, "velocity": 40.0, "acceleration": 400.0,
                                  "repeats": 1, "dwell": 0.5}, {"joint": 0}, clock=clock)
        m.start()
        log = self.setp_log()
        # the drive is enabled where it stands, then the measurement's limits
        self.assertIn("x.enable TRUE", log)
        self.assertIn("ecatjog0.maxv 40.0", log)
        self.assertIn("ecatjog0.maxa 400.0", log)
        self.assertEqual(log[-1].split()[0] in ("sampler.0.enable",), True)
        self.assertIn("sampler.0.enable TRUE", log)
        # never past the drive test's travel: out by 20, not 30
        self.assertEqual(m.targets, [70.0, 50.0])
        self.assertEqual(m.step(), "enabling")
        self.set_fake_pin("x.stat-op-enabled", "TRUE")
        self.assertEqual(m.step(), "settle")
        clock.t += 0.31
        self.assertEqual(m.step(), "moving")
        self.assertEqual(self.setp_log()[-1], "ecatsel0.in1 70.0")
        self.assertEqual(m.step(), "moving")     # the limit3 is not there yet
        self.set_fake_pin("ecatjog0.out", "70.0")
        self.assertEqual(m.step(), "dwell")
        clock.t += 0.5
        self.assertEqual(m.step(), "moving")
        self.assertEqual(self.setp_log()[-1], "ecatsel0.in1 50.0")
        self.set_fake_pin("ecatjog0.out", "50.0")
        self.assertEqual(m.step(), "dwell")
        clock.t += 0.5
        self.assertEqual(m.step(), "draining")
        log = self.setp_log()
        self.assertIn("sampler.0.enable FALSE", log)
        self.assertIn("x.enable FALSE", log)
        # the drive test's own velocity and acceleration are back
        self.assertEqual(log[-2:], ["ecatjog0.maxv 5.0", "ecatjog0.maxa 50.0"])
        self.assertEqual(m.step(), "done")
        with open(self.samples + ".args") as f:
            self.assertEqual(f.read(), "-c 0 -t")
        rec = m.recording
        self.assertEqual(len(rec), 1 + len(recording(targets=(10, 0)).data["cmd"]) - 1)
        self.assertEqual(rec.meta["turnaround-cycles"], 2)
        self.assertEqual(rec.meta["trials"], {"0x2009:00": [25600, 2000]})
        self.assertEqual(rec.meta["limits"]["following-error"], [200, 4000])
        self.assertTrue(os.path.exists(m.path))
        self.assertFalse(os.path.exists(m.path + ".raw"))
        res = response.analyse(response.Recording.load(m.path))
        self.assertAlmostEqual(res.lag, 0.002, delta=0.0003)

    def test_released_early(self):
        dt = self.prepared()
        clock = Clock()
        m = self.measurement(dt, {"distance": 5.0}, clock=clock)
        m.start()
        self.set_fake_pin("x.stat-op-enabled", "TRUE")
        m.step()
        clock.t += 0.31
        m.step()
        m.abort()
        self.assertEqual(m.state, "draining")
        self.assertEqual(m.ended, "the button was released")
        self.assertIn("x.enable FALSE", self.setp_log())
        self.assertEqual(m.step(), "done")      # what was recorded is kept
        self.assertEqual(m.recording.meta["ended"], "the button was released")

    def test_the_drive_stops_by_itself(self):
        dt = self.prepared()
        clock = Clock()
        m = self.measurement(dt, {"distance": 5.0}, clock=clock)
        m.start()
        self.set_fake_pin("x.stat-op-enabled", "TRUE")
        m.step()
        clock.t += 0.31
        m.step()
        self.set_fake_pin("x.stat-op-enabled", "FALSE")
        self.set_fake_pin("x.fault-reason", "1")
        self.assertEqual(m.step(), "draining")
        self.assertIn("left \"Operation enabled\" (fault-reason 1)", m.ended)
        self.assertEqual(m.step(), "done")

    def test_the_drive_never_enables(self):
        dt = self.prepared()
        clock = Clock()
        m = self.measurement(dt, {"distance": 5.0}, clock=clock)
        m.start()
        clock.t += 3.1
        self.assertEqual(m.step(), "draining")
        self.assertIn("did not reach", m.ended)
        m.step()
        self.assertIn(m.state, ("done", "failed"))

    def test_refused(self):
        dt = self.prepared()
        with self.assertRaisesRegex(testmode.TestError, "give a distance"):
            response.Measurement(dt, {"distance": 0.0})
        dt.prepared = False
        with self.assertRaisesRegex(testmode.TestError, "prepared CSP"):
            response.Measurement(dt, {"distance": 5.0})


class TestIniValues(unittest.TestCase):
    def test_from_the_joint(self):
        with tempfile.TemporaryDirectory() as d:
            ini = os.path.join(d, "m.ini")
            with open(ini, "w") as f:
                f.write("[KINS]\nJOINTS = 2\n[JOINT_1]\nMAX_VELOCITY = 100\nMAX_ACCELERATION = 1000\n"
                        "FERROR = 1.0\nMIN_FERROR = 0.25\n")
            c = {"linuxcnc": {"joints": [{"joint": 1, "slave": "x", "scale": 100.0}]}}
            self.assertEqual(response.ini_values(ini, c, "x"),
                             {"joint": 1, "MAX_VELOCITY": 100.0, "MAX_ACCELERATION": 1000.0,
                              "FERROR": 1.0, "MIN_FERROR": 0.25})
            self.assertEqual(response.ini_values(ini, c, "y"), {})
            self.assertEqual(response.ini_values(None, c, "x"), {})

    def test_from_the_hal_files(self):
        # a configuration written by hand: no linuxcnc.joints, the HAL file connects the joint
        with tempfile.TemporaryDirectory() as d:
            ini = os.path.join(d, "m.ini")
            with open(ini, "w") as f:
                f.write("[KINS]\nJOINTS = 2\n[HAL]\nHALFILE = m.hal\n"
                        "[JOINT_1]\nMAX_VELOCITY = 400\nMAX_ACCELERATION = 1500\n")
            with open(os.path.join(d, "m.hal"), "w") as f:
                f.write("net xp <= joint.1.motor-pos-cmd\nnet xp => x.pos-cmd\n")
            c = cfg()
            self.assertEqual(response.ini_values(ini, c, "x"),
                             {"joint": 1, "MAX_VELOCITY": 400.0, "MAX_ACCELERATION": 1500.0})
            self.assertEqual(response.ini_values(ini, c, "io"), {})


try:
    from PyQt5.QtWidgets import QApplication
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from ecatsetup import gui, session
except ImportError:
    gui = None


@unittest.skipIf(gui is None, "PyQt5 is not installed")
class TestResponseCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_plot_lanes_and_result(self):
        rec = recording(kp=80.0)
        rec.data["torque"] = [0.0] * len(rec)
        lanes = gui.RecordingPlot.lanes(rec)
        self.assertEqual([x[0] for x in lanes], ["position", "following error", "torque"])
        self.assertEqual([s[0] for s in lanes[1][2]], ["LinuxCNC", "drive (0x60F4)"])
        s = session.Session()
        w = gui.MainWindow(s)
        self.addCleanup(w.close)
        card = w.test_page.response

        class Done:
            recording = rec
            ended = None
            path = "/somewhere/x.samples"
        card.finished(Done())
        self.assertIn("effective position gain", card.result.text())
        self.assertIn("/somewhere/x.samples", card.saved.text())
        self.assertIs(card.last, rec)
        card.plot.grab()        # paints without an error
        self.assertEqual(card.kinds, ["feedforward"])
        # the drive's settings, what the result points at first
        from ecatsetup import tuning
        found = [tuning.Candidate(0x2009, 0, "Filter Time", "command-filter", "uint16", 25600,
                                  0, 25600, 6400, "us", "a filter", from_quirk=True),
                 tuning.Candidate(0x2026, 1, "PVIA_Kp", "position-loop", "uint16", 2000)]
        found[1].applies = False
        card.show_settings(found)
        t = card.settings_table
        self.assertFalse(t.isHidden())
        self.assertEqual(t.rowCount(), 2)
        self.assertEqual(t.item(0, 0).text(), "0x2009:00")
        self.assertEqual(t.item(0, 2).text(), "25600 us")
        self.assertIn("at the maximum of its range", t.item(0, 3).text())
        self.assertIn("not used in the drive's present mode", t.item(1, 3).text())
        self.assertFalse(card.settings_hint.isHidden())
        card.show_settings([])
        self.assertTrue(t.isHidden())

    def test_try_keep_undo_and_compare(self):
        from ecatsetup import tuning
        for box in ("warning", "question"):
            p = mock.patch.object(gui.QMessageBox, box, side_effect=AssertionError("a dialog: %s" % box))
            p.start()
            self.addCleanup(p.stop)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = mock.patch.object(tuning, "TRIALS_FILE", os.path.join(d.name, "trials.json"))
        p.start()
        self.addCleanup(p.stop)
        sdos = {(0x2009, 0): 25600, (0x6041, 0): 0x0231}

        def download(pos, i, sub, v, t=None):
            sdos[(i, sub)] = int(v)
        for name, fn in (("upload", lambda pos, i, sub, t=None: sdos.get((i, sub))),
                         ("download", download)):
            p = mock.patch.object(gui.tool, name, fn)
            p.start()
            self.addCleanup(p.stop)
        s = session.Session()
        s.cfg = {"slaves": [dict(ECT60, settings=[])]}
        w = gui.MainWindow(s)
        self.addCleanup(w.close)
        card = w.test_page.response

        class DT:
            name = "x"
            slave = s.cfg["slaves"][0]
        card.drive = lambda: DT
        card.show_settings([tuning.Candidate(0x2009, 0, "Filter Time", "command-filter", "uint16",
                                             25600, 0, 25600)])
        card.settings_table.selectRow(0)
        self.assertFalse(card.try_row.isHidden())
        # the field and what is being tried come above the list, which can be
        # taller than the window (under it they were out of sight at the machine)
        order = [card.lay.indexOf(x) for x in (card.try_row, card.trials_table,
                                                 card.settings_table)]
        self.assertEqual(order, sorted(order))
        self.assertEqual(card.try_value.text(), "25600")
        card.try_value.setText("2000")
        card.try_setting()
        self.assertEqual(sdos[(0x2009, 0)], 2000)
        self.assertEqual(card.trials_table.rowCount(), 1)
        self.assertEqual(card.trials_table.item(0, 2).text(), "25600")
        self.assertEqual(card.settings_table.item(0, 2).text(), "2000")
        card.undo_trial(tuning.pending()[0])
        self.assertEqual(sdos[(0x2009, 0)], 25600)
        self.assertEqual(card.trials_table.rowCount(), 0)
        card.settings_table.selectRow(0)
        card.try_value.setText("2000")
        card.try_setting()
        card.keep_trial(tuning.pending()[0])
        self.assertEqual(s.cfg["slaves"][0]["settings"],
                         [{"object": "0x2009:00", "name": "Filter Time", "type": "UINT",
                           "value": 2000}])
        self.assertEqual(tuning.pending(), [])
        self.assertEqual(sdos[(0x2009, 0)], 2000)       # still in the drive's RAM
        # a measurement after another: what changed, first
        before = recording(kp=1000.0, window=25, trials={})
        after = recording(kp=1000.0, window=5, trials={"0x2009:00": [25600, 2000]})
        for rec in (before, after):
            class Done:
                recording = rec
                ended = None
                path = "x.samples"
            card.finished(Done())
        self.assertTrue(card.result.text().startswith(
            "Since the last measurement: 0x2009:00 25600 -> 2000"))
        s.saved = copy.deepcopy(s.cfg)    # closing would ask about saving
        # hidden without a prepared CSP drive
        card.show_drive(ECT60)
        self.assertTrue(card.isHidden())


if __name__ == "__main__":
    unittest.main()
