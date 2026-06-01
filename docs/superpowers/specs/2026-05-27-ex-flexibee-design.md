# ex-flexibee — Design Spec

> Type: extractor
> Component ID: keboola.ex-abra-flexi
> Status: draft
> Date: 2026-05-27

## 1. Overview & source system

`keboola.ex-abra-flexi` extracts records from **ABRA Flexi** (formerly FlexiBee), a Czech
cloud/on-premise accounting & ERP system, into Keboola Storage. A user picks one or more
*evidence types* (e.g. issued invoices, address book, bank movements) and the component pulls
them as tables, optionally restricted to a rolling date window on each record's `lastUpdate`.

- **Source system:** ABRA Flexi / FlexiBee REST API — <https://www.flexibee.eu/api/>
- **Primary use case:** loading accounting/ERP data (invoices, contacts, payments, orders) into
  Keboola for analytics and downstream reporting.

ABRA Flexi exposes ~249 evidence types via a uniform REST interface; ~50 are marked `SUPPORTED`.
The component is intentionally **generic over evidence types** rather than hardcoding a fixed set,
because which evidences matter varies per customer.

## 2. Keboola mapping

How ABRA Flexi concepts map onto how Keboola runs a component:

| ABRA Flexi concept | Keboola construct |
|---|---|
| One evidence type (e.g. `faktura-vydana`) | One **config row** → one output table |
| Connection (host, company, credentials) | **Root config** parameters, shared across rows |
| Record `id` | Output table **primary key** |
| Rolling window on `lastUpdate` | Row param `date_from`/`date_to` → URL path filter |
| Record list response | Rows in the output table (nested ref fields flattened) |
| Password | `#password` (encrypted config key) |
| "Does this connection work?" | `test_connection` **sync action** |
| "Which evidences exist?" | `list_evidences` **sync action** → row dropdown |

**Config rows, not single config.** One row = one evidence type. Rationale: each evidence is an
independent table with its own date window and load behaviour; rows give per-evidence configuration,
independent enable/disable, and optional platform parallelism. Per the platform, credentials live in
the root config and are merged into each row at runtime — the component always receives a single
flattened `config.json`.

**Incremental strategy — rolling window + upsert (no fragile state).** Rather than opaque
state-file watermarking, the user configures a date window on `lastUpdate`:

- `date_from` accepts relative expressions (`"5 days ago"`, `"last 5 days"`, `"yesterday"`) or
  absolute dates (`"2026-01-01"`), parsed with `keboola.utils.date.parse_datetime_interval`
  (already a dependency; wraps `dateparser`). Empty = full history.
- `date_to` defaults to now.
- The window is applied as a **URL path filter** (see §4 for the critical syntax caveat).
- Output uses Keboola **incremental load with primary key `id`**, so overlapping windows on
  successive runs **upsert** safely — no duplicates, no missed records, no reliance on a
  hand-maintained cursor.

This means `state.json` is **not required** for correctness. (A future v2 could add the Changes
API for delete-capture; see §9.)

**Output bucket / table naming.** Default bucket behaviour applies if enabled in the Developer
Portal (`in.c-keboola.ex-abra-flexi-{configId}`). Table names derive from the evidence path
(e.g. `faktura-vydana`). The component sets `destination` but is aware the platform may override it
under default-bucket mode.

## 3. Authentication & connection

- **Chosen auth: HTTP Basic.** ABRA Flexi's primary and recommended method; works headlessly with
  no admin-UI app registration. Credentials are a username + password the customer already has.
  Verified live against the public demo (`winstrom:winstrom`).
  - Rejected: session-token login (`POST /login-logout/login.json`) — adds a login/keep-alive
    lifecycle with no benefit for a short-lived extractor run. SAMLv2/OpenID are enterprise,
    on-prem-only, and overkill.
- **Connection: REST/JSON.** Base URL pattern `https://{host}[:{port}]/c/{company}/{evidence}.json`.
  Cloud uses standard HTTPS (443); on-premise typically uses port `5434`. The component accepts a
  full `base_url`, so both work without special-casing.
- **`ssl_verify` flag** (default true) lets on-prem users with self-signed certificates connect.

**Provisioning — no blockers.** Credentials are obtained headlessly; no vendor-side app
registration or admin clickthrough. A public demo (`https://demo.flexibee.eu/c/demo/`,
`winstrom:winstrom`) is live and data-rich (10,999 invoices), suitable for development and VCR
recording. Per the user, the team *may* have a real instance but is unsure it holds proper data —
so **demo is the source of truth**; an optional re-record against a real instance can happen before
release if its data proves richer.

