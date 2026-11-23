"""Persistence, transactional consistency and sub-linear cost tests."""

import random
import sqlite3

import pytest

from app import algorithm
from app.store import Store, StoreError, tree_size

OLD, NEW, PENDING = algorithm.OLD, algorithm.NEW, algorithm.PENDING


@pytest.fixture()
def store(tmp_path):
    instance = Store(str(tmp_path / "test.sqlite3"))
    yield instance
    instance.close()


def brute(store, n, label):
    verdicts = {}
    for row in store.conn.execute("SELECT bottle, verdict FROM samples"):
        verdicts[row[0]] = row[1]
    return algorithm.brute_force(n, verdicts, label)


def test_import_then_summary_matches_brute_force(store):
    rng = random.Random(11)
    n = 300
    rows = []
    for bottle in range(1, n + 1):
        if rng.random() < 0.2:
            rows.append((bottle, rng.choice([OLD, NEW, PENDING])))
    store.import_rows(n, 150, rows)
    got = store.summary()
    want = brute(store, n, 150)
    for key in ("candidates", "must", "partial", "bounds", "counts"):
        assert got[key] == want[key]


def test_single_verdict_update_is_incremental_and_correct(store):
    n = 500
    rows = [(b, OLD if b <= 250 else NEW) for b in range(1, n + 1, 10)]
    store.import_rows(n, 240, rows)
    rng = random.Random(5)
    for _ in range(60):
        bottle = rng.randint(1, n)
        verdict = rng.choice([OLD, NEW, PENDING])
        store.set_verdict(bottle, verdict)
        got = store.summary()
        want = brute(store, n, 240)
        for key in ("candidates", "must", "partial", "bounds"):
            assert got[key] == want[key], (bottle, verdict, key)
    # a single update touches O(log N) nodes, not O(N)
    sample = store.get_sample(7)
    target = NEW if (sample and sample["verdict"] == NEW) else OLD
    info = store.set_verdict(7, target)
    assert info["changed"] is True
    assert info["update_nodes"] <= 2 * (tree_size(n).bit_length() + 1) * 2


def test_persistent_tree_survives_restart_without_replay(tmp_path):
    path = str(tmp_path / "restart.sqlite3")
    n = 1000
    rows = [(b, OLD if b <= 400 else NEW) for b in range(1, n + 1, 7)]
    first = Store(path)
    first.import_rows(n, 450, rows)
    first.set_verdict(900, OLD)
    before = first.summary()
    first.close()

    second = Store(path)
    after = second.summary()
    assert after["candidates"] == before["candidates"]
    assert after["must"] == before["must"]
    assert after["version"] == before["version"]
    # Query cost is the same cold or warm: it reads the persisted tree.
    assert after["query_nodes"] <= before["query_nodes"] + 2
    second.close()


def test_failed_import_keeps_previous_committed_result(store):
    n = 100
    store.import_rows(n, 40, [(10, OLD), (90, NEW)])
    committed = store.summary()
    with pytest.raises(StoreError):
        store.import_rows(n, 40, [(10, OLD), (10, NEW)])  # duplicate bottle
    assert store.summary() == committed

    with pytest.raises(StoreError):
        store.import_rows(n, 40, [(1000, OLD)])  # out of range
    assert store.summary() == committed

    with pytest.raises(StoreError):
        store.set_verdict(101, NEW)  # out of range
    assert store.summary() == committed


