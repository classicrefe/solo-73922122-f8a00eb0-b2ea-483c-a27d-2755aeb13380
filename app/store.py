"""SQLite persistence and the sparse (compressed) reconciliation index.

Two kinds of domain data live here, as required:

1. the retained-sample verdicts (``samples`` table), and
2. the recognized last-old-label bottle number (``meta.label_cut``).

The conflict function ``f(cut)`` is not recomputed per request, and it is no
longer stored as a dense segment tree (that cost ``2 * 2^ceil(log2(N+1)) - 1``
index rows -- 262143 rows for N = 100000, even with only a handful of judged
samples).  It is now kept as a *sparse refcounted* segment tree in the
``seg2`` table (see :mod:`app.algorithm`): a node row exists only while at
least one judged (old/new) sample's range-add visits it, so the index holds

    O(1 + K log(N+1)) rows,  K = #samples judged old/new,

and reverting a sample to ``pending`` deletes exactly the rows that sample
had materialized.  A single verdict change touches O(log N) nodes; a report
reads the root plus O(log N) nodes for the first/last/successor searches.
Both stay sub-linear in the bottle count whether the process is warm or
cold, because queries always go to the persisted index rather than replaying
the samples.  ``meta.index_rows`` is maintained inside the same transactions
so the row count itself is an O(1) read.

Schema versioning: v1 databases (dense ``seg`` table, no ``schema_version``
key) are upgraded in place in a single transaction -- the sparse index is
built from ``samples`` (verdicts and ``label_cut`` are preserved), then the
legacy table is dropped.  If the upgrade fails, the transaction rolls back
and the v1 database is left untouched, so the previous service version keeps
serving the old committed results.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from . import algorithm
from .algorithm import tree_size

INF = algorithm.INF
OLD = algorithm.OLD
NEW = algorithm.NEW
PENDING = algorithm.PENDING
VERDICTS = algorithm.VERDICTS

SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    bottle     INTEGER PRIMARY KEY CHECK (bottle >= 1),
    verdict    TEXT NOT NULL CHECK (verdict IN ('old', 'new', 'pending')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seg2 (
    idx      INTEGER PRIMARY KEY,
    mn       INTEGER NOT NULL,
    cnt      INTEGER NOT NULL,
    self_add INTEGER NOT NULL DEFAULT 0,
    ref      INTEGER NOT NULL DEFAULT 0
);
"""

