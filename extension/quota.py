"""Fetch live Devin quota percentages — helper for the Devin Quota Status
extension. Stdlib only. Prints exactly one JSON object to stdout and
exits 0 either way:

    {"daily_pct": .., "weekly_pct": .., "daily_reset_at": ..,
     "weekly_reset_at": .., "overage_balance": ..}
    or {"error": "<short message>"}

Usage: python quota.py <globalStorage_dir> [log_csv_path]
(org id is cached in <globalStorage_dir>/org_id.txt; the dir is created)

If a second argument is given it is the path of a CSV time-series log:
after a successful fetch one row is appended (timestamp_iso, daily_pct,
weekly_pct, daily_reset_at, weekly_reset_at, overage_balance) — only
when the percentages changed since the last row or the last row is
older than 3600 s. Missing/empty path = no logging.

Ported from devin_usage_widget.pyw (_read_session_token / _org_id /
_fetch_quota_bg / QuotaLogger seeding). The session token is read from
Devin Desktop's own state and never leaves the machine / is never
printed.
"""

import csv
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime

APPDATA = os.environ.get("APPDATA", "")
TIMEOUT = 10


def _read_session_token():
    """The Devin Desktop app's session token from its global state —
    never leaves the machine, used read-only for quota lookups."""
    try:
        db = os.path.join(APPDATA, "devin", "User", "globalStorage",
                          "state.vscdb")
        con = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro",
                              uri=True)
        row = con.execute("SELECT value FROM ItemTable WHERE "
                          "key='windsurfAuthStatus'").fetchone()
        con.close()
        return json.loads(row[0])["apiKey"] if row else None
    except Exception:
        return None


def _org_id(token, cache_dir):
    """Devin org id — cached in <cache_dir>/org_id.txt, else fetched once
    from app.devin.ai."""
    try:
        with open(os.path.join(cache_dir, "org_id.txt"),
                  encoding="utf-8") as f:
            org = f.read().strip()
            if org:
                return org
    except Exception:
        pass
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            "https://app.devin.ai/api/auth/windsurf/eligible-organizations",
            headers={"Authorization": "Bearer " + token}), timeout=TIMEOUT)
        orgs = json.loads(r.read())
        org = orgs[0]["org_id"] if orgs else None
        if org:
            os.makedirs(cache_dir, exist_ok=True)
            with open(os.path.join(cache_dir, "org_id.txt"), "w",
                      encoding="utf-8") as f:
                f.write(org)
        return org
    except Exception:
        return None


def _log_pct(path, q):
    """Append one row to the pct time-series CSV at *path*. Writes only
    when (daily_pct, weekly_pct) differ from the last row or the last
    row is older than 3600 s — same rule as the widget's QuotaLogger,
    seeded by reading the file's last line so each invocation is
    stateless. Any failure is silent (logging must never break the
    status-bar poll)."""
    try:
        if not path:
            return
        daily_pct = q.get("daily_percentage")
        weekly_pct = q.get("weekly_percentage")
        pcts = (str(daily_pct), str(weekly_pct))
        now = time.time()
        try:  # seed from the last row: skip if same pcts and fresh
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 65536))
                last = f.read().decode("utf-8", "replace") \
                    .strip().splitlines()[-1]
            row = next(csv.reader([last]))
            if (row[1], row[2]) == pcts:
                try:
                    last_ts = datetime.fromisoformat(row[0]).timestamp()
                except ValueError:
                    last_ts = os.path.getmtime(path)
                if now - last_ts < 3600:
                    return
        except Exception:
            pass
        new = not os.path.exists(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)),
                    exist_ok=True)
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp_iso", "daily_pct", "weekly_pct",
                            "daily_reset_at", "weekly_reset_at",
                            "overage_balance"])
            w.writerow([iso, daily_pct, weekly_pct,
                        q.get("daily_reset_at"),
                        q.get("weekly_reset_at"),
                        q.get("overage_balance")])
    except Exception:
        pass


def main():
    try:
        token = _read_session_token()
        if not token:
            raise RuntimeError("no session token in state.vscdb")
        org = _org_id(token, sys.argv[1] if len(sys.argv) > 1 else ".")
        if not org:
            raise RuntimeError("could not resolve org id")
        r = urllib.request.urlopen(urllib.request.Request(
            f"https://api.devin.ai/{org}/billing/quota/usage",
            headers={"Authorization": "Bearer " + token,
                     "x-cog-org-id": org}), timeout=TIMEOUT)
        q = json.loads(r.read())
        _log_pct(sys.argv[2] if len(sys.argv) > 2 else "", q)
        print(json.dumps({
            "daily_pct": q.get("daily_percentage"),
            "weekly_pct": q.get("weekly_percentage"),
            "daily_reset_at": q.get("daily_reset_at"),
            "weekly_reset_at": q.get("weekly_reset_at"),
            "overage_balance": q.get("overage_balance"),
        }))
    except Exception as e:
        print(json.dumps({"error": str(e)[:200]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
