#!/usr/bin/env python3
"""
Live ServiceTitan engine for the 4 Day Call board.

Five boards, each a (company, trade) pair scoped to specific business units:

    sierra-hvac        SIE   HVAC - Service, HVAC - Maintenance
    sierra-plumbing    SIE   Plumbing - Service, Plumbing - Maintenance, Plumbing - Drains
    ultimate-hvac      ULT   HVAC - Service, HVAC - Maintenance
    ultimate-plumbing  ULT   Plumbing - Service, Plumbing - Maintenance
    russett-hvac       RUS   HVAC - Service, HVAC - Maintenance

Per board, for each of the next 4 weekdays (starting today, company-local):
  - opps / nonOpps = calls on board (jobs with an appointment that day, not
    canceled, in the board's BUs) split by ServiceTitan's PER-JOB Opportunity
    flag. That flag is only exposed through the Reporting API, so each day is
    joined to a jobs report by job number (report "Number"/"JobNumber" ==
    jpm jobNumber; verified 133/133 on 2026-07-10). Where a tenant's report
    does not (yet) have the Opportunity column, the engine falls back to the
    job-type class (Demand / Marketed Tune Up / System Check = opportunity)
    and marks the day "oppSource": "class" so the dashboard can flag it.
  - ropps = calls on board carrying the board's replacement-opp tag, excluding
    jobs that also carry the Management Removed ROPP tag. HVAC boards use the
    ROPP tag; plumbing boards use TROP (Sierra) / T-ROPP (Ultimate).

Techs available / calls needed / replacement opps needed are manual inputs
entered on the dashboard itself (shared through the Apps Script budget store);
they are not computed here.

CLI smoke test:
    py build/call_board_live.py                # all boards
    py build/call_board_live.py sierra-hvac    # one board (still fetches its tenant)
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from servicetitan_client import st_post
from command_center_live import fetch_all, local_day_window_utc, local_today, _load_json, _save_json

CONFIG_CACHE = os.path.join(ROOT, "data", "call-board-st-config.json")
CONFIG_TTL_HOURS = 24 * 7   # job-type class cache
REPORT_META_TTL_HOURS = 6   # re-check report schemas often enough to pick up new columns
BOARD_DAYS = 4

# Fallback when a tenant's report has no per-job Opportunity column yet.
OPPORTUNITY_CLASSES = {"Demand", "Marketed Tune Up", "System Check"}

COMPANIES = {
    "sierra": {
        "tenant": "SIE", "tz": "pacific",
        "name": "Sierra Air Conditioning & Plumbing", "short": "Sierra",
        # "HVAC Total Call Count_Claude_RJ" - has the per-job Opportunity column
        "report": {"category": "operations", "id": 645181053, "number_field": "Number"},
        "weather": {"lat": 36.17, "lon": -115.14, "tz": "America/Los_Angeles"},  # Las Vegas
    },
    "ultimate": {
        "tenant": "ULT", "tz": "mountain",
        "name": "Ultimate Heating Air & Plumbing", "short": "Ultimate",
        # "Upcoming Jobs Report_Claude_Report_Ultimate" - engine starts using its
        # Opportunity column automatically once RJ adds it in the report builder
        "report": {"category": "operations", "id": 136063716, "number_field": "JobNumber"},
        "weather": {"lat": 43.615, "lon": -116.202, "tz": "America/Boise"},      # Boise
    },
    "russett": {
        "tenant": "RUS", "tz": "arizona",
        "name": "Russett Southwest", "short": "Russett",
        # "Upcoming Jobs Report_Claude_Report_RSW" - same: add Opportunity to activate
        "report": {"category": "operations", "id": 174608850, "number_field": "JobNumber"},
        "weather": {"lat": 32.222, "lon": -110.975, "tz": "America/Phoenix"},    # Tucson
    },
}

BOARDS = {
    "sierra-hvac": {
        "company": "sierra", "trade": "HVAC",
        "title": "HVAC - Service & Maintenance",
        "bus": [333, 342817560],
        "ropp_tags": [962027],             # "ROPP"
        "ropp_removed_tags": [545867780],  # "Management Removed ROPP"
    },
    "sierra-plumbing": {
        "company": "sierra", "trade": "Plumbing",
        "title": "Plumbing - Service, Maintenance & Drains",
        "bus": [353, 354, 595105985],
        "ropp_tags": [396774589],          # "TROP" (Sierra's plumbing replacement-opp tag)
        "ropp_removed_tags": [545867780],
    },
    "ultimate-hvac": {
        "company": "ultimate", "trade": "HVAC",
        "title": "HVAC - Service & Maintenance",
        "bus": [2691, 2692],
        "ropp_tags": [52206586],           # "ROPP" (not "Possible ROPP")
        "ropp_removed_tags": [],           # tenant has no Management Removed ROPP tag
    },
    "ultimate-plumbing": {
        "company": "ultimate", "trade": "Plumbing",
        "title": "Plumbing - Service & Maintenance",
        "bus": [8450, 128196],
        "ropp_tags": [79756058],           # "T-ROPP" (not "Possible TROPP")
        "ropp_removed_tags": [],
    },
    "russett-hvac": {
        "company": "russett", "trade": "HVAC",
        "title": "HVAC - Service & Maintenance",
        "bus": [221, 53208412],
        "ropp_tags": [63640008],           # "ROPP"
        "ropp_removed_tags": [],
    },
}


def board_days(tz, n=BOARD_DAYS):
    """The next n weekdays starting today (company-local); weekends skipped."""
    days, d = [], local_today(tz)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def job_type_classes(tenant):
    """{job_type_id: class} cached on disk (job types rarely change)."""
    cache = _load_json(CONFIG_CACHE, {})
    entry = cache.get(f"{tenant}:classes")
    if entry and time.time() - entry.get("at", 0) < CONFIG_TTL_HOURS * 3600:
        return {int(k): v for k, v in entry["classes"].items()}
    classes = {str(j["id"]): (j.get("class") or "") for j in
               fetch_all(tenant, "/jpm/v2/tenant/{tenant}/job-types",
                         {"active": "Any"}, page_size=100)}
    cache[f"{tenant}:classes"] = {"at": time.time(), "classes": classes}
    _save_json(CONFIG_CACHE, cache)
    return {int(k): v for k, v in classes.items()}


# ------------------------------------------------------------- reporting API
def _report_request(tenant, method, path, json_body=None, params=None, retries=6):
    """The Reporting API is aggressively rate limited; honor its retry hints."""
    from servicetitan_client import st_get
    for attempt in range(retries):
        try:
            if method == "GET":
                return st_get(tenant, path, params=params)
            return st_post(tenant, path, json_body=json_body, params=params)
        except RuntimeError as e:
            msg = str(e)
            if "429" not in msg:
                raise
            m = re.search(r"[Tt]ry again in (\d+) seconds", msg)
            wait = int(m.group(1)) + 2 if m else 20 * (attempt + 1)
            print(f"  reporting 429 for {tenant}, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"Reporting API still rate limited after {retries} tries ({tenant} {path})")


def _report_fields(company):
    """Field names of the tenant's jobs report, cached briefly so a newly added
    Opportunity column is picked up without a code change."""
    co = COMPANIES[company]
    rep = co["report"]
    cache = _load_json(CONFIG_CACHE, {})
    key = f"{co['tenant']}:report:{rep['id']}"
    entry = cache.get(key)
    if entry and time.time() - entry.get("at", 0) < REPORT_META_TTL_HOURS * 3600:
        return entry["fields"]
    meta = _report_request(
        co["tenant"], "GET",
        f"/reporting/v2/tenant/{{tenant}}/report-category/{rep['category']}/reports/{rep['id']}")
    fields = [f["name"] for f in meta.get("fields", [])]
    cache[key] = {"at": time.time(), "fields": fields}
    _save_json(CONFIG_CACHE, cache)
    return fields


def _opportunity_field(fields):
    return next((f for f in fields if "opportunit" in f.lower()), None)


def report_opportunity_flags(company, day, bus):
    """{job_number(str): bool} from the tenant's jobs report for one day,
    or None when the report has no Opportunity column yet."""
    co = COMPANIES[company]
    rep = co["report"]
    fields = _report_fields(company)
    opp_field = _opportunity_field(fields)
    if not opp_field:
        return None
    body = {"parameters": [
        {"name": "DateType", "value": 3},               # Job Start Date
        {"name": "From", "value": day.isoformat()},
        {"name": "To", "value": day.isoformat()},
        {"name": "BusinessUnitId", "value": sorted(bus)},
    ]}
    r = _report_request(
        co["tenant"], "POST",
        f"/reporting/v2/tenant/{{tenant}}/report-category/{rep['category']}/reports/{rep['id']}/data",
        json_body=body, params={"pageSize": 5000})
    names = [f["name"] for f in r.get("fields", [])]
    try:
        i_num = names.index(rep["number_field"])
        i_opp = names.index(opp_field)
    except ValueError:
        return None
    return {str(row[i_num]): bool(row[i_opp]) for row in r.get("data", [])}


# ------------------------------------------------------------------ weather
def _weather(company):
    """Min/max forecast from Open-Meteo (free, no key). None on failure."""
    w = COMPANIES[company]["weather"]
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={w['lat']}&longitude={w['lon']}"
           "&daily=temperature_2m_max,temperature_2m_min,weather_code"
           "&temperature_unit=fahrenheit&forecast_days=10"
           f"&timezone={urllib.parse.quote(w['tz'])}")
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            d = json.load(resp)["daily"]
        return {date: {"min": round(mn), "max": round(mx), "code": code}
                for date, mn, mx, code in zip(d["time"], d["temperature_2m_min"],
                                              d["temperature_2m_max"], d["weather_code"])}
    except Exception as e:
        print(f"weather fetch failed for {company}: {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------- metric core
def _board_jobs(tenant, tz, day):
    """Non-canceled jobs with an appointment starting on the given local day."""
    start, end = local_day_window_utc(tz, day)
    jobs = fetch_all(tenant, "/jpm/v2/tenant/{tenant}/jobs",
                     {"appointmentStartsOnOrAfter": start, "appointmentStartsBefore": end})
    return [j for j in jobs if j.get("jobStatus") != "Canceled"]


def compute(only_board=None):
    wanted = {k: b for k, b in BOARDS.items() if only_board in (None, k)}
    if not wanted:
        raise ValueError(f"Unknown board '{only_board}'. Known: {', '.join(BOARDS)}")

    out = {"generatedAt": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
           "boards": {}, "weather": {}}

    companies = {b["company"] for b in wanted.values()}
    for company in companies:
        out["weather"][company] = _weather(company)

    # Per (tenant, day): one jpm pull and one report pull, shared by that
    # tenant's boards. Report BU filter = union of the tenant's board BUs.
    day_jobs, day_flags = {}, {}
    for company in companies:
        co = COMPANIES[company]
        tenant_bus = sorted({bu for k, b in wanted.items() if b["company"] == company for bu in b["bus"]})
        for day in board_days(co["tz"]):
            day_jobs[(company, day)] = _board_jobs(co["tenant"], co["tz"], day)
            try:
                day_flags[(company, day)] = report_opportunity_flags(company, day, tenant_bus)
            except Exception as e:
                print(f"report pull failed for {company} {day}: {e}", file=sys.stderr)
                day_flags[(company, day)] = None

    for key, b in wanted.items():
        company = b["company"]
        co = COMPANIES[company]
        classes = job_type_classes(co["tenant"])
        bus = set(b["bus"])
        ropp = set(b["ropp_tags"])
        removed = set(b["ropp_removed_tags"])
        days = []
        for day in board_days(co["tz"]):
            jobs = [j for j in day_jobs[(company, day)] if j.get("businessUnitId") in bus]
            flags = day_flags[(company, day)]
            opps = non_opps = ropps = from_report = 0
            for j in jobs:
                flag = flags.get(str(j.get("jobNumber"))) if flags else None
                if flag is None:
                    is_opp = classes.get(j.get("jobTypeId")) in OPPORTUNITY_CLASSES
                else:
                    is_opp = flag
                    from_report += 1
                if is_opp:
                    opps += 1
                else:
                    non_opps += 1
                tags = set(j.get("tagTypeIds") or [])
                if (ropp & tags) and not (removed & tags):
                    ropps += 1
            # a report that misses most of the board means the join is broken -
            # call it class-sourced so nobody trusts a half-applied flag silently
            if flags is None:
                src = "class"
            elif not jobs or from_report >= len(jobs) * 0.7:
                src = "job"
            else:
                src = "class"
            days.append({"date": day.isoformat(), "dow": day.strftime("%A"),
                         "opps": opps, "nonOpps": non_opps, "ropps": ropps,
                         "oppSource": src})
        out["boards"][key] = {
            "company": company, "companyName": co["name"], "companyShort": co["short"],
            "trade": b["trade"], "title": b["title"], "days": days,
        }
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    only = sys.argv[1] if len(sys.argv) > 1 else None
    t0 = time.time()
    print(json.dumps(compute(only), indent=2))
    print(f"-- computed in {time.time() - t0:.1f}s", file=sys.stderr)