# NOTE: ``schema_version`` is deliberately absent from DEFAULT_META: its
# absence is how a v1 database is recognized.  It is written explicitly once
# the sparse index is in place.
DEFAULT_META = {
    "label_cut": "",
    "seg_size": "0",
    "version": "0",
    "samples_old": "0",
    "samples_new": "0",
    "samples_pending": "0",
    "index_rows": "0",
    "updated_at": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# SQL node-store backend for the generic sparse-tree helpers
# ---------------------------------------------------------------------------
def _sql_get(cur: sqlite3.Cursor, idx: int):
    row = cur.execute(
        "SELECT mn, cnt, self_add, ref FROM seg2 WHERE idx = ?", (idx,)
    ).fetchone()
    return (row[0], row[1], row[2], row[3]) if row else None


def _sql_put(cur: sqlite3.Cursor, idx: int, mn: int, cnt: int,
             self_add: int, ref: int) -> None:
    cur.execute(
        "INSERT INTO seg2 (idx, mn, cnt, self_add, ref) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(idx) DO UPDATE SET mn = excluded.mn, cnt = excluded.cnt, "
        "self_add = excluded.self_add, ref = excluded.ref",
        (idx, mn, cnt, self_add, ref),
    )


def _sql_delete(cur: sqlite3.Cursor, idx: int) -> None:
    cur.execute("DELETE FROM seg2 WHERE idx = ?", (idx,))


def tree_add(cur: sqlite3.Cursor, size: int, n_bottles: int,
             lo: int, hi: int, delta: int) -> Tuple[int, int]:
    """Range-add on inclusive cut range; O(log N) node visits."""
    return algorithm.sparse_range_add(
        lambda idx: _sql_get(cur, idx),
        lambda idx, mn, cnt, self_add, ref: _sql_put(
            cur, idx, mn, cnt, self_add, ref),
        lambda idx: _sql_delete(cur, idx),
        n_bottles, size, lo, hi, delta,
    )


def verdict_delta(cur: sqlite3.Cursor, n_bottles: int, size: int, bottle: int,
                  verdict: str, delta: int) -> Tuple[int, int]:
    """Insert/remove one sample's contribution.  O(log N)."""
    if verdict == OLD:
        return tree_add(cur, size, n_bottles, 0, bottle - 1, delta)
    if verdict == NEW:
        return tree_add(cur, size, n_bottles, bottle, n_bottles, delta)
    return 0, 0


def rebuild_index(cur: sqlite3.Cursor, n_bottles: int,
                  verdicts: Mapping[int, str]) -> Tuple[int, int]:
    """Replace the persisted sparse index.  O(K log N) rows, K = judged.

    The tree is composed in memory (dict backend) and written in one bulk
    insert; ``pending`` samples carry no constraint and contribute no rows.
    Returns ``(tree_size, index_rows)``.
    """
    tree = algorithm.SparseTree.build(n_bottles, verdicts)
    cur.execute("DELETE FROM seg2")
    cur.executemany(
        "INSERT INTO seg2 (idx, mn, cnt, self_add, ref) VALUES (?, ?, ?, ?, ?)",
        ((idx, row[0], row[1], row[2], row[3])
         for idx, row in tree.nodes.items()),
    )
    return tree.size, tree.rows


class SparseTreeView:
    """Read-only query facade over the persisted sparse segment tree.

    Mirrors the subset of :class:`algorithm.MinCountTree` used by
    :func:`algorithm.summarize`.  Missing rows are worth the default
    (mn=0, cnt=width) -- or INF beyond cut N -- as computed by
    :func:`algorithm.node_default`.
    """

    __slots__ = ("cur", "n", "size", "visits")

    def __init__(self, cur: sqlite3.Cursor, n: int, size: int):
        self.cur = cur
        self.n = n
        self.size = size
        self.visits = 0

    def _get(self, idx: int):
        self.visits += 1
        return _sql_get(self.cur, idx)

    def all_min(self) -> Tuple[int, int]:
        return algorithm.sparse_all_min(self._get, self.n, self.size)

    def find_first(self, target: int, lo: int = 0, hi: Optional[int] = None) -> int:
        hi = self.n if hi is None else hi
        return algorithm.sparse_find_first(self._get, self.n, self.size,
                                           target, lo, hi)

    def find_last(self, target: int, lo: int = 0, hi: Optional[int] = None) -> int:
        hi = self.n if hi is None else hi
        return algorithm.sparse_find_last(self._get, self.n, self.size,
                                          target, lo, hi)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class StoreError(Exception):
    """Domain error with a message suitable for the web UI."""


class Store:
    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=15
        )
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA busy_timeout = 15000")
        self.conn.execute("PRAGMA cache_size = -64000")
        try:
            with self.lock:
                self.conn.executescript(SCHEMA)
                self._ensure_meta()
                self._migrate_if_needed()
                self._recover_if_needed()
        except Exception:
            self.conn.close()
            raise

    # -- setup / migration / recovery -------------------------------------
    def _ensure_meta(self):
        with self._tx(write=True) as cur:
            for key, value in DEFAULT_META.items():
                cur.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                    (key, value),
                )

    def _migrate_if_needed(self):
        """Upgrade a v1 database in place, atomically.

        The sparse index is built from the ``samples`` table (verdicts and
        ``label_cut`` are preserved) and the legacy dense ``seg`` table is
        dropped, all in one transaction.  Any failure rolls the whole thing
        back: the v1 database keeps its original index, so the previous
        service version can still serve the old committed results.
        """
        with self._tx(write=True) as cur:
            meta = self._meta(cur)
            if meta.get("schema_version") == SCHEMA_VERSION:
                return
            n = int(meta["total_bottles"])
            if n > 0:
                verdicts = {
                    row[0]: row[1]
                    for row in cur.execute("SELECT bottle, verdict FROM samples")
                }
                size, rows = rebuild_index(cur, n, verdicts)
            else:
                cur.execute("DELETE FROM seg2")
                size, rows = 0, 0
            cur.execute("DROP TABLE IF EXISTS seg")
            self._set_meta(cur, schema_version=SCHEMA_VERSION,
                           seg_size=size, index_rows=rows)

    def _recover_if_needed(self):
        """O(1) sanity check at open; rebuild only if the index is broken.

        A normal restart reuses the persisted index directly: this reads a
        handful of meta rows plus the root node and nothing else.
        """
        with self._tx(write=True) as cur:
            meta = self._meta(cur)
            n = int(meta["total_bottles"])
            if n <= 0:
                return
            size = int(meta["seg_size"])
            judged = int(meta["samples_old"]) + int(meta["samples_new"])
            root = cur.execute("SELECT 1 FROM seg2 WHERE idx = 1").fetchone()
            # Every judged sample's range-add visits the root, so the root
            # row exists iff at least one judged sample is stored.
            consistent = size == tree_size(n) and ((root is not None) == (judged > 0))
            if not consistent:
                verdicts = {
                    row[0]: row[1]
                    for row in cur.execute("SELECT bottle, verdict FROM samples")
                }
                new_size, rows = rebuild_index(cur, n, verdicts)
                self._set_meta(cur, seg_size=new_size, index_rows=rows)

    # -- transactions -----------------------------------------------------
    class _Txn:
        def __init__(self, store, write):
            self.store = store
            self.write = write
            self.cur = None

        def __enter__(self):
            self.cur = self.store.conn.cursor()
            self.cur.execute("BEGIN IMMEDIATE" if self.write else "BEGIN")
            return self.cur

        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self.cur.execute("COMMIT")
                else:
                    self.cur.execute("ROLLBACK")
            finally:
                self.cur.close()
            return False

    def _tx(self, write=True):
        return Store._Txn(self, write)

    @staticmethod
    def _meta(cur) -> Dict[str, str]:
        meta = dict(DEFAULT_META)
        meta["total_bottles"] = "0"
        for key, value in cur.execute("SELECT key, value FROM meta"):
            meta[key] = value
        return meta

    @staticmethod
    def _set_meta(cur, **values):
        for key, value in values.items():
            cur.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    @staticmethod
    def _state(meta: Mapping[str, str]) -> Dict:
        return {
            "total_bottles": int(meta["total_bottles"]),
            "label_cut": int(meta["label_cut"]) if meta.get("label_cut") else None,
            "samples_old": int(meta["samples_old"]),
            "samples_new": int(meta["samples_new"]),
            "samples_pending": int(meta["samples_pending"]),
            "updated_at": meta["updated_at"],
        }

    # -- reads ------------------------------------------------------------
    def summary(self) -> Dict:
        """Consistent report from one committed snapshot.  O(log N)."""
        with self.lock, self._tx(write=False) as cur:
            meta = self._meta(cur)
            n = int(meta["total_bottles"])
            version = int(meta["version"])
            state = self._state(meta)
            size = int(meta["seg_size"])
            base = {
                "state": state,
                "version": version,
                "index_rows": int(meta["index_rows"]),
            }
            if n <= 0 or size == 0:
                return {"initialized": False, **base}
            # Row count of the pre-compression dense tree, for comparison.
            base["legacy_index_rows"] = 2 * size - 1
            if not meta.get("label_cut"):
                return {
                    "initialized": True,
                    "label_missing": True,
                    "query_nodes": 0,
                    **base,
                }
            label_cut = int(meta["label_cut"])
            view = SparseTreeView(cur, n, size)
            result = algorithm.summarize(view, n, label_cut)
            return {
                "initialized": True,
                "label_missing": False,
                "query_nodes": view.visits,
                **result,
                **base,
            }

    def get_sample(self, bottle: int) -> Optional[Dict]:
        with self.lock:
            row = self.conn.execute(
                "SELECT bottle, verdict, updated_at FROM samples WHERE bottle = ?",
                (bottle,),
            ).fetchone()
            if row is None:
                return None
            return {"bottle": row[0], "verdict": row[1], "updated_at": row[2]}

    # -- writes -----------------------------------------------------------
    def import_rows(self, total_bottles: int, label_cut: Optional[int],
                    rows: Iterable[Tuple[int, str]]) -> Dict:
        """Replace all samples and rebuild the index in one transaction."""
        rows = list(rows)
        if not (1 <= total_bottles <= 100_000):
            raise StoreError("总瓶数必须在 1 到 100000 之间。")
        if label_cut is not None and not (0 <= label_cut <= total_bottles):
            raise StoreError(f"换标瓶号必须在 0 到总瓶数 {total_bottles} 之间。")
        counts = {"old": 0, "new": 0, "pending": 0}
        cleaned: List[Tuple[int, str, str]] = []
        seen = set()
        now = _now()
        for bottle, verdict in rows:
            if verdict not in VERDICTS:
                raise StoreError(f"未知留样判定：{verdict!r}")
            if not (1 <= bottle <= total_bottles):
                raise StoreError(f"瓶号 {bottle} 超出 1..{total_bottles}")
            if bottle in seen:
                raise StoreError(f"瓶号 {bottle} 在 CSV 中重复。")
            seen.add(bottle)
            counts[verdict] += 1
            cleaned.append((bottle, verdict, now))
        if not cleaned:
            raise StoreError("CSV 中没有有效留样记录。")

        with self.lock, self._tx(write=True) as cur:
            old_meta = self._meta(cur)
            cur.execute("DELETE FROM samples")
            cur.executemany(
                "INSERT INTO samples (bottle, verdict, updated_at) VALUES (?, ?, ?)",
                cleaned,
            )
            verdicts = {bottle: verdict for bottle, verdict, _ in cleaned}
            size, index_rows = rebuild_index(cur, total_bottles, verdicts)
            version = int(old_meta["version"]) + 1
            if label_cut is None:
                old_label = old_meta.get("label_cut") or ""
                if old_label and not (0 <= int(old_label) <= total_bottles):
                    old_label = ""
                label_cut = int(old_label) if old_label else None
            values = {
                "total_bottles": total_bottles,
                "seg_size": size,
                "version": version,
                "samples_old": counts["old"],
                "samples_new": counts["new"],
                "samples_pending": counts["pending"],
                "index_rows": index_rows,
                "updated_at": now,
                "label_cut": label_cut if label_cut is not None else "",
            }
            self._set_meta(cur, **values)
            return {
                "rows": len(cleaned),
                "counts": counts,
                "total_bottles": total_bottles,
                "index_rows": index_rows,
                "version": version,
            }

    def set_label_cut(self, label_cut: int) -> Dict:
        with self.lock, self._tx(write=True) as cur:
            meta = self._meta(cur)
            n = int(meta["total_bottles"])
            if n <= 0:
                raise StoreError("尚未导入留样，无法设置换标瓶号。")
            if not (0 <= label_cut <= n):
                raise StoreError(f"换标瓶号必须在 0 到 {n} 之间。")
            version = int(meta["version"]) + 1
            self._set_meta(cur, label_cut=label_cut, version=version,
                           updated_at=_now())
            return {"version": version, "label_cut": label_cut}

    def set_verdict(self, bottle: int, verdict: str) -> Dict:
        """Change one retained sample's verdict in O(log N).

        The sample row and the index rows change in the same transaction;
        withdrawing a judgement (``pending``) reclaims exactly the index
        rows that judgement had materialized.
        """
        if verdict not in VERDICTS:
            raise StoreError(f"未知留样判定：{verdict!r}")
        with self.lock, self._tx(write=True) as cur:
            meta = self._meta(cur)
            n = int(meta["total_bottles"])
            size = int(meta["seg_size"])
            if n <= 0 or size == 0:
                raise StoreError("尚未导入留样。")
            if not (1 <= bottle <= n):
                raise StoreError(f"瓶号必须在 1 到 {n} 之间。")
            row = cur.execute(
                "SELECT verdict FROM samples WHERE bottle = ?", (bottle,)
            ).fetchone()
            previous = row[0] if row else None
            if previous == verdict:
                return {
                    "changed": False,
                    "previous": previous,
                    "verdict": verdict,
                    "version": int(meta["version"]),
                    "update_nodes": 0,
                    "index_rows_delta": 0,
                }
            visited = 0
            rows_delta = 0
            if previous is not None:
                v, d = verdict_delta(cur, n, size, bottle, previous, -1)
                visited += v
                rows_delta += d
                cur.execute("DELETE FROM samples WHERE bottle = ?", (bottle,))
            v, d = verdict_delta(cur, n, size, bottle, verdict, +1)
            visited += v
            rows_delta += d
            cur.execute(
                "INSERT INTO samples (bottle, verdict, updated_at) VALUES (?, ?, ?)",
                (bottle, verdict, _now()),
            )
            counts = {
                "samples_old": int(meta["samples_old"]),
                "samples_new": int(meta["samples_new"]),
                "samples_pending": int(meta["samples_pending"]),
            }
            if previous:
                counts["samples_" + previous] -= 1
            counts["samples_" + verdict] += 1
            version = int(meta["version"]) + 1
            self._set_meta(cur, version=version, updated_at=_now(),
                           index_rows=int(meta["index_rows"]) + rows_delta,
                           **counts)
            return {
                "changed": True,
                "previous": previous,
                "verdict": verdict,
                "version": version,
                "update_nodes": visited,
                "index_rows_delta": rows_delta,
            }

    def reset(self) -> None:
        with self.lock, self._tx(write=True) as cur:
            cur.execute("DELETE FROM samples")
            cur.execute("DELETE FROM seg2")
            for key, value in DEFAULT_META.items():
                self._set_meta(cur, **{key: value})
            self._set_meta(cur, total_bottles=0)

    def close(self):
        with self.lock:
            self.conn.close()
