"""Devin CLI session usage widget v2.

Always-on-top dark tkinter window listing ALL currently running Devin CLI
sessions with token usage and estimated cost in DKK, read live from
%APPDATA%\\devin\\cli\\sessions.db (read-only; WAL mode) and
session_locks\\*.lock probes. Stdlib only. State and the usage log are
kept in a ``data\\`` subfolder next to this script (avoids the MSIX
LOCALAPPDATA redirection of Store Python builds).

Usage:
    python devin_usage_widget.pyw          # GUI
    python devin_usage_widget.pyw --dump   # print running-session table, exit
"""

import base64
import ctypes
import csv
import glob
import json
import msvcrt
import os
import sqlite3
import struct
import sys
import threading
import time
import tkinter as tk
import urllib.request
import winsound
from datetime import datetime, timezone

APPDATA = os.environ.get("APPDATA", "")
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
DB_PATH = os.path.join(APPDATA, "devin", "cli", "sessions.db")
LOCK_DIR = os.path.join(APPDATA, "devin", "cli", "session_locks")
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
LOG_PATH = os.path.join(STATE_DIR, "usage_log.csv")
QUOTA_LOG_PATH = os.path.join(STATE_DIR, "quota_log.csv")

POLL_MS = 2000
ACTIVE_FALLBACK_SEC = 600
LOG_MIN_INTERVAL = 60
WIN_W = 460
COST_WARN_USD = 1.00
BEEP_ON_WARN = True
USD_TO_DKK_FALLBACK = 6.85  # used only if no live/cached rate
FX_TTL_SEC = 86400          # re-fetch the USD->DKK rate once a day
MONTHLY_BUDGET_USD = 20.00  # Pro subscription, excl. 25% VAT
QUOTA_TTL_SEC = 30          # re-poll the account quota every 30 s
QUOTA_DAY_OFFSET = 8 * 3600  # quota days reset at 08:00 UTC
# Billing cycle starts on the 1st of the month at 09:26 UTC — verified
# against api.devin.ai /billing/subscription (current_period_start).

BG = "#1e1e1e"
FG = "#d4d4d4"
DIM = "#808080"
ACCENT = "#4ec9b0"
ERR = "#f44747"
FONT = ("Consolas", 9)

TOKEN_UIDS = ("input_tokens", "cached_input_tokens", "output_tokens")

# A model turn counts toward tokens/cost iff the live model config
# (model_configs_v5.*.bin) lists token prices for it — field 32 entries
# labelled "Input"/"Cached input"/"Output" — AND the turn ran after the
# model's promo/free period ended (field 19 promo end). Models without
# prices (SWE-2, compactor, summarizer, ...) and turns during a promo are
# free; their tokens are reported separately as free_tokens.
# The config is reloaded whenever the file's mtime changes.

# Promo ends observed in earlier configs. Once a model goes paid its
# field-19 marker disappears from the config, so known ends are seeded
# here and merged with anything the widget has observed (persisted in
# data\state.json under "promo_ends"). Values are unix seconds.
PROMO_END_SEED = {
    "glm-5-2": 1789541999,        # promo ended 2026-09-16 ~08:59 UTC
    "GLM-5.2 High": 1789541999,
    "swe-1-7": 1789541999,        # swe-1-7* promo ended same day
    "swe-1-7-medium": 1789541999,
    "swe-1-7-lightning": 1789541999,
    "swe-1-7-lightning-medium": 1789541999,
}


# ------------------------------------------------------------- pricing

def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]; i += 1
        r |= (c & 0x7f) << s; s += 7
        if c < 128:
            return r, i


def _fields(b):
    i = 0; out = []
    while i < len(b):
        k, i = _varint(b, i)
        f, w = k >> 3, k & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 1:
            v = b[i:i + 8]; i += 8
        elif w == 5:
            v = struct.unpack('<f', b[i:i + 4])[0]; i += 4
        elif w == 2:
            l, i = _varint(b, i); v = b[i:i + l]; i += l
        else:
            raise ValueError(w)
        out.append((f, v))
    return out


def _model_configs_path():
    files = glob.glob(os.path.join(
        LOCALAPPDATA, "devin", "cli", "model_configs_v5.*.bin"))
    return max(files, key=os.path.getmtime) if files else None


def load_pricing():
    """Returns (pricing, names, promo, priced_ids).

    pricing:    {model_id or display_name: {"Input": $/1M, "Cached input":,
                                            "Output":}}
    names:      {model_id: display_name}
    promo:      {model_id or display_name: unix_ts}  -- promo/free end time,
                field 19 -> sub-field 2 -> sub-field 1
    priced_ids: set of model ids that have token pricing (billable)
    """
    try:
        path = _model_configs_path()
        if not path:
            return {}, {}, {}, set()
        raw = base64.b64decode(
            json.load(open(path, encoding="utf-8"))["payload"])
        pricing = {}
        names = {}
        promo = {}
        priced_ids = set()
        for f, v in _fields(raw):
            if f != 1:
                continue
            name = mid = None; prices = {}; promo_ts = None
            for ff, vv in _fields(v):
                if ff == 1:
                    name = vv.decode()
                elif ff == 19:
                    try:
                        d19 = dict(_fields(vv))
                        if 2 in d19:
                            d2 = dict(_fields(d19[2]))
                            if 1 in d2:
                                promo_ts = int(d2[1])
                    except Exception:
                        pass
                elif ff == 22:
                    mid = vv.decode()
                elif ff == 32:
                    d = dict(_fields(vv))
                    if 1 in d and 2 in d:
                        prices[d[1].decode()] = float(d[2])
            if mid and name:
                names[mid] = name
            if mid and prices:
                priced_ids.add(mid)
            for key in (mid, name):
                if key and prices:
                    pricing[key] = prices
                if key and promo_ts:
                    promo[key] = promo_ts
        return pricing, names, promo, priced_ids
    except Exception:
        return {}, {}, {}, set()


