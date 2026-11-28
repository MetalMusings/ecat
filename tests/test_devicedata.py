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
        self.assertIn(("errors", "0x8611"), paths)
        self.assertIn(("homing", "extras", "36"), paths)
        self.assertIn(("tuning", "0x2009:00"), paths)
        self.assertIn(("measure", "ignore", "0x6077"), paths)
        self.assertTrue(devicedata.get(raw, ("errors", "0x8611")).startswith("Position following"))
        self.assertIsNone(devicedata.get(raw, ("commands", "0x2011")))

    def test_put_and_merge_go_fact_by_fact(self):
        base = shipped("rtelligent-ect60.yaml")
        top = {"commands": {"0x2011": "mine"}, "errors": {"0x8611": "mine", "0x8612": "new"},
               "settings": [{"object": "0x2009:00", "type": "UINT", "value": 2000}]}
        m = devicedata.merge(base, top)
        self.assertEqual(devicedata.get(m, ("commands", "0x2011")), "mine")
        self.assertEqual(devicedata.get(m, ("commands", "0x2010"))["note"],
                         "Zero Position on the ECT60, a command, not a setting")
        self.assertEqual(devicedata.get(m, ("errors", "0x8611")), "mine")
        self.assertEqual(devicedata.get(m, ("errors", "0x8612")), "new")
        self.assertEqual(devicedata.get(m, ("settings", "0x2009:00"))["value"], 2000)
        self.assertEqual(len(m["homing"]["methods"]), 17)
        devicedata.put(m, ("settings", "0x2009:00"), None)
        self.assertNotIn("settings", m)
        self.assertTrue(devicedata.get(base, ("errors", "0x8611")).startswith("Position"))   # base untouched

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

    def test_compare_tells_a_conflict_from_another_machines_value(self):
        rows = devicedata.compare(ECT60, {"commands": {"0x2010": "other words"},
                                          "settings": [{"object": "0x2009:00", "type": "UINT", "value": 9}]})
        self.assertEqual([(r["differs"], r["sort"]) for r in rows],
                         [("conflict", devicedata.PROPERTY), ("new", devicedata.EXPERIENCE)])

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
        devicedata.put(good, ("errors", "0x8611"), "shared words")
        good["sources"] = {"errors 0x8611": {"kind": "tried", "date": "2026-11-20"}}
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
        row = next(r for r in diff if r["path"] == ("errors", "0x8611"))
        self.assertEqual((row["shared"], row["same"]), ("shared words", False))
        self.assertTrue(row["now"].startswith("Position following"))
        self.assertEqual(row["source"]["kind"], "tried")
        self.assertTrue(next(r for r in diff if r["path"] == ("commands", "0x2010"))["same"])
        devicedata.take(ECT60, "ECT60", found[0], ("errors", "0x8611"))
        self.assertEqual(quirks.of(ECT60)["errors"][0x8611], "shared words")
        src = devicedata.sources(ECT60)["errors 0x8611"]
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


class TestFromElsewhere(Folders):
    """A quirk file from a forum or a file: the same check, refused for another device."""

    def test_raw_yaml_or_a_post_with_a_block(self):
        raw = devicedata.strip(shipped("rtelligent-ect60.yaml"))
        text = yaml.safe_dump(raw)
        self.assertEqual(devicedata.from_text(text), raw)
        post = "Here is mine for the ECT60:\n\n```yaml\n%s```\nWorks on my mill." % text
        self.assertEqual(devicedata.from_text(post), raw)
        for bad in ("just words", "- a list", "x" * (300 * 1024), "a: [unclosed"):
            with self.assertRaises(devicedata.Error):
                devicedata.from_text(bad)

    def test_checked_and_refused_for_another_device(self):
        raw = devicedata.strip(shipped("rtelligent-ect60.yaml"))
        e = devicedata.imported(raw, ECT60, "mine.yaml")
        self.assertEqual(e["problems"], [])
        self.assertIn("another device", devicedata.imported(raw, EL7041, "mine.yaml")["problems"][0])
        self.assertTrue(devicedata.imported(dict(raw, pins={}), ECT60, "x")["problems"])
        devicedata.put(raw, ("commands", "0x2011"), "from a forum")
        e = devicedata.imported(raw, ECT60, "mine.yaml")
        devicedata.take(ECT60, "ECT60", e, ("commands", "0x2011"))
        self.assertEqual(devicedata.sources(ECT60)["commands 0x2011"]["ref"], "mine.yaml")


