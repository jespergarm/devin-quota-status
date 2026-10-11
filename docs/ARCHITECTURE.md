# Architecture

Single-file tkinter app (`devin_usage_widget.pyw`, ~800 lines), stdlib only.
No dependencies, no build step. Windows-only (uses `msvcrt`, `ctypes` Win32,
`winsound`).

## Data flow

```
%APPDATA%\devin\cli\sessions.db          (WAL SQLite, read-only)
%APPDATA%\devin\cli\session_locks\*.lock (running-session probes)
%LOCALAPPDATA%\devin\cli\model_configs_v5.*.bin (pricing/promo, protobuf)
        │
        ▼  every POLL_MS (2 s)
UsageDB.running_stats()
   ├─ refresh_pricing()     reload model config if mtime changed
   ├─ active_session_ids() lock-probe, fallback last_activity_at < 10 min
   ├─ sessions_info()      title/model/credits per session
   └─ get_stats()          incremental row_id cursor per session →
                           per-turn tokens, cost, free_tokens
        │
        ▼
Widget._tick()
   ├─ UsageLogger.log()    append data\usage_log.csv on change (≥60 s apart)
   ├─ _check_warn()        cost threshold + free→paid blink/beep
   ├─ _rebuild_table() / _update_rows()
   └─ _fit_height(), _maybe_save_pos() → data\state.json
```

## Module map

| Block | What it does |
|-------|--------------|
| `_varint`, `_fields` | Minimal protobuf wire decoder for the model config |
| `load_pricing()` | Parse `model_configs_v5.*.bin` → pricing, names, promo ends, priced ids |
| `running_session_ids()` | `msvcrt.locking` probe of `session_locks\*.lock` |
| `connect_db`, `UsageDB` | Read-only SQLite access; incremental per-session accumulators (`_acc[sid]["last_row_id"]` + `seen_msgs` dedup set — the CLI stores each turn multiple times), cost per turn, free→paid diffing (`newly_priced`) |
| `fmt_*`, `trunc`, `ToolTip` | Display helpers |
| `collect_stats`, `dump` | Headless path for `--dump` |
| `UsageLogger` | CSV append on change, max once per `LOG_MIN_INTERVAL` |
| `Widget` | Table rows + expandable detail lines, dark titlebar, position persistence, warn/blink logic, 2 s tick |
| `_single_instance` | Named mutex `Local\DevinUsageWidget`; second launch raises the existing window |

## Key behaviors

- **Billing rule:** a turn is billed iff its `generation_model` has token
  prices in the live config **and** the turn ran after the model's promo
  end (promo ends persist in `state.json` since the config drops the
  marker once a model goes paid); otherwise tokens accumulate as
  `free_tokens`.
  See [DATA-SOURCES.md](DATA-SOURCES.md#billing-rule-used-by-the-widget).
- **Free→paid detection:** `refresh_pricing()` diffs the priced-id set on
  every config reload. Newly priced ids → `db.newly_priced` → affected rows'
  Model cell blinks red (toggled each poll via `_blink_phase`), one beep,
  footer shows `no longer free: <names>`. Click the row to acknowledge.
- **Cost warning:** a session's Cost cell turns red at `COST_WARN_USD`
  ($1.00) with a one-time beep (`BEEP_ON_WARN`).
- **Currency:** all math is in USD (`cost_usd` everywhere incl. the CSV
  log). Only the *display* converts to DKK — `dkk_rate()` fetches the daily
  ECB USD→DKK rate from frankfurter.dev once per day (cached as `usd_dkk`
  in `state.json`), falling back to the cached rate or
  `USD_TO_DKK_FALLBACK` when offline. `fmt_cost()` renders `x.xx kr`.
- **State:** window position saved on every move (survives hard kills) to
  `data\state.json`. `data\` sits next to the script because MSIX Store
  Python virtualizes `%LOCALAPPDATA%` writes.
- **Sort order:** sessions sorted by `last_activity_at` desc; the most
  recently active row is highlighted in `ACCENT`.

## File layout

```
devin_usage_widget.pyw   the app (GUI + --dump)
extension\               the status-bar extension (extension.js + quota.py)
build_vsix.py            stdlib-only VSIX packager for the extension
data\state.json          window position (gitignored)
data\usage_log.csv       append-only usage log (gitignored)
docs\                    this documentation
```
