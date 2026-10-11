# Development Notes

Gotchas, debugging recipes, and machine-specific wiring. Read
[ARCHITECTURE.md](ARCHITECTURE.md) for how the code is organized and
[DATA-SOURCES.md](DATA-SOURCES.md) for the reverse-engineered formats.

## Debugging

- `python devin_usage_widget.pyw --dump` — print the running-session table
  plus a JSON blob per session to stdout, then exit. First thing to run when
  the widget shows wrong numbers or nothing at all.
- `data\usage_log.csv` — history of per-session totals; useful for
  cross-checking what the widget displayed.

## tkinter / Win32 gotchas

- Never use `config(tip_text=...)` or any invented tk option — throws
  `TclError` inside `_build_row`, and since `_order` is set before rebuild
  the table stays permanently empty while the footer swallows the error.
  Store arbitrary strings as Python attributes (`w._tip_text`).
- ttk widgets don't take bg colors → all widgets are plain `tk` with
  explicit `bg`.
- Dark titlebar: `DwmSetWindowAttribute(hwnd, 20, 1)` (+ fallback 19),
  caption color attr 35, text color 36, then `withdraw()`/`deiconify()` to
  repaint.
- Single instance: `kernel32.CreateMutexW("Local\\DevinUsageWidget")`,
  `GetLastError()==183` → `FindWindowW(None, "Devin Usage")` +
  `ShowWindow(SW_RESTORE)` + `SetForegroundWindow`. The handle must stay
  referenced for process lifetime (`_MUTEX` global).
- `sessions.last_activity_at` is **seconds**, not ms.
- MSIX Store Python virtualizes `%LOCALAPPDATA%` writes — that's why state
  and log live in `data\` next to the script instead.

## Autostart (optional)

The widget no longer autostarts by default (the status-bar extension is
the live view). If you want it back: drop a shortcut to a console-less
launcher in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`,
or add a `SessionStart` hook that runs the `.pyw` via `pythonw`. The
named mutex keeps it single-instance, so multiple triggers are safe.

## Testing free→paid detection

`refresh_pricing()` diffs priced ids on every config reload; newly priced
ids land in `db.newly_priced` → Model cell blinks red + beep + footer
notice. Verified once by simulating `swe-2-high` gaining a price. To
re-test, monkeypatch `load_pricing()` to return an extra entry in
`priced_ids`.
