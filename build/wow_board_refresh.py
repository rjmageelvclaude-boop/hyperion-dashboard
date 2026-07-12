#!/usr/bin/env python3
"""Refresh the Week-over-Week scoreboard from the live ServiceTitan API.

Writes site/wow-board/data.json. Run by .github/workflows/refresh.yml in the
second wait block (after the CA board), because the WoW engine reads the CA
board's tenant caches for the HVAC Sales channel metrics.

WOW_BOARD_BUDGET caps the closed-week backfill per run (seconds); missing
weeks resume from data/wow-board-history.json on the next run.
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import wow_board_live as engine

OUT_PATH = os.path.join(ROOT, "site", "wow-board", "data.json")
TIME_BUDGET_SECS = int(os.environ.get("WOW_BOARD_BUDGET", "420"))


def main():
    t0 = time.time()
    data = engine.compute(
        time_budget_secs=TIME_BUDGET_SECS,
        progress=lambda co, key, secs: print(f"  {co} {key} in {secs:.1f}s", flush=True))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, OUT_PATH)
    weeks = {co: len(b["weeks"]) for co, b in data["companies"].items()}
    print(f"wow board refreshed in {time.time() - t0:.1f}s "
          f"complete={data['complete']} weeks={weeks}")


if __name__ == "__main__":
    main()
