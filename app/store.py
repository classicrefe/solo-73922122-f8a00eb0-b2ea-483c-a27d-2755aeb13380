"""SQLite persistence and the persistent (on-disk) min-count segment tree.

Two kinds of domain data live here, as required:

1. the retained-sample verdicts (``samples`` table), and
2. the recognized last-old-label bottle number (``meta.label_cut``).

The conflict function ``f(cut)`` is not recomputed per request.  Its lazy
min-count segment tree is stored in the ``seg`` table, one row per node.  A
single verdict change touches O(log N) tree nodes and one sample row; a report
reads the root plus O(log N) nodes for the first/last/successor searches.
Both are sub-linear in the number of bottles whether the process is warm or
cold, because queries always go to the persisted tree rather than replaying
the samples.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from . import algorithm

INF = algorithm.INF
OLD = algorithm.OLD
NEW = algorithm.NEW
PENDING = algorithm.PENDING
VERDICTS = algorithm.VERDICTS

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
CREATE TABLE IF NOT EXISTS seg (
    idx  INTEGER PRIMARY KEY,
    mn   INTEGER NOT NULL,
    cnt  INTEGER NOT NULL,
    lazy INTEGER NOT NULL DEFAULT 0
);
"""

DEFAULT_META = {
    "label_cut": "",
    "seg_size": "0",
    "version": "0",
    "samples_old": "0",
    "samples_new": "0",
    "samples_pending": "0",
    "updated_at": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tree_size(n_bottles: int) -> int:
    size = 1
    while size < n_bottles + 1:
        size <<= 1
    return size


class TreeView:
    """Read-only query facade over the persisted segment tree.

    Mirrors the subset of :class:`algorithm.MinCountTree` used by
    :func:`algorithm.summarize`.  ``acc`` carries the sum of lazy values of
    ancestors, because a stored node value does not include its ancestors'
    pending additions.
    """

    __slots__ = ("cur", "n", "size", "visits")

    def __init__(self, cur: sqlite3.Cursor, n: int, size: int):
        self.cur = cur
        self.n = n
        self.size = size
        self.visits = 0

    def _row(self, idx: int) -> Tuple[int, int, int]:
        self.visits += 1
        row = self.cur.execute(
            "SELECT mn, cnt, lazy FROM seg WHERE idx = ?", (idx,)
        ).fetchone()
        if row is None:  # pragma: no cover - corruption guard
            raise RuntimeError(f"segment tree node {idx} missing")
        return row[0], row[1], row[2]

    def all_min(self) -> Tuple[int, int]:
        mn, cnt, _ = self._row(1)
        return mn, cnt

    def find_first(self, target: int, lo: int = 0, hi: Optional[int] = None) -> int:
        hi = self.n if hi is None else hi
        lo = max(lo, 0)
        hi = min(hi, self.n)
        if lo > hi:
            return -1
        return self._find_first(1, 0, self.size - 1, lo, hi, target, 0)

    def _find_first(self, idx, nl, nr, ql, qr, target, acc) -> int:
        if nr < ql or nl > qr or nl > self.n:
            return -1
        mn, cnt, lazy = self._row(idx)
        if cnt == 0 or mn + acc != target:
            return -1
        if nl == nr:
            return nl
        mid = (nl + nr) // 2
        found = self._find_first(idx * 2, nl, mid, ql, qr, target, acc + lazy)
        if found != -1:
            return found
        return self._find_first(idx * 2 + 1, mid + 1, nr, ql, qr, target, acc + lazy)

    def find_last(self, target: int, lo: int = 0, hi: Optional[int] = None) -> int:
        hi = self.n if hi is None else hi
        lo = max(lo, 0)
        hi = min(hi, self.n)
        if lo > hi:
            return -1
        return self._find_last(1, 0, self.size - 1, lo, hi, target, 0)

    def _find_last(self, idx, nl, nr, ql, qr, target, acc) -> int:
        if nr < ql or nl > qr or nl > self.n:
            return -1
        mn, cnt, lazy = self._row(idx)
        if cnt == 0 or mn + acc != target:
            return -1
        if nl == nr:
            return nl
        mid = (nl + nr) // 2
        found = self._find_last(idx * 2 + 1, mid + 1, nr, ql, qr, target, acc + lazy)
        if found != -1:
            return found
        return self._find_last(idx * 2, nl, mid, ql, qr, target, acc + lazy)


# ---------------------------------------------------------------------------
# Tree construction / mutation against a cursor (caller owns the transaction)
# ---------------------------------------------------------------------------
def rebuild_tree(cur: sqlite3.Cursor, n_bottles: int,
                 verdicts: Mapping[int, str]) -> int:
    """Replace the persisted tree for cuts 0..N.  O(N + samples)."""
    size = tree_size(n_bottles)
    diff = [0] * (n_bottles + 2)
    for bottle, verdict in verdicts.items():
        if verdict == OLD:
            diff[0] += 1
            diff[bottle] -= 1
        elif verdict == NEW:
            diff[bottle] += 1
            diff[n_bottles + 1] -= 1

    values = [0] * (n_bottles + 1)
    running = 0
    for cut in range(n_bottles + 1):
        running += diff[cut]
        values[cut] = running

    total_nodes = 2 * size
    mn = [INF] * total_nodes
    cnt = [0] * total_nodes
    for i in range(n_bottles + 1):
        mn[size + i] = values[i]
        cnt[size + i] = 1
    for i in range(size - 1, 0, -1):
        left = i * 2
        right = left + 1
        if mn[left] < mn[right]:
            mn[i] = mn[left]
            cnt[i] = cnt[left]
        elif mn[right] < mn[left]:
            mn[i] = mn[right]
            cnt[i] = cnt[right]
        else:
            mn[i] = mn[left]
            cnt[i] = cnt[left] + cnt[right]

    cur.execute("DELETE FROM seg")
    cur.executemany(
        "INSERT INTO seg (idx, mn, cnt, lazy) VALUES (?, ?, ?, 0)",
        ((i, mn[i], cnt[i]) for i in range(1, total_nodes)),
    )
    return size


def tree_add(cur: sqlite3.Cursor, size: int, n_bottles: int,
             lo: int, hi: int, delta: int) -> int:
    """Range-add on inclusive cut range, O(log N) node updates."""
    if delta == 0:
        return 0
    lo = max(lo, 0)
    hi = min(hi, n_bottles)
    if lo > hi:
        return 0
    return _add(cur, 1, 0, size - 1, lo, hi, delta)


def _read(cur, idx):
    row = cur.execute("SELECT mn, cnt, lazy FROM seg WHERE idx = ?", (idx,)).fetchone()
    return row[0], row[1], row[2]


def _add(cur, idx, nl, nr, lo, hi, delta) -> int:
    if nr < lo or nl > hi:
        return 0
    mn, cnt, lazy = _read(cur, idx)
    if lo <= nl and nr <= hi:
        cur.execute(
            "UPDATE seg SET mn = ?, lazy = ? WHERE idx = ?",
            (mn + delta, lazy + delta, idx),
        )
        return 1
    if lazy:
        # Push the pending addition into both children; afterwards the node's
        # own lazy is zero and the children's stored values are authoritative.
        cur.execute(
            "UPDATE seg SET mn = mn + ?, lazy = lazy + ? WHERE idx IN (?, ?)",
            (lazy, lazy, idx * 2, idx * 2 + 1),
        )
        cur.execute("UPDATE seg SET lazy = 0 WHERE idx = ?", (idx,))
        lazy = 0
    mid = (nl + nr) // 2
    visits = 1
    if lo <= mid:
        visits += _add(cur, idx * 2, nl, mid, lo, hi, delta)
    if hi > mid:
        visits += _add(cur, idx * 2 + 1, mid + 1, nr, lo, hi, delta)
    lmn, lcnt, _ = _read(cur, idx * 2)
    rmn, rcnt, _ = _read(cur, idx * 2 + 1)
    if lmn < rmn:
        new_mn, new_cnt = lmn, lcnt
    elif rmn < lmn:
        new_mn, new_cnt = rmn, rcnt
    else:
        new_mn, new_cnt = lmn, lcnt + rcnt
    cur.execute("UPDATE seg SET mn = ?, cnt = ? WHERE idx = ?", (new_mn, new_cnt, idx))
    return visits


def verdict_delta(cur, n_bottles: int, size: int, bottle: int,
                  verdict: str, delta: int) -> int:
    """Insert/remove one sample's contribution.  O(log N)."""
    if verdict == OLD:
        return tree_add(cur, size, n_bottles, 0, bottle - 1, delta)
    if verdict == NEW:
        return tree_add(cur, size, n_bottles, bottle, n_bottles, delta)
    return 0


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
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._ensure_meta()
            self._recover_tree_if_needed()

    # -- setup / recovery -------------------------------------------------
    def _ensure_meta(self):
        with self._tx(write=True) as cur:
            for key, value in DEFAULT_META.items():
                cur.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                    (key, value),
                )

    def _recover_tree_if_needed(self):
        with self._tx(write=True) as cur:
            meta = self._meta(cur)
            n = int(meta["total_bottles"])
            size = int(meta["seg_size"])
            if n <= 0:
                return
            expected = tree_size(n)
            stored = cur.execute("SELECT COUNT(*) FROM seg").fetchone()[0]
            if size != expected or stored != 2 * expected - 1:
                verdicts = {
                    row[0]: row[1]
                    for row in cur.execute("SELECT bottle, verdict FROM samples")
                }
                self._set_meta(cur, seg_size=rebuild_tree(cur, n, verdicts))

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
            base = {"state": state, "version": version}
            if n <= 0 or int(meta["seg_size"]) == 0:
                return {"initialized": False, **base}
            size = int(meta["seg_size"])
            if not meta.get("label_cut"):
                return {
                    "initialized": True,
                    "label_missing": True,
                    "query_nodes": 0,
                    **base,
                }
            label_cut = int(meta["label_cut"])
            view = TreeView(cur, n, size)
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
        """Replace all samples and rebuild the tree in one transaction."""
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
            size = rebuild_tree(cur, total_bottles, verdicts)
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
                "updated_at": now,
                "label_cut": label_cut if label_cut is not None else "",
            }
            self._set_meta(cur, **values)
            return {
                "rows": len(cleaned),
                "counts": counts,
                "total_bottles": total_bottles,
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
        """Change one retained sample's verdict in O(log N)."""
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
                }
            visited = 0
            if previous is not None:
                visited += verdict_delta(cur, n, size, bottle, previous, -1)
                cur.execute("DELETE FROM samples WHERE bottle = ?", (bottle,))
            visited += verdict_delta(cur, n, size, bottle, verdict, +1)
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
            self._set_meta(cur, version=version, updated_at=_now(), **counts)
            return {
                "changed": True,
                "previous": previous,
                "verdict": verdict,
                "version": version,
                "update_nodes": visited,
            }

    def reset(self) -> None:
        with self.lock, self._tx(write=True) as cur:
            cur.execute("DELETE FROM samples")
            cur.execute("DELETE FROM seg")
            for key, value in DEFAULT_META.items():
                self._set_meta(cur, **{key: value})
            self._set_meta(cur, total_bottles=0)

    def close(self):
        with self.lock:
            self.conn.close()