# -------------------------------------------------------- running locks

def running_session_ids():
    out = set()
    try:
        names = os.listdir(LOCK_DIR)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".lock"):
            continue
        p = os.path.join(LOCK_DIR, n)
        try:
            h = open(p, "r+b")
        except OSError:
            out.add(n[:-5]); continue
        try:
            msvcrt.locking(h.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(h.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            out.add(n[:-5])
        finally:
            h.close()
    return out


# ----------------------------------------------------------------- db

def connect_db():
    uri = "file:" + DB_PATH.replace("\\", "/") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA busy_timeout = 1000")
    return con


class UsageDB:
    def __init__(self, pricing, names, promo, priced_ids):
        self.pricing = pricing
        self.names = names
        self.promo = promo
        self.priced_ids = priced_ids
        self.newly_priced = set()  # ids that went free -> paid after startup
        self.free_until = self._load_free_until(promo)
        self._save_promo_ends()
        self._cfg_path = _model_configs_path()
        self._cfg_mtime = (os.path.getmtime(self._cfg_path)
                           if self._cfg_path else None)
        self._cycle_start = _cycle_start_ts()
        self.con = None
        self._connect()
        self._acc = {}  # session_id -> accumulator dict
        # Backlog of sessions never accumulated — processed in the
        # background (scan_step) so the window paints immediately.
        self._scan_q = [r[0] for r in self._q(
            "SELECT id FROM sessions ORDER BY last_activity_at DESC")]

    def scan_step(self, n=4):
        """Accumulate a few backlog sessions. Returns True when done."""
        for sid in self._scan_q[:n]:
            self.accumulate(sid)
        del self._scan_q[:n]
        return not self._scan_q

    def scan_all(self):
        while self._scan_q:
            self.scan_step(len(self._scan_q))

    def _load_free_until(self, promo):
        """model -> promo-end unix ts. Merges the built-in seed, persisted
        observations (state.json "promo_ends") and the live config, so a
        promo end survives after the model goes paid — the config drops
        its field-19 marker once prices appear."""
        fu = dict(PROMO_END_SEED)
        try:
            st = json.load(open(STATE_PATH, encoding="utf-8"))
            for k, v in (st.get("promo_ends") or {}).items():
                fu[k] = max(fu.get(k, 0), v)
        except Exception:
            pass
        for k, v in promo.items():
            fu[k] = max(fu.get(k, 0), v)
        return fu

    def _save_promo_ends(self):
        try:
            st = {}
            try:
                st = json.load(open(STATE_PATH, encoding="utf-8"))
            except Exception:
                pass
            st["promo_ends"] = self.free_until
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(st, f)
        except Exception:
            pass

    def refresh_pricing(self):
        """Reload pricing/names if the model config file changed."""
        try:
            path = _model_configs_path()
            mtime = os.path.getmtime(path) if path else None
            if path and mtime and mtime != self._cfg_mtime:
                self._cfg_path, self._cfg_mtime = path, mtime
                pricing, names, promo, priced_ids = load_pricing()
                self.newly_priced |= priced_ids - self.priced_ids
                self.pricing, self.names, self.promo = pricing, names, promo
                self.priced_ids = priced_ids
                changed = False
                for k, v in promo.items():
                    if v > self.free_until.get(k, 0):
                        self.free_until[k] = v
                        changed = True
                if changed:
                    self._save_promo_ends()
        except OSError:
            pass

    def _connect(self):
        try:
            if self.con is not None:
                self.con.close()
        except Exception:
            pass
        self.con = connect_db()

    def _q(self, sql, params=()):
        try:
            return self.con.execute(sql, params).fetchall()
        except sqlite3.Error:
            self._connect()
            return self.con.execute(sql, params).fetchall()

    def active_session_ids(self):
        ids = running_session_ids()
        if ids:
            return ids
        cutoff = time.time() - ACTIVE_FALLBACK_SEC
        return {r[0] for r in self._q(
            "SELECT id FROM sessions WHERE hidden = 0 "
            "AND last_activity_at > ?", (cutoff,))}

    def sessions_info(self, ids):
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        out = {}
        for sid, title, model, la, meta in self._q(
                "SELECT id, title, model, last_activity_at, metadata "
                f"FROM sessions WHERE id IN ({marks})", tuple(ids)):
            credits = acus = None
            if meta:
                try:
                    m = json.loads(meta)
                    credits = m.get("total_credit_cost")
                    acus = m.get("total_acu_cost")
                except (ValueError, AttributeError):
                    pass
            out[sid] = {"title": title or sid, "model": model,
                        "last_activity_at": la or 0,
                        "credits": credits, "acus": acus}
        return out

    def _price_turn(self, gen_model, disp_model, itok, ctok, otok, ts):
        # gen_model is the actual serving model id — authoritative. The
        # display-name dimension is only a fallback for turns where gen is
        # missing; it reflects the session's selected model, which wrongly
        # prices internal turns (e.g. summarizer reporting the chat model).
        key = gen_model or disp_model
        p = self.pricing.get(key or "")
        if not p:
            return None
        free_until = self.free_until.get(key or "")
        if free_until and (ts is None or ts <= free_until):
            return None  # turn ran during the model's free/promo period
        return (itok * p.get("Input", 0) +
                ctok * p.get("Cached input", 0) +
                otok * p.get("Output", 0)) / 1e6

    def accumulate(self, sid):
        acc = self._acc.setdefault(sid, {
            "last_row_id": -1,
            "seen_msgs": set(),  # request_id/message_id already counted
            "totals": {u: 0.0 for u in TOKEN_UIDS},
            "messages": 0,
            "cost": 0.0,
            "cost_cycle": 0.0,  # cost of turns in the current billing cycle
            "free_tokens": 0.0,
            "last_gen": None,
            "model": None,
            # qday -> model_key -> [in, cached, out, usd, turns, free_tok]
            "days": {},
        })
        # The CLI re-writes each assistant turn on every sync (same
        # request_id/message_id, identical metrics; later copies add
        # metadata.extensions) — dedup so each turn counts once.
        # mn.created_at is the local *sync* time, not the turn time; the
        # real per-turn timestamp is metadata.created_at (ISO string).
        # Older turns lack response_dimensions entirely — use
        # metadata.metrics for those instead of dropping them.
        rows = self._q(
            "SELECT mn.row_id, "
            "  COALESCE(json_extract(mn.chat_message,'$.metadata.request_id'),"
            "           json_extract(mn.chat_message,'$.message_id')), "
            "  json_extract(mn.chat_message,'$.metadata.generation_model'),"
            "  json_extract(mn.chat_message,'$.metadata.created_at'),"
            "  json_extract(mn.chat_message,"
            "               '$.metadata.metrics.input_tokens'),"
            "  json_extract(mn.chat_message,"
            "               '$.metadata.metrics.cache_read_tokens'),"
            "  json_extract(mn.chat_message,"
            "               '$.metadata.metrics.cache_creation_tokens'),"
            "  json_extract(mn.chat_message,"
            "               '$.metadata.metrics.output_tokens'),"
            "  json_extract(mn.chat_message,"
            "               '$.metadata.response_dimensions') "
            "FROM message_nodes mn "
            "WHERE mn.session_id = ? AND mn.row_id > ? "
            "  AND json_extract(mn.chat_message,'$.role')='assistant' "
            "ORDER BY mn.row_id",
            (sid, acc["last_row_id"]))
        for (row_id, req, gen, cts,
             m_in, m_cr, m_cc, m_out, rd) in rows:
            acc["last_row_id"] = row_id
            key = req if req is not None else ("row", row_id)
            if key in acc["seen_msgs"]:
                continue
            acc["seen_msgs"].add(key)
            cur = {"gen": gen, "disp": None, "ts": _iso_ts(cts),
                   "t": {u: 0.0 for u in TOKEN_UIDS}, "msgs": 0}
            try:
                dims = json.loads(rd) if rd else None
            except (ValueError, TypeError):
                dims = None
            if dims:
                for d in dims:
                    if not isinstance(d, dict):
                        continue
                    uid = d.get("uid")
                    kind = d.get("kind") or {}
                    cum = (kind.get("CumulativeMetric") or {}).get("value")
                    met = (kind.get("Metric") or {}).get("value")
                    if uid == "model":
                        cur["disp"] = met or cur["disp"]
                    elif uid == "agent_messages":
                        cur["msgs"] += cum or 0
                    elif uid in cur["t"] and cum is not None:
                        cur["t"][uid] += float(cum)
            else:
                # cache_creation is billed ~input rate -> fold into input
                cur["t"]["input_tokens"] = (m_in or 0) + (m_cc or 0)
                cur["t"]["cached_input_tokens"] = m_cr or 0
                cur["t"]["output_tokens"] = m_out or 0
                cur["msgs"] = 1
            self._finalize_turn(acc, cur)
        return acc

    def _finalize_turn(self, acc, cur):
        t = cur["t"]
        if cur["disp"]:
            acc["model"] = cur["disp"]
        if cur["gen"]:
            acc["last_gen"] = cur["gen"]
        cost = self._price_turn(cur["gen"], cur["disp"],
                                t["input_tokens"],
                                t["cached_input_tokens"],
                                t["output_tokens"], cur["ts"])
        if cur["ts"] is not None:
            # per-quota-day bucket — free turns recorded too (cost 0,
            # tokens in free_tok) to test whether they consume quota
            b = acc["days"].setdefault(_qday(cur["ts"]), {}).setdefault(
                cur["gen"] or cur["disp"] or "?", [0.0] * 6)
            b[0] += t["input_tokens"]
            b[1] += t["cached_input_tokens"]
            b[2] += t["output_tokens"]
            b[3] += cost or 0.0
            b[4] += 1
            if cost is None:
                b[5] += (t["input_tokens"] + t["cached_input_tokens"]
                         + t["output_tokens"])
        if cost is None:
            # no token pricing in the live config -> free model
            acc["free_tokens"] += sum(t[u] for u in TOKEN_UIDS)
            return
        for u in TOKEN_UIDS:
            acc["totals"][u] += t[u]
        acc["messages"] += int(cur.get("msgs") or 0)
        acc["cost"] += cost
        if cur["ts"] and cur["ts"] >= self._cycle_start:
            acc["cost_cycle"] += cost

    def get_stats(self, sid, info):
        acc = self.accumulate(sid)
        t = acc["totals"]
        raw_model = acc["model"] or info.get("model") or "-"
        disp_model = self.names.get(raw_model, raw_model)
        lg = acc["last_gen"] or ""
        fu = self.free_until.get(lg) or self.free_until.get(disp_model)
        billable_now = bool(self.pricing.get(lg)) and \
            (fu is None or time.time() > fu)
        priced = bool(acc["cost"] > 0 or billable_now)
        return {
            "session_id": sid,
            "title": info.get("title", sid),
            "model": disp_model,
            "gen_model": acc["last_gen"],
            "priced": priced,
            "promo_until": fu,
            "last_activity_at": info.get("last_activity_at", 0),
            "messages": acc["messages"],
            "input_tokens": int(t["input_tokens"]),
            "cached_input_tokens": int(t["cached_input_tokens"]),
            "output_tokens": int(t["output_tokens"]),
            "total_tokens": int(sum(t[u] for u in TOKEN_UIDS)),
            "cost_usd": acc["cost"],
            "cost_cycle_usd": acc["cost_cycle"],
            "free_tokens": int(acc["free_tokens"]),
            "credits": info.get("credits"),
            "acus": info.get("acus"),
        }

    def running_stats(self):
        self.refresh_pricing()
        ids = self.active_session_ids()
        # accumulators are kept for non-running sessions too — their
        # cost_cycle still counts toward this month's spend
        infos = self.sessions_info(ids)
        stats = [self.get_stats(sid, infos.get(sid, {})) for sid in ids]
        stats.sort(key=lambda s: s["last_activity_at"], reverse=True)
        return stats

    def cycle_spend_usd(self):
        """List-price USD spent on billable turns since the current
        billing cycle started (1st of the month)."""
        return sum(a["cost_cycle"] for a in self._acc.values())

    def window_usage(self, qd_from, qd_to):
        """model_key -> [in, cached, out, cost_usd, turns, free_tokens]
        summed over all accumulated sessions for quota days
        qd_from..qd_to inclusive (see _qday)."""
        out = {}
        for acc in self._acc.values():
            for qd, models in acc["days"].items():
                if not qd_from <= qd <= qd_to:
                    continue
                for mk, b in models.items():
                    o = out.setdefault(mk, [0.0] * 6)
                    for i in range(6):
                        o[i] += b[i]
        return out

    def rescan_recent(self, since_ts):
        """Re-accumulate sessions with last_activity_at >= since_ts
        (unix seconds — same unit as sessions.last_activity_at).
        Incremental: accumulate() only reads rows past last_row_id."""
        queued = set(self._scan_q)  # those will be scanned anyway
        for (sid,) in self._q(
                "SELECT id FROM sessions WHERE last_activity_at >= ?",
                (since_ts,)):
            if sid not in queued:
                self.accumulate(sid)


# ------------------------------------------------------------- helpers


def _cycle_start_ts():
    """Start of the current monthly billing cycle — the 1st at ~09:26 UTC
    (Stripe period anchor seen via api.devin.ai /billing/subscription)."""
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, 9, 26,
                    tzinfo=timezone.utc).timestamp()

def _qday(ts):
    """unix seconds -> quota-day index (days bounded at 08:00 UTC)."""
    return int((ts - QUOTA_DAY_OFFSET) // 86400)


def _quota_windows(q):
    """(day_from, day_to, week_from, week_to) quota-day indices for a
    /billing/quota/usage dict. The day window is the single quota day
    ending at daily_reset_at; the week is the 7 quota days ending at
    weekly_reset_at. Missing/unparseable reset strings fall back to the
    current quota day / the 7 days ending there."""
    try:
        qd_day = _qday(datetime.fromisoformat(
            q["daily_reset_at"]).timestamp()) - 1
    except Exception:
        qd_day = _qday(time.time())
    try:
        qd_wk_end = _qday(datetime.fromisoformat(
            q["weekly_reset_at"]).timestamp()) - 1
    except Exception:
        qd_wk_end = _qday(time.time())
    return qd_day, qd_day, qd_wk_end - 6, qd_wk_end


def _iso_ts(s):
    """'2026-09-07T13:28:28.525767700Z' -> unix seconds (or None)."""
    if not s:
        return None
    try:
        s = s[:26].rstrip("Z")  # trim nanoseconds to microseconds
        return datetime.fromisoformat(s).replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def fmt_int(n):
    return f"{int(n):,}"


def fmt_tokens(n):
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e4:
        return f"{n / 1e3:.1f}k"
    return str(int(n))


_dkk_rate = None


def dkk_rate():
    """USD->DKK exchange rate. Fetches the daily ECB rate from
    frankfurter.dev once per day, caches it in state.json, and falls back
    to the last cached value or USD_TO_DKK_FALLBACK when offline."""
    global _dkk_rate
    if _dkk_rate:
        return _dkk_rate
    cached = 0.0
    try:
        st = json.load(open(STATE_PATH, encoding="utf-8"))
        cached = float(st.get("usd_dkk") or 0)
        fresh = time.time() - float(st.get("usd_dkk_ts") or 0) < FX_TTL_SEC
    except Exception:
        fresh = False
    if not (cached and fresh):
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(
                "https://api.frankfurter.dev/v1/latest?base=USD&symbols=DKK",
                headers={"User-Agent": "Mozilla/5.0"}), timeout=3))
            rate = float(r["rates"]["DKK"])
            st = {}
            try:
                st = json.load(open(STATE_PATH, encoding="utf-8"))
            except Exception:
                pass
            st["usd_dkk"] = rate
            st["usd_dkk_ts"] = time.time()
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(st, f)
            cached = rate
        except Exception:
            pass
    _dkk_rate = cached or USD_TO_DKK_FALLBACK
    return _dkk_rate