## 4. Data model & endpoints

- **In scope for v1:** any evidence type the user configures (one row each). No fixed allow-list;
  the `list_evidences` sync action surfaces what the connected company exposes. Common targets:
  `faktura-vydana`, `faktura-prijata`, `adresar`, `banka`, `pokladni-pohyb`,
  `objednavka-vydana`, `objednavka-prijata`, `cenik`.
- **Response shape:** `{"winstrom": {"@rowCount": "N", "<evidence>": [ {record}, ... ]}}`.
  Records carry reference fields in three correlated forms, e.g. `mena`, `mena@ref`, `mena@showAs`.
  These are flattened to columns (the `@`-suffixed variants become `mena_ref`, `mena_showAs`).
- **Detail level:** default `detail=full` (all fields) per the user's choice. A row-level override
  to `summary` or a custom field list is exposed but defaults to full.
- **Pagination:** offset-based — `?start={n}&limit={n}`. The component loops, requesting
  `add-row-count=true` on the first page to learn `@rowCount`, then pages until exhausted. A
  page size of ~100–500 is a reasonable default.
- **Rate limits:** none documented. The component stays polite (sequential paging; modest page
  size) and retries transient 5xx with backoff.

### ⚠️ Critical API caveat — filters go in the URL path, not query string

This was verified live and **must** be respected by the implementation:

- `GET /c/demo/faktura-vydana.json?filter=...` → **filter silently ignored**, returns ALL rows,
  no error. A garbage filter also returns all rows. Using the query-string form would silently
  break incremental and quietly pull the full table every run.
- `GET /c/demo/faktura-vydana/(lastUpdate gt '2026-05-27T00:00:00+02:00').json` → **correct**;
  filter inside parentheses in the path is applied (verified: rowCount dropped from 10,999 to 4).

The client MUST build filters as path segments `(<expr>)` and URL-encode them. There must be a
test asserting that a filtered request returns fewer rows than an unfiltered one, to guard against
regressing to the query-string form.

- **WQL operators (verified):** use `gt` / `lt` — `ge` / `le` are **rejected** with
  "Špatný formát WQL dotazu" (bad WQL format). Range = `(lastUpdate gt '<from>' and lastUpdate lt '<to>')`.
- **Timestamp format (verified):** ISO 8601 **with offset and time component** is required;
  a date-only value (`'2026-05-27'`) is rejected. Format as `%Y-%m-%dT%H:%M:%S+00:00` (UTC).
- **Ordering:** `?order=lastUpdate@D` works (verified) if needed for diagnostics.
- **Line items / sub-records** (`*-polozka`) are separate evidences (`NOT_DIRECT`). v1 does **not**
  auto-expand them; a user who wants invoice lines adds `faktura-vydana-polozka` as its own row.
- **Bulk/async export:** not used — standard paged list endpoints are sufficient.

## 5. Configuration & schema

> Handoff: the actual `configSchema.json` / `configRowSchema.json` is built by
> **component-build-ui**. This section describes the fields only.

**Root config (`configSchema.json`):**

| Field | Type | Req | Notes |
|---|---|---|---|
| `base_url` | string | ✅ | e.g. `https://demo.flexibee.eu` or `https://host:5434` |
| `company` | string | ✅ | the `firma` identifier, e.g. `demo` |
| `username` | string | ✅ | |
| `#password` | string | ✅ | encrypted |
| `ssl_verify` | boolean | | default `true`; off for self-signed on-prem |

Sync actions on the root: **`test_connection`** (button) — calls `evidence-list.json` and reports
success/failure as a `UserException` on failure.

**Row config (`configRowSchema.json`):**

| Field | Type | Req | Notes |
|---|---|---|---|
| `evidence` | string (enum) | ✅ | populated by `list_evidences` sync-action dropdown |
| `date_from` | string | | relative (`"5 days ago"`, `"last 5 days"`) or absolute (`2026-01-01`); empty = all. Filters `lastUpdate ge`. |
| `date_to` | string | | default now; filters `lastUpdate le` |
| `detail` | string (enum) | | `full` (default) / `summary` / `custom` |
| `custom_fields` | string | | shown when `detail=custom`; comma-separated field list |
| `custom_filter` | string | | optional raw ABRA Flexi filter expression, AND-ed into the path filter (advanced) |
| `limit` | integer | | page size; default e.g. 200 |

Sync actions on the row: **`list_evidences`** — calls `evidence-list.json`, returns evidence
paths + human names for the `evidence` dropdown. Conditional UI: `custom_fields` shown only when
`detail=custom` (via `options.dependencies`).

