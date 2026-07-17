#!/usr/bin/env python3
"""
One-shot refresh for the CA Commission Audit board: computes expected-vs-paid
commission for every Comfort Advisor install job of the year from the live
ServiceTitan API and writes site/ca-pay-board/data.json.

Closed months are cached in data/ca-pay-board-history.json (persisted between
runs by the Actions cache) and frozen 45 days past month-end - commission
corrections and Direct Adjustments trickle in for weeks.

Run by .github/workflows/refresh.yml on the same cadence as the other boards.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import ca_pay_board_live as engine

OUT_PATH = os.path.join(ROOT, "site", "ca-pay-board", "data.json")


def main():
    t0 = time.time()
    data = engine.compute(log=lambda msg: print(f"  {msg}", flush=True))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, OUT_PATH)
    js = data["jobs"]
    var = sum(j["variance"] for j in js)
    print(f"wrote {OUT_PATH} in {time.time() - t0:.0f}s "
          f"({len(js)} jobs, YTD variance ${var:+,.0f})")


if __name__ == "__main__":
    main()