# --- live account quota (api.devin.ai) -------------------------------

_quota = {}            # last fetched quota dict ({} = unknown)
_quota_thread = None


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


def _org_id(token):
    """Devin org id — fetched once from app.devin.ai, cached in
    state.json."""
    try:
        st = json.load(open(STATE_PATH, encoding="utf-8"))
        if st.get("org_id"):
            return st["org_id"]
    except Exception:
        st = {}
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            "https://app.devin.ai/api/auth/windsurf/eligible-organizations",
            headers={"Authorization": "Bearer " + token}), timeout=5)
        orgs = json.loads(r.read())
        org = orgs[0]["org_id"] if orgs else None
        if org:
            st["org_id"] = org
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(st, f)
        return org
    except Exception:
        return None


def _fetch_quota_bg():
    global _quota
    try:
        token = _read_session_token()
        org = token and _org_id(token)
        if not org:
            return
        r = urllib.request.urlopen(urllib.request.Request(
            f"https://api.devin.ai/{org}/billing/quota/usage",
            headers={"Authorization": "Bearer " + token,
                     "x-cog-org-id": org}), timeout=5)
        q = json.loads(r.read())
        q["_ts"] = time.time()
        _quota = q
    except Exception:
        pass


def quota_usage():
    """Latest {daily_percentage, weekly_percentage, ...} dict. Refreshes
    in a background thread; never blocks the UI."""
    global _quota_thread
    stale = not _quota or time.time() - _quota.get("_ts", 0) > QUOTA_TTL_SEC
    if stale and not (_quota_thread and _quota_thread.is_alive()):
        _quota_thread = threading.Thread(target=_fetch_quota_bg,
                                         daemon=True)
        _quota_thread.start()
    return _quota


