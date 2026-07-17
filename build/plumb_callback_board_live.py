#!/usr/bin/env python3
"""
Live ServiceTitan engine for the PLUMBING Install Callback Board.

Same cohort methodology as the HVAC board (callback_board_live.py, RJ's
v7 rules): each install month is tracked for return trips within
30/60/90/180 days, split into a Recall/Warranty bucket (the install had a
problem) and a Finish bucket (we couldn't finish on install day), and a
window is only final once every install has had that long to fail. The
cohort math (aggregate / crew / kpis / merge) is IMPORTED from the HVAC
engine so the two boards can never drift apart - only scope,
classification and the maintenance/trade exclusions differ.

Scope: the plumbing install work of each company.

    sierra    SIE  BU 408662213  Plumbing - Install
    ultimate  ULT  BU 8450       Plumbing - Service - no install BU exists,
                                 so only install-TYPED jobs (Part Install /
                                 Install * / * Replacement / repipe) join
                                 the cohort. Replacements sold on an
                                 age-typed service call are missed.
    brothers  BRO  BU 2218908    Plumbing Install

Russett has no active plumbing department, so it is not on this board.

Plumbing-specific classification differences (audited 2026-07-16 on
June 2026 jobs before building):

    - "Plumbing Part Install 3-5 HRS" is SOLD work at Sierra ($240k in
      June alone), not warranty part returns like "Install HVAC Part" -
      so there is NO special part category here. Revenue part installs in
      the install BU are cohort installs; $0 ones become service returns
      when linked, like any other free visit.
    - Finish bucket types: "Plumbing Finish Job" (SIE), "Plumbing Install
      Finish" (BRO). There is no plumbing Startup commissioning - BRO's
      "Sprinkler Start Up" is seasonal maintenance and must NOT be a
      callback, so all sprinkler/winterization work is excluded.
    - HVAC business units are hard-excluded (mirror of the HVAC board
      excluding plumbing BUs): their recalls belong on the HVAC board.
    - BRO install types embed the age of the unit being REPLACED
      ("Water Heater 7+") - the old-system exclusion is skipped inside
      the install BU so those stay cohort installs.
    - Planned second visits: Permit Inspection / City Permit Inspection,
      Plumbing Drywall, Plumbing Job Walk - counted in the footer slot
      (stored under the "drywall" key so the shared aggregate applies).

Everything else matches the HVAC board: callbacks must link to an install
no more than 365 days back (recall link -> project -> location), service
returns count only 1-180 days out when the visit was no-charge AND
un-quoted, closed months cache in data/plumb-callback-board-history.json.

CLI smoke test:
    py build/plumb_callback_board_live.py                # summary, all companies
    py build/plumb_callback_board_live.py sierra 2026-06 # one company-month, raw
"""
import datetime as dt
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from command_center_live import (fetch_all, local_today, _load_json,
                                 map_companies, update_history)
from tech_board_live import month_window_utc
# shared cohort machinery - classification-independent, keep in lockstep
from callback_board_live import (WINDOWS, RECALL_MAX_GAP, SERVICE_MAX_GAP,
                                 OPEN_CB_LOOKBACK_DAYS, RECENT_LIMIT,
                                 MONTH_FREEZE_DAYS, MONTH_RECHECK_HOURS,
                                 WINDOW_CLOSED_MONTHS, RE_TAGS,
                                 aggregate, crew_rows, _kpis, _merge_months,
                                 new_index, index_installs, _link, _parse,
                                 _day, _fill_crews, _fill_hours,
                                 _estimate_job_ids, job_types, bu_names,
                                 tech_names, _month_key)

HISTORY_FILE = os.path.join(ROOT, "data", "plumb-callback-board-history.json")
CACHE_V = 1                  # bump when classification/schema changes

COMPANIES = {
    "sierra":   {"tenant": "SIE", "tz": "pacific",  "label": "Sierra",
                 "color": "#1663c7", "bu": 408662213, "instRe": None},
    "ultimate": {"tenant": "ULT", "tz": "mountain", "label": "Ultimate",
                 "color": "#c7161d", "bu": 8450,
                 # no install BU: only install-typed service jobs are cohort
                 "instRe": re.compile(r"\binstall|replacement|repipe", re.I)},
    "brothers": {"tenant": "BRO", "tz": "mountain", "label": "Brothers",
                 "color": "#c2410c", "bu": 2218908, "instRe": None},
}

