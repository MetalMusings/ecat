"""Shared device data (prototype): facts, layers, remembering, sharing, the
check, reports, fetching and choosing (ecatsetup/devicedata.py)."""

import glob
import io
import os
import sys
import tarfile
import tempfile
import unittest
import unittest.mock
import urllib.parse

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
NO_DIR = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_DEVICES"] = NO_DIR

from ecatsetup import devicedata, quirks  # noqa: E402

ECT60 = (0xA88, 0xA880002)
EL7041 = (2, 0x1B813052)


def shipped(name):
    with open(os.path.join(quirks.SHIPPED, name)) as f:
        return yaml.safe_load(f)


class Folders(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.own = os.path.join(self.dir.name, "devices")
        os.environ["ECATSETUP_DEVICES"] = self.own
        os.environ["ECATSETUP_SHARED_CACHE"] = os.path.join(self.dir.name, "cache")
        quirks.load(reload=True)

    def tearDown(self):
        os.environ["ECATSETUP_DEVICES"] = NO_DIR
        os.environ.pop("ECATSETUP_SHARED_CACHE", None)
        os.environ.pop("ECATSETUP_DEVICES_URL", None)
        quirks.load(reload=True)
        self.dir.cleanup()


class TestFacts(unittest.TestCase):
    def test_a_files_facts_and_their_values(self):
        raw = shipped("rtelligent-ect60.yaml")
        paths = devicedata.facts(raw)
        self.assertIn(("commands", "0x2010"), paths)
        self.assertIn(("homing", "input-bits", "IN5"), paths)
        self.assertIn(("homing", "extras", "36"), paths)
        self.assertIn(("tuning", "0x2009:00"), paths)
        self.assertIn(("measure", "ignore", "0x6077"), paths)
        self.assertEqual(devicedata.get(raw, ("homing", "input-bits", "IN5")), 20)
        self.assertIsNone(devicedata.get(raw, ("commands", "0x2011")))

    def test_put_and_merge_go_fact_by_fact(self):
        base = shipped("rtelligent-ect60.yaml")
        top = {"commands": {"0x2011": "mine"}, "homing": {"input-bits": {"IN5": 21}},
               "settings": [{"object": "0x2009:00", "type": "UINT", "value": 2000}]}
        m = devicedata.merge(base, top)
        self.assertEqual(devicedata.get(m, ("commands", "0x2011")), "mine")
        self.assertEqual(devicedata.get(m, ("commands", "0x2010")),
                         "Zero Position on the ECT60, a command, not a setting")
        self.assertEqual(devicedata.get(m, ("homing", "input-bits", "IN5")), 21)
        self.assertEqual(devicedata.get(m, ("homing", "input-bits", "IN4")), 19)
        self.assertEqual(devicedata.get(m, ("settings", "0x2009:00"))["value"], 2000)
        self.assertEqual(len(m["homing"]["methods"]), 17)
        devicedata.put(m, ("settings", "0x2009:00"), None)
        self.assertNotIn("settings", m)
        self.assertEqual(devicedata.get(base, ("homing", "input-bits", "IN5")), 20)   # base untouched

    def test_object_keys_in_any_form_are_one_fact(self):
        raw = {"commands": {"0x2010": "a"}}
        devicedata.put(raw, ("commands", "0x2010"), "b")
        self.assertEqual(raw["commands"], {"0x2010": "b"})
        self.assertEqual(devicedata.get({"commands": {0x2010: "c"}}, ("commands", "0x2010")), "c")


class TestRemember(Folders):
    def test_a_fact_kept_with_its_source_is_laid_over_the_shipped_file(self):
        path = devicedata.remember(ECT60, "ECT60", ("commands", "0x2011"), "mine",
                                   {"kind": "manual", "ref": "manual 2.2.9", "date": "2026-11-26"})
        self.assertEqual(os.path.dirname(path), self.own)
        q = quirks.of(ECT60)
        self.assertEqual(q["commands"][0x2011], "mine")
        self.assertIn(0x2010, q["commands"])                       # the shipped file's, kept
        self.assertEqual(devicedata.sources(ECT60)["commands 0x2011"]["ref"], "manual 2.2.9")
        # a second fact goes into the same file
        devicedata.remember(ECT60, "ECT60", ("settings", "0x2009:00"),
                            {"type": "UINT", "value": 2000}, {"kind": "tried"})
        self.assertEqual(len(glob.glob(os.path.join(self.own, "*.yaml"))), 1)
        self.assertEqual(quirks.settings(ECT60), [{"object": "0x2009:00", "type": "UINT", "value": 2000}])
        # and None takes it away again, with its source
        devicedata.remember(ECT60, "ECT60", ("commands", "0x2011"), None, None)
        self.assertNotIn(0x2011, quirks.of(ECT60)["commands"])
        self.assertNotIn("commands 0x2011", devicedata.sources(ECT60))

    def test_a_fact_the_quirk_reader_refuses_is_not_kept(self):
        with self.assertRaises(devicedata.Error):
            devicedata.remember(ECT60, "ECT60", ("settings", "0x2009:00"), {"value": 1}, None)
        self.assertEqual(glob.glob(os.path.join(self.own, "*.yaml")), [])

    def test_save_as_quirk_file_keeps_what_was_remembered(self):
        devicedata.remember(ECT60, "ECT60", ("commands", "0x2011"), "mine", {"kind": "manual"})
        slave = {"identity": {"vendor": "0xA88", "product": "0x0A880002"}, "name": "x",
                 "settings": [{"object": "0x2009:00", "type": "UINT", "value": 3000}]}
        quirks.save(slave, devicedata.own_path(ECT60))
        q = quirks.of(ECT60)
        self.assertEqual(q["commands"][0x2011], "mine")
        self.assertEqual(q["settings"][0]["value"], 3000)
        self.assertIn("commands 0x2011", q["sources"])


class TestCheck(unittest.TestCase):
    def test_every_shipped_file_can_be_shared(self):
        for path in glob.glob(os.path.join(quirks.SHIPPED, "*.yaml")):
            with open(path) as f:
                raw = devicedata.strip(yaml.safe_load(f))
            if "match" not in raw:
                # for lcec types only (beckhoff-el70x1-position): a shared file needs a device
                self.assertIn("match: vendor and product are needed", devicedata.check(raw))
                continue
            self.assertEqual(devicedata.check(raw), [], path)
            self.assertNotIn("pins", raw)

    def test_what_is_refused(self):
        good = devicedata.strip(shipped("beckhoff-el7041.yaml"))
        self.assertEqual(devicedata.check(good), [])
        cases = [
            (dict(good, pins={"0x6000:01": {"pin": "x"}}), "pins: pin names"),
            ({k: v for k, v in good.items() if k != "format"}, "format: a version number"),
            (dict(good, format=99), "newer than this check"),
            (dict(good, colour="red"), "unknown keys: colour"),
            ({k: v for k, v in good.items() if k != "match"}, "match: vendor and product"),
            (dict(good, sources={"commands 0x9999": {"kind": "manual"}}), "no such fact"),
            (dict(good, sources={"hal": {"ref": "x"}}), "needs kind"),
            (dict(good, name="x" * 3000), "longer than"),
        ]
        for raw, why in cases:
            self.assertTrue(any(why in p for p in devicedata.check(raw)), (why, devicedata.check(raw)))

    def test_hal_lines_only_in_the_checkable_form(self):
        ok = shipped("beckhoff-el7041.yaml")["hal"]
        self.assertEqual(devicedata.check_hal(ok), [])

        def bad(**change):
            h = dict(ok, **change)
            return devicedata.check_hal(h)
        self.assertTrue(bad(lines=["loadusr -w rm -rf /"]))
        self.assertTrue(bad(lines=["net estop-ext {name}-auto-reset.out"]))         # a machine signal
        self.assertTrue(bad(lines=["net {name}-x iocontrol.0.emc-enable-in"]))      # a machine pin
        self.assertTrue(bad(lines=["setp motion.servo.overruns 0"]))
        self.assertTrue(bad(lines=["setp {name}-enable-edge.in-edge [EMC]X"]))
        self.assertTrue(bad(lines=["source other.hal"]))
        self.assertTrue(bad(load={"hal_parport": ["{name}-pp"]}))
        self.assertTrue(bad(load={"edge": ["estop"]}))
        self.assertTrue(bad(addf=["motion-command-handler"]))
        self.assertEqual(bad(load={"conv_s32_float": ["{name}-c"]}, addf=["{name}-c"], lines=[
            "net {signal:0x6000:11} => {name}-c.in", "net {name}-v {name}-c.out => {pin:0x7000:11}"]), [])


class TestShare(unittest.TestCase):
    def test_the_post_carries_the_stripped_file(self):
        raw = shipped("rtelligent-ect60.yaml")
        raw["pins"] = {"0x6064:00": {"pin": "my-x", "scale": 66.6}}
        title, body, url = devicedata.share(raw)
        self.assertIn("Rtelligent ECT60", title)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(q["category"], [devicedata.CATEGORY])
        self.assertEqual(q["body"], [body])
        back = devicedata.from_post(body + "\nThanks!\n")
        self.assertEqual(back, devicedata.strip(raw))
        self.assertNotIn("pins", back)
        self.assertIn("CC0", body)
        self.assertEqual(devicedata.repo_path(back, 12), "devices/00000a88-0a880002/12.yaml")
        with self.assertRaises(devicedata.Error):
            devicedata.from_post("no file here")


class TestReports(unittest.TestCase):
    def test_a_report_round_trip(self):
        body = devicedata.report("devices/00000a88-0a880002/12.yaml", "worked",
                                 ["commands 0x2011"], revision="0x202", words="Fine on my lathe.")
        r = devicedata.read_report(body)
        self.assertEqual(r["result"], "worked")
        self.assertEqual(r["used"], ["commands 0x2011"])
        self.assertEqual(r["revision"], "0x202")
        self.assertEqual(r["words"], "Fine on my lathe.")
        self.assertIsNone(devicedata.read_report("just words"))
        self.assertIsNone(devicedata.read_report("```ecat-report\nresult: great\n```\n"))
        with self.assertRaises(devicedata.Error):
            devicedata.report("x", "great", [])
        self.assertEqual(devicedata.summary([{"result": "worked"}, {"result": "worked"},
                                             {"result": "did not work"}]),
                         "2 worked, 1 did not work")


def archive(path, files):
    """A GitHub-like tar.gz of the data repository (one top folder)."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            if data is None:            # a symbolic link
                info = tarfile.TarInfo("repo-main/" + name)
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
                continue
            b = data.encode()
            info = tarfile.TarInfo("repo-main/" + name)
            info.size = len(b)
            tar.addfile(info, io.BytesIO(b))


class TestFetch(Folders):
    def test_fetched_files_are_checked_again_and_chosen_one_by_one(self):
        good = devicedata.strip(shipped("rtelligent-ect60.yaml"))
        devicedata.put(good, ("homing", "input-bits", "IN5"), 22)
        good["sources"] = {"homing input-bits IN5": {"kind": "tried", "date": "2026-11-20"}}
        evil = dict(good, hal={"lines": ["loadusr -w rm -rf /"]})
        index = {"format": 1, "files": [
            {"path": "devices/00000a88-0a880002/3.yaml", "discussion": 3, "url": "https://x/3",
             "author": "a", "upvotes": 1, "reports": [{"result": "worked"}]},
            {"path": "devices/00000a88-0a880002/4.yaml", "discussion": 4, "author": "b",
             "upvotes": 9}]}
        tgz = os.path.join(self.dir.name, "repo.tar.gz")
        archive(tgz, {"index.json": __import__("json").dumps(index),
                      "devices/00000a88-0a880002/3.yaml": yaml.safe_dump(good),
                      "devices/00000a88-0a880002/4.yaml": yaml.safe_dump(evil),
                      "devices/00000a88-0a880002/5.yaml": None,
                      "tools/intake.py": "print('not taken')",
                      "../../escape.yaml": "x: 1"})
        os.environ["ECATSETUP_DEVICES_URL"] = "file://" + tgz
        self.assertEqual(devicedata.fetch(), 3)
        self.assertFalse(os.path.exists(os.path.join(devicedata.cache_dir(), "tools")))
        self.assertTrue(devicedata.fetched()["url"].startswith("file://"))
        found = devicedata.shared_for(ECT60)
        self.assertEqual([f["discussion"] for f in found], [3, 4])   # a refused file last
        self.assertEqual(found[0]["problems"], [])
        self.assertTrue(found[1]["problems"])
        diff = devicedata.compare(ECT60, found[0]["raw"])
        row = next(r for r in diff if r["path"] == ("homing", "input-bits", "IN5"))
        self.assertEqual((row["now"], row["shared"], row["same"]), (20, 22, False))
        self.assertEqual(row["source"]["kind"], "tried")
        self.assertTrue(next(r for r in diff if r["path"] == ("commands", "0x2010"))["same"])
        devicedata.take(ECT60, "ECT60", found[0], ("homing", "input-bits", "IN5"))
        self.assertEqual(quirks.of(ECT60)["homing"]["input-bits"]["IN5"], 22)
        src = devicedata.sources(ECT60)["homing input-bits IN5"]
        self.assertEqual((src["kind"], src["ref"], src["by"]), ("shared", "https://x/3", "a"))
        self.assertIn("tried on a machine", src["given"])

    def test_a_failed_fetch_keeps_the_old_cache(self):
        os.environ["ECATSETUP_DEVICES_URL"] = "file://" + os.path.join(self.dir.name, "none.tar.gz")
        with self.assertRaises(devicedata.Error):
            devicedata.fetch()
        self.assertIsNone(devicedata.fetched())
        self.assertEqual(devicedata.shared_for(ECT60), [])


class TestNewerFormat(unittest.TestCase):
    def test_a_newer_file_is_read_without_what_it_adds(self):
        q = quirks.parse({"format": 2, "name": "n", "colour": "red", "commands": {"0x2010": "z"}})
        self.assertEqual(q["commands"], {0x2010: "z"})
        with self.assertRaises(quirks.QuirkError):
            quirks.parse({"format": 1, "colour": "red"})


def tool(root, name):
    import importlib.util
    spec = importlib.util.spec_from_file_location("devrepo_" + name, os.path.join(root, "tools", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRepoTools(unittest.TestCase):
    """The data repository's Action: intake.py takes a post in, index.py builds
    index.json from the threads; on a repository built by scripts/devices-repo-build."""

    def setUp(self):
        import subprocess
        self.dir = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.dir.name, "repo")
        subprocess.run([os.path.join(os.path.dirname(__file__), "..", "scripts", "devices-repo-build"),
                        self.root], check=True, capture_output=True)
        self.intake, self.index = tool(self.root, "intake"), tool(self.root, "index")

    def tearDown(self):
        self.dir.cleanup()

    def event(self, body, number=7, slug="general"):
        return {"action": "created", "discussion": {
            "number": number, "body": body, "node_id": "D_x", "html_url": "https://x/%d" % number,
            "user": {"login": "alice"}, "category": {"slug": slug}}}

    def test_a_post_is_taken_in_refused_or_skipped(self):
        self.assertTrue(os.path.isfile(os.path.join(self.root, "checker", "ecatsetup", "devicedata.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, ".github", "workflows", "intake.yml")))
        _, body, _ = devicedata.share(shipped("rtelligent-ect60.yaml"))
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            self.assertEqual(self.intake.take(self.event(body), self.root, dry=True), "taken")
            self.assertEqual(self.intake.take(self.event(body), self.root, dry=True), "same")
            bad = body.replace("format: 1", "format: 1\npins: {}")
            self.assertEqual(self.intake.take(self.event(bad, 8), self.root, dry=True), "refused")
            self.assertEqual(self.intake.take(self.event(body, 9, "q-a"), self.root, dry=True), "skipped")
            # an edit that moves it to another device keeps one file
            other = body.replace("0x0A880002", "0x0A880003")
            self.assertEqual(self.intake.take(self.event(other), self.root, dry=True), "taken")
        self.assertIn("Taken in as `devices/00000a88-0a880002/7.yaml`", out.getvalue())
        self.assertIn("pins: pin names and scales", out.getvalue())
        kept = sorted(glob.glob(os.path.join(self.root, "devices", "*", "*.yaml")))
        self.assertEqual([os.path.relpath(k, self.root) for k in kept],
                         ["devices/00000a88-0a880003/7.yaml"])
        with open(kept[0]) as f:
            text = f.read()
        self.assertIn("Discussion #7 by alice", text)
        self.assertEqual(devicedata.check(yaml.safe_load(text)), [])

    def test_the_index_has_votes_and_reports_and_drops_deleted_threads(self):
        _, body, _ = devicedata.share(shipped("rtelligent-ect60.yaml"))
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            self.intake.take(self.event(body, 7), self.root, dry=True)
            self.intake.take(self.event(body, 8), self.root, dry=True)
        rel = "devices/00000a88-0a880002/7.yaml"
        rep = devicedata.report(rel, "worked", ["commands 0x2010"], words="ok here")
        threads = [{"number": 7, "url": "https://x/7", "upvoteCount": 3, "author": {"login": "alice"},
                    "comments": {"nodes": [{"author": {"login": "bob"}, "body": rep, "updatedAt": "t"},
                                           {"author": {"login": "eve"}, "body": "nice"}]}}]
        idx = self.index.build(threads, self.root)
        self.assertEqual(len(idx["files"]), 1)          # 8's thread is gone: its file removed
        e = idx["files"][0]
        self.assertEqual((e["path"], e["upvotes"], e["author"]), (rel, 3, "alice"))
        self.assertEqual([(r["author"], r["result"], r["words"]) for r in e["reports"]],
                         [("bob", "worked", "ok here")])
        self.assertFalse(os.path.exists(os.path.join(self.root, "devices/00000a88-0a880002/8.yaml")))
        # an empty answer (a failed query) removes nothing
        self.index.build([], self.root)
        self.assertTrue(os.path.exists(os.path.join(self.root, rel)))


if __name__ == "__main__":
    unittest.main()
