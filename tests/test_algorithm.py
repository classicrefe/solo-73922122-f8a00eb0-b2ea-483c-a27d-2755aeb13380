"""Randomized differential tests: fast segment tree vs. brute force."""

import random

from app import algorithm
from app.algorithm import OLD, NEW, PENDING


def random_case(rng):
    n = rng.randint(1, 40)
    verdicts = {}
    for bottle in range(1, n + 1):
        if rng.random() < 0.55:
            verdicts[bottle] = rng.choice([OLD, NEW, PENDING, PENDING])
    label = rng.randint(0, n)
    return n, verdicts, label


def test_build_and_summarize_matches_brute_force():
    rng = random.Random(20240117)
    for _ in range(400):
        n, verdicts, label = random_case(rng)
        tree = algorithm.build_tree(n, verdicts)
        fast = algorithm.summarize(tree, n, label)
        slow = algorithm.brute_force(n, verdicts, label)
        assert fast["candidates"] == slow["candidates"], (n, verdicts, label)
        assert fast["must"] == slow["must"]
        assert fast["partial"] == slow["partial"]
        assert fast["bounds"] == slow["bounds"]
        assert fast["counts"] == slow["counts"]


def test_incremental_updates_match_brute_force():
    rng = random.Random(777)
    for _ in range(120):
        n, verdicts, label = random_case(rng)
        tree = algorithm.build_tree(n, verdicts)
        current = dict(verdicts)
        for _step in range(25):
            bottle = rng.randint(1, n)
            new_verdict = rng.choice([OLD, NEW, PENDING])
            previous = current.get(bottle, PENDING)
            if previous != PENDING:
                algorithm.apply_verdict(tree, n, bottle, previous, -1)
            if new_verdict != PENDING:
                algorithm.apply_verdict(tree, n, bottle, new_verdict, +1)
            if new_verdict == PENDING:
                current.pop(bottle, None)
            else:
                current[bottle] = new_verdict
            label = rng.randint(0, n)
            fast = algorithm.summarize(tree, n, label)
            slow = algorithm.brute_force(n, current, label)
            assert fast["candidates"] == slow["candidates"]
            assert fast["must"] == slow["must"]
            assert fast["partial"] == slow["partial"]
            assert fast["bounds"] == slow["bounds"]


def test_pending_samples_never_constrain():
    n = 50
    label = 20
    base = algorithm.brute_force(n, {}, label)
    assert base["candidates"]["count"] == n + 1
    assert base["must"] is None
    # with no evidence at all every bottle except the label cut itself may or
    # may not be wrong, and the honest bounds span the whole range
    assert base["partial"] == {"start": 1, "end": 50, "count": 50}
    assert base["bounds"] == {"min": 0, "max": n - label}

    with_pending = algorithm.brute_force(n, {5: PENDING, 30: PENDING}, label)
    assert with_pending == base


def test_conflicting_samples_produce_honest_tie():
    # One old sample at #8 forces the recipe cut to be >= 8; with the label cut
    # at #5 every cut 8..10 is equally consistent -> a genuine ambiguity.
    n = 10
    verdicts = {8: OLD}
    slow = algorithm.brute_force(n, verdicts, 5)
    assert slow["candidates"]["min_conflicts"] == 0
    assert slow["candidates"]["first"] == 8
    assert slow["candidates"]["last"] == 10
    assert slow["must"] == {"start": 6, "end": 8, "count": 3}
    assert slow["partial"] == {"start": 9, "end": 10, "count": 2}
    assert slow["bounds"] == {"min": 3, "max": 5}
    assert slow["counts"] == {"must": 3, "partial": 2, "union": 5}


def test_sparse_tree_matches_brute_force_and_dense_tree():
    rng = random.Random(20261123)
    for _ in range(150):
        n, verdicts, label = random_case(rng)
        sparse = algorithm.SparseTree.build(n, verdicts)
        dense = algorithm.build_tree(n, verdicts)
        slow = algorithm.brute_force(n, verdicts, label)
        fast = algorithm.summarize(sparse, n, label)
        assert fast == algorithm.summarize(dense, n, label)
        for key in ("candidates", "must", "partial", "bounds", "counts"):
            assert fast[key] == slow[key], (n, verdicts, label, key)
        # the sparse index is bounded by K * O(log N), K = judged samples
        judged = sum(1 for v in verdicts.values() if v != PENDING)
        height = sparse.size.bit_length()
        assert sparse.rows <= max(1, judged * 4 * height)


def test_sparse_tree_incremental_updates_and_reclamation():
    rng = random.Random(4242)
    for _ in range(120):
        n, verdicts, label = random_case(rng)
        sparse = algorithm.SparseTree.build(n, verdicts)
        current = dict(verdicts)
        for _step in range(25):
            bottle = rng.randint(1, n)
            new_verdict = rng.choice([OLD, NEW, PENDING])
            previous = current.get(bottle)
            if previous is not None:
                sparse.apply(bottle, previous, -1)
            if new_verdict != PENDING:
                sparse.apply(bottle, new_verdict, +1)
            if new_verdict == PENDING:
                current.pop(bottle, None)
            else:
                current[bottle] = new_verdict
            label = rng.randint(0, n)
            fast = algorithm.summarize(sparse, n, label)
            slow = algorithm.brute_force(n, current, label)
            for key in ("candidates", "must", "partial", "bounds", "counts"):
                assert fast[key] == slow[key], (n, current, label, key)
            # incremental state must equal a from-scratch build, row for row
            rebuilt = algorithm.SparseTree.build(n, current)
            assert sparse.nodes == rebuilt.nodes
        # withdrawing every judgement reclaims all index rows
        for bottle, verdict in list(current.items()):
            sparse.apply(bottle, verdict, -1)
        assert sparse.rows == 0
        mn, cnt = sparse.all_min()
        assert (mn, cnt) == (0, n + 1)
