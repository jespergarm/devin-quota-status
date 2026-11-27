# Security Policy

## Scope

This project is a local, unofficial monitoring utility. Its most sensitive
behavior is intentionally reading Devin Desktop's existing session token
from `%APPDATA%\devin\User\globalStorage\state.vscdb` so it can query
Devin's own quota endpoint.

The token must only be sent to Devin-controlled endpoints. It must never be
logged, embedded in examples, committed, uploaded, or sent to a third-party
endpoint. The only documented non-Devin endpoint is the unauthenticated
USD→DKK exchange-rate lookup at `api.frankfurter.dev`.

## Reporting a vulnerability

Please do **not** include any of the following in a public issue:

- session tokens, API keys, cookies, or authorization headers
- `state.vscdb` or excerpts from it
- `org_id.txt` or organization identifiers you do not want public
- `data\usage_log.csv`, session titles, prompts, code, or account/billing data
- screenshots showing private session names or account details

Open a minimal public issue without sensitive data, or use GitHub's private
vulnerability reporting if it is enabled for this repository. Redact local
paths, organization ids, account identifiers, and session titles.

## Data written locally

Runtime data is stored in `data\` next to the scripts and is excluded by
`.gitignore`. Depending on which tool is run, it may contain:

- quota percentage and reset-time history
- aggregate token/cost usage and model names
- session ids
- cached organization id
- cached exchange-rate state

This data is local-only, but it is not secret-free. Do not publish or share
the `data\` directory.

## Supply-chain posture

The runtime tools currently use only the Python standard library and the
Devin/VS Code extension host API. There are intentionally no npm or Python
runtime dependencies. Treat any future dependency addition as a security-
relevant change.
