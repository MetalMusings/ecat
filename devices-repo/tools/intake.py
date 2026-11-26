#!/usr/bin/env python3
"""Take in a quirk file posted as a GitHub Discussion (the data repository's
Action; PROTOTYPE). The post's ```yaml block is checked with EtherCAT Setup's
own check (checker/ecatsetup/devicedata.py, the same as the window's); a file
that passes is kept as devices/<vendor>-<product>/<discussion>.yaml, and the
thread is told either way. Nobody reviews it beforehand: the users who choose
its facts do, one by one, in the window.

    intake.py EVENT_JSON [--dry]

The post is untrusted: it is read from the event file, parsed with
yaml.safe_load and never put into a shell command.
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "checker"))

import yaml  # noqa: E402

from ecatsetup import devicedata  # noqa: E402

COMMENT = ("mutation($id: ID!, $body: String!) {"
           " addDiscussionComment(input: {discussionId: $id, body: $body}) { comment { id } } }")


def say(node_id, body, dry):
    if dry:
        print("COMMENT:", body)
        return
    subprocess.run(["gh", "api", "graphql", "-f", "query=" + COMMENT, "-f", "id=" + node_id,
                    "-f", "body=" + body], check=True, stdout=subprocess.DEVNULL)


def files_of(number, root):
    """The files kept for a Discussion (its device may have changed in an edit)."""
    out = []
    devices = os.path.join(root, "devices")
    for d in sorted(os.listdir(devices)) if os.path.isdir(devices) else []:
        p = os.path.join(devices, d, "%d.yaml" % number)
        if os.path.isfile(p):
            out.append(p)
    return out


def take(event, root=ROOT, dry=False):
    """Returns what happened: "taken", "same", "refused" or "skipped"."""
    d = event.get("discussion") or {}
    cat = ((d.get("category") or {}).get("slug") or "").lower()
    if cat != devicedata.CATEGORY:
        return "skipped"
    number, who = int(d["number"]), (d.get("user") or {}).get("login", "?")
    try:
        raw = devicedata.from_post(d.get("body") or "")
        problems = devicedata.check(raw)
    except devicedata.Error as e:
        raw, problems = None, [str(e)]
    if problems:
        kept = files_of(number, root)
        say(d["node_id"], "Not taken in (checked automatically):\n\n%s\n\n%s" % (
            "\n".join("- %s" % p for p in problems),
            "Edit the post to put it right; it is checked again. The file taken in before stays."
            if kept else "Edit the post to put it right; it is checked again."), dry)
        return "refused"
    rel = devicedata.repo_path(raw, number)
    path = os.path.join(root, rel)
    text = ("# From Discussion #%d by %s (%s). %s\n" % (number, who, d.get("html_url", ""),
                                                        devicedata.LICENCE)
            + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    old = None
    if os.path.exists(path):
        with open(path) as f:
            old = f.read()
    for other in files_of(number, root):
        if os.path.abspath(other) != os.path.abspath(path):
            os.remove(other)
    if old == text:
        return "same"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    n = len(devicedata.facts(raw))
    say(d["node_id"], "Taken in as `%s` (%d fact%s), checked automatically. Others find it with "
        "*Fetch shared data* in EtherCAT Setup and choose its facts one by one. Tell how it went "
        "with *Report* in the window (or as a comment)." % (rel, n, "" if n == 1 else "s"), dry)
    return "taken"


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    with open(argv[0]) as f:
        event = json.load(f)
    print(take(event, dry="--dry" in argv))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
