"""Minimal Devin quota strip — a borderless single-line text bar pinned
topmost, showing "Day X% (reset) · Week Y% (reset)" exactly like the old
status-bar item. Visible only while Devin.exe is running.

Polls quota.py every POLL_SEC seconds in a background thread.
Drag with left mouse to move. Right-click: Refresh / Exit.
"""

import ctypes
import json
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path

def _python_for_helper():
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        candidate = str(Path(exe).with_name("python.exe"))
        if Path(candidate).exists():
            return candidate
    return exe


PY = _python_for_helper()
QUOTA_PY = str(Path(__file__).resolve().parent / "quota.py")
STORE_DIR = str(Path(__file__).resolve().parent / "data")
LOG_CSV = str(Path(STORE_DIR) / "quota_pct_log.csv")

POLL_SEC = 300
DEVIN_CHECK_SEC = 15
BG = "#1e1e1e"
FG = "#d4d4d4"
DIM = "#808080"
WARN = "#d7a62f"
ERR = "#f44747"
FONT = ("Consolas", 10)
WDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday",
         "Thursday", "Friday", "Saturday"]
PS_QUERY = (0x0400 | 0x0010)  # QUERY_INFORMATION | QUERY_LIMITED...


def devin_running():
    """True while any Devin.exe process exists (cheap psapi check)."""
    procs = (ctypes.c_ulong * 1024)()
    needed = ctypes.c_ulong()
    if not ctypes.windll.psapi.EnumProcesses(
            procs, ctypes.sizeof(procs), ctypes.byref(needed)):
        return True  # can't tell -> assume running, stay visible
    buf = ctypes.create_unicode_buffer(260)
    for pid in procs[: needed.value // 4]:
        if not pid:
            continue
        h = ctypes.windll.kernel32.OpenProcess(PS_QUERY, False, pid)
        if not h:
            continue
        try:
            ctypes.windll.psapi.GetProcessImageFileNameW(
                h, buf, 260)
            if buf.value.lower().endswith("devin.exe"):
                return True
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    return False


def fetch_quota():
    try:
        out = subprocess.run(
            [PY, QUOTA_PY, STORE_DIR, LOG_CSV],
            capture_output=True, text=True, timeout=60,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        q = json.loads(out.stdout.strip())
        return None if q.get("error") else q
    except Exception:
        return None


def fmt_reset(iso):
    try:
        d = datetime.fromisoformat(iso).astimezone()
        return f"{WDAYS[int(d.strftime('%w'))]} {d.hour:02d}:00"
    except Exception:
        return "?"


class Widget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)       # no title bar / borders
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)

        self.lbl = tk.Label(self.root, bg=BG, fg=FG, font=FONT,
                            padx=6, pady=1)
        self.lbl.pack()

        self.lbl.bind("<Button-1>", self._drag_start)
        self.lbl.bind("<B1-Motion>", self._drag)
        self.root.bind("<Double-Button-1>", lambda e: self.refresh())

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Refresh", command=self.refresh)
        menu.add_separator()
        menu.add_command(label="Exit", command=self.root.destroy)
        self.lbl.bind("<Button-3>",
                      lambda e: menu.tk_popup(e.x_root, e.y_root))
        self.menu = menu

        self._dx = self._dy = 0
        self._after_id = None
        self._last_good = None
        self.lbl.config(text="Devin quota ...")
        # default: middle of Devin's menu bar (drag to adjust)
        self.root.geometry("+1100+22")
        self.refresh()
        self._devin_check()

    def _drag_start(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag(self, e):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        ww = self.root.winfo_width() or 1
        wh = self.root.winfo_height() or 1
        x = min(max(e.x_root - self._dx, 0), max(sw - ww, 0))
        y = min(max(e.y_root - self._dy, 0), max(sh - wh, 0))
        self.root.geometry(f"+{x}+{y}")

    def _devin_check(self):
        if devin_running():
            self.root.deiconify()
        else:
            self.root.withdraw()
        self.root.after(DEVIN_CHECK_SEC * 1000, self._devin_check)

    def refresh(self):
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        q = fetch_quota()
        self.root.after(0, self._show, q)

    def _show(self, q):
        if not q:
            if self._last_good:
                q = self._last_good
            else:
                self.lbl.config(text="Devin quota: n/a", fg=ERR)
                self._rearm()
                return
        else:
            self._last_good = q
        d, w = q["daily_pct"], q["weekly_pct"]
        color = FG
        if d >= 100 or w >= 100:
            color = ERR
        elif d >= 90 or w >= 90:
            color = WARN
        self.lbl.config(
            text=f"Day {d:.0f}% ({fmt_reset(q['daily_reset_at'])})"
                 f"  ·  Week {w:.0f}% ({fmt_reset(q['weekly_reset_at'])})",
            fg=color)
        self._rearm()

    def _rearm(self):
        if self._after_id:
            self.root.after_cancel(self._after_id)
        self._after_id = self.root.after(POLL_SEC * 1000, self.refresh)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Widget().run()
