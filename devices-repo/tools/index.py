#!/usr/bin/env python3
"""Build index.json from the threads as they are (the data repository's
Action; PROTOTYPE): for each kept file its Discussion, author, up-votes and
the reports in its comments (```ecat-report blocks, which their authors may
edit later). A file whose Discussion is gone (deleted by the owner on a
report) is removed. The window reads index.json and the files only: no
GitHub API, no token.

    index.py OWNER/REPO            (gh api graphql, GH_TOKEN)
    index.py --from FILE.json      (the discussions as a list, for tests)
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "checker"))

from ecatsetup import devicedata  # noqa: E402

QUERY = """query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    discussions(first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { number url upvoteCount createdAt body author { login }
        comments(first: 100) { nodes { author { login } body updatedAt } } } } } }"""


def discussions(repo):
    owner, name = repo.split("/", 1)
    out, after = [], None
    while True:
        cmd = ["gh", "api", "graphql", "-f", "query=" + QUERY, "-f", "owner=" + owner,
               "-f", "name=" + name]
        if after:
            cmd += ["-f", "after=" + after]
        page = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
        d = page["data"]["repository"]["discussions"]
        out += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            return out
        after = d["pageInfo"]["endCursor"]


def build(threads, root=ROOT):
    """Write index.json; remove files whose Discussion is gone. Returns the index."""
    by_number = {int(t["number"]): t for t in threads}
    files = []
    devices = os.path.join(root, "devices")
    for d in sorted(os.listdir(devices)) if os.path.isdir(devices) else []:
        if not os.path.isdir(os.path.join(devices, d)):
            continue
        for f in sorted(os.listdir(os.path.join(devices, d))):
            if not f.endswith(".yaml"):
                continue
            rel = "devices/%s/%s" % (d, f)
            number = int(f[:-5])
            t = by_number.get(number)
            if t is None:
                # gone: deleted on a report. An empty answer removes nothing (a failed query
                # must not empty the repository)
                if by_number:
                    os.remove(os.path.join(root, rel))
                continue
            raw = devicedata.read_raw(os.path.join(root, rel)) or {}
            reports = []
            for c in (t.get("comments") or {}).get("nodes") or []:
                r = devicedata.read_report(c.get("body") or "")
                if r and r.get("file") in (None, rel):
                    r["author"] = (c.get("author") or {}).get("login")
                    r["updated"] = c.get("updatedAt")
                    reports.append(r)
            vendor, product = d.split("-")
            files.append({"path": rel, "vendor": "0x" + vendor.upper(), "product": "0x" + product.upper(),
                          "name": raw.get("name"), "discussion": number, "url": t.get("url"),
                          "author": (t.get("author") or {}).get("login"),
                          "created": t.get("createdAt"), "words": devicedata.post_words(t.get("body") or ""),
                          "upvotes": t.get("upvoteCount", 0), "facts": len(devicedata.facts(raw)),
                          "reports": reports})
    index = {"format": devicedata.FORMAT, "files": files}
    with open(os.path.join(root, "index.json"), "w") as f:
        json.dump(index, f, indent=1, sort_keys=True)
        f.write("\n")
    return index


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    if argv[0] == "--from":
        with open(argv[1]) as f:
            threads = json.load(f)
    else:
        threads = discussions(argv[0])
    idx = build(threads)
    print("%d files" % len(idx["files"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
