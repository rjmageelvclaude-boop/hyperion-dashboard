#!/usr/bin/env python3
"""
One-shot refresh for the employee Scorecard: assembles Week/MTD/YTD KPIs
(week computed live, MTD/YTD from the sibling boards' data.json) plus the
encrypted pay block, and writes site/scorecard/data.json.

MUST run after the sibling boards in refresh.yml (it reads their data.json),
alongside the GOAT board. Closed pay months are cached in
data/scorecard-pay-history.json; a hard time budget keeps the pay backfill
inside the workflow step timeout and resumes on the next run.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import scorecard_live as engine

OUT_PATH = os.path.join(ROOT, "site", "scorecard", "data.json")
TIME_BUDGET_SECS = int(os.environ.get("SCORECARD_BUDGET", "300"))


def main():
    t0 = time.time()
    data = engine.compute(
        time_budget_secs=TIME_BUDGET_SECS,
        progress=lambda co, what, secs: print(f"  {co} {what} in {secs:.1f}s", flush=True))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, OUT_PATH)
    print(f"wrote {OUT_PATH} in {time.time() - t0:.0f}s "
          f"(complete={data['complete']}, {len(data['employees'])} employees, "
          f"pay={'encrypted' if data['pay'] else 'omitted'})")
    if data["payNote"]:
        print(f"pay note: {data['payNote']}")


if __name__ == "__main__":
    main()