## 6. Code architecture

- **API client module** (`src/client/flexibee_client.py`) separate from `component.py`:
  - Holds `base_url`, `company`, auth, `ssl_verify`.
  - `test_connection()` — lightweight `evidence-list` call.
  - `list_evidences()` — returns `[(path, name, importStatus), ...]`.
  - `iter_records(evidence, *, date_from, date_to, detail, custom_fields, custom_filter, limit)` —
    a generator that builds the **path filter** (the `(...)` form — never query string), pages via
    `start`/`limit`, and yields flattened record dicts.
  - Builds on `keboola.http_client.HttpClient` (retry/backoff for 5xx, base-URL handling).
- **`configuration.py`** — pydantic models (root + row) extending the existing scaffold; validates
  required fields and parses `date_from`/`date_to` via `dateparser`.
- **`component.py`** — thin orchestrator:
  - `run()`: build client → resolve date window → create out table def (incremental + PK `id`) →
    stream records from `iter_records` to CSV → write manifest.
  - Sync-action methods `test_connection` / `list_evidences` decorated with `@sync_action`.
  - Flattening helper for `@ref`/`@showAs` fields.
- **Error handling / exit codes:**
  - `UserException` (exit 1): auth failure (401), unknown evidence (404), invalid `date_from`
    that `dateparser` can't parse, unreachable host.
  - Unhandled (exit 2): genuinely unexpected errors.
- **Dependencies:** `keboola.component`, `dateparser`. Reuse `keboola.http_client` if available;
  otherwise `requests`.

## 7. Testing

- **Datadir tests** (`keboola.datadirtest`):
  - Happy path: fetch a small evidence with `detail=full`, assert output table + manifest + PK.
  - Incremental window: `date_from` set, assert the **filtered** result is smaller than unfiltered
    (the regression guard for the path-vs-query-string filter trap).
  - Full load: no `date_from`, overwrite mode.
  - Failure: bad credentials → exit 1; unknown evidence → exit 1; unparseable `date_from` → exit 1.
- **VCR functional tests** (`generate-vcr-tests`):
  - Record against the **public demo** (`demo.flexibee.eu`, `winstrom:winstrom`).
  - Cassettes: a paged `faktura-vydana` list, a filtered list, `evidence-list` (for sync actions),
    an auth-failure response.
  - **Sanitizers:** scrub the `Authorization` Basic header and any `#password`. The demo creds are
    public but still scrubbed for hygiene.
  - Optional: re-record against a real instance before release if its data is richer (per user).
- **Sync-action tests:** `test_connection` success + failure; `list_evidences` returns a non-empty
  list with expected keys.
- **Sample payloads already captured** during research (seed cassettes): `evidence-list.json`
  (249 evidences), `faktura-vydana` list (paged, with `@rowCount`), `adresar` list, a path-filtered
  `lastUpdate` response, and a `changes.json` sample. These can seed cassettes directly.

## 8. Deployment & validation (CF test project)

- Use **kbagent** to register `keboola.ex-abra-flexi` in the CF test project and create a test config:
  root config pointing at `demo.flexibee.eu`/`demo`/`winstrom`, with two rows
  (`faktura-vydana` incremental `date_from="30 days ago"`, `adresar` full).
- Run the config; a successful end-to-end run produces two Storage tables — `faktura-vydana`
  (dozens–hundreds of rows for a 30-day window) and `adresar` (a few hundred rows) — each with
  primary key `id` and flattened reference columns.
- Developer Portal publishing is out of scope until that workflow has a dedicated skill.

## 9. Open risks & blockers

1. **Filter-in-path trap (handled, but high-impact if regressed).** Query-string filters are
   silently ignored — a regression would quietly pull full tables every run. Mitigated by §4 and a
   dedicated datadir test. *Owner: implementation.*
2. **`lastUpdate` reliability for incremental.** Watermark/window strategies miss **hard deletes**
   and assume `lastUpdate` is bumped on every change. Acceptable for v1 (upsert semantics); v2 can
   add the **Changes API** (`/changes.json`, version cursor — verified live) for delete-capture.
3. **No documented rate limits.** Conservative paging + 5xx backoff; revisit if a real instance
   throttles. Low risk.
4. **Real test instance data unknown.** Demo is the dev/VCR source of truth; real-instance
   re-record is optional and gated on data quality. No blocker.
5. **Reference-field flattening shape.** `@ref`/`@showAs` flattening convention should be confirmed
   against a couple of evidence types during implementation so column names are stable across runs.
   Low risk.
