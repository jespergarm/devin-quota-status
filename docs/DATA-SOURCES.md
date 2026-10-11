# Data Sources

Everything below was reverse-engineered from Devin CLI's local state on this
machine (Windows 11, Python 3.11 MSIX build). All formats are undocumented
internals — a CLI update can break any of them. If numbers look wrong, run
`python devin_usage_widget.pyw --dump` first to see what changed.

## 1. `sessions.db` — session metadata and per-turn metrics

**Path:** `%APPDATA%\devin\cli\sessions.db`

SQLite in **WAL mode**, written live by the CLI. Always open read-only:

```python
sqlite3.connect("file:<path>?mode=ro", uri=True)
con.execute("PRAGMA busy_timeout = 1000")
```

Never copy the file while reading live — the newest rows sit in the `-wal`
file, not the main database.

### Relevant schema

```sql
sessions(id TEXT, title, model, last_activity_at INTEGER,  -- UNIX SECONDS
         hidden INTEGER, metadata TEXT)                    -- JSON
message_nodes(row_id, session_id, node_id, parent_node_id,
              chat_message TEXT,                           -- JSON per node
              created_at, metadata)
```

`message_nodes.created_at` is the **local sync/insert time** — the CLI
re-writes history in batches, so hundreds of rows share one timestamp.
The real per-turn time is `chat_message.metadata.created_at` (ISO 8601
string, e.g. `2026-09-07T13:28:28.525767700Z`). Use it for any
time-based billing decision, never the column.

- `sessions.metadata` JSON: `total_credit_cost`, `total_acu_cost`
  (0 on this account), `client_meta`.
- `chat_message` JSON per assistant turn:
  - `$.role` = `"assistant"`
  - `$.metadata.generation_model` — the **actual serving model id** per turn.
    This is how Fusion splits lead vs sidekick (e.g.
    `claude-fable-5-1-low` vs `swe-2-medium`). Values seen: model ids, plus
    `compactor`, `summarizer`, rarely empty.
  - `$.metadata.metrics`: `input_tokens`, `output_tokens`,
    `cache_read_tokens`, `cache_creation_tokens`, `ttft_ms`, `tpot_ms`, ...
  - `$.metadata.response_dimensions`: array of
    `{uid, group_title, kind:{CumulativeMetric:{label,value}} | {Metric:{label,value}}}`.
    Uids seen: `agent_messages`, `model` (Metric, display name),
    `input_tokens`, `output_tokens`, `cached_input_tokens`.
    **These are per-turn values — sum across turns for session totals.**
    Same numbers `/session-stats` shows.
  - `$.metadata.request_id` + `$.message_id`: turn identifiers — see below.

### Duplicate rows — MUST dedup

The CLI writes each assistant turn **more than once** in `message_nodes`
(observed 1–8 copies per turn; session re-syncs rewrite history). Copies
share `metadata.request_id`, `message_id`, and identical token metrics;
later copies usually add `metadata.extensions`. Summing raw rows
overcounts tokens/cost by 2× or more. **Always dedup on
`COALESCE(request_id, message_id)`** — verified: within a request_id group
the token values never differ.

Note: `metadata.metrics` uses `cache_read_tokens`/`cache_creation_tokens`
(there is no `cached_input_tokens` key there).

### Incremental aggregation

Keep `last_row_id` per session and query `WHERE row_id > ?` each poll.
Many turns lack `response_dimensions` entirely (it was added ~Sep 2026) —
for those, use `metadata.metrics` per-turn (`cache_creation_tokens` folds
into input at ~input price). A session-level fallback is NOT enough:
sessions mix turns with and without response_dimensions.

### Useful SQL

```sql
-- token usage per serving model for a session
SELECT json_extract(chat_message,'$.metadata.generation_model') g,
       json_extract(je.value,'$.uid') uid,
       SUM(json_extract(je.value,'$.kind.CumulativeMetric.value'))
FROM message_nodes,
     json_each(json_extract(chat_message,'$.metadata.response_dimensions')) je
WHERE session_id=? AND json_extract(chat_message,'$.role')='assistant'
GROUP BY g, uid;
```

## 2. `session_locks\*.lock` — running-session detection

**Path:** `%APPDATA%\devin\cli\session_locks\<session_id>.lock`

A running CLI holds a **byte-range lock** on its file. Plain `open()`
sometimes succeeds on locked files; the reliable probe is:

```python
msvcrt.locking(h.fileno(), msvcrt.LK_NBLCK, 1)  # OSError => locked => running
```

Fallback when probing fails or finds nothing: `last_activity_at` within
10 minutes (`ACTIVE_FALLBACK_SEC`).

## 3. `model_configs_v5.*.bin` — models, prices, promo ends

**Path:** `%LOCALAPPDATA%\devin\cli\model_configs_v5.<digest>.bin`

JSON wrapper `{version, identity_digest, fetched_at_secs, payload}` where
`payload` is base64 **protobuf**. Parsed with a minimal wire decoder
(`_fields` in the widget: varint / 32-bit float / 64-bit / length-delimited).
Always use the newest file by mtime; the widget reloads it whenever the
mtime changes.

Top level: repeated field 1 = one message per model.

