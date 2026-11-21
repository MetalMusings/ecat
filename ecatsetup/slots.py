"""A modular device's modules laid out in its slots (ETG.2000 13.4.3, Slots and Modules).

The slave reports the modules it has (0xF050, detected module list); its ESI file's
Slots say which module may sit where and how indexes move from slot to slot, its
Modules what each module maps. An index marked DependOnSlot moves by the slot's
number times the slot increment (SlotPdoIncrement for a PDO, SlotIndexIncrement for
an entry, an InitCmd or an object); DependOnSlotGroup by the slot group's number
times the group increment. A slot's own increments win over the device's. Slots are
numbered from 0 here (ETG.2000 does not say; with 0 the first slot keeps the indexes
the file gives, as Beckhoff's AX8000 descriptions expect).
"""

import dataclasses
from dataclasses import dataclass, field


@dataclass
class Layout:
    placed: list = field(default_factory=list)    # (slot number, EsiSlot, ident, EsiModule or None)
    pdos: list = field(default_factory=list)      # EsiPdo with moved indexes and their sync managers
    init_cmds: list = field(default_factory=list)  # (transition, index, subindex, data, complete), moved
    settings: list = field(default_factory=list)  # 0xF030 (configured modules) when the device wants it
    notes: list = field(default_factory=list)
    problems: list = field(default_factory=list)


def assign(slots, idents):
    """Which slot each detected module sits in: [(number, EsiSlot, ident)], walking the
    slot types in order (each takes from MinInstances to MaxInstances modules it allows),
    and the problems found."""
    out, problems = [], []
    t, count = 0, 0
    for n, ident in enumerate(idents):
        while t < len(slots.slots):
            sl = slots.slots[t]
            if ident in sl.idents and count < max(sl.max, 1):
                break
            if count < sl.min:
                problems.append("slot %d (%s) needs a module, but 0x%08X is not one it takes"
                                % (n, sl.name, ident))
            t, count = t + 1, 0
        if t == len(slots.slots):
            problems.append("module 0x%08X at %d fits no slot of the ESI file" % (ident, n))
            continue
        out.append((n, slots.slots[t], ident))
        count += 1
    return out, problems


def _inc(slot, slots, name):
    own = getattr(slot, name)
    return own if own is not None else getattr(slots, name)


def layout(slots, idents, sm_rx=2, sm_tx=3):
    """The PDOs and InitCmds of the modules a slave reports, laid out in its slots."""
    out = Layout()
    placed, out.problems = assign(slots, idents)
    names = {}
    for n, slot, ident in placed:
        mod = slots.modules.get(ident)
        out.placed.append((n, slot, ident, mod))
        if mod is None:
            if ident:
                out.problems.append("module 0x%08X (slot %d, %s) is not in the ESI file's Modules"
                                    % (ident, n, slot.name))
            continue
        names[mod.name] = names.get(mod.name, 0) + 1
    seen = {}
    for n, slot, ident, mod in out.placed:
        if mod is None:
            continue
        pdo_inc, idx_inc = _inc(slot, slots, "pdo_increment"), _inc(slot, slots, "index_increment")
        gpdo_inc, gidx_inc = _inc(slot, slots, "group_pdo_increment"), _inc(slot, slots, "group_index_increment")

        def move(index, how, inc, ginc):
            return index + (n * inc if how[0] else 0) + (slot.group * ginc if how[1] else 0)
        for k, pdo in enumerate(mod.pdos):
            how = mod.pdo_moves.get(k, (False, False))
            entries = [dataclasses.replace(e, index=move(e.index, mod.entry_moves.get((k, j), (False, False)),
                                                         idx_inc, gidx_inc) if e.index else 0)
                       for j, e in enumerate(pdo.entries)]
            name = pdo.name if names.get(mod.name, 0) == 1 else "%s %d" % (pdo.name, n)
            moved = dataclasses.replace(pdo, index=move(pdo.index, how, pdo_inc, gpdo_inc), entries=entries,
                                        sm=pdo.sm if pdo.sm is not None else (sm_rx if pdo.direction == "rx" else sm_tx),
                                        name=name)
            if moved.index in seen:
                out.problems.append("PDO 0x%04X of slot %d is also slot %d's: the slot increments do not "
                                    "separate them" % (moved.index, n, seen[moved.index]))
            seen[moved.index] = n
            if moved.entries:            # an empty PDO maps nothing
                out.pdos.append(moved)
        for transitions, index, sub, data, complete, how in mod.init_cmds:
            out.init_cmds.append((transitions, move(index, how, idx_inc, gidx_inc), sub, data, complete))
        out.notes.append("slot %d (%s): module 0x%08X %s" % (n, slot.name, ident, mod.name))
    if slots.download_ident_list and idents:
        # the configured module list, as the ESI file asks (written in PREOP: count 0, the
        # idents, then the count)
        out.settings = ([{"object": "0xF030:00", "type": "USINT", "value": 0}] +
                        [{"object": "0xF030:%02X" % (k + 1), "type": "UDINT", "value": ident}
                         for k, ident in enumerate(idents)] +
                        [{"object": "0xF030:00", "type": "USINT", "value": len(idents)}])
        out.notes.append("the configured module list 0xF030 is written at start, as the ESI file asks")
    return out
