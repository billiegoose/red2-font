"""Deterministic kerning matrices, class compression, and history proofs."""

from dataclasses import dataclass
from types import SimpleNamespace

STRATEGIES = ("balanced", "area", "pixels", "left-first", "right-first")


def dependent_triplets(advances):
    """Yield (A, B, C, extra pixels) when A places C beyond the B/C pair.

    With A at zero, adjacent layout puts C at d(A,B) + d(B,C).
    A matters exactly when d(A,C) exceeds that sum. If no triple violates
    this inequality, induction proves adjacent pairs suffice for any string.
    """
    for a, row_a in advances.items():
        for b, ab in row_a.items():
            for c, bc in advances[b].items():
                extra = row_a[c] - ab - bc
                if extra > 0:
                    yield a, b, c, extra


def widen_pairs(advances, nominal):
    """Repair violations by increasing B/C, never tightening another pair.

    This deterministic, greedy construction is a feasible table, not a claim
    of a globally minimum widening. Recheck after each sweep because a changed
    direct A/C constraint can expose another violation. Stop only at a full
    triangle-inequality proof, so the result also works for longer strings.

    Every sweep visits A, B, and C in ascending glyph-key order, updating in
    place. This order is part of the policy: dictionary insertion order must
    never affect which feasible widening is chosen.

    The upper-bound condition checked below ensures repairs cannot exceed
    nominal advances. Integer entries then imply finite termination. The
    original RED2 table satisfies this through its right-box-edge constraint.
    """
    codes = sorted(advances)
    table = {a: {b: advances[a][b] for b in codes} for a in codes}
    for a, row in table.items():
        for b, step in row.items():
            if not 0 <= step <= nominal[a] or step + nominal[b] < nominal[a]:
                raise ValueError("Pair table does not satisfy nominal bounds")
    while True:
        changed = False
        for a in codes:
            row_a = table[a]
            for b in codes:
                ab = row_a[b]
                row_b = table[b]
                for c in codes:
                    ac = row_a[c]
                    required = ac - ab
                    if row_b[c] < required:
                        if required > nominal[b]:
                            raise ValueError("Repair would exceed nominal advance")
                        row_b[c] = required
                        changed = True
        if not changed:
            return table


class TableKerning:
    """Expose a proposed table to the existing renderer and state compiler."""

    def __init__(self, original, advances):
        self.glyphs = original.glyphs
        self.spacing = original.spacing
        self.fixed_advances = original.fixed_advances
        self.nominal_advance = original.nominal_advance
        self.advances = advances

    def advance(self, left, right):
        return self.advances[left][right]


def verify_two_glyph_history(kerning):
    """Verify the FPGA's two-history recurrence reproduces full layout.

    For any four characters A B C D, the earliest A must never constrain D
    more strongly than the last two glyphs B and C already do. This local
    property proves the recurrence works for arbitrarily long strings.
    Return the number of ordered quadruples checked, or raise ValueError with
    an actual counterexample. It is deliberately checked for every export:
    different artwork and spacing values can invalidate the property.
    """
    codes = sorted(kerning.glyphs)
    advances = {left: {right: kerning.advance(left, right) for right in codes}
                for left in codes}
    for a in codes:
        row_a = advances[a]
        for b in codes:
            row_b = advances[b]
            xb = row_a[b]
            for c in codes:
                row_c = advances[c]
                xc = max(row_a[c], xb + row_b[c])
                for d in codes:
                    if row_a[d] > max(xb + row_b[d], xc + row_c[d]):
                        example = ''.join(chr(code) for code in (a, b, c, d))
                        raise ValueError(
                            f"Two-glyph history is insufficient for {example!r} "
                            f"at spacing {kerning.spacing}; exact FPGA/Python "
                            "layout equivalence cannot be guaranteed."
                        )
    return len(codes) ** 4


def exact_classes(matrix):
    codes = sorted(matrix)
    left, right = {}, {}
    for a in codes:
        left.setdefault(tuple(matrix[a][b] for b in codes), []).append(a)
        right.setdefault(tuple(matrix[b][a] for b in codes), []).append(a)
    return list(left.values()), list(right.values())


@dataclass
class ClassTable:
    left: list
    right: list
    values: list

    def expand(self):
        return {a: {b: self.values[i][j] for j, group in enumerate(self.right) for b in group}
                for i, group in enumerate(self.left) for a in group}


