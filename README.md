# Devin Quota Status

Unofficial Windows utilities for seeing **live Devin account quota** before
a session pauses:

- **Quota strip** — a tiny always-on-top `Day X% · Week Y%` strip with
  reset times. Recommended for current Devin Desktop builds.
- **Devin Desktop extension** — the original status-bar integration for
  Devin Desktop builds that still show extension status-bar items.
- **Usage analyzer** — a tkinter window showing running Devin CLI sessions,
  token usage, estimated list-price cost, and per-model quota-day/week
  breakdowns.

> **Unofficial** — not affiliated with Cognition. These tools rely on
> undocumented Devin Desktop/CLI internals, so a Devin update can break
> them.

## Quick start: quota strip

1. Install Python 3 for Windows. No third-party Python packages are needed.
2. Download and extract the latest release, or clone this repository.
3. Double-click `devin_quota_widget.pyw` (or run it with `pythonw`).

The strip appears near the top of the screen while Devin is running. Drag
it with the left mouse button. Right-click it for **Refresh** or **Exit**.

To start it automatically at logon, review and then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\register_widget_task.ps1
```

The script only registers a per-user scheduled task; it does not install a
service or require administrator rights.

## Devin Desktop status-bar extension

For Devin Desktop versions that still expose the extension status bar:

1. Download `devin-quota-status-<version>.vsix` from
   [Releases](../../releases).
2. Install it:

   ```bat
   "%LOCALAPPDATA%\Programs\Devin\bin\devin-desktop.cmd" --install-extension devin-quota-status-<version>.vsix
   ```

3. `Ctrl+Shift+P` → `Developer: Reload Window`.

The item shows `Day N% · Week N%`; click to refresh. It turns yellow at the
warning threshold (default 90%) and red at 100%.

## Usage analyzer

```bat
python devin_usage_widget.pyw          # always-on-top window
python devin_usage_widget.pyw --dump   # print the table and exit
```

This reads Devin CLI's local `sessions.db` read-only and estimates cost at
list price using the daily ECB USD→DKK rate. Cost figures are estimates,
not invoices.

## Privacy and credential handling

The quota helper reads Devin Desktop's existing session token from the
local `state.vscdb` database. The token is:

- used only as a bearer token for Devin's own endpoints
  (`app.devin.ai` and `api.devin.ai`),
- never written to disk by this project,
- never printed to the terminal or shown in the UI,
- never sent to a third-party service.

The only non-Devin network call is an unauthenticated exchange-rate lookup
at `api.frankfurter.dev` used by the optional cost display.

Runtime files are written under `data\` next to the scripts and are ignored
by git. They can include quota history, org id, and aggregate token/cost usage
in CSV logs. They are not uploaded anywhere, but you should not share
that folder. Delete `data\` if you want to remove the
local history.

See [SECURITY.md](SECURITY.md) before publishing logs, screenshots, or bug
reports.

## Build the VSIX

No npm/vsce dependency is required — `build_vsix.py` zips the extension
with Python's standard library:

```bat
python build_vsix.py
```

## Documentation

- [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md) — reverse-engineered local
  data formats and quota endpoint notes.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — data flow and file layout.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — debugging recipes.

## Caveats

- Windows-only for the widgets and scheduled-task script.
- Relies on undocumented internals (`state.vscdb`, quota endpoint,
  `sessions.db` schema, model-config protobuf). If something breaks, run
  `python devin_usage_widget.pyw --dump` first.
- Cost and quota-correlation figures are reverse-engineered estimates, not
  billing or invoice data.

## License

MIT — see [LICENSE](LICENSE).
