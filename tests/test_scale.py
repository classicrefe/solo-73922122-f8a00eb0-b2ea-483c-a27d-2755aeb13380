"""End-to-end proof that single edits and reports are sub-linear.

The store is exercised twice at very different sizes and the *actual* work
(SQL statements executed, tree nodes read) is measured, not just wall time.
A linear implementation would scale its work with the bottle count; this one
must stay flat while N grows 100x.
"""

import tempfile
import time
from pathlib import Path

from app.demo import TOTAL_BOTTLES
from app.store import Store, tree_size

OLD, NEW, PENDING = "old", "new", "pending"


def _build_rows(n, cut):
    rows = []
    bottle = 251
    while bottle <= n:
        rows.append((bottle, OLD if bottle <= cut else NEW))
        bottle += 997
    return rows


def _instrument(store):
    counter = {"statements": 0}
    original = store.conn.set_trace_callback
    original(lambda statement: counter.__setitem__("statements", counter["statements"] + 1))
    return counter


def measure(n):
    store = Store(str(Path(tempfile.mkdtemp()) / f"scale-{n}.sqlite3"))
    store.reset()
    rows = _build_rows(n, n // 2)
    store.import_rows(n, n // 2, rows)
    counter = _instrument(store)
    counter["statements"] = 0

    started = time.perf_counter()
    report = store.summary()
    query_time = time.perf_counter() - started
    query_statements = counter["statements"]

    counter["statements"] = 0
    started = time.perf_counter()
    info = store.set_verdict(n // 2 + 5, OLD)
    update_time = time.perf_counter() - started
    update_statements = counter["statements"]

    counter["statements"] = 0
    started = time.perf_counter()
    store.summary()
    query_after_update = time.perf_counter() - started

    store.conn.set_trace_callback(None)
    store.close()
    return {
        "n": n,
        "query_statements": query_statements,
        "query_nodes": report["query_nodes"],
        "update_statements": update_statements,
        "update_nodes": info["update_nodes"],
        "query_ms": query_time * 1000,
        "update_ms": update_time * 1000,
        "query_after_ms": query_after_update * 1000,
        "height": tree_size(n).bit_length(),
    }


def test_work_is_flat_between_1k_and_100k_bottles():
    small = measure(1_000)
    large = measure(TOTAL_BOTTLES)
    print("\nscale measurements:")
    for key in ("query_statements", "query_nodes", "update_statements",
                "update_nodes", "query_ms", "update_ms"):
        print(f"  {key:>20}: {small[key]:>8.1f}  ->  {large[key]:>8.1f}")
    # 100x more bottles must not multiply the per-operation work; a small
    # allowance covers the deeper tree (10 vs 17 levels).
    assert large["query_statements"] <= small["query_statements"] * 2
    assert large["update_statements"] <= small["update_statements"] * 2
    assert large["query_nodes"] < 400
    assert large["update_nodes"] < 200
    assert large["query_ms"] < 50
    assert large["update_ms"] < 50