def compress_classes(matrix, tolerance=1, strategy="balanced", protected=()):
    """Merge classes while every original adjustment loses at most tolerance.

    A class block stores its minimum original adjustment. Its maximum minus
    minimum must be <= tolerance. Preserve extrema from the ORIGINAL table:
    otherwise repeated merges could silently accumulate more than N pixels.
    Protected pairs must retain their original adjustment exactly.

    If present in the baseline, whole-row/column minima preserve the directed
    triangle invariant. For a
    column merge, D'(A,C) = max_c D(A,c) <= D(A,B) + max_c D(B,c).
    A row merge obeys the same inequality after accounting for nominal widths:
    D'(A,C) = max_a [D(a,C) + nominal(A) - nominal(a)]. Both transformations
    only increase advances, and can therefore be composed safely.

    Greedy merge priorities are heuristics, not a global optimum. Sorted glyph
    keys and explicit tie breaks make every strategy independent of insertion
    order. Merges are selected by score, not hash iteration or randomness.
    """
    if tolerance < 0:
        raise ValueError("Tolerance must be nonnegative")
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy!r}")
    protected = set(protected)
    left, right = exact_classes(matrix)
    low = [[matrix[a[0]][b[0]] for b in right] for a in left]
    high = [row[:] for row in low]
    locked = [[max((matrix[a][b] for a in l for b in r if (a, b) in protected), default=-1)
               for r in right] for l in left]
    while True:
        best = None
        for axis, groups in enumerate((left, right)):
            opposite = right if axis == 0 else left
            if strategy == "left-first":
                rank = axis
            elif strategy == "right-first":
                rank = 1 - axis
            elif strategy == "balanced":
                rank = int(len(groups) < max(len(left), len(right)))
            else:
                rank = 0
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    cost = 0
                    for k, other in enumerate(opposite):
                        lo1, lo2 = (low[i][k], low[j][k]) if axis == 0 else (low[k][i], low[k][j])
                        hi1, hi2 = (high[i][k], high[j][k]) if axis == 0 else (high[k][i], high[k][j])
                        lock1, lock2 = (locked[i][k], locked[j][k]) if axis == 0 else (locked[k][i], locked[k][j])
                        minimum = min(lo1, lo2)
                        if max(hi1, hi2) - minimum > tolerance or max(lock1, lock2) > minimum:
                            break
                        cost += len(other) * (len(groups[i]) * (lo1 - minimum)
                                              + len(groups[j]) * (lo2 - minimum))
                    else:
                        loss = cost / len(opposite) if strategy == "area" else cost
                        score = (rank, loss, -len(groups[i]) - len(groups[j]),
                                 axis, groups[i][0], groups[j][0])
                        if best is None or score < best[0]:
                            best = score, axis, i, j
        if best is None:
            return ClassTable(left, right, low)
        _, axis, i, j = best
        groups = left if axis == 0 else right
        groups[i] = sorted(groups[i] + groups[j])
        del groups[j]
        for values, operation in ((low, min), (high, max), (locked, max)):
            if axis == 0:
                values[i] = [operation(a, b) for a, b in zip(values[i], values[j])]
                del values[j]
            else:
                for row in values:
                    row[i] = operation(row[i], row[j])
                    del row[j]


def incompatibility_clique(matrix, tolerance, axis):
    """Return a deterministic, verified lower-bound certificate, not an optimum.

    Profiles differing by > N at any coordinate have disjoint allowed values
    there and cannot share a class. Every pair in this clique conflicts, so
    any class representation with this per-pair error bound needs at least
    len(clique) classes on the selected axis, regardless of merge strategy.
    """
    codes = sorted(matrix)
    groups = exact_classes(matrix)[axis]
    profiles = [[matrix[g[0]][c] if axis == 0 else matrix[c][g[0]] for c in codes]
                for g in groups]
    edges = {i: {j for j in range(len(groups))
                 if any(abs(a - b) > tolerance for a, b in zip(profiles[i], profiles[j]))}
             for i in range(len(groups))}
    best = []
    for start in range(len(groups)):
        clique, candidates = [start], set(edges[start])
        while candidates:
            vertex = max(candidates, key=lambda v: (len(candidates & edges[v]), -v))
            clique.append(vertex)
            candidates &= edges[vertex]
        if len(clique) > len(best):
            best = clique
    assert all(j in edges[i] for i in best for j in best if i != j)
    return [groups[i][0] for i in best]


def prepare_baseline(advances, nominal, widen=False):
    """Widen only on request; always return an independently sorted table."""
    if widen:
        return widen_pairs(advances, nominal)
    codes = sorted(advances)
    return {a: {b: advances[a][b] for b in codes} for a in codes}


def verify_required_history(advances, history, spacing=4):
    """Apply the existing hardware proof, or the stronger optional pair proof."""
    if history == 1:
        witness = next(dependent_triplets(advances), None)
        if witness is not None:
            raise ValueError(f"One-glyph history is insufficient for {witness[:3]}")
        return len(advances) ** 3
    if history == 2:
        return verify_two_glyph_history(SimpleNamespace(
            glyphs=advances, spacing=spacing, advance=lambda a, b: advances[a][b]))
    raise ValueError("History must be one or two glyphs")


def select_candidate(variants):
    """Minimize padded storage, then worst-pair error counts, with stable ties.

    Among equal power-of-two ROM allocations, compare the number of pairs at
    the largest error first, then the next-largest error, down through 1px.
    Total widening is diagnostic data, not the selection objective. Only
    candidates that passed the required history proof may be selected.
    """
    valid = [i for i, entry in enumerate(variants) if entry["history_valid"]]
    if not valid:
        raise ValueError("No approximate candidate satisfies the required history invariant: "
                         + "; ".join(entry["history_failure"] for entry in variants))
    maximum = max(variants[i]["max_added_pixels"] for i in valid)

    def score(i):
        entry = variants[i]
        bits = (entry["left"] - 1).bit_length() + (entry["right"] - 1).bit_length()
        histogram = entry["extra_pixel_histogram"]
        return (1 << bits,
                tuple(histogram.get(str(extra), 0) for extra in range(maximum, 0, -1)),
                entry["cells"], max(entry["left"], entry["right"]), i)

    return min(valid, key=score)
