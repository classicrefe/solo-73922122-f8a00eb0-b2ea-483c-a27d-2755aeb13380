"""In-place upgrade of v1 (dense-index) databases to the sparse index.

A v1 database is fabricated with the pre-compression schema (dense ``seg``
table, no ``schema_version`` meta key).  The upgrade must preserve the
retained samples and the label cut, produce exactly the same reconciliation
results, and be atomic: if it fails, the v1 database -- and therefore the
previous service version's answers -- must remain intact.
"""

import sqlite3

import pytest

from app import algorithm
from app.algorithm import OLD, NEW, PENDING
from app.store import Store, tree_size

N = 500
LABEL = 240
ROWS = [(b, OLD if b <= 250 else NEW) for b in range(1, N + 1, 10)] + [
    (252, PENDING),
    (255, PENDING),
]


def make_v1_db(path, n=N, label=LABEL, rows=ROWS, version="7"):
    """Write a v1-format database exactly as the old service would have."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE samples (
            bottle     INTEGER PRIMARY KEY CHECK (bottle >= 1),
            verdict    TEXT NOT NULL CHECK (verdict IN ('old', 'new', 'pending')),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE seg (
            idx  INTEGER PRIMARY KEY,
            mn   INTEGER NOT NULL,
            cnt  INTEGER NOT NULL,
            lazy INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    verdicts = {b: v for b, v in rows}
    tree = algorithm.build_tree(n, verdicts)
    conn.executemany(
        "INSERT INTO seg (idx, mn, cnt, lazy) VALUES (?, ?, ?, 0)",
        ((i, tree.mn[i], tree.cnt[i]) for i in range(1, 2 * tree.size)),
    )
    conn.executemany(
        "INSERT INTO samples (bottle, verdict, updated_at) VALUES (?, ?, ?)",
        [(b, v, "2026-01-01T00:00:00+00:00") for b, v in rows],
    )
    counts = {"old": 0, "new": 0, "pending": 0}
    for _, v in rows:
        counts[v] += 1
    meta = {
        "label_cut": str(label),
        "seg_size": str(tree.size),
        "version": version,
        "samples_old": str(counts["old"]),
        "samples_new": str(counts["new"]),
        "samples_pending": str(counts["pending"]),
        "total_bottles": str(n),
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta.items())
    conn.commit()
    conn.close()
    return verdicts


class V1TreeView:
    """The old service's read path over the dense ``seg`` table."""

    def __init__(self, cur, n, size):
        self.cur = cur
        self.n = n
        self.size = size

    def _row(self, idx):
        row = self.cur.execute(
            "SELECT mn, cnt, lazy FROM seg WHERE idx = ?", (idx,)
        ).fetchone()
        return row[0], row[1], row[2]

    def all_min(self):
        mn, cnt, _ = self._row(1)
        return mn, cnt

    def find_first(self, target, lo=0, hi=None):
        hi = self.n if hi is None else hi
        return self._find(1, 0, self.size - 1, lo, hi, target, 0, True)

    def find_last(self, target, lo=0, hi=None):
        hi = self.n if hi is None else hi
        return self._find(1, 0, self.size - 1, lo, hi, target, 0, False)

    def _find(self, idx, nl, nr, ql, qr, target, acc, first):
        if nr < ql or nl > qr or nl > self.n:
            return -1
        mn, cnt, lazy = self._row(idx)
        if cnt == 0 or mn + acc != target:
            return -1
        if nl == nr:
            return nl
        mid = (nl + nr) // 2
        near = (idx * 2, nl, mid) if first else (idx * 2 + 1, mid + 1, nr)
        far = (idx * 2 + 1, mid + 1, nr) if first else (idx * 2, nl, mid)
        found = self._find(near[0], near[1], near[2], ql, qr, target,
                           acc + lazy, first)
        if found != -1:
            return found
        return self._find(far[0], far[1], far[2], ql, qr, target,
                          acc + lazy, first)


def v1_summary(path, n=N, label=LABEL):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    size = int(cur.execute(
        "SELECT value FROM meta WHERE key = 'seg_size'").fetchone()[0])
    view = V1TreeView(cur, n, size)
    result = algorithm.summarize(view, n, label)
    conn.close()
    return result


