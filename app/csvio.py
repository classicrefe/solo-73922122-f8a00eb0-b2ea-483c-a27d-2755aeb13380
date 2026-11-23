"""Review-CSV import.

Accepted formats (header required; column order fixed but header names are
tolerant of case/whitespace):

    bottle,verdict
    1,old
    2,new
    3,pending

Verdict aliases such as ``O/N/P``, ``旧/新/待``, ``旧配方/新配方/待核`` are
accepted.  Any unrecognized verdict, duplicate bottle, or out-of-range bottle
rejects the *whole* file so a partially imported shift can never be mistaken
for a committed review state.
"""

from __future__ import annotations

import csv
import io
from typing import List, Tuple

from .store import StoreError
from .algorithm import OLD, NEW, PENDING

MAX_ROWS = 100_000
MAX_BYTES = 8 * 1024 * 1024

ALIASES = {
    "old": OLD, "o": OLD, "旧": OLD, "旧配方": OLD, "老": OLD, "老配方": OLD,
    "1": OLD,
    "new": NEW, "n": NEW, "新": NEW, "新配方": NEW, "2": NEW,
    "pending": PENDING, "p": PENDING, "待": PENDING, "待核": PENDING,
    "待确认": PENDING, "待定": PENDING, "0": PENDING,
}


def _normalize_verdict(raw: str) -> str:
    token = (raw or "").strip().lower()
    if token in ALIASES:
        return ALIASES[token]
    raise StoreError(f"第 {{line}} 行的留样判定无法识别：{raw!r}")


def parse_review_csv(data: bytes) -> Tuple[List[Tuple[int, str]], int]:
    """Return ([(bottle, verdict)], line_count)."""
    if len(data) > MAX_BYTES:
        raise StoreError("CSV 大于 8 MB，请拆分后导入。")
    text = None
    for encoding in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise StoreError("CSV 编码无法识别（请使用 UTF-8 或 GB18030）。")

    reader = csv.reader(io.StringIO(text, newline=""))
    rows: List[Tuple[int, str]] = []
    seen = set()
    line_no = 0
    header_skipped = False
    try:
        for raw in reader:
            line_no += 1
            if not raw or all(not cell.strip() for cell in raw):
                continue
            if not header_skipped:
                header_skipped = True
                first, second = (raw + ["", ""])[:2]
                if first.strip().lower() in ("bottle", "bottle_no", "瓶号", "编号"):
                    continue
            if len(raw) < 2:
                raise StoreError(f"第 {line_no} 行少于两列。")
            bottle_token = raw[0].strip().lstrip("#")
            try:
                bottle = int(bottle_token)
            except ValueError:
                raise StoreError(f"第 {line_no} 行的瓶号不是整数：{raw[0]!r}")
            try:
                verdict = _normalize_verdict(raw[1])
            except StoreError as exc:
                raise StoreError(str(exc).replace("{line}", str(line_no)))
            if bottle in seen:
                raise StoreError(f"第 {line_no} 行瓶号 {bottle} 重复。")
            seen.add(bottle)
            rows.append((bottle, verdict))
            if len(rows) > MAX_ROWS:
                raise StoreError("CSV 超过 100000 行上限。")
    except csv.Error as exc:
        raise StoreError(f"CSV 解析失败：{exc}")
    if not rows:
        raise StoreError("CSV 中没有有效留样记录。")
    return rows, line_no