# ---------------------------------------------------------------- classify
# HVAC BUs: hard-excluded, even typed recalls (they're the HVAC board's).
# Every HVAC BU at SIE/ULT/BRO literally contains "HVAC" (verified 2026-07-16).
RE_HVAC_BU = re.compile(r"hvac", re.I)
# other-trade / non-field BUs: service returns from these are not ours
# (typed recalls still count when they link back to one of our installs)
RE_TRADE_BU = re.compile(r"electric|excavat|landscap|solar|\bsales\b"
                         r"|inventory|administrative", re.I)
# plumbing-department BUs (for the open-callback scan: SIE/BRO book most
# typed recalls in the Service BU, not the install BU)
RE_PLUMB_BU = re.compile(r"plumb|drain|sewer", re.I)
RE_QA = re.compile(r"quality assurance|qa crew|q/a", re.I)
# planned second visits, not callbacks (footer count)
RE_PLANNED = re.compile(r"permit inspection|drywall|job walk", re.I)
RE_DRIVEBY = re.compile(r"drive ?by", re.I)
# membership / planned / seasonal visits. Sprinkler work (incl. BRO's
# "Sprinkler Start Up" season openers and "Sprinkler Recall") is a separate
# system we never install on this board - all of it is excluded.
RE_MAINT = re.compile(r"tune|maint|membership|\bmsa\b|club|check ?up|inspect"
                      r"|safety|filter|rejuv|\bsam\b|flush|sprinkler|irrigat"
                      r"|winteriz|dewinter|start ?up|blow ?out|\bhcc\b"
                      r"|agreement", re.I)
RE_EST = re.compile(r"estimate|second opinion|proposal|\bbid\b", re.I)
# type names that pin the serviced system as YEARS old - can't be the
# install we did months ago ("Water Heater 7+", "Tankless ... 14+", "6+ yrs")
RE_OLDSYS = re.compile(r"\b[456789]\s*\+|\b1[0-9]\s*\+", re.I)
# HVAC-trade work booked inside a plumbing BU
RE_HVAC_TYPE = re.compile(r"furnace|condenser|compressor|heat pump|mini ?split"
                          r"|thermostat|t-?stat|refriger|freon|\bducts?\b"
                          r"|ductwork|evap cooler|swamp|air condition"
                          r"|\ba/?c\b|\bhvac\b|indoor air|air quality|\biaq\b"
                          r"|humidifier|boiler|no cool|no heat", re.I)
# new-construction phase visits: planned, not callbacks
RE_NEWCON = re.compile(r"\bnc\s*-|new construction|rough ?in", re.I)
RE_RECALL = re.compile(r"recall|warranty|client resolution|call ?back", re.I)
RE_FINISH = re.compile(r"\bfinish\b", re.I)
# bucket split (RJ): "finish" = we couldn't finish on install day
RE_FINISH_TY = RE_FINISH


def classify(type_name, bu_name="", install_bu=False):
    """excluded | qa | permit | recall | neutral.
    neutral resolves by BU: install BU + revenue (+ instRe gate) -> install
    (cohort); anything else -> service-return candidate (counts when linked).
    install_bu skips the old-system exclusion: BRO install types carry the
    age of the unit being REPLACED ("Water Heater 7+" is a real install)."""
    n = (type_name or "").strip()
    if RE_HVAC_BU.search(bu_name or ""):
        return "excluded"
    if RE_QA.search(n):
        return "qa"
    if RE_PLANNED.search(n):
        return "permit"
    if (RE_DRIVEBY.search(n) or RE_MAINT.search(n) or RE_EST.search(n)
            or (not install_bu and RE_OLDSYS.search(n))
            or RE_HVAC_TYPE.search(n) or RE_NEWCON.search(n)):
        return "excluded"
    if RE_RECALL.search(n) or RE_FINISH.search(n):
        return "recall"
    return "neutral"


