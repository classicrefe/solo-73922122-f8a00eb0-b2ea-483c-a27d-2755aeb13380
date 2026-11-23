"""Concurrency: multiple worker processes writing the same SQLite database.

Writers serialize on ``BEGIN IMMEDIATE``; each one re-reads the tree nodes it
touches inside its transaction.  After all writers finish, the persisted tree
must still agree with a from-scratch rebuild over the sample table.
"""

import multiprocessing as mp
import random
import time

from app import algorithm
from app.store import Store

N = 3000
LABEL = 1500


def _writer(path, seed):
    rng = random.Random(seed)
    store = Store(path)
    for _ in range(40):
        bottle = rng.randint(1, N)
        verdict = rng.choice(["old", "new", "pending"])
        for attempt in range(6):
            try:
                store.set_verdict(bottle, verdict)
                break
            except Exception:
                time.sleep(0.05 * (attempt + 1))
    store.close()


def test_parallel_writers_leave_consistent_state(tmp_path):
    path = str(tmp_path / "concurrent.sqlite3")
    store = Store(path)
    rows = [(b, "old" if b <= LABEL else "new") for b in range(1, N + 1, 3)]
    store.import_rows(N, LABEL, rows)
    store.close()

    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_writer, args=(path, seed)) for seed in (1, 2, 3, 4)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
    assert all(proc.exitcode == 0 for proc in procs), [p.exitcode for p in procs]

    check = Store(path)
    verdicts = {
        row[0]: row[1]
        for row in check.conn.execute("SELECT bottle, verdict FROM samples")
    }
    want = algorithm.brute_force(N, verdicts, LABEL)
    got = check.summary()
    for key in ("candidates", "must", "partial", "bounds", "counts"):
        assert got[key] == want[key], key
    check.close()


def test_persisted_tree_equals_rebuild_after_parallel_writes(tmp_path):
    path = str(tmp_path / "rebuild.sqlite3")
    store = Store(path)
    rows = [(b, "old" if b <= LABEL else "new") for b in range(1, N + 1, 3)]
    store.import_rows(N, LABEL, rows)
    store.close()

    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_writer, args=(path, seed)) for seed in (7, 8, 9)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
    assert all(proc.exitcode == 0 for proc in procs)

    check = Store(path)
    # Snapshot the incrementally maintained tree leaves (materialized).
    size = int(check._meta(check.conn.cursor())["seg_size"])
    from app.store import TreeView

    with check._tx(write=False) as cur:
        view = TreeView(cur, N, size)
        minimum, count = view.all_min()
        leaves = []
        for cut in range(N + 1):
            leaf = view._find_first(1, 0, size - 1, cut, cut, minimum, 0)
            leaves.append(leaf)
    assert count == sum(1 for cut in leaves if cut >= 0)
    # Rebuild a fresh store and compare candidate summaries, which is the
    # observable contract.
    verdicts = {
        row[0]: row[1]
        for row in check.conn.execute("SELECT bottle, verdict FROM samples")
    }
    fresh_path = str(tmp_path / "fresh.sqlite3")
    fresh = Store(fresh_path)
    fresh.import_rows(N, LABEL, list(verdicts.items()))
    for key in ("candidates", "must", "partial", "bounds", "counts"):
        assert check.summary()[key] == fresh.summary()[key], key
    fresh.close()
    check.close()
