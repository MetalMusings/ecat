"""The Help tab's documents: split at headings, links, search, and every place the
window points to exists (a renamed heading turns this red)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import helpdocs  # noqa: E402

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")


class TestSplit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        with open(os.path.join(self.dir.name, "a.md"), "w") as f:
            f.write("# Title\n\nIntro.\n\n## One\n\nText one, see [two](#two-b).\n\n```\n# not a heading\n```\n"
                    "### Sub\n\nDeep words.\n\n## Two: b\n\nText two, [other](b.md#x) and [web](https://x.org).\n"
                    "## One\n\nAgain.\n")
        with open(os.path.join(self.dir.name, "b.md"), "w") as f:
            f.write("# B\n\n## X\n\nx.\n")
        self.docs = helpdocs.Docs(self.dir.name, ("a.md", "b.md"))

    def test_headings_and_anchors(self):
        self.assertEqual([(s.level, s.anchor) for s in self.docs.sections],
                         [(1, "title"), (2, "one"), (3, "sub"), (2, "two-b"), (2, "one-1"), (1, "b"), (2, "x")])
        one = self.docs.find("a.md", "one")
        self.assertEqual(self.docs.sections[one].children, [self.docs.find("a.md", "sub")])
        self.assertEqual(self.docs.path(self.docs.find("a.md", "sub")), ["Title", "One", "Sub"])

    def test_text_with_and_without_subsections(self):
        one = self.docs.find("a.md", "one")
        self.assertIn("Deep words", self.docs.text(one))
        self.assertNotIn("Deep words", self.docs.text(one, whole=False))
        self.assertNotIn("Text two", self.docs.text(one))
        self.assertIn("# not a heading", self.docs.text(one))     # in a code block: text
        self.assertNotIn("Text one", self.docs.text(self.docs.find("a.md")))   # a file's title: its own text

    def test_links(self):
        self.assertEqual(self.docs.link("#two-b", "a.md"), self.docs.find("a.md", "two-b"))
        self.assertEqual(self.docs.link("b.md#x", "a.md"), self.docs.find("b.md", "x"))
        self.assertEqual(self.docs.link("b.md", "a.md"), self.docs.find("b.md"))
        self.assertIsNone(self.docs.link("https://x.org", "a.md"))
        self.assertIsNone(self.docs.link("development.md#y", "a.md"))   # not one of the documents

    def test_search(self):
        self.assertEqual(self.docs.search("deep WORDS"), [self.docs.find("a.md", "sub")])
        self.assertEqual(self.docs.search("again one")[0], self.docs.find("a.md", "one-1"))   # title first
        self.assertEqual(self.docs.search("   "), [])

    def test_no_folder(self):
        docs = helpdocs.Docs(os.path.join(self.dir.name, "none"))
        self.assertEqual(docs.sections, [])


class TestTheDocuments(unittest.TestCase):
    def setUp(self):
        self.docs = helpdocs.Docs(DOCS)

    def test_every_topic_exists(self):
        for key, (f, anchor) in helpdocs.TOPICS.items():
            self.assertIsNotNone(self.docs.find(f, anchor), "%s -> %s#%s" % (key, f, anchor))

    def test_every_link_between_the_documents_works(self):
        import re
        for i, s in enumerate(self.docs.sections):
            for href in re.findall(r"\]\(([^)\s]+)\)", self.docs.text(i, whole=False)):
                target = href.partition("#")[0]
                if re.match(r"^[a-z]+:", href) or (target and os.path.basename(target) not in helpdocs.FILES):
                    continue
                if target.endswith((".png", ".jpg")):
                    continue
                self.assertIsNotNone(self.docs.link(href, s.file), "%s: %s" % (s.file, href))

    def test_found_where_the_program_runs_from(self):
        self.assertTrue(os.path.exists(os.path.join(helpdocs.docs_dir(), "ecatsetup.md")))


if __name__ == "__main__":
    unittest.main()
