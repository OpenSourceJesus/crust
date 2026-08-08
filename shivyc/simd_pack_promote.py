"""Loop register-promotion for ``_Nbit`` packed globals (-fsimd-pack-globals).

A packed global is normally read by decompressing it out of ``xmm15`` (a
``movq``/shift/mask) and written by recompressing into ``xmm15`` plus its memory
mirror -- on *every* access. In a loop that hammers a few packed globals, that
per-access cost repeats every iteration.

This pass hoists that cost out of the loop. For a loop that reads/writes a small
set of packed globals and is safe to transform, it decompresses each global into
a fresh local *once before* the loop, rewrites the loop body to use that local
(an ordinary value the register allocator keeps in a GP register), and
recompresses back *once after* the loop. The decompress/recompress are ordinary
``Set`` commands, so they reuse the existing packed read/write lowering; only
their *count* drops from once-per-iteration to once-per-loop.

Safety. The transform is applied to a natural loop only when, within it: there
is no function call (a callee could observe the global through its memory
mirror, which is stale while the live value sits in a register) and no
``return``; every branch stays inside the loop or exits to the loop's single
merge label (so the recompress placed there runs on every exit, including
``break``); no outside branch jumps into the loop interior (so the decompress
always dominates the uses); and the global's address is never taken anywhere in
the function (no aliasing). A global whose occurrences cannot all be rewritten
is left untouched.
"""

import re

import shivyc.il_cmds.control as control
import shivyc.il_cmds.value as value
from shivyc.il_gen import ILValue

_NAME_RE = re.compile(r"_(\d+)bit$")


def _packed_globals(symbol_table):
    """Map ILValue -> name for static globals whose name qualifies for packing."""
    out = {}
    for v, storage in symbol_table.storage.items():
        if storage != symbol_table.STATIC:
            continue
        name = symbol_table.names.get(v)
        if not name:
            continue
        m = _NAME_RE.search(name)
        if m and 1 <= int(m.group(1)) <= 8:
            out[v] = name
    return out


def _targets(cmd):
    return cmd.targets() if hasattr(cmd, "targets") else []


def _replace_operand(cmd, old, new):
    """Replace ILValue ``old`` with ``new`` in every operand field of ``cmd``."""
    for attr, val in list(vars(cmd).items()):
        if val is old:
            setattr(cmd, attr, new)
        elif isinstance(val, list) and any(x is old for x in val):
            setattr(cmd, attr, [new if x is old else x for x in val])


def optimize(il_code, symbol_table):
    """Apply loop register-promotion of packed globals, in place."""
    globs = _packed_globals(symbol_table)
    if not globs:
        return
    for fn in il_code.commands:
        il_code.commands[fn] = _promote_in_function(
            il_code.commands[fn], globs)


def _natural_loops(cmds, label_idx):
    """Outermost natural loops as (start_idx, back_jump_idx, end_label)."""
    regions = []
    for i, c in enumerate(cmds):
        # A while/for back edge is an unconditional Jump to an earlier label,
        # immediately followed by the loop's exit (merge) label.
        if isinstance(c, control.Jump) and c.label in label_idx:
            s = label_idx[c.label]
            if s < i and i + 1 < len(cmds) and isinstance(
                    cmds[i + 1], control.Label):
                regions.append((s, i, cmds[i + 1].label))

    def contains(a, b):
        return a[0] <= b[0] and b[1] <= a[1] and a != b

    outer = [r for r in regions if not any(contains(o, r) for o in regions)]
    # Drop any pair that partially overlaps without nesting (irreducible CFG).
    safe = []
    for r in outer:
        if all(r is o or r[1] < o[0] or o[1] < r[0]   # disjoint
               or contains(r, o) or contains(o, r)    # nested
               for o in outer):
            safe.append(r)
    return safe


def _region_is_safe(cmds, start, end, end_label, label_idx):
    """Whether the loop body cmds[start..end] admits promotion.

    Parameter names avoid a single-letter `s`: py2c's type inference has
    treated that name as `char*` and broken `make bootstrap`.
    """
    for k in range(start, end + 1):
        c = cmds[k]
        if isinstance(c, (control.Call, control.Return)):
            return False
        for t in _targets(c):
            ti = label_idx.get(t)
            # Every internal branch must stay inside the loop or exit to the
            # loop's single merge label.
            if t != end_label and not (ti is not None and start <= ti <= end):
                return False
    # No branch from outside may jump into the loop interior (past the header),
    # which would bypass the decompress.
    for k, c in enumerate(cmds):
        if start <= k <= end:
            continue
        for t in _targets(c):
            ti = label_idx.get(t)
            if ti is not None and start < ti <= end:
                return False
    return True


def _promote_in_function(cmds, globs):
    # Globals whose address is taken anywhere in this function are unsafe to
    # cache in a register (a pointer could read/write the memory directly).
    addr_taken = {c.var for c in cmds
                  if isinstance(c, value.AddrOf) and c.var in globs}
    candidates = {g for g in globs if g not in addr_taken}
    if not candidates:
        return cmds

    label_idx = {c.label: i for i, c in enumerate(cmds)
                 if isinstance(c, control.Label)}
    regions = _natural_loops(cmds, label_idx)
    if not regions:
        return cmds

    pre_insert = {}   # index -> commands to insert before it
    post_insert = {}  # index -> commands to insert after it

    for (start, end, end_label) in regions:
        if not _region_is_safe(cmds, start, end, end_label, label_idx):
            continue
        body = cmds[start:end + 1]
        # Packed globals actually accessed in this loop.
        used = []
        seen = set()
        for c in body:
            for v in c.inputs() + c.outputs():
                if v in candidates and v not in seen:
                    seen.add(v)
                    used.append(v)
        if not used:
            continue

        promoted = []
        for g in used:
            temp = ILValue(g.ctype)
            # Substitute g -> temp inside the loop, with rollback if any
            # occurrence sits in a field we cannot rewrite (so we never leave a
            # half-rewritten global behind).
            snaps = [(c, dict(vars(c))) for c in body]
            for c in body:
                _replace_operand(c, g, temp)
            if any(g in (c.inputs() + c.outputs()) for c in body):
                for c, snap in snaps:
                    c.__dict__.clear()
                    c.__dict__.update(snap)
                continue
            promoted.append((g, temp))

        if not promoted:
            continue
        end_idx = label_idx[end_label]
        pre_insert.setdefault(start, []).extend(
            value.Set(temp, g) for g, temp in promoted)      # temp = g
        post_insert.setdefault(end_idx, []).extend(
            value.Set(g, temp) for g, temp in promoted)      # g = temp

    if not pre_insert and not post_insert:
        return cmds

    out = []
    for i, c in enumerate(cmds):
        out.extend(pre_insert.get(i, ()))
        out.append(c)
        out.extend(post_insert.get(i, ()))
    return out