| Field | Type | Meaning |
|-------|------|---------|
| 1 | string | display name, e.g. "Claude Fable 5.1 Low" |
| 3 | float | **credits per message** (plan billing multiplier) — fable-low=150, astra-medium=200, swe-2-high=9.0, glm-5-2=1.5 |
| 10 | varint | provider enum (observed 1,2,3,9 — not billing) |
| 18 | varint | context window tokens |
| **19** | message | **promo/free marker**; sub-field 2 → sub-field 1 = promo end unix_ts. Models WITHOUT prices still carry it |
| 22 | string | model id, e.g. `claude-fable-5-1-low` |
| 23 | message | serving config (tokenizer, endpoint, api names) |
| 24 | varint | cost tier enum (1–4 = Free/$/$$/$$$ probably) |
| 30 | message | selector UI metadata (family, effort options) |
| **32** | repeated msg | **token prices**: {1:label, 2:$/1M float, 3:"1M tokens", 4:?, 5:?, 6, 7:note}. Labels: "Input", "Cached input", "Output" — or {1:"Sidekick"} on fusion entries |

### Billing rule used by the widget

A turn counts iff its `generation_model` has field-32 token prices **and
the turn ran after the model's promo end** (turn ts =
`metadata.created_at`; display name only used when `generation_model` is
absent). No prices, or `turn_ts <= promo_end`, = free → tokens counted
separately as `free_tokens`.

The promo end is dropped from the config once a model goes paid, so the
widget persists observed promo ends in `data\state.json` (`promo_ends`)
and seeds known ones (`PROMO_END_SEED` in the script): glm-5-2 and
swe-1-7* promos ended 2026-09-16.

Fusion entries (`fusion-*`) have their own field-32 prices, but actual
turns carry the real model id anyway (lead → priced, sidekick → free), so
per-turn attribution is automatic.

Prices seen (USD/1M, input/cached/output): Fable 5.1 Low 10/0.25/50;
Astra Medium 10/1/50; GLM-5.2 High 1.4/0.26/4.4 (priced since its promo
ended 2026-09-16); Luna Medium 0.2/0.02/1.2. Fusion entries additionally
carry `Sidekick input`/`Sidekick cached input`/`Sidekick output` labels.

All math is in USD; the widget converts to DKK for display using the
daily ECB USD→DKK rate (frankfurter.dev, cached 24 h in `state.json`
as `usd_dkk`, fallback `USD_TO_DKK_FALLBACK`).

## 4. `user_status.*.bin` — plan info

**Path:** `%LOCALAPPDATA%\devin\cli\user_status.<digest>.bin`

Same JSON+protobuf wrapper as the model config. Contains user name/email,
plan name ("Pro"), and its own copy of the model list (field 33) with the
same per-model fields — including the credit multipliers (field 3).

Example (a **"Pro"** plan): quota-based
(`uses_quota: true` via `api.devin.ai/{org}/billing/quota/usage`) —
flat monthly price, no per-token billing. Usage is limited by
daily/weekly quota percentages, not dollars. The widget polls that
endpoint every 30 s (see `quota_usage()`).

If plan billing turns out to be credits-per-message rather than per-token,
cost should switch to `field3 × agent_messages`.

### Quota calibration logs — `quota_log.csv` vs `quota_pct_log.csv`

Two CSVs track the same polled quota endpoint for different purposes:

- **`data\quota_log.csv`** — written by the **widget** (manual `--dump`
  runs / GUI; it no longer runs continuously). A row whenever the polled
  quota percentages change (or hourly): the live
  `daily_percentage`/`weekly_percentage` plus **per-model token and
  list-price-USD totals** for the current quota day and quota week,
  summed from `sessions.db` turns.
- **`data\quota_pct_log.csv`** — written by the **status-bar
  extension** on every poll (`quota.py` argv[2] = `devinQuota.logPath`).
  Percentage time series only: `timestamp_iso, daily_pct, weekly_pct,
  daily_reset_at, weekly_reset_at, overage_balance`. The token side can
  be reconstructed from `sessions.db` turn timestamps
  (`chat_message.metadata.created_at`) bucketed by quota day/week — see
  `window_usage()` in the widget.

Quota days reset at **08:00 UTC** — `daily_reset_at` is always an 08:00
UTC instant and `weekly_reset_at` ends a 7-quota-day window. The logs
exist to calibrate how many tokens / list-price USD 1% of quota
corresponds to per model. Open hypothesis to verify against it: turns
on promo-free models (no field-32 prices) may or may not consume quota —
their tokens are logged separately (`free_tok` per model,
`*_tokens_free` columns in `quota_log.csv`).

Caution: the Desktop app's Plan-info panel renders `cachedPlanInfoData`
from `state.vscdb`, which can be days stale (observed 5 days behind).
The widget/extension poll the live endpoint — trust the logs over the
panel.

## Free models on this plan (as of 2026-09-15)

Several models are free during promo — the model selector badges them
"promo/free". Unpriced (free) at time of writing:

- `swe-2-high` — promo until 2026-10-16
- `swe-2-medium`, `swe-2-max`
- `swe-1-7*` — until 2026-09-16
- internal: `swe-check`, `subagent-default`, `memory-migration-default`,
  `compactor`, `summarizer`

`glm-5-2` and `swe-1-7*` left this list on 2026-09-16 (promo ended) and
are now billed (glm: 1.4/0.26/4.4, swe-1-7: 0.5/0.2/2.5 per 1M). Turns
from before that date remain free — verified: all glm-5-2 turns in the
local DB ran ≤ 2026-09-15.

Caution: the `model` response dimension is a display name for the
*session's* model — a `summarizer` turn can report e.g. "GPT-5.6 Luna
Medium Thinking". Only trust `generation_model` for billing; use the
display name only when `generation_model` is absent.