def test_upgrade_preserves_samples_label_and_results(tmp_path):
    path = str(tmp_path / "v1.sqlite3")
    verdicts = make_v1_db(path)
    want = algorithm.brute_force(N, verdicts, LABEL)

    store = Store(path)  # triggers the in-place upgrade
    got = store.summary()
    for key in ("candidates", "must", "partial", "bounds", "counts"):
        assert got[key] == want[key], key
    # label cut, samples (incl. pending) and version are preserved
    assert got["state"]["label_cut"] == LABEL
    assert got["version"] == 7
    assert {
        row[0]: row[1]
        for row in store.conn.execute("SELECT bottle, verdict FROM samples")
    } == verdicts
    assert store.get_sample(252)["verdict"] == PENDING
    # the legacy dense index is gone; the sparse index honours the bound
    assert store.conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'seg'").fetchone() is None
    judged = sum(1 for v in verdicts.values() if v != PENDING)
    height = tree_size(N).bit_length()
    assert got["index_rows"] <= judged * 4 * height
    assert got["index_rows"] < got["legacy_index_rows"]
    assert store._meta(store.conn.cursor())["schema_version"] == "2"
    # and the upgraded database keeps accepting incremental edits
    store.set_verdict(255, OLD)
    verdicts2 = dict(verdicts)
    verdicts2[255] = OLD
    want2 = algorithm.brute_force(N, verdicts2, LABEL)
    got2 = store.summary()
    for key in ("candidates", "must", "partial", "bounds", "counts"):
        assert got2[key] == want2[key], key
    store.close()


def test_upgrade_then_restart_reuses_sparse_index(tmp_path):
    path = str(tmp_path / "v1-restart.sqlite3")
    make_v1_db(path)
    first = Store(path)
    before = first.summary()
    first.close()
    second = Store(path)
    after = second.summary()
    assert after["index_rows"] == before["index_rows"]
    assert after["candidates"] == before["candidates"]
    assert after["version"] == before["version"]
    second.close()


def test_failed_upgrade_leaves_v1_database_untouched(tmp_path):
    path = str(tmp_path / "v1-fail.sqlite3")
    verdicts = make_v1_db(path)
    # Sabotage the not-yet-created sparse table so the migration aborts
    # halfway through its transaction.
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE seg2 (
            idx      INTEGER PRIMARY KEY,
            mn       INTEGER NOT NULL,
            cnt      INTEGER NOT NULL,
            self_add INTEGER NOT NULL DEFAULT 0,
            ref      INTEGER NOT NULL DEFAULT 0
        );
        CREATE TRIGGER block_seg2 BEFORE INSERT ON seg2
        BEGIN SELECT RAISE(ABORT, 'injected failure'); END;
        """
    )
    conn.close()

    with pytest.raises(sqlite3.DatabaseError):
        Store(path)

    # The v1 database is untouched: dense index, samples, label cut, and no
    # schema_version flip -- so the previous service version still answers.
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM seg").fetchone()[0] == \
        2 * tree_size(N) - 1
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == len(ROWS)
    assert conn.execute(
        "SELECT value FROM meta WHERE key = 'label_cut'").fetchone()[0] == str(LABEL)
    assert conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone() is None
    conn.close()
    old_service = v1_summary(path)
    want = algorithm.brute_force(N, verdicts, LABEL)
    for key in ("candidates", "must", "partial", "bounds", "counts"):
        assert old_service[key] == want[key], key

    # Once the obstruction is removed, the upgrade retries cleanly.
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER block_seg2")
    conn.close()
    store = Store(path)
    assert store.summary()["candidates"] == want["candidates"]
    store.close()


def test_empty_v1_database_upgrades_trivially(tmp_path):
    path = str(tmp_path / "v1-empty.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE samples (
            bottle     INTEGER PRIMARY KEY CHECK (bottle >= 1),
            verdict    TEXT NOT NULL CHECK (verdict IN ('old', 'new', 'pending')),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE seg (
            idx  INTEGER PRIMARY KEY,
            mn   INTEGER NOT NULL,
            cnt  INTEGER NOT NULL,
            lazy INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO meta (key, value) VALUES ('total_bottles', '0');
        """
    )
    conn.close()
    store = Store(path)
    assert store.summary()["initialized"] is False
    assert store.conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'seg'").fetchone() is None
    store.close()