# reason buckets, first match wins (type name + summary, html stripped)
REASONS = [
    ("Parts on order",        r"warranty part|parts? (on order|ordered|arriv|in\b)"
                              r"|waiting on|special order|back ?order|part instl"),
    ("Leak / drip",           r"leak|dripp|seep|water (damage|on|in)\b|flood"),
    ("No hot water / heater", r"no hot water|not heating|cold water|luke ?warm"
                              r"|pilot|error code|not work|stopped work"
                              r"|won'?t (light|ignite|stay|turn)"),
    ("Clog / drain / sewage", r"clog|back ?up|backing up|stoppage|sewage|sewer"
                              r"|drain|\bodor\b|smell"),
    ("Pressure / flow",       r"pressure|low flow|no water\b|\bprv\b|regulator"),
    ("Gas issue",             r"gas (leak|smell|line|valve)|\bco\b alarm"),
    ("Softener / RO / quality", r"soften|filtration|reverse osmosis|\br\.?o\.?\b"
                              r"|hard water|taste|water quality"),
    ("Fixture / faucet",      r"faucet|toilet|sink|disposal|shower|tub\b"
                              r"|valve|hose bib"),
    ("Noise / hammer",        r"noise|noisy|loud|rattl|vibrat|humming|banging"
                              r"|hammer"),
    ("Permit / inspection",   r"permit|inspect|code correction"),
    ("Goodwill / comfort",    r"inconvenience|goodwill|courtesy"),
]
REASONS = [(lbl, re.compile(rx, re.I)) for lbl, rx in REASONS]


def reason(type_name, summary, cat):
    txt = RE_TAGS.sub(" ", f"{type_name or ''} {summary or ''}")
    for label, rx in REASONS:
        if rx.search(txt):
            return label
    return "Other / unspecified"


# equipment category from the install job-type name (tankless before the
# generic water-heater match)
EQUIP = [("Tankless WH", r"tankless"), ("Tanked WH", r"water heater"),
         ("Softener / RO", r"soften|filtration|reverse osmosis|conditioner"
                           r"|water quality|water treat"),
         ("Repipe", r"repipe"), ("Sewer / Main Line", r"sewer|main line"),
         ("Gas Line", r"gas line"), ("Toilet", r"toilet"),
         ("Sink / Faucet / Disposal", r"sink|faucet|disposal"),
         ("Part / Repair Install", r"part install")]
EQUIP = [(lbl, re.compile(rx, re.I)) for lbl, rx in EQUIP]


def equip_cat(type_name):
    for label, rx in EQUIP:
        if rx.search(type_name or ""):
            return label
    return "Other"


# ---------------------------------------------------------------- fetch
def month_events(company, year, month, idx):
    """Classified + linked events for jobs COMPLETED in the month.

    Mutates idx with this month's installs. Returns
    {"installs": [...], "callbacks": [...], "qa": n, "drywall": n}
    ("drywall" holds the planned permit/drywall/job-walk visits so the
    shared aggregate() applies unchanged).
    """
    co = COMPANIES[company]
    tenant, bu, inst_re = co["tenant"], co["bu"], co["instRe"]
    start, end = month_window_utc(co["tz"], year, month)
    jt, bus = job_types(tenant), bu_names(tenant)

    jobs = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/jobs",
                     {"completedOnOrAfter": start, "completedBefore": end,
                      "jobStatus": "Completed"},
                     page_size=500, max_pages=400)
    # Canceled jobs KEEP their completedOn; jobStatus filter verified
    # honored server-side, but this endpoint ignores other filters.
    jobs = [j for j in jobs if j.get("jobStatus") == "Completed"]
    jobs.sort(key=lambda j: j.get("completedOn") or "")
    quoted = _estimate_job_ids(tenant, start, end)

    def is_cohort(j, tname):
        return (j.get("businessUnitId") == bu
                and float(j.get("total") or 0) > 0
                and classify(tname, bus.get(bu), install_bu=True) == "neutral"
                and (inst_re is None or inst_re.search(tname or "")))

    # pass 1: cohort installs into the index, so same-month callbacks link
    installs, appt_of_inst = [], {}
    for j in jobs:
        tname = jt.get(j.get("jobTypeId"))
        if is_cohort(j, tname):
            rec = {"i": j["id"], "d": _day(j.get("completedOn")),
                   "loc": j.get("locationId"), "proj": j.get("projectId"),
                   "t": round(float(j["total"]), 2), "eq": equip_cat(tname),
                   "tc": []}
            installs.append(rec)
            appt_of_inst[j["id"]] = [a for a in (j.get("firstAppointmentId"),
                                                 j.get("lastAppointmentId")) if a]
    index_installs(idx, installs)

    # pass 2: callbacks
    callbacks, appt_of_cb = [], {}
    qa = planned = 0
    for j in jobs:
        tname = jt.get(j.get("jobTypeId"))
        jbu = j.get("businessUnitId")
        cat = classify(tname, bus.get(jbu), install_bu=jbu == bu)
        if cat == "excluded":
            continue
        if cat == "qa":
            qa += jbu == bu
            continue
        if cat == "permit":
            planned += jbu == bu
            continue
        if cat == "neutral" and is_cohort(j, tname):
            continue                      # cohort install, handled above

        ref = _day(j.get("createdOn")) or _day(j.get("completedOn"))
        d = _day(j.get("completedOn"))
        # RJ: a callback is a trip we ate the cost on. Billed = normal
        # service business. Recall-TYPED jobs are exempt (explicitly booked
        # as recalls, sometimes with billable warranty).
        free = float(j.get("total") or 0) == 0 or bool(j.get("noCharge"))
        if cat == "recall":
            orig = _link(idx, j.get("recallForId"), j.get("projectId"),
                         j.get("locationId"), ref, grace_days=3)
            # every callback must tie to an install no more than a year back
            if orig is None or not d:
                continue
            if (_parse(d) - _parse(orig["d"])).days > RECALL_MAX_GAP:
                continue
            bucket = "finish" if RE_FINISH_TY.search(tname or "") else "recall"
            src = bucket
        else:                             # neutral -> service-return candidate
            if not free:
                continue                  # billed visit = sold work, not ours
            if j["id"] in quoted:
                continue                  # tech quoted work = opportunity call
            if RE_TRADE_BU.search(bus.get(jbu) or ""):
                continue                  # other-trade demand work, not ours
            orig = _link(idx, None, j.get("projectId"), j.get("locationId"),
                         ref, grace_days=0)
            if orig is None or not d:
                continue
            gap = (_parse(d) - _parse(orig["d"])).days
            if gap < 1 or gap > SERVICE_MAX_GAP:
                continue
            bucket, src = "recall", "service"  # problem with our install

        cb = {"i": j["id"], "b": bucket, "s": src, "ty": tname or "?", "d": d,
              "rsn": reason(tname, j.get("summary"), cat),
              "oi": orig["i"], "om": orig["d"][:7],
              "gap": max(0, (_parse(d) - _parse(orig["d"])).days), "hrs": 0}
        callbacks.append(cb)
        appt_of_cb[j["id"]] = [a for a in (j.get("firstAppointmentId"),
                                           j.get("lastAppointmentId")) if a]

    _fill_crews(tenant, installs, appt_of_inst)
    _fill_hours(tenant, callbacks, appt_of_cb)
    return {"installs": installs, "callbacks": callbacks,
            "qa": qa, "drywall": planned}


