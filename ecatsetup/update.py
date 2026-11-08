"""Update an ecat configuration from the bus, keeping the user's choices.

Everything in a slave's entry in the file is a choice or was accepted as
one: its name, pins, bit splits, scaling, settings, mapping, profile mode
and DC. So a slave that is still on the bus keeps its entry as it is. The
bus only changes which slaves there are:

- a slave at the same position with the same identity is kept;
- a slave of the same kind at another position, because slaves were put
  in or taken out before it, is taken to have moved and keeps its entry
  with the new position (slaves report no serial numbers, so this goes by
  kind and order in the chain);
- a slave not in the file gets the proposal made from its own data;
- a slave in the file but not on the bus is left out.

Every change is reported as a note.
"""

import copy


def _kind(s):
    ident = s.get("identity") or {}
    return (int(str(ident.get("vendor")), 16), int(str(ident.get("product")), 16))


def _describe(s):
    return "%s (%s)" % (s.get("name"), s.get("device") or "unknown device")


def _align(a, b):
    """Pairs (i, j) of slaves of the same kind, in order in both chains:
    the most pairs, and of those the most at the same position."""
    n, m = len(a), len(b)
    # best[i][j]: (pairs, same position) for a[i:], b[j:]
    best = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            score = max(best[i + 1][j], best[i][j + 1])
            if _kind(a[i]) == _kind(b[j]):
                p, q = best[i + 1][j + 1]
                score = max(score, (p + 1, q + (a[i]["position"] == b[j]["position"])))
            best[i][j] = score
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if _kind(a[i]) == _kind(b[j]):
            p, q = best[i + 1][j + 1]
            if best[i][j] == (p + 1, q + (a[i]["position"] == b[j]["position"])):
                pairs.append((i, j))
                i, j = i + 1, j + 1
                continue
        if best[i][j] == best[i + 1][j]:
            i += 1
        else:
            j += 1
    return pairs


def merge(old, fresh):
    """old: the configuration in the file; fresh: one proposed from the bus
    (config.from_proposals). Returns (configuration, notes)."""
    notes = []
    cfg = {"ecat": fresh["ecat"], "master": copy.deepcopy(old.get("master") or {})}

    if "dc" in fresh["master"] and "dc" not in cfg["master"]:
        cfg["master"]["dc"] = copy.deepcopy(fresh["master"]["dc"])
        notes.append("master: added DC settings, since slaves use distributed clocks")

    old_slaves = sorted(old.get("slaves") or [], key=lambda s: s["position"])
    bus = sorted(fresh["slaves"], key=lambda s: s["position"])
    # The slaves form a chain: a slave put in or taken out shifts the ones
    # after it. Match the two chains by kind, keeping their order, as many
    # slaves as possible, and of those the matching that moves the fewest.
    matched = {}  # bus position -> old slave
    for i, j in _align(old_slaves, bus):
        s, b = old_slaves[i], bus[j]
        matched[b["position"]] = s
        if s["position"] != b["position"]:
            notes.append("slave %s: moved from position %d to %d"
                         % (_describe(s), s["position"], b["position"]))
    kept = {id(s) for s in matched.values()}
    gone = [s for s in old_slaves if id(s) not in kept]

    names = {s["name"] for s in matched.values()}
    slaves = []
    for b in bus:
        s = matched.get(b["position"])
        if s is None:
            new = copy.deepcopy(b)
            base, n = new["name"], 2
            while new["name"] in names:
                new["name"] = "%s-%d" % (base, n)
                n += 1
            names.add(new["name"])
            slaves.append(new)
            notes.append("slave %d: new, %s, proposed from its own data" % (b["position"], _describe(new)))
            continue
        s = copy.deepcopy(s)
        s["position"] = b["position"]
        old_rev = s["identity"].get("revision")
        new_rev = b["identity"].get("revision")
        if new_rev is not None and old_rev is not None and int(old_rev, 16) != int(new_rev, 16):
            s["identity"]["revision"] = new_rev
            notes.append("slave %d %s: revision %s, the file had %s; check its process data"
                         % (b["position"], s["name"], new_rev, old_rev))
        if s.get("device") != b.get("device") and b.get("device"):
            s["device"] = b["device"]
        slaves.append(s)

    for s in gone:
        notes.append("slave %s at position %d: not on the bus, left out" % (_describe(s), s["position"]))

    cfg["slaves"] = slaves
    return cfg, notes
