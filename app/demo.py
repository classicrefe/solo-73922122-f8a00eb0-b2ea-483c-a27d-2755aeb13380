"""Deterministic demo dataset used by the web UI and by the sample CSV.

The shift has 100000 bottles, the last old label is #58720 and the true
recipe cut is #58400 (bottles #58401..#58720 were filled with the new recipe
but still carry the old label).  Retained samples are taken every 307 bottles
plus a few key bottles left as "pending":

* #58581 and #58800 are pending, so without operator input every cut between
  #58400 and #58887 is equally consistent with the reviewed samples;
* supplementing #58800 as "old" pins the recipe cut to >= #58800 and creates
  an 80-bottle must-relabel segment.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

TOTAL_BOTTLES = 100_000
LABEL_CUT = 58_720
TRUE_RECIPE_CUT = 58_400
PENDING_BOTTLES = (58_581, 58_800)


def demo_rows() -> List[Tuple[int, str]]:
    samples: Dict[int, str] = {}
    bottle = 251
    while bottle <= TOTAL_BOTTLES:
        samples[bottle] = "old" if bottle <= TRUE_RECIPE_CUT else "new"
        bottle += 307
    samples[TRUE_RECIPE_CUT] = "old"
    for pending in PENDING_BOTTLES:
        samples[pending] = "pending"
    return sorted(samples.items())


def demo_csv() -> str:
    lines = ["bottle,verdict"]
    lines.extend(f"{bottle},{verdict}" for bottle, verdict in demo_rows())
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(demo_csv(), end="")