def open_callbacks(company):
    """Typed recall jobs booked but not yet completed (created in the last
    OPEN_CB_LOOKBACK_DAYS) in ANY plumbing BU - unlike HVAC, most plumbing
    recalls are booked in the Service BU, not the install BU."""
    co = COMPANIES[company]
    since = (dt.datetime.utcnow()
             - dt.timedelta(days=OPEN_CB_LOOKBACK_DAYS)).strftime(
                 "%Y-%m-%dT00:00:00Z")
    jt, bus = job_types(co["tenant"]), bu_names(co["tenant"])
    n = 0
    for j in fetch_all(co["tenant"], "/jpm/v2/tenant/{tenant}/jobs",
                       {"createdOnOrAfter": since}, page_size=500,
                       max_pages=400):
        bn = bus.get(j.get("businessUnitId")) or ""
        if (RE_PLUMB_BU.search(bn)
                and j.get("jobStatus") not in ("Completed", "Canceled")
                and classify(jt.get(j.get("jobTypeId")), bn) == "recall"):
            n += 1
    return n


# ---------------------------------------------------------------- caching
def compute_company(company, deadline=None, progress=None):
    """{month_key: events} across the window, cached. Returns (months, complete).

    Identical mechanics to the HVAC board: oldest -> newest so the install
    index is complete when a month's callbacks link; budget-skipped months
    are written un-freezable (at=0) so they recompute with full context.
    """
    cache = _load_json(HISTORY_FILE, {}).get(company, {})
    months, today = _window_months(company)
    current_key = _month_key(today.year, today.month)
    idx = new_index()
    result, complete = {}, True
    ctx_gap = False               # a prior month was skipped this run

    for year, month in months:
        key = _month_key(year, month)
        entry = cache.get(key)
        if entry and entry.get("v") == CACHE_V and key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            frozen = entry.get("final") and (today - month_end).days >= MONTH_FREEZE_DAYS
            fresh = time.time() - entry.get("at", 0) < MONTH_RECHECK_HOURS * 3600
            if frozen or fresh:
                result[key] = entry["events"]
                index_installs(idx, entry["events"]["installs"])
                continue
        if deadline and time.time() > deadline and key != current_key:
            complete = False          # out of budget - next run resumes here
            ctx_gap = True
            if entry and entry.get("v") == CACHE_V:
                result[key] = entry["events"]
                index_installs(idx, entry["events"]["installs"])
            continue
        t0 = time.time()
        events = month_events(company, year, month, idx)
        result[key] = events
        if key != current_key:
            month_end = dt.date(year + (month == 12), month % 12 + 1, 1)
            update_history(HISTORY_FILE, company, key, {
                "at": 0 if ctx_gap else time.time(), "events": events,
                "v": CACHE_V,
                "final": (not ctx_gap
                          and (today - month_end).days >= MONTH_FREEZE_DAYS)})
        if progress:
            progress(company, key, time.time() - t0)
    return result, complete