def fmt_cost(usd):
    return f"{usd * dkk_rate():.2f} kr"


def trunc(s, n):
    s = str(s)
    return s[: n - 3] + "..." if len(s) > n else s


class ToolTip:
    """Simple dark hover tooltip for a tk widget."""

    def __init__(self, widget):
        self.widget = widget
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<Button-1>", self._leave, add="+")

    def _enter(self, _e):
        self._after = self.widget.after(500, self._show)

    def _show(self):
        text = getattr(self.widget, "_tip_text", None)
        if not text or self.tip:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        tk.Label(self.tip, text=text, bg="#252526", fg="#d4d4d4",
                 font=FONT, padx=6, pady=2, bd=1, relief="solid",
                 highlightthickness=0,
                 justify="left").pack()
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip.geometry(f"+{x}+{y}")

    def _leave(self, _e=None):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


def dump():
    pricing, names, promo, priced_ids = load_pricing()
    db = UsageDB(pricing, names, promo, priced_ids)
    db.scan_all()
    stats = db.running_stats()
    if not stats:
        print("no running sessions")
        return 1
    print(f"{'SESSION':<24} {'MODEL':<18} {'TOKENS':>10} {'COST':>9}")
    print("-" * 64)
    for s in stats:
        print(f"{trunc(s['title'], 22):<24} {trunc(s['model'], 16):<18} "
              f"{fmt_tokens(s['total_tokens']):>10} {fmt_cost(s['cost_usd']):>9}")
    print("-" * 64)
    rate = dkk_rate()
    print(f"{len(stats)} sessions  "
          f"total tokens {fmt_int(sum(s['total_tokens'] for s in stats))}  "
          f"total cost {sum(s['cost_usd'] for s in stats) * rate:.2f} kr")
    spent = sum(s.get("cost_cycle_usd", 0) for s in stats)
    print(f"this cycle: {spent * rate:.2f} kr of "
          f"{MONTHLY_BUDGET_USD * rate:.0f} kr "
          f"({(MONTHLY_BUDGET_USD - spent) * rate:.2f} kr left)")
    _fetch_quota_bg()
    if _quota:
        print(f"quota: week {_quota.get('weekly_percentage')}%  "
              f"day {_quota.get('daily_percentage')}%")
        d0, d1, w0, w1 = _quota_windows(_quota)
        hdr = (f"{'model':32} {'in':>12} {'cache':>13} {'out':>10} "
               f"{'usd':>8} {'turns':>6}")
        for label, lo, hi in (("quota day window", d0, d1),
                              ("quota week window", w0, w1)):
            print(f"{label} (qdays {lo}..{hi}):")
            print(hdr)
            tot = [0.0] * 6
            for m, b in sorted(db.window_usage(lo, hi).items(),
                               key=lambda kv: (-kv[1][3],
                                               -(kv[1][0] + kv[1][1]
                                                 + kv[1][2]))):
                for i in range(6):
                    tot[i] += b[i]
                print(f"{m:32} {int(b[0]):>12,} {int(b[1]):>13,} "
                      f"{int(b[2]):>10,} {b[3]:>8.2f} {int(b[4]):>6}")
            print(f"{'TOTAL':32} {int(tot[0]):>12,} {int(tot[1]):>13,} "
                  f"{int(tot[2]):>10,} {tot[3]:>8.2f} {int(tot[4]):>6}")
        QuotaLogger().log(db, _quota)
    for s in stats:
        print(json.dumps(s))
    return 0


