# InterMountain Dashboards

Live operations dashboards for the Redwood InterMountain group (Sierra, Ultimate,
Russett + Brothers/Pioneer credentials), computed straight from the **ServiceTitan
API** and hosted on GitHub Pages:

https://rjmageelvclaude-boop.github.io/intermountain-dashboards/

| Board | Path | What it shows |
|---|---|---|
| Command Center | `/command-center/` | Daily ops: revenue, sales, board counts, calls, TGLs, plumbing, MTD pacing (access-code gate) |
| 4 Day Call Board | `/call-board/` | Rotating live capacity boards for the next four days |
| Comfort Advisors | `/ca-board/` | CA leaderboards MTD/YTD + GOAT trip pacing |
| Tech Leaderboard | `/tech-board/` | HVAC service tech KPIs MTD/YTD |
| Plumber Leaderboard | `/plumb-board/` | Sierra + Ultimate plumber KPIs MTD/YTD |
| CSR Board | `/csr-board/` | Call-center KPIs Daily/MTD/YTD |
| Install Leaderboard | `/install-board/` | Install crew KPIs MTD/YTD |
| Silo Leaderboard | `/silo-board/` | Silo lead-gen tech KPIs MTD/YTD |
| GOAT Board | `/goat-board/` | All-department 2026 incentive-trip tracker |
| Install Callbacks | `/callback-board/` | HVAC install callback cohorts (30/60/90/180-day rates) |
| Install Callbacks — Plumbing | `/plumb-callback-board/` | Plumbing install callback cohorts, same methodology |

## How it works

```
.github/workflows/refresh.yml        every ~10 min (Apps Script dispatcher + cron)
      │
      ▼
build/<board>_refresh.py  ─►  site/<board>/data.json  ─►  site/<board>/index.html
(8 boards in parallel,          one JSON per board         static page, reloads
 GOAT last - it reads                                      itself every 5 min
 the other boards' output)
```

- `build/servicetitan_client.py` — thread-safe API client: pooled keep-alive
  connections, gzip, token caching, 429/5xx retry with backoff. Creds live in
  `secrets/servicetitan.json` (git-ignored; in CI from the `ST_SECRETS_JSON` secret).
- `build/command_center_live.py` — shared helpers (`fetch_all` concurrent paging,
  timezone windows, JSON caches) + the Command Center engine.
- `build/<board>_live.py` — each board's metric engine. Closed months are cached in
  `data/<board>-history.json` (persisted via the Actions cache) with per-run time
  budgets, so a normal run only recomputes the current month.

## Local use

Python via the `py` launcher (no third-party deps). Smoke tests:

```
py build/servicetitan_client.py SIE /crm/v2/tenant/{tenant}/customers?pageSize=1
py build/command_center_live.py sierra          # today's Command Center numbers
py build/tech_board_live.py sierra 2026-06      # one board, one company-month
```

Serve the site locally with `.claude/launch.json` (`hyperion-site`, port 8777).

## History

This repo originally hosted the Hyperion Club contest dashboard, fed by emailed
Enterprise Hub reports (Gmail → parser). That board was retired 2026-07-11 — the
plan is to rebuild the network-wide contest feed on the API once credentials
covering all Redwood tenants are available (the current apps cover only the five
InterMountain tenants).
