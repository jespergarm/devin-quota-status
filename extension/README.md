# Devin Quota Status

Shows the live Devin account quota in the status bar (bottom-right):

`$(pulse) Day 11% · Week 6%`

Polls `api.devin.ai/{org}/billing/quota/usage` via a small stdlib-only
Python helper (`quota.py`) that reuses the Devin Desktop app's own
session token (read from `state.vscdb`, never leaves the machine).

- Click the status item to refresh immediately.
- Tooltip shows reset times (local), extra-usage balance and last
  update time. Turns yellow at >= `warnPercent` and red at 100%.
- A warning notification pops once per scope (day/week) when a quota
  crosses `warnPercent`; at 100% it becomes an error notification.
  Re-arms when the quota drops back below the threshold (after reset).
- After each successful fetch, `quota.py` appends the percentages to a
  CSV time series — one row whenever the daily/weekly pct changes, or
  hourly otherwise. Columns: `timestamp_iso, daily_pct, weekly_pct,
  daily_reset_at, weekly_reset_at, overage_balance`.

## Requirements

- Devin Desktop (uses its session token and extension host)
- Any Python 3 (stdlib only — no packages to install)

## Settings

- `devinQuota.pythonPath` (default empty = auto-detect) — Python 3
  interpreter for `quota.py`. Auto-detect tries `python`, `python3`,
  `py` on PATH, then common install dirs. The Windows Store `python`
  alias stub is ignored. Set explicitly if auto-detect picks the wrong
  interpreter.
- `devinQuota.intervalSeconds` (default `30`, min `10`) — poll interval
  (also refreshes on window focus)
- `devinQuota.warnPercent` (default `90`) — warn when daily or weekly
  quota reaches this %
- `devinQuota.warnEnabled` (default `true`) — enable warning
  notifications
- `devinQuota.logPath` (default empty = log to the extension's global
  storage as `quota_pct_log.csv`) — CSV path for the quota-percentage
  time series; `off` disables logging