# ------------------------------------------------------------- logging

class UsageLogger:
    def __init__(self):
        self._last = {}  # sid -> (key, ts)

    def log(self, stats):
        try:
            new = not os.path.exists(LOG_PATH)
            os.makedirs(STATE_DIR, exist_ok=True)
            now = time.time()
            iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
            with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp_iso", "session_id", "title",
                                "model", "messages", "input_tokens",
                                "cached_input_tokens", "output_tokens",
                                "total_tokens", "cost_usd"])
                for s in stats:
                    key = (s["messages"], s["total_tokens"], s["cost_usd"])
                    last = self._last.get(s["session_id"])
                    if last and (last[0] == key or
                                 now - last[1] < LOG_MIN_INTERVAL):
                        continue
                    self._last[s["session_id"]] = (key, now)
                    w.writerow([iso, s["session_id"], s["title"], s["model"],
                                s["messages"], s["input_tokens"],
                                s["cached_input_tokens"], s["output_tokens"],
                                s["total_tokens"], f"{s['cost_usd']:.4f}"])
        except Exception:
            pass


class QuotaLogger:
    """Appends a row to data\\quota_log.csv whenever the polled quota
    percentages change (or hourly): live daily/weekly % plus per-model
    token and list-price-USD totals for the current quota day and quota
    week. Purpose: calibrate how many tokens / list-price USD 1% of
    quota corresponds to per model — including whether promo-free
    models consume quota (their tokens land in the *_free columns)."""

    def __init__(self):
        self._last_pcts = None  # (daily_pct, weekly_pct) as strings
        self._last_ts = 0.0
        try:  # seed from the last row so a restart isn't a duplicate
            with open(QUOTA_LOG_PATH, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 65536))
                last = f.read().decode("utf-8", "replace") \
                    .strip().splitlines()[-1]
            row = next(csv.reader([last]))
            self._last_pcts = (row[1], row[2])
            try:
                self._last_ts = datetime.fromisoformat(row[0]).timestamp()
            except ValueError:
                self._last_ts = os.path.getmtime(QUOTA_LOG_PATH)
        except Exception:
            pass

    @staticmethod
    def _by_model(u):
        return json.dumps(
            {m: {"in": int(b[0]), "cache": int(b[1]), "out": int(b[2]),
                 "usd": round(b[3], 4), "turns": int(b[4]),
                 "free_tok": int(b[5])}
             for m, b in sorted(u.items(),
                                key=lambda kv: (-kv[1][3], kv[0]))},
            separators=(",", ":"))

    def log(self, db, q):
        try:
            if not q or "_ts" not in q:
                return
            daily_pct = q.get("daily_percentage")
            weekly_pct = q.get("weekly_percentage")
            pcts = (str(daily_pct), str(weekly_pct))
            now = time.time()
            if (pcts == self._last_pcts
                    and now - self._last_ts < 3600):
                return
            d0, d1, w0, w1 = _quota_windows(q)
            day = db.window_usage(d0, d1)
            week = db.window_usage(w0, w1)

            def agg(u):  # -> (free_tokens, paid_tokens, cost_usd)
                tot = sum(b[0] + b[1] + b[2] for b in u.values())
                free = sum(b[5] for b in u.values())
                usd = sum(b[3] for b in u.values())
                return int(free), int(tot - free), usd

            d_free, d_paid, d_usd = agg(day)
            w_free, w_paid, w_usd = agg(week)
            new = not os.path.exists(QUOTA_LOG_PATH)
            os.makedirs(STATE_DIR, exist_ok=True)
            iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
            with open(QUOTA_LOG_PATH, "a", newline="",
                      encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp_iso", "daily_pct", "weekly_pct",
                                "daily_reset_at", "weekly_reset_at",
                                "overage_balance", "day_tokens_free",
                                "day_tokens_paid", "day_cost_usd",
                                "week_tokens_free", "week_tokens_paid",
                                "week_cost_usd", "day_by_model_json",
                                "week_by_model_json"])
                w.writerow([iso, daily_pct, weekly_pct,
                            q.get("daily_reset_at"),
                            q.get("weekly_reset_at"),
                            q.get("overage_balance"),
                            d_free, d_paid, f"{d_usd:.4f}",
                            w_free, w_paid, f"{w_usd:.4f}",
                            self._by_model(day), self._by_model(week)])
            self._last_pcts = pcts
            self._last_ts = now
        except Exception:
            pass


