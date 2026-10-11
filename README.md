# Devin Quota Status

See your **live Devin account quota** at a glance — daily and weekly
percentages right in the Devin Desktop status bar:

`$(pulse) Day 11% (Fri 16) · Week 6% (Sat 16)`

No more wondering why a session paused: yellow at your warning
threshold, red at 100%, with the reset time shown inline.

> **Unofficial** — not affiliated with Cognition. Works by polling the
> same `api.devin.ai/{org}/billing/quota/usage` endpoint the app uses,
> via a tiny stdlib-only Python helper that reuses Devin Desktop's own
> session token (read from `state.vscdb`; it never leaves your machine).

## Features

- Status-bar item: `Day N% · Week N%` with reset times; click to refresh.
- Tooltip: reset times (local), extra-usage balance, last update.
- Colors: yellow at `warnPercent` (default 90), red at 100%.
- One notification per quota scope when it crosses the threshold
  (error notification at 100%); re-arms after reset.
- Optional CSV time series of the percentages — one row on change or
  hourly (great for calibrating "how many tokens is 1% of quota").
- Polls every `intervalSeconds` (default 30) and on window focus.

## Install

1. Grab `devin-quota-status-<version>.vsix` from
   [Releases](../../releases) (or build it — below).
2. Install into Devin Desktop:

   ```bat
   "%LOCALAPPDATA%\Programs\Devin\bin\devin-desktop.cmd" --install-extension devin-quota-status-<version>.vsix
   ```

3. `Ctrl+Shift+P` → `Developer: Reload Window`.

**Requirements:** Devin Desktop (Windows) + any Python 3 (stdlib only —
no packages). The extension auto-detects the interpreter
(`devinQuota.pythonPath` overrides).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `devinQuota.pythonPath` | `""` (auto-detect) | Python 3 for `quota.py` |
| `devinQuota.intervalSeconds` | `30` | Poll interval (min 10) |
| `devinQuota.warnPercent` | `90` | Warning threshold |
| `devinQuota.warnEnabled` | `true` | Warning notifications |
| `devinQuota.logPath` | `""` (global storage) | CSV path, or `off` |

## Build the VSIX

No npm/vsce needed — `build_vsix.py` zips it with Python's `zipfile`:

```bat
python build_vsix.py
```

## Bonus: usage widget

`devin_usage_widget.pyw` is a standalone stdlib-only tkinter tool that
reads Devin CLI's `sessions.db` and shows per-session billed tokens and
estimated cost (list price × daily ECB USD→DKK rate), plus per-model
quota-day/week breakdowns:

```bat
python devin_usage_widget.pyw          # always-on-top window
python devin_usage_widget.pyw --dump   # print the table and exit
```

Windows-only, no dependencies. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how it works.

## How it works / docs

- [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md) — reverse-engineered
  `sessions.db` schema, session-lock probing, `model_configs_v5.*.bin`
  protobuf layout, quota endpoint.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — data flow, billing
  rule, file layout.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — debugging recipes.

## Caveats

- Relies on **undocumented internals** of Devin Desktop/CLI
  (`state.vscdb` token, quota endpoint, `sessions.db` schema). A Devin
  update can break it — `--dump` is the first thing to run.
- Cost figures are **list-price estimates**, not your invoice.

## License

MIT — see [LICENSE](LICENSE).