def test_write_failure_rolls_back_tree_and_samples(store):
    n = 200
    store.import_rows(n, 100, [(50, OLD), (150, NEW)])
    committed = store.summary()
    # Force the index update to fail mid-transaction by making the seg2
    # table read-only for the duration of the call.
    store.conn.execute("CREATE TRIGGER fail_seg_ins BEFORE INSERT ON seg2 "
                       "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    store.conn.execute("CREATE TRIGGER fail_seg_upd BEFORE UPDATE ON seg2 "
                       "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.DatabaseError):
        store.set_verdict(60, NEW)
    store.conn.execute("DROP TRIGGER fail_seg_ins")
    store.conn.execute("DROP TRIGGER fail_seg_upd")
    assert store.summary() == committed
    assert store.get_sample(60) is None


def test_summary_consistent_under_concurrent_writes(store):
    """A report never sees a half-applied verdict change."""
    n = 400
    store.import_rows(n, 200, [(b, OLD if b <= 200 else NEW) for b in range(1, n + 1, 3)])
    for bottle in range(201, 400, 5):
        store.set_verdict(bottle, OLD)
        report = store.summary()
        assert report["state"]["samples_old"] + report["state"]["samples_new"] + \
            report["state"]["samples_pending"] == store.conn.execute(
                "SELECT COUNT(*) FROM samples").fetchone()[0]


def test_label_is_required_then_settable(store):
    store.import_rows(50, None, [(5, OLD)])
    assert store.summary()["label_missing"] is True
    store.set_label_cut(20)
    report = store.summary()
    assert report["label_missing"] is False
    assert report["label_cut"] == 20


def test_100k_single_update_and_query_are_sublinear(store):
    n = 100_000
    rows = [(b, OLD if b <= 58_400 else NEW) for b in range(251, n + 1, 307)]
    rows = [row for row in rows if row[0] not in (58_581, 58_800)]
    rows.append((58_581, PENDING))
    rows.append((58_800, PENDING))
    store.import_rows(n, 58_720, rows)
    report = store.summary()
    assert report["candidates"]["count"] >= 1
    # tree height for 100001 cuts is 17; every operation visits O(height)
    assert report["query_nodes"] < 400
    assert report["must"] is None
    assert report["bounds"]["min"] == 0
    # the compressed index holds O(K log N) rows, not O(N): K = 325 judged
    judged = sum(1 for _, v in rows if v != PENDING)
    height = tree_size(n).bit_length()
    assert report["index_rows"] <= judged * 4 * height
    assert report["index_rows"] < report["legacy_index_rows"] // 10
    assert report["legacy_index_rows"] == 2 * tree_size(n) - 1 == 262_143
    info = store.set_verdict(58_800, OLD)
    assert info["changed"] is True
    assert info["update_nodes"] < 200
    after = store.summary()
    assert after["query_nodes"] < 400
    # The fix changes the relabel range, which is what the demo shows.
    assert after["must"] == {"start": 58_721, "end": 58_800, "count": 80}
    assert after["partial"] == {"start": 58_801, "end": 58_887, "count": 87}
    assert after["bounds"] == {"min": 80, "max": 167}


def test_pending_revert_reclaims_exact_index_rows(store):
    n = 100_000
    rows = [(b, OLD if b <= 58_400 else NEW) for b in range(251, n + 1, 307)]
    store.import_rows(n, 58_720, rows)
    before = store.summary()["index_rows"]
    info = store.set_verdict(58_800, OLD)
    assert info["index_rows_delta"] > 0
    mid = store.summary()["index_rows"]
    assert mid == before + info["index_rows_delta"]
    # reverting to pending reclaims exactly the extra rows
    undo = store.set_verdict(58_800, PENDING)
    assert undo["index_rows_delta"] == -info["index_rows_delta"]
    assert store.summary()["index_rows"] == before
    # ... while the pending detail stays queryable
    assert store.get_sample(58_800)["verdict"] == PENDING
    # and withdrawing every judgement empties the index entirely
    for bottle, _ in rows:
        store.set_verdict(bottle, PENDING)
    assert store.summary()["index_rows"] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM seg2").fetchone()[0] == 0
    # pending details are still all there (the imported rows plus #58800)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM samples WHERE verdict = 'pending'").fetchone()[0] == len(rows) + 1


def test_sql_index_matches_inmemory_sparse_tree(store):
    """The SQL backend and the dict backend must produce identical rows."""
    n = 500
    rows = [(b, OLD if b <= 250 else NEW) for b in range(1, n + 1, 10)]
    store.import_rows(n, 240, rows)
    rng = random.Random(23)
    for _ in range(80):
        store.set_verdict(rng.randint(1, n), rng.choice([OLD, NEW, PENDING]))
    got = {
        row[0]: tuple(row[1:])
        for row in store.conn.execute(
            "SELECT idx, mn, cnt, self_add, ref FROM seg2")
    }
    verdicts = {
        row[0]: row[1]
        for row in store.conn.execute("SELECT bottle, verdict FROM samples")
    }
    reference = algorithm.SparseTree.build(n, verdicts)
    want = {idx: tuple(vals) for idx, vals in reference.nodes.items()}
    assert got == want
    assert store.summary()["index_rows"] == len(want) == \
        store.conn.execute("SELECT COUNT(*) FROM seg2").fetchone()[0]


def test_restart_reuses_persisted_index_without_rebuild(tmp_path, monkeypatch):
    path = str(tmp_path / "restart.sqlite3")
    n = 1000
    rows = [(b, OLD if b <= 400 else NEW) for b in range(1, n + 1, 7)]
    first = Store(path)
    first.import_rows(n, 450, rows)
    first.set_verdict(900, OLD)
    before = first.summary()
    first.close()

    import app.store as store_module

    calls = []
    real_rebuild = store_module.rebuild_index

    def spy(cur, n_, verdicts):
        calls.append(1)
        return real_rebuild(cur, n_, verdicts)

    monkeypatch.setattr(store_module, "rebuild_index", spy)
    second = Store(path)
    after = second.summary()
    # a normal restart must not rebuild or otherwise rewrite the index
    assert calls == []
    assert after["index_rows"] == before["index_rows"]
    assert after["candidates"] == before["candidates"]
    assert after["query_nodes"] <= before["query_nodes"] + 2
    second.close()