# ----------------------------------------------------------------- gui

class Widget:
    def __init__(self, root):
        self.root = root
        pricing, names, promo, priced_ids = load_pricing()
        self.db = UsageDB(pricing, names, promo, priced_ids)
        self.logger = UsageLogger()
        self.qlogger = QuotaLogger()
        self._last_rescan = 0.0  # last rescan_recent() for quota windows
        self.expanded = set()
        self.state = self._load_state()
        self._row_widgets = {}   # sid -> dict of labels
        self._detail_frames = {}
        self._order = []
        self._tips = []
        self._warned = set()  # session ids that already crossed the cost threshold
        self._blink = set()   # session ids whose model just went paid
        self._blink_phase = False
        self._newly_seen = set()  # model ids already announced

        root.title("Devin Usage")
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.92)
        pos = self.state.get("pos") or "+100+100"
        self._last_pos = pos
        root.geometry(f"{WIN_W}x200{pos}")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._dark_titlebar()

        self.show_all = bool(self.state.get("show_all"))
        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=8, pady=(5, 0))
        self.budget_lbl = tk.Label(top, bg=BG, fg=FG, font=FONT, anchor="w")
        self.budget_lbl.pack(side="left")
        self.filter_lbl = tk.Label(top, bg=BG, fg=DIM, font=FONT,
                                   anchor="e", cursor="hand2")
        self.filter_lbl.pack(side="right")
        self.filter_lbl.bind("<Button-1>", self._toggle_filter)

        self.table = tk.Frame(root, bg=BG)
        self.table.pack(fill="x", padx=8, pady=(6, 0))

        self._tick()
        self.root.after(80, self._scan_step)

    def _scan_step(self):
        """Background-fill: accumulate closed sessions so their turns
        count toward this cycle's spend, without blocking startup."""
        try:
            done = self.db.scan_step()
        except Exception:
            done = True
        if not done:
            self.root.after(80, self._scan_step)

    def _dark_titlebar(self):
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            dwm = ctypes.windll.dwmapi
            val = ctypes.c_int(1)
            r = dwm.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
            if r != 0:
                dwm.DwmSetWindowAttribute(
                    hwnd, 19, ctypes.byref(val), ctypes.sizeof(val))
            for attr, cref in ((35, 0x001E1E1E), (36, 0x00D4D4D4)):
                c = ctypes.c_int(cref)
                dwm.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(c), ctypes.sizeof(c))
            self.root.withdraw()
            self.root.deiconify()
        except Exception:
            pass

    def _load_state(self):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            g = self.root.geometry()
            pos = g[g.index("+"):]
            self._last_pos = pos
            st = self._load_state()  # merge — keeps e.g. "promo_ends"
            st["pos"] = pos
            st["show_all"] = self.show_all
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(st, f)
        except Exception:
            pass

    def _maybe_save_pos(self):
        """Persist position whenever it changes — survives hard kills."""
        try:
            g = self.root.geometry()
            pos = g[g.index("+"):]
            if pos != getattr(self, "_last_pos", None):
                self._save_state()
        except Exception:
            pass

    def _on_close(self):
        self._save_state()
        self.root.destroy()

    def _toggle_filter(self, _e):
        self.show_all = not self.show_all
        self._order = []  # force rebuild
        self._save_state()

    def _toggle(self, sid):
        self._blink.discard(sid)  # acknowledge the free->paid warning
        if sid in self.expanded:
            self.expanded.discard(sid)
        else:
            self.expanded.add(sid)
        self._order = []  # force rebuild

    def _build_row(self, parent, s, hot):
        fr = tk.Frame(parent, bg=BG, cursor="hand2")
        fr.pack(fill="x")
        tfg = ACCENT if hot else FG
        warn = s["cost_usd"] >= COST_WARN_USD
        blink = (s["session_id"] in self._blink and self._blink_phase)
        c1 = tk.Label(fr, text=trunc(s["title"], 22), bg=BG, fg=tfg,
                      font=FONT, anchor="w", width=24)
        c1._tip_text = s["title"] if len(str(s["title"])) > 22 else None
        c2 = tk.Label(fr, text=trunc(s["model"], 16), bg=BG,
                      fg=ERR if blink else FG,
                      font=FONT, anchor="w", width=18)
        c2._tip_text = s["model"] if len(str(s["model"])) > 16 else None
        c3 = tk.Label(fr, text=fmt_tokens(s["total_tokens"]), bg=BG, fg=FG,
                      font=FONT, anchor="e", width=9)
        c4 = tk.Label(fr, text=fmt_cost(s["cost_usd"]), bg=BG,
                      fg=ERR if warn else FG, font=FONT, anchor="e", width=9)
        for w in (c1, c2, c3, c4):
            w.pack(side="left")
            w.bind("<Button-1>", lambda e, i=s["session_id"]: self._toggle(i))
        fr.bind("<Button-1>", lambda e, i=s["session_id"]: self._toggle(i))
        self._tips.append(ToolTip(c1))
        self._tips.append(ToolTip(c2))
        labels = {"title": c1, "model": c2, "tokens": c3, "cost": c4}

        det = None
        if s["session_id"] in self.expanded:
            det = tk.Frame(parent, bg=BG)
            det.pack(fill="x", padx=(18, 0))
            lines = [
                f"Input {fmt_int(s['input_tokens'])}   "
                f"Cached {fmt_int(s['cached_input_tokens'])}   "
                f"Output {fmt_int(s['output_tokens'])}   "
                f"Messages {fmt_int(s['messages'])}"]
            if s["free_tokens"]:
                lines.append(f"Free/promo tokens (not counted) "
                             f"{fmt_int(s['free_tokens'])}")
            if not s["priced"]:
                if s["promo_until"]:
                    until = time.strftime(
                        "%d %b %Y", time.localtime(s["promo_until"]))
                    lines.append(f"Free / promo model until {until}")
                else:
                    lines.append("Free model (no token pricing)")
            if s["credits"] or s["acus"]:
                lines.append(f"Credits {s['credits']}   "
                             f"ACUs {s['acus']:.2f}")
            for ln in lines:
                tk.Label(det, text=ln, bg=BG, fg=DIM,
                         font=("Consolas", 8), anchor="w").pack(fill="x")
        return labels, det

    def _rebuild_table(self, stats):
        for t in self._tips:
            t._leave()
        self._tips = []
        for w in self.table.winfo_children():
            w.destroy()
        self._row_widgets = {}
        hdr = tk.Frame(self.table, bg=BG)
        hdr.pack(fill="x")
        for txt, wd, anch in (("Session", 24, "w"), ("Model", 18, "w"),
                              ("Tokens", 9, "e"), ("Cost", 9, "e")):
            tk.Label(hdr, text=txt, bg=BG, fg=DIM, font=FONT,
                     anchor=anch, width=wd).pack(side="left")
        for i, s in enumerate(stats):
            labels, det = self._build_row(self.table, s, hot=(i == 0))
            self._row_widgets[s["session_id"]] = labels

    def _update_rows(self, stats):
        for i, s in enumerate(stats):
            w = self._row_widgets.get(s["session_id"])
            if not w:
                continue
            w["title"].config(text=trunc(s["title"], 22),
                              fg=ACCENT if i == 0 else FG)
            w["title"]._tip_text = (s["title"]
                                    if len(str(s["title"])) > 22 else None)
            w["model"].config(
                text=trunc(s["model"], 16),
                fg=(ERR if (s["session_id"] in self._blink
                            and self._blink_phase) else FG))
            w["model"]._tip_text = (s["model"]
                                    if len(str(s["model"])) > 16 else None)
            w["tokens"].config(text=fmt_tokens(s["total_tokens"]))
            w["cost"].config(text=fmt_cost(s["cost_usd"]),
                             fg=ERR if s["cost_usd"] >= COST_WARN_USD else FG)

    def _fit_height(self):
        self.root.update_idletasks()
        h = (self.table.winfo_reqheight() +
             self.budget_lbl.winfo_reqheight() + 14)
        g = self.root.geometry()
        pos = g[g.index("+"):]
        self.root.geometry(f"{WIN_W}x{h}{pos}")

    def _check_warn(self, stats):
        beep = False
        # free -> paid model transitions (detected on config reload)
        new = self.db.newly_priced - self._newly_seen
        for mid in new:
            self._newly_seen.add(mid)
            beep = True
        if new:
            self._priced_notice = ", ".join(
                self.db.names.get(m, m) for m in sorted(new))
        for s in stats:
            if s["cost_usd"] >= COST_WARN_USD \
                    and s["session_id"] not in self._warned:
                self._warned.add(s["session_id"])
                beep = True
            if (s.get("gen_model") in self.db.newly_priced
                    and s["session_id"] not in self._blink):
                self._blink.add(s["session_id"])
                beep = True
        if beep and BEEP_ON_WARN:
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass

    def _tick(self):
        err = ""
        try:
            stats = self.db.running_stats()
            self.logger.log(stats)
            self._check_warn(stats)
            shown = (stats if self.show_all
                     else [s for s in stats if s["cost_usd"] > 0])
            order = [s["session_id"] for s in shown]
            if order != self._order:
                self._order = order
                self._rebuild_table(shown)
            else:
                self._update_rows(shown)
            hidden = len(stats) - len(shown)
            self.filter_lbl.config(
                text="[paid only]" if self.show_all else f"[+{hidden}]")
            rate = dkk_rate()
            budget = MONTHLY_BUDGET_USD * rate
            spent = sum(s["cost_usd"] for s in stats) * rate
            left = budget - self.db.cycle_spend_usd() * rate
            q = quota_usage()
            now = time.time()
            if now - self._last_rescan >= 60:
                self._last_rescan = now
                # pick up turns from sessions no longer running so the
                # quota-day/week window sums stay current; since = start
                # of the current quota day minus 1 day of slack
                since = (_qday(now) - 1) * 86400 + QUOTA_DAY_OFFSET
                self.db.rescan_recent(since)
                if not self.db._scan_q:  # windows incomplete mid-backlog
                    self.qlogger.log(self.db, q)
            qpct = ""
            if q:
                qpct = (f"   uge {q.get('weekly_percentage', '?')}%"
                        f"/dag {q.get('daily_percentage', '?')}%")
            exhausted = q and (q.get("weekly_percentage", 0) >= 100
                               or q.get("daily_percentage", 0) >= 100)
            self.budget_lbl.config(
                text=f"Budget {budget:.0f} kr/mo   {spent:.2f} kr   "
                     f"left {left:.2f} kr{qpct}",
                fg=ERR if (left <= 0 or exhausted) else FG)
            self._fit_height()
            self._maybe_save_pos()
            self._blink_phase = not self._blink_phase
        except Exception as e:
            err = str(e)[:60]
        notice = getattr(self, "_priced_notice", "")
        if err or notice:
            self.budget_lbl.config(
                text=("! " + (err or "no longer free: " + notice))[:70],
                fg=ERR)
        self.root.after(POLL_MS, self._tick)


_MUTEX = None  # keep the handle alive for process lifetime


def _single_instance():
    global _MUTEX
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Local\\DevinUsageWidget")
    if ctypes.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        hwnd = ctypes.windll.user32.FindWindowW(None, "Devin Usage")
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        sys.exit(0)


def main():
    if "--dump" in sys.argv:
        sys.exit(dump())
    _single_instance()
    root = tk.Tk()
    Widget(root)
    root.mainloop()


if __name__ == "__main__":
    main()