class TestKinds(unittest.TestCase):
    """The one table of fact kinds (devicedata.KINDS)."""

    def test_every_key_of_the_format_has_kinds(self):
        heads = {k.path[0] for k in devicedata.KINDS}
        # lcec: still read (and ignored) in older files; ecatsetup/lcec holds it now
        self.assertEqual(quirks.KEYS - {"name", "match", "format", "sources", "lcec"}, heads)

    def test_every_shipped_fact_has_a_kind_a_section_and_a_known_card(self):
        # devicedataview.CARDS (not imported here: no Qt in this module's tests)
        cards = {"settings", "process-data", "homing", "sdo", "switches", "move", "response", "sync",
                 "operation", "source", "workbench", "diagnose"}
        for path in glob.glob(os.path.join(quirks.SHIPPED, "*.yaml")):
            with open(path) as f:
                raw = yaml.safe_load(f)
            for p in devicedata.facts(raw):
                k = devicedata.kind(p)
                self.assertIsNotNone(k, (path, p))
                self.assertIn(k.section, devicedata.SECTIONS)
                self.assertTrue(k.card is None or k.card in cards, k)
                self.assertNotIn("{", devicedata.label(p))
        self.assertEqual(devicedata.label(("errors", "0x8611")), "Error code 0x8611: what it means")

    def test_every_kind_has_a_sort_and_only_machine_choices_stay_home(self):
        for k in devicedata.KINDS:
            self.assertIn(k.sort, devicedata.SORTS, k)
            self.assertEqual(k.share, k.sort != devicedata.MACHINE, k)
        self.assertEqual(devicedata.sort(("commands", "0x2010")), devicedata.PROPERTY)
        self.assertEqual(devicedata.sort(("settings", "0x2009:00")), devicedata.EXPERIENCE)
        self.assertTrue(devicedata.kind(("homing", "methods")).listing)

    def test_a_property_that_differs_is_a_conflict_experience_is_context(self):
        self.assertEqual(devicedata.differs(("commands", "0x2010"), "a", "a"), "same")
        self.assertEqual(devicedata.differs(("commands", "0x2010"), None, "a"), "new")
        self.assertEqual(devicedata.differs(("homing", "methods"), [21], [21, 35]), "conflict")
        self.assertEqual(devicedata.differs(("settings", "0x2009:00"), {"value": 2000}, {"value": 6400}),
                         "context")
        self.assertIn("one is wrong", devicedata.differs_text(("homing", "methods"), [21], [35]))

    def test_a_source_says_its_list_relation_and_context(self):
        src = {"kind": "manual", "ref": "ECT manual 2.4.6", "list": "closed", "relation": "completes"}
        text = devicedata.source_text(src)
        self.assertIn("only these", text)
        self.assertIn("the drive and its ESI file say nothing", text)
        self.assertIn("on plasma cutter, CSP", devicedata.source_text(
            {"kind": "tried", "context": "plasma cutter, CSP"}))
        self.assertIn("seen by the program", devicedata.source_text({"kind": "observed"}))
        raw = {"format": 1, "match": {"vendor": "0x1", "product": "0x2"},
               "homing": {"methods": [21]}, "sources": {"homing methods": dict(src)}}
        self.assertEqual(devicedata.check(raw), [])
        raw["sources"]["homing methods"]["list"] = "half"
        raw["sources"]["homing methods"]["colour"] = "red"
        problems = devicedata.check(raw)
        self.assertTrue(any("list is closed or open" in p for p in problems), problems)
        self.assertTrue(any("unknown keys colour" in p for p in problems), problems)

    def test_what_is_not_known_yet(self):
        drive = {"profile": "cia402", "dc": {"assign-activate": "0x300"}}
        found = devicedata.unknowns(drive, {}, [])
        self.assertEqual([c for _, _, c in found], ["sdo", "response", "operation", "operation", "sync"])
        self.assertEqual(devicedata.unknowns(drive, shipped("rtelligent-ect60.yaml"), [{"period-us": 500}]), [])
        self.assertEqual(devicedata.unknowns({"profile": "generic"}, {}, []), [])


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
                    "createdAt": "2026-11-26T18:00:00Z", "body": body.replace(devicedata.WRITE_HERE, "My mill's X."),
                    "comments": {"nodes": [{"author": {"login": "bob"}, "body": rep, "updatedAt": "t"},
                                           {"author": {"login": "eve"}, "body": "nice"}]}}]
        idx = self.index.build(threads, self.root)
        self.assertEqual(len(idx["files"]), 1)          # 8's thread is gone: its file removed
        e = idx["files"][0]
        self.assertEqual((e["path"], e["upvotes"], e["author"]), (rel, 3, "alice"))
        self.assertEqual((e["created"], e["words"]), ("2026-11-26T18:00:00Z", "My mill's X."))
        line = devicedata.describe(dict(e, raw=devicedata.read_raw(os.path.join(self.root, rel))))
        self.assertTrue(line.startswith("#7 · Rtelligent ECT60 · 0x2010 is a command, not a setting, "), line)
        self.assertIn("more · alice, 2026-11-26 · 3 up, 1 worked", line)
        self.assertEqual(devicedata.post_words(body), "")
        self.assertEqual([(r["author"], r["result"], r["words"]) for r in e["reports"]],
                         [("bob", "worked", "ok here")])
        self.assertFalse(os.path.exists(os.path.join(self.root, "devices/00000a88-0a880002/8.yaml")))
        # an empty answer (a failed query) removes nothing
        self.index.build([], self.root)
        self.assertTrue(os.path.exists(os.path.join(self.root, rel)))


if __name__ == "__main__":
    unittest.main()
