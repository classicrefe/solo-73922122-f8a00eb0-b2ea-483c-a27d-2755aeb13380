"""Core reconciliation algorithm.

Terminology (bottle numbers are 1..N and cuts are "last bottle of the old
recipe/label"):

* A candidate recipe cut ``R`` means bottles ``1..R`` were filled with the old
  recipe and bottles ``R+1..N`` with the new recipe.
* A verified (reviewed) retained sample says "old" or "new".  A sample at
  bottle ``j`` judged "old" requires ``j <= R``; judged "new" requires
  ``j > R``.  A "pending" sample imposes no constraint.
* For a cut ``R`` the number of samples it contradicts is

      f(R) = #{old samples j > R} + #{new samples j <= R}.

  ``f`` is a sum of box functions.  It is maintained in a lazy segment tree
  over all cuts ``0..N`` with range-add / global-min / first-last-min queries.
* The admissible recipe cuts are exactly ``argmin f`` (all cuts with the
  fewest conflicts).
* With label cut ``L`` (last old *label*) the bottles that are mislabelled
  under recipe cut ``R`` are exactly ``(min(L, R), max(L, R)]`` -- all cuts are
  anchored at ``L``.  Hence, over all admissible cuts, the must-wrong bottles
  are the intersection ``(max C, L]`` (or ``(L, min C]``) and the possible but
  not certain ones are ``(min C, max C]``.  Only two segments are ever needed,
  so reporting is O(log N) plus O(1) output.

Everything in this module is independent of Flask/SQLite so it can be tested
directly.
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping, Optional

INF = 1 << 30

OLD = "old"
NEW = "new"
PENDING = "pending"
VERDICTS = (OLD, NEW, PENDING)


def _merge(mn, cnt, i: int) -> None:
    left = i * 2
    right = left + 1
    lm = mn[left]
    rm = mn[right]
    if lm < rm:
        mn[i] = lm
        cnt[i] = cnt[left]
    elif rm < lm:
        mn[i] = rm
        cnt[i] = cnt[right]
    else:
        mn[i] = lm
        cnt[i] = cnt[left] + cnt[right]


class MinCountTree:
    """Lazy segment tree: range add, global minimum, count of minima.

    Also supports finding the first/last position equal to the global minimum
    inside an index window.  All operations are O(log M) node visits where
    ``M`` is the number of cuts (``N + 1 <= 100001``).
    """

    __slots__ = ("n", "size", "mn", "cnt", "lazy", "visits")

    def __init__(self, values: Iterable[int]):
        values = list(values)
        self.n = len(values)
        size = 1
        while size < self.n:
            size <<= 1
        self.size = size
        mn = [INF] * (2 * size)
        cnt = [0] * (2 * size)
        lazy = [0] * (2 * size)
        base = size
        for i, value in enumerate(values):
            mn[base + i] = value
            cnt[base + i] = 1
        for i in range(size - 1, 0, -1):
            _merge(mn, cnt, i)
        self.mn = mn
        self.cnt = cnt
        self.lazy = lazy
        self.visits = 0

    # -- basic queries ----------------------------------------------------
    def all_min(self):
        return self.mn[1], self.cnt[1]

    # -- range add --------------------------------------------------------
    def add(self, lo: int, hi: int, delta: int) -> None:
        """Add ``delta`` on the inclusive index range [lo, hi]."""
        if delta == 0 or lo > hi or self.n == 0:
            return
        lo = max(lo, 0)
        hi = min(hi, self.n - 1)
        if lo > hi:
            return
        self._add(1, 0, self.size - 1, lo, hi, delta)

    def _add(self, i, node_lo, node_hi, lo, hi, delta):
        self.visits += 1
        if lo <= node_lo and node_hi <= hi:
            self.mn[i] += delta
            self.lazy[i] += delta
            return
        self._push(i)
        mid = (node_lo + node_hi) // 2
        if lo <= mid:
            self._add(i * 2, node_lo, mid, lo, hi, delta)
        if hi > mid:
            self._add(i * 2 + 1, mid + 1, node_hi, lo, hi, delta)
        _merge(self.mn, self.cnt, i)

    def _push(self, i):
        z = self.lazy[i]
        if z:
            left = i * 2
            right = left + 1
            self.mn[left] += z
            self.lazy[left] += z
            self.mn[right] += z
            self.lazy[right] += z
            self.lazy[i] = 0

    # -- lowest-value index search ---------------------------------------
    def find_first(self, target: int, lo: int = 0, hi: Optional[int] = None) -> int:
        if hi is None:
            hi = self.n - 1
        lo = max(lo, 0)
        hi = min(hi, self.n - 1)
        if lo > hi:
            return -1
        return self._find_first(1, 0, self.size - 1, lo, hi, target)

    def _find_first(self, i, node_lo, node_hi, lo, hi, target):
        self.visits += 1
        if node_hi < lo or node_lo > hi or self.mn[i] != target:
            return -1
        if node_lo == node_hi:
            return node_lo
        self._push(i)
        mid = (node_lo + node_hi) // 2
        found = self._find_first(i * 2, node_lo, mid, lo, hi, target)
        if found != -1:
            return found
        return self._find_first(i * 2 + 1, mid + 1, node_hi, lo, hi, target)

    def find_last(self, target: int, lo: int = 0, hi: Optional[int] = None) -> int:
        if hi is None:
            hi = self.n - 1
        lo = max(lo, 0)
        hi = min(hi, self.n - 1)
        if lo > hi:
            return -1
        return self._find_last(1, 0, self.size - 1, lo, hi, target)

    def _find_last(self, i, node_lo, node_hi, lo, hi, target):
        self.visits += 1
        if node_hi < lo or node_lo > hi or self.mn[i] != target:
            return -1
        if node_lo == node_hi:
            return node_lo
        self._push(i)
        mid = (node_lo + node_hi) // 2
        found = self._find_last(i * 2 + 1, mid + 1, node_hi, lo, hi, target)
        if found != -1:
            return found
        return self._find_last(i * 2, node_lo, mid, lo, hi, target)


# ---------------------------------------------------------------------------
# Building / editing the conflict function
# ---------------------------------------------------------------------------
def build_tree(total_bottles: int, verdicts: Mapping[int, str]) -> MinCountTree:
    """Build f(0..N) in O(N + number_of_samples) using a difference array."""
    n = total_bottles
    diff = [0] * (n + 2)
    for bottle, verdict in verdicts.items():
        if verdict == OLD:
            # conflicts for every cut R with R <= bottle - 1
            diff[0] += 1
            diff[bottle] -= 1
        elif verdict == NEW:
            # conflicts for every cut R with R >= bottle
            diff[bottle] += 1
            diff[n + 1] -= 1
    values = [0] * (n + 1)
    running = 0
    for cut in range(n + 1):
        running += diff[cut]
        values[cut] = running
    return MinCountTree(values)


def apply_verdict(tree: MinCountTree, total_bottles: int, bottle: int,
                  verdict: str, delta: int) -> None:
    """Add/remove one sample contribution in O(log N)."""
    if verdict == OLD:
        tree.add(0, bottle - 1, delta)
    elif verdict == NEW:
        tree.add(bottle, total_bottles, delta)


def segment(start: int, end: int) -> Dict[str, int]:
    """Inclusive bottle segment helper (bottles start..end)."""
    return {"start": start, "end": end, "count": end - start + 1}


def summarize(tree: MinCountTree, total_bottles: int, label_cut: int) -> Dict:
    """O(log N) reconciliation summary for the committed conflict function."""
    n = total_bottles
    minimum, cut_count = tree.all_min()
    first_cut = tree.find_first(minimum, 0, n)
    last_cut = tree.find_last(minimum, 0, n)
    # nearest admissible cuts on both sides of the label cut
    pred = tree.find_last(minimum, 0, label_cut)
    succ = tree.find_first(minimum, label_cut, n)

    lower = INF
    if pred >= 0:
        lower = min(lower, label_cut - pred)
    if succ >= 0:
        lower = min(lower, succ - label_cut)
    if lower == INF:  # cannot happen: argmin is non-empty and inside 0..n
        lower = 0
    upper = max(label_cut - first_cut, last_cut - label_cut)

    # Must-wrong bottles: present in every candidate cut's mislabel interval.
    if last_cut <= label_cut:
        must = segment(last_cut + 1, label_cut) if last_cut < label_cut else None
    elif first_cut >= label_cut:
        must = segment(label_cut + 1, first_cut) if first_cut > label_cut else None
    else:  # admissible cuts straddle the label cut -> no bottle is certain
        must = None

    # Possible-but-not-certain bottles are always (first_cut, last_cut].
    partial = segment(first_cut + 1, last_cut) if first_cut < last_cut else None

    must_count = must["count"] if must else 0
    partial_count = partial["count"] if partial else 0

    return {
        "total_bottles": n,
        "label_cut": label_cut,
        "candidates": {
            "count": cut_count,
            "first": first_cut,
            "last": last_cut,
            "contiguous": last_cut - first_cut + 1 == cut_count,
            "min_conflicts": minimum,
        },
        "must": must,
        "partial": partial,
        "bounds": {"min": lower, "max": upper},
        "counts": {
            "must": must_count,
            "partial": partial_count,
            "union": must_count + partial_count,
        },
    }


def brute_force(total_bottles: int, verdicts: Mapping[int, str], label_cut: int) -> Dict:
    """Reference implementation used by the test-suite (O(N * samples))."""
    n = total_bottles
    conflicts = []
    for cut in range(n + 1):
        value = 0
        for bottle, verdict in verdicts.items():
            if verdict == OLD and bottle > cut:
                value += 1
            elif verdict == NEW and bottle <= cut:
                value += 1
        conflicts.append(value)
    best = min(conflicts)
    candidates = [cut for cut, value in enumerate(conflicts) if value == best]

    wrong_sets = []
    for cut in candidates:
        lo, hi = min(label_cut, cut), max(label_cut, cut)
        wrong_sets.append(set(range(lo + 1, hi + 1)))
    must_set = set.intersection(*wrong_sets) if wrong_sets else set()
    union_set = set.union(*wrong_sets) if wrong_sets else set()

    def pack(items):
        if not items:
            return None
        ordered = sorted(items)
        return {"start": ordered[0], "end": ordered[-1], "count": len(ordered)}

    return {
        "total_bottles": n,
        "label_cut": label_cut,
        "candidates": {
            "count": len(candidates),
            "first": candidates[0],
            "last": candidates[-1],
            "contiguous": candidates[-1] - candidates[0] + 1 == len(candidates),
            "min_conflicts": best,
        },
        "must": pack(must_set),
        "partial": pack(union_set - must_set),
        "bounds": {
            "min": min(abs(label_cut - cut) for cut in candidates),
            "max": max(abs(label_cut - cut) for cut in candidates),
        },
        "counts": {
            "must": len(must_set),
            "partial": len(union_set - must_set),
            "union": len(union_set),
        },
    }