def _window_months(company):
    """[(y, m)] oldest->newest: 18 closed months + the current month."""
    today = local_today(COMPANIES[company]["tz"])
    y, m = today.year, today.month
    out = []
    for _ in range(WINDOW_CLOSED_MONTHS + 1):
        out.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return list(reversed(out)), today


# ---------------------------------------------------------------- public API
def compute(time_budget_secs=None, progress=None):
    deadline = time.time() + time_budget_secs if time_budget_secs else None

    def one(company):
        months, ok = compute_company(company, deadline=deadline, progress=progress)
        try:
            open_cb = open_callbacks(company)
        except Exception as e:
            print(f"WARNING: {company} open-callback scan failed ({e})", flush=True)
            open_cb = None
        try:
            names = tech_names(COMPANIES[company]["tenant"])
        except Exception as e:
            print(f"WARNING: {company} technician fetch failed ({e})", flush=True)
            names = {}
        return months, ok, open_cb, names

    results = map_companies(one, COMPANIES)
    today = local_today("pacific")
    boards, complete = {}, True
    month_sets, open_total = {}, 0
    for company, (months, ok, open_cb, names) in results.items():
        complete = complete and ok
        month_sets[company] = months
        open_total += open_cb or 0
        agg = aggregate(months, today)
        for r in agg["recent"]:
            r["co"] = COMPANIES[company]["label"]
        boards[company] = dict(
            agg, kpis=_kpis(agg, open_cb, today),
            crew=crew_rows(months, names, today, COMPANIES[company]["label"]))

    combined = aggregate(_merge_months(month_sets), today)
    combined["recent"] = sorted(
        (r for c in COMPANIES for r in boards[c]["recent"]),
        key=lambda r: r["date"] or "", reverse=True)[:RECENT_LIMIT]
    boards["combined"] = dict(
        combined, kpis=_kpis(combined, open_total, today),
        crew=sorted((r for c in COMPANIES for r in boards[c]["crew"]),
                    key=lambda r: (-r["rate90"], -r["inst"])))

    return {
        "updated": dt.datetime.now().strftime("%a %b %d %Y %H:%M:%S"),
        "complete": complete,
        "today": today.isoformat(),
        "companies": {c: {"label": co["label"], "color": co["color"]}
                      for c, co in COMPANIES.items()},
        "windows": list(WINDOWS),
        "boards": boards,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 2:
        company, ym = sys.argv[1], sys.argv[2]
        y, m = map(int, ym.split("-"))
        ev = month_events(company, y, m, new_index())
        rec = sum(1 for c in ev["callbacks"] if c["b"] == "recall")
        fin = len(ev["callbacks"]) - rec
        lk = sum(1 for c in ev["callbacks"] if c.get("oi") is not None)
        print(f"{company} {ym}: {len(ev['installs'])} installs, "
              f"{len(ev['callbacks'])} callbacks ({rec} recall/warr, {fin} finish, "
              f"{lk} linked), qa {ev['qa']}, planned {ev['drywall']}")
        for c in ev["callbacks"][:20]:
            print(f"  {c['d']} {c['b']:7s} gap={c['gap']} {c['rsn']:22s} {c['ty']}")
    else:
        t0 = time.time()
        data = compute(progress=lambda co, k, s: print(f"  {co} {k} {s:.1f}s",
                                                       flush=True))
        for c, b in data["boards"].items():
            k = b["kpis"]
            print(f"{c:9s} 30d {k['rate30']:4.1f}%  90d {k['rate90']:4.1f}%  "
                  f"180d {k['rate180']:4.1f}%  visits/100 {k['visitsPer100']:5.1f}  "
                  f"hrs/100 {k['hrsPer100']:4d}  open {k['openCallbacks']}")
        print(f"-- computed in {time.time() - t0:.0f}s "
              f"(complete={data['complete']})")
