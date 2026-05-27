# ex-flexibee Extractor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `keboola.ex-flexibee`, a config-row-based Keboola extractor that pulls any ABRA Flexi (FlexiBee) evidence type into a Storage table, with an optional rolling date window on `lastUpdate`.

**Architecture:** A thin `Component.run()` orchestrator reads the merged (root + row) config, builds a `FlexiBeeClient` (HTTP Basic auth over `keboola.http_client.HttpClient`), and streams paged records into one output table per row (incremental upsert on `id`). The client owns URL building — including the critical path-based WQL filter — pagination, and record flattening. Two sync actions (`testConnection`, `list_evidences`) back the UI.

**Tech Stack:** Python 3.13, `keboola-component`, `keboola-http-client`, `keboola-utils` (relative-date parsing), `pydantic` v2, `pytest`, `keboola.datadirtest`, `ruff`.

---

## Context the engineer must know

**The component is greenfield-scaffolded.** `src/component.py` and `src/configuration.py` currently hold cookiecutter "Hello World" example code that must be replaced. The data folder lives at `../data` relative to `src/` by default; `/data` is gitignored.

**Config rows.** This is a row-based component. The platform merges the root config's `parameters` with each row's `parameters` into a single `config.json` before the component runs. So `self.configuration.parameters` contains **both** connection fields (from root) and evidence fields (from row) flattened together. Test fixtures mirror this — a single `parameters` object.

**The ABRA Flexi filter trap (verified live, do not regress).** ABRA Flexi **silently ignores** a `?filter=` query-string parameter — it returns ALL rows with HTTP 200 and no error. Filters MUST be embedded in the URL **path** inside parentheses:

- ✅ `GET /c/demo/faktura-vydana/(lastUpdate gt '2026-05-01T00:00:00+00:00').json`
- ❌ `GET /c/demo/faktura-vydana.json?filter=lastUpdate gt '...'` (ignored, returns everything)

WQL specifics, all verified against `https://demo.flexibee.eu`:
- Operators are `gt` / `lt`. `ge` / `le` are **rejected** ("Špatný formát WQL dotazu").
- Timestamps need full ISO 8601 with time + offset. Date-only (`'2026-05-27'`) is **rejected**. Use `%Y-%m-%dT%H:%M:%S+00:00`.
- Range: `(lastUpdate gt '<from>' and lastUpdate lt '<to>')`.

**Response shape (verified against the public demo):** `{"winstrom": {"@rowCount": "N", "<evidence>": [ {record}, ... ]}}`. Records carry reference fields in correlated forms, e.g. `mena`, `mena@ref`, `mena@showAs`.

**Credentials for live/recording — `secrets.env` (gitignored), via substitution only:**
The repo root holds `secrets.env` with keys `USERNAME`, `PASSWORD`, `WEBSITE` (the base URL to log in to). **Hard rules:**
- **Never read, print, echo, or commit the values** — especially `PASSWORD`. `secrets.env` is gitignored (`*.env`).
- Use them **via shell/env substitution** only, e.g. `set -a; . ./secrets.env; set +a` then reference `$USERNAME` / `$PASSWORD` / `$WEBSITE` — never inline the literal value into a command, file, or log.
- Committed test fixtures (`config.json`) must **not** contain real credentials. Use a placeholder password and rely on VCR cassette replay (which matches on URL/method, not auth). Cassette sanitizers MUST scrub the `Authorization` header before anything is written to disk/committed.
- The company (`firma`) is the path segment after `/c/` in `WEBSITE` if present; otherwise determine it during recording from `WEBSITE`/the instance. Do not hardcode `demo`.
- The public demo (`https://demo.flexibee.eu/c/demo/`, `winstrom`/`winstrom`) remains a fallback for shape verification only; the real instance in `secrets.env` is the recording target.

**Verified library signatures:**
```python
from keboola.component import ComponentBase, UserException
from keboola.component.base import sync_action
from keboola.component.sync_actions import ValidationResult, SelectElement, MessageType
from keboola.http_client import HttpClient
from keboola.utils.date import parse_datetime_interval
```
- `HttpClient(base_url, max_retries=10, backoff_factor=0.3, status_forcelist=(500,502,504), auth=(user,pwd), ...)`; `.get(endpoint_path=None, params=None, headers=None, is_absolute_path=False, **kwargs) -> requests.Response`. Pass `verify=` through `**kwargs`. Returns a `requests.Response` — call `.json()`.
- `self.create_out_table_definition(name, primary_key=[...], incremental=True|False, schema=[...]) -> TableDefinition`; write CSV to `table_def.full_path`; then `self.write_manifest(table_def)`.
- `@sync_action('actionName')` on a `ComponentBase` method; return a `list[SelectElement]` for dropdowns or a `ValidationResult(message, type)` for test-connection; `comp.execute_action()` routes by `config.json` `action`.
- `parse_datetime_interval(period_from, period_to, strformat=None)` → `(datetime, datetime)` when `strformat=None`, else `(str, str)`. Accepts `"5 days ago"`, `"yesterday"`, `"2026-01-01"`. Raises `ValueError` if from > to. Wraps `dateparser`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/client/__init__.py` | Marks `client` a package (empty). |
| `src/client/flexibee_client.py` | `FlexiBeeClient` — URL building (path WQL filter), pagination, record flattening, `test_connection`, `list_evidences`, `iter_records`. The only place that talks HTTP. |
| `src/configuration.py` | Pydantic models: `Configuration` (merged root+row). Validation + date-window resolution. Replaces scaffold. |
| `src/component.py` | `Component(ComponentBase)` — `run()` orchestrator + two `@sync_action` methods. Replaces scaffold. |
| `component_config/configSchema.json` | Root (connection) UI schema. |
| `component_config/configRowSchema.json` | Row (evidence) UI schema. |
| `tests/test_flexibee_client.py` | Unit tests for URL building, flattening, pagination (mocked HTTP). |
| `tests/test_configuration.py` | Unit tests for config validation + date resolution. |
| `tests/functional/` | `keboola.datadirtest` cases (happy/incremental/full/error) with VCR cassettes. |

> **Handoff note:** `configSchema.json` / `configRowSchema.json` field *design* is owned by `component-build-ui`. Tasks 9–10 below give working baseline JSON so the component runs end-to-end; treat them as a starting point that `component-build-ui` may refine (conditional fields, dropdown wiring).

---

## Task 1: Add `dateparser` typing-safety dep check & create client package

**Files:**
- Create: `src/client/__init__.py`

- [ ] **Step 1: Confirm dependencies resolve**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv sync`
Expected: completes without error; `keboola-utils`, `keboola-http-client`, `keboola-component`, `pydantic` installed. (`dateparser` arrives transitively via `keboola-utils`.)

- [ ] **Step 2: Create the client package marker**

Create `src/client/__init__.py` with a single line:

```python
"""ABRA Flexi (FlexiBee) API client package."""
```

- [ ] **Step 3: Verify import path resolves**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run python -c "import sys; sys.path.insert(0,'src'); import client; print('ok')"`
Expected: prints `ok`

- [ ] **Step 4: Commit**

```bash
git add src/client/__init__.py
git commit -m "chore: add client package for flexibee component"
```

---

## Task 2: FlexiBeeClient — URL/path filter builder (the trap guard)

**Files:**
- Create: `src/client/flexibee_client.py`
- Test: `tests/test_flexibee_client.py`

- [ ] **Step 1: Write the failing test for path + filter building**

Create `tests/test_flexibee_client.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from client.flexibee_client import FlexiBeeClient  # noqa: E402


def _client():
    return FlexiBeeClient(
        base_url="https://demo.flexibee.eu",
        company="demo",
        username="winstrom",
        password="winstrom",
        ssl_verify=True,
    )


def test_build_evidence_path_no_filter():
    c = _client()
    assert c.build_evidence_path("faktura-vydana", wql=None) == "c/demo/faktura-vydana.json"


def test_build_evidence_path_with_filter_goes_in_path_not_query():
    c = _client()
    path = c.build_evidence_path(
        "faktura-vydana",
        wql="lastUpdate gt '2026-05-01T00:00:00+00:00'",
    )
    # Filter MUST be inside parentheses in the path, NOT a query string.
    assert path == "c/demo/faktura-vydana/(lastUpdate gt '2026-05-01T00:00:00+00:00').json"
    assert "?filter=" not in path
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_flexibee_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'client.flexibee_client'`

- [ ] **Step 3: Write minimal implementation**

Create `src/client/flexibee_client.py`:

```python
"""Client for the ABRA Flexi (FlexiBee) REST API."""

from __future__ import annotations


class FlexiBeeClientError(Exception):
    """Raised for FlexiBee API errors that should surface to the user."""


class FlexiBeeClient:
    """Talks to one ABRA Flexi company over HTTP Basic auth.

    All record filtering uses WQL embedded in the URL *path* inside parentheses.
    The `?filter=` query parameter is silently ignored by the API and must never be used.
    """

    def __init__(
        self,
        base_url: str,
        company: str,
        username: str,
        password: str,
        ssl_verify: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.company = company
        self.username = username
        self.password = password
        self.ssl_verify = ssl_verify

    def build_evidence_path(self, evidence: str, wql: str | None) -> str:
        """Build the relative endpoint path for an evidence list call.

        With a WQL expression the filter is embedded in the path as `(<wql>)`.
        """
        if wql:
            return f"c/{self.company}/{evidence}/({wql}).json"
        return f"c/{self.company}/{evidence}.json"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_flexibee_client.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add src/client/flexibee_client.py tests/test_flexibee_client.py
git commit -m "feat: flexibee client evidence path builder with path-based WQL filter"
```

---

## Task 3: FlexiBeeClient — WQL window builder from datetimes

**Files:**
- Modify: `src/client/flexibee_client.py`
- Test: `tests/test_flexibee_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_flexibee_client.py`:

```python
from datetime import datetime  # noqa: E402


def test_build_lastupdate_wql_both_bounds():
    c = _client()
    wql = c.build_lastupdate_wql(
        datetime(2026, 5, 1, 0, 0, 0),
        datetime(2026, 5, 27, 23, 59, 59),
    )
    assert wql == (
        "lastUpdate gt '2026-05-01T00:00:00+00:00' "
        "and lastUpdate lt '2026-05-27T23:59:59+00:00'"
    )


def test_build_lastupdate_wql_from_only():
    c = _client()
    wql = c.build_lastupdate_wql(datetime(2026, 5, 1, 0, 0, 0), None)
    assert wql == "lastUpdate gt '2026-05-01T00:00:00+00:00'"


def test_build_lastupdate_wql_none_returns_none():
    c = _client()
    assert c.build_lastupdate_wql(None, None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_flexibee_client.py -k wql -v`
Expected: FAIL — `AttributeError: 'FlexiBeeClient' object has no attribute 'build_lastupdate_wql'`

- [ ] **Step 3: Write minimal implementation**

Add `from datetime import datetime` to the top of `src/client/flexibee_client.py`, then add this method to `FlexiBeeClient`:

```python
    _WQL_TS_FORMAT = "%Y-%m-%dT%H:%M:%S+00:00"

    def build_lastupdate_wql(
        self,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> str | None:
        """Build a WQL `lastUpdate` window. Returns None when both bounds are absent.

        Uses `gt` / `lt` (the API rejects `ge` / `le`) and full ISO timestamps with
        offset (the API rejects date-only values).
        """
        clauses: list[str] = []
        if date_from is not None:
            clauses.append(f"lastUpdate gt '{date_from.strftime(self._WQL_TS_FORMAT)}'")
        if date_to is not None:
            clauses.append(f"lastUpdate lt '{date_to.strftime(self._WQL_TS_FORMAT)}'")
        if not clauses:
            return None
        return " and ".join(clauses)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_flexibee_client.py -k wql -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/client/flexibee_client.py tests/test_flexibee_client.py
git commit -m "feat: flexibee lastUpdate WQL window builder (gt/lt, ISO offset)"
```

---

## Task 4: FlexiBeeClient — record flattening

**Files:**
- Modify: `src/client/flexibee_client.py`
- Test: `tests/test_flexibee_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_flexibee_client.py`:

```python
def test_flatten_record_reference_fields():
    c = _client()
    record = {
        "id": "1",
        "kod": "FV001",
        "mena": "code:CZK",
        "mena@ref": "/c/demo/mena/31.json",
        "mena@showAs": "CZK: Ceska koruna",
    }
    flat = c.flatten_record(record)
    assert flat == {
        "id": "1",
        "kod": "FV001",
        "mena": "code:CZK",
        "mena_ref": "/c/demo/mena/31.json",
        "mena_showAs": "CZK: Ceska koruna",
    }


def test_flatten_record_list_values_json_encoded():
    c = _client()
    record = {"id": "2", "external-ids": ["ext:DATIVERY:abc"]}
    flat = c.flatten_record(record)
    assert flat["id"] == "2"
    assert flat["external-ids"] == '["ext:DATIVERY:abc"]'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_flexibee_client.py -k flatten -v`
Expected: FAIL — `AttributeError: ... has no attribute 'flatten_record'`

- [ ] **Step 3: Write minimal implementation**

Add `import json` to the top of `src/client/flexibee_client.py`, then add to `FlexiBeeClient`:

```python
    @staticmethod
    def flatten_record(record: dict) -> dict:
        """Flatten one FlexiBee record into a flat dict of stringy columns.

        `@`-suffixed reference variants (`x@ref`, `x@showAs`) become `x_ref` / `x_showAs`.
        List/dict values are JSON-encoded so they fit a single CSV cell.
        """
        flat: dict = {}
        for key, value in record.items():
            col = key.replace("@", "_")
            if isinstance(value, (list, dict)):
                flat[col] = json.dumps(value, ensure_ascii=False)
            else:
                flat[col] = value
        return flat
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_flexibee_client.py -k flatten -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/client/flexibee_client.py tests/test_flexibee_client.py
git commit -m "feat: flexibee record flattening for reference and list fields"
```

---

## Task 5: FlexiBeeClient — paginated record iteration (mocked HTTP)

**Files:**
- Modify: `src/client/flexibee_client.py`
- Test: `tests/test_flexibee_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_flexibee_client.py`:

```python
from unittest import mock  # noqa: E402


def _winstrom_page(evidence, records, row_count=None):
    body = {evidence: records}
    if row_count is not None:
        body["@rowCount"] = str(row_count)
    return {"winstrom": body}


def test_iter_records_paginates_until_exhausted():
    c = _client()
    page1 = _winstrom_page("faktura-vydana", [{"id": "1"}, {"id": "2"}], row_count=3)
    page2 = _winstrom_page("faktura-vydana", [{"id": "3"}])
    page3 = _winstrom_page("faktura-vydana", [])

    responses = []
    for body in (page1, page2, page3):
        resp = mock.Mock()
        resp.json.return_value = body
        responses.append(resp)

    c._http.get = mock.Mock(side_effect=responses)

    rows = list(c.iter_records("faktura-vydana", wql=None, detail="full", limit=2))

    assert [r["id"] for r in rows] == ["1", "2", "3"]
    # First call requests add-row-count and start=0; second start=2.
    first_params = c._http.get.call_args_list[0].kwargs["params"]
    second_params = c._http.get.call_args_list[1].kwargs["params"]
    assert first_params["start"] == 0
    assert first_params["limit"] == 2
    assert first_params["detail"] == "full"
    assert second_params["start"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_flexibee_client.py -k iter_records -v`
Expected: FAIL — `AttributeError: ... has no attribute '_http'` / `iter_records`

- [ ] **Step 3: Write minimal implementation**

Add `from keboola.http_client import HttpClient` to the imports of `src/client/flexibee_client.py`. Extend `__init__` to build the HTTP client (append after the existing attribute assignments):

```python
        self._http = HttpClient(
            base_url=f"{self.base_url}/",
            auth=(self.username, self.password),
            max_retries=5,
            backoff_factor=0.5,
            status_forcelist=(500, 502, 503, 504),
        )
```

Add the iterator method to `FlexiBeeClient`:

```python
    def iter_records(
        self,
        evidence: str,
        wql: str | None,
        detail: str = "full",
        custom_fields: str | None = None,
        limit: int = 200,
    ):
        """Yield flattened records for one evidence, paging via start/limit.

        `detail` is "full", "summary", or "custom:<fields>" — when `custom_fields`
        is given it overrides `detail` with `custom:<fields>`.
        """
        endpoint = self.build_evidence_path(evidence, wql)
        detail_value = f"custom:{custom_fields}" if custom_fields else detail
        start = 0
        first = True
        while True:
            params = {"start": start, "limit": limit, "detail": detail_value}
            if first:
                params["add-row-count"] = "true"
            response = self._http.get(endpoint_path=endpoint, params=params, verify=self.ssl_verify)
            body = response.json().get("winstrom", {})
            page = body.get(evidence, [])
            if not page:
                break
            for record in page:
                yield self.flatten_record(record)
            if len(page) < limit:
                break
            start += limit
            first = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_flexibee_client.py -k iter_records -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/client/flexibee_client.py tests/test_flexibee_client.py
git commit -m "feat: flexibee paginated record iteration over start/limit"
```

---

## Task 6: FlexiBeeClient — test_connection & list_evidences

**Files:**
- Modify: `src/client/flexibee_client.py`
- Test: `tests/test_flexibee_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_flexibee_client.py`:

```python
def test_list_evidences_returns_path_name_pairs():
    c = _client()
    body = {
        "evidences": {
            "evidence": [
                {"evidencePath": "faktura-vydana", "evidenceName": "Vydane faktury"},
                {"evidencePath": "adresar", "evidenceName": "Adresy firem"},
            ]
        }
    }
    resp = mock.Mock()
    resp.json.return_value = body
    c._http.get = mock.Mock(return_value=resp)

    result = c.list_evidences()
    assert result == [
        ("faktura-vydana", "Vydane faktury"),
        ("adresar", "Adresy firem"),
    ]


def test_test_connection_raises_on_http_error():
    c = _client()
    c._http.get = mock.Mock(side_effect=Exception("401 Unauthorized"))
    try:
        c.test_connection()
    except FlexiBeeClientError as e:
        assert "connect" in str(e).lower() or "401" in str(e)
    else:
        raise AssertionError("expected FlexiBeeClientError")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_flexibee_client.py -k "list_evidences or test_connection" -v`
Expected: FAIL — methods not defined

- [ ] **Step 3: Write minimal implementation**

Add to `FlexiBeeClient`:

```python
    def list_evidences(self) -> list[tuple[str, str]]:
        """Return (evidencePath, evidenceName) pairs for the connected company."""
        endpoint = f"c/{self.company}/evidence-list.json"
        response = self._http.get(endpoint_path=endpoint, verify=self.ssl_verify)
        evidences = response.json().get("evidences", {}).get("evidence", [])
        return [(e.get("evidencePath", ""), e.get("evidenceName", "")) for e in evidences]

    def test_connection(self) -> None:
        """Hit evidence-list to confirm auth/host. Raises FlexiBeeClientError on failure."""
        try:
            self.list_evidences()
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as a connection failure
            raise FlexiBeeClientError(f"Could not connect to ABRA Flexi: {exc}") from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_flexibee_client.py -k "list_evidences or test_connection" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/client/flexibee_client.py tests/test_flexibee_client.py
git commit -m "feat: flexibee list_evidences and test_connection"
```

---

## Task 7: Configuration model (root + row merged)

**Files:**
- Modify: `src/configuration.py` (replace scaffold entirely)
- Test: `tests/test_configuration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_configuration.py`:

```python
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from configuration import Configuration  # noqa: E402

BASE = {
    "base_url": "https://demo.flexibee.eu",
    "company": "demo",
    "username": "winstrom",
    "#password": "winstrom",
    "evidence": "faktura-vydana",
}


def test_minimal_config_valid():
    cfg = Configuration(**BASE)
    assert cfg.base_url == "https://demo.flexibee.eu"
    assert cfg.company == "demo"
    assert cfg.password == "winstrom"
    assert cfg.evidence == "faktura-vydana"
    assert cfg.detail == "full"
    assert cfg.ssl_verify is True
    assert cfg.limit == 200


def test_missing_required_field_raises_user_exception():
    from keboola.component import UserException

    data = dict(BASE)
    del data["company"]
    with pytest.raises(UserException):
        Configuration(**data)


def test_password_alias_mapping():
    cfg = Configuration(**BASE)
    assert cfg.password == "winstrom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_configuration.py -v`
Expected: FAIL — the scaffold `Configuration` has fields `print_hello` / `#api_token`, not these.

- [ ] **Step 3: Write minimal implementation**

Replace the entire contents of `src/configuration.py` with:

```python
import logging

from keboola.component.exceptions import UserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Configuration(BaseModel):
    """Merged root + row configuration the component receives at runtime."""

    model_config = ConfigDict(populate_by_name=True)

    # --- connection (root config) ---
    base_url: str
    company: str
    username: str
    password: str = Field(alias="#password")
    ssl_verify: bool = True

    # --- evidence (row config) ---
    evidence: str
    date_from: str = ""
    date_to: str = ""
    detail: str = "full"
    custom_fields: str = ""
    custom_filter: str = ""
    limit: int = 200

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            messages = [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Validation Error: {', '.join(messages)}")
        logging.debug("Configuration loaded for evidence '%s'", self.evidence)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_configuration.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/configuration.py tests/test_configuration.py
git commit -m "feat: flexibee merged root+row configuration model"
```

---

## Task 8: Date-window resolution helper

**Files:**
- Modify: `src/configuration.py`
- Test: `tests/test_configuration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_configuration.py`:

```python
from datetime import datetime  # noqa: E402


def test_resolve_window_both_empty_returns_none():
    cfg = Configuration(**BASE)
    date_from, date_to = cfg.resolve_window()
    assert date_from is None
    assert date_to is None


def test_resolve_window_absolute_dates():
    data = dict(BASE, date_from="2026-05-01", date_to="2026-05-27")
    cfg = Configuration(**data)
    date_from, date_to = cfg.resolve_window()
    assert isinstance(date_from, datetime)
    assert isinstance(date_to, datetime)
    assert date_from < date_to


def test_resolve_window_invalid_from_raises_user_exception():
    from keboola.component import UserException

    data = dict(BASE, date_from="not a date", date_to="today")
    cfg = Configuration(**data)
    with pytest.raises(UserException):
        cfg.resolve_window()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_configuration.py -k resolve_window -v`
Expected: FAIL — `AttributeError: 'Configuration' object has no attribute 'resolve_window'`

- [ ] **Step 3: Write minimal implementation**

Add `from datetime import datetime` and `from keboola.utils.date import parse_datetime_interval` to the imports of `src/configuration.py`, then add this method to `Configuration`:

```python
    def resolve_window(self) -> tuple[datetime | None, datetime | None]:
        """Resolve date_from/date_to strings into datetimes for the WQL window.

        Empty `date_from` => no lower bound (None). Empty `date_to` defaults to "now".
        Relative ("5 days ago") and absolute ("2026-05-01") strings are accepted.
        """
        if not self.date_from:
            return None, None
        date_to = self.date_to or "now"
        try:
            start, end = parse_datetime_interval(self.date_from, date_to)
        except (ValueError, TypeError) as exc:
            raise UserException(
                f"Invalid date range: date_from='{self.date_from}', date_to='{date_to}': {exc}"
            )
        return start, end
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_configuration.py -k resolve_window -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/configuration.py tests/test_configuration.py
git commit -m "feat: resolve relative/absolute date window for flexibee"
```

---

## Task 9: Component.run() orchestrator

**Files:**
- Modify: `src/component.py` (replace scaffold entirely)
- Test: covered end-to-end by datadir tests in Task 11 (no separate unit test here — `run()` is thin glue over already-tested units).

- [ ] **Step 1: Replace the scaffold component**

Replace the entire contents of `src/component.py` with:

```python
"""ABRA Flexi (FlexiBee) extractor component."""

import csv
import logging

from keboola.component import ComponentBase, UserException
from keboola.component.base import sync_action
from keboola.component.sync_actions import SelectElement, ValidationResult

from client.flexibee_client import FlexiBeeClient, FlexiBeeClientError
from configuration import Configuration


class Component(ComponentBase):
    def __init__(self):
        super().__init__()

    def _build_client(self, cfg: Configuration) -> FlexiBeeClient:
        return FlexiBeeClient(
            base_url=cfg.base_url,
            company=cfg.company,
            username=cfg.username,
            password=cfg.password,
            ssl_verify=cfg.ssl_verify,
        )

    def run(self):
        cfg = Configuration(**self.configuration.parameters)
        client = self._build_client(cfg)

        date_from, date_to = cfg.resolve_window()
        wql_parts = []
        window_wql = client.build_lastupdate_wql(date_from, date_to)
        if window_wql:
            wql_parts.append(window_wql)
        if cfg.custom_filter:
            wql_parts.append(cfg.custom_filter)
        wql = " and ".join(wql_parts) if wql_parts else None

        incremental = bool(cfg.date_from)
        logging.info("Extracting evidence '%s' (incremental=%s)", cfg.evidence, incremental)

        # Buffer records so we can compute the full column union before writing.
        # FlexiBee records have varying keys (e.g. `external-ids` appears only on some),
        # so a fixed first-row header would silently drop later fields. Evidence sizes
        # are modest (thousands of flat rows), so buffering is acceptable for v1.
        records = list(
            client.iter_records(
                cfg.evidence,
                wql=wql,
                detail=cfg.detail,
                custom_fields=cfg.custom_fields or None,
                limit=cfg.limit,
            )
        )

        columns: list[str] = []
        seen: set[str] = set()
        for record in records:
            for key in record:
                if key not in seen:
                    seen.add(key)
                    columns.append(key)

        if "id" not in seen:
            # Output requires a stable primary key; FlexiBee records always carry `id`.
            columns.insert(0, "id")

        table = self.create_out_table_definition(
            f"{cfg.evidence}.csv",
            primary_key=["id"],
            incremental=incremental,
            schema=columns,
        )

        with open(table.full_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow(record)

        if not records:
            logging.warning("No records returned for evidence '%s'", cfg.evidence)

        self.write_manifest(table)
        logging.info("Wrote %d rows for evidence '%s'", len(records), cfg.evidence)

    @sync_action("testConnection")
    def test_connection(self) -> ValidationResult:
        cfg = Configuration(**self.configuration.parameters)
        client = self._build_client(cfg)
        try:
            client.test_connection()
        except FlexiBeeClientError as exc:
            raise UserException(str(exc))
        return ValidationResult("Connection successful.")

    @sync_action("listEvidences")
    def list_evidences(self) -> list[SelectElement]:
        cfg = Configuration(**self.configuration.parameters)
        client = self._build_client(cfg)
        try:
            evidences = client.list_evidences()
        except Exception as exc:  # noqa: BLE001
            raise UserException(f"Could not list evidences: {exc}")
        return [SelectElement(value=path, label=f"{name} ({path})") for path, name in evidences]


if __name__ == "__main__":
    try:
        comp = Component()
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
```

> Note: `test_connection` / `list_evidences` call `Configuration(...)`; for the `listEvidences` action the row may not yet have `evidence` selected. The model defaults make `evidence` required, so the UI must supply connection fields for these actions. If `component-build-ui` needs these actions to run without `evidence`, relax `evidence` to optional there — flagged as a UI-coupling follow-up.

- [ ] **Step 2: Verify it imports and lints**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run ruff check src/ && uv run python -c "import sys; sys.path.insert(0,'src'); import component; print('ok')"`
Expected: ruff passes; prints `ok`

- [ ] **Step 3: Commit**

```bash
git add src/component.py
git commit -m "feat: flexibee component run orchestrator and sync actions"
```

---

## Task 10: Baseline config schemas

**Files:**
- Modify: `component_config/configSchema.json`
- Modify: `component_config/configRowSchema.json`

> Baseline only — `component-build-ui` owns final field design, conditional visibility, and dropdown wiring. These make the component runnable and the sync actions reachable.

- [ ] **Step 1: Write the root schema**

Replace `component_config/configSchema.json` with:

```json
{
  "type": "object",
  "title": "ABRA Flexi connection",
  "required": ["base_url", "company", "username", "#password"],
  "properties": {
    "base_url": {
      "type": "string",
      "title": "Base URL",
      "description": "e.g. https://demo.flexibee.eu or https://your-host:5434",
      "propertyOrder": 1
    },
    "company": {
      "type": "string",
      "title": "Company (firma)",
      "description": "Company identifier, e.g. demo",
      "propertyOrder": 2
    },
    "username": {
      "type": "string",
      "title": "Username",
      "propertyOrder": 3
    },
    "#password": {
      "type": "string",
      "title": "Password",
      "format": "password",
      "propertyOrder": 4
    },
    "ssl_verify": {
      "type": "boolean",
      "title": "Verify SSL certificate",
      "default": true,
      "propertyOrder": 5
    },
    "test_connection": {
      "type": "button",
      "format": "sync-action",
      "propertyOrder": 6,
      "options": {
        "async": {
          "label": "Test connection",
          "action": "testConnection"
        }
      }
    }
  }
}
```

- [ ] **Step 2: Write the row schema**

Replace `component_config/configRowSchema.json` with:

```json
{
  "type": "object",
  "title": "Evidence",
  "required": ["evidence"],
  "properties": {
    "evidence": {
      "type": "string",
      "title": "Evidence type",
      "description": "ABRA Flexi evidence path to extract",
      "format": "select",
      "options": {
        "async": {
          "label": "Re-load evidence types",
          "action": "listEvidences",
          "autoload": ["parameters.base_url", "parameters.company", "parameters.username"]
        }
      },
      "propertyOrder": 1
    },
    "date_from": {
      "type": "string",
      "title": "Date from (lastUpdate)",
      "description": "Relative ('5 days ago') or absolute ('2026-01-01'). Empty = full history. Enables incremental upsert.",
      "propertyOrder": 2
    },
    "date_to": {
      "type": "string",
      "title": "Date to (lastUpdate)",
      "description": "Relative or absolute. Empty = now.",
      "propertyOrder": 3
    },
    "detail": {
      "type": "string",
      "title": "Detail level",
      "enum": ["full", "summary", "custom"],
      "default": "full",
      "propertyOrder": 4
    },
    "custom_fields": {
      "type": "string",
      "title": "Custom fields",
      "description": "Comma-separated field list (used when Detail level = custom)",
      "propertyOrder": 5,
      "options": {
        "dependencies": {
          "detail": "custom"
        }
      }
    },
    "custom_filter": {
      "type": "string",
      "title": "Advanced WQL filter",
      "description": "Optional raw ABRA Flexi WQL, AND-ed into the lastUpdate window. Use gt/lt, full ISO timestamps.",
      "propertyOrder": 6
    },
    "limit": {
      "type": "integer",
      "title": "Page size",
      "default": 200,
      "propertyOrder": 7
    }
  }
}
```

- [ ] **Step 3: Validate JSON**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run python -c "import json; json.load(open('component_config/configSchema.json')); json.load(open('component_config/configRowSchema.json')); print('valid')"`
Expected: prints `valid`

- [ ] **Step 4: Commit**

```bash
git add component_config/configSchema.json component_config/configRowSchema.json
git commit -m "feat: flexibee baseline config + row schemas with sync actions"
```

---

## Task 11: Datadir functional test scaffolding + happy-path VCR

**Files:**
- Create: `tests/functional/__init__.py`
- Create: `tests/test_functional.py`
- Create: `tests/functional/happy_path/source/data/config.json`
- Create: `tests/functional/happy_path/expected/data/out/tables/faktura-vydana.csv.manifest` (created by recording, see steps)

> Uses `keboola.datadirtest`. The `generate-vcr-tests` skill is the canonical way to record cassettes against the demo. This task sets up the directory shape and one recorded happy-path case; defer the full matrix to `component-test` / `generate-vcr-tests`.

- [ ] **Step 1: Create the datadir test runner**

Create `tests/functional/__init__.py` (empty), and `tests/test_functional.py`:

```python
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from keboola.datadirtest import DataDirTester  # noqa: E402


class TestFunctional(unittest.TestCase):
    def test_functional(self):
        functional_tests_dir = Path(__file__).parent / "functional"
        tester = DataDirTester(functional_tests_dir=str(functional_tests_dir))
        tester.run()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Create the happy-path config fixture (placeholders, no real secrets)**

Create `tests/functional/happy_path/source/data/config.json`. Connection values are **placeholders** — the real instance comes from `secrets.env` via substitution at recording time (see Step 3). Use a narrow window to keep the cassette small:

```json
{
  "parameters": {
    "base_url": "https://flexibee.example.com",
    "company": "PLACEHOLDER_COMPANY",
    "username": "PLACEHOLDER_USER",
    "#password": "PLACEHOLDER_PASSWORD",
    "ssl_verify": true,
    "evidence": "faktura-vydana",
    "date_from": "2026-05-26",
    "date_to": "2026-05-27",
    "detail": "full",
    "limit": 200
  }
}
```

- [ ] **Step 3: Record the cassette against the real instance (substitution, sanitized)**

Follow `component-developer:generate-vcr-tests` to record. **Credentials rules (from the plan Context):**
- Load the real instance via env substitution: `set -a; . ./secrets.env; set +a`, then drive the recording with `$WEBSITE` / `$USERNAME` / `$PASSWORD`. Never inline or echo the password.
- Configure the VCR sanitizer to scrub the `Authorization` header (and any password) **before** the cassette is written. Verify the written cassette contains no credential material.
- After recording, the committed `config.json` keeps the placeholder values from Step 2; the VCR matcher must be configured (per generate-vcr-tests) so replay does not depend on the real host/credentials.

Expected: a sanitized cassette is created; `expected/data/out/tables/faktura-vydana.csv` and `.manifest` exist; manifest shows `primary_key: ["id"]` and `incremental: true`.

- [ ] **Step 3a: Audit the cassette for leaked secrets before committing**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && set -a; . ./secrets.env; set +a; grep -rqF "$PASSWORD" tests/functional/ && echo "LEAK FOUND - DO NOT COMMIT" || echo "clean"`
Expected: prints `clean`. (This greps for the secret without printing it. If it prints LEAK, fix the sanitizer before proceeding.)

- [ ] **Step 4: Run the datadir test in replay mode**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_functional.py -v`
Expected: PASS — replays the cassette, output matches `expected/`.

- [ ] **Step 5: Commit**

```bash
git add tests/functional/ tests/test_functional.py
git commit -m "test: flexibee happy-path datadir+VCR functional test"
```

---

## Task 12: Datadir error & full-load cases

**Files:**
- Create: `tests/functional/bad_credentials/source/data/config.json`
- Create: `tests/functional/full_load/source/data/config.json`
- (Cassettes + expected trees via generate-vcr-tests, as in Task 11.)

- [ ] **Step 1: Bad-credentials case (expects exit 1)**

Create `tests/functional/bad_credentials/source/data/config.json` (placeholders; recording uses `$USERNAME` from `secrets.env` with a deliberately wrong password to capture a 401):

```json
{
  "parameters": {
    "base_url": "https://flexibee.example.com",
    "company": "PLACEHOLDER_COMPANY",
    "username": "PLACEHOLDER_USER",
    "#password": "PLACEHOLDER_PASSWORD",
    "ssl_verify": true,
    "evidence": "faktura-vydana",
    "detail": "full",
    "limit": 200
  }
}
```

Add `tests/functional/bad_credentials/expected/data/out/` empty and a `tests/functional/bad_credentials/source/data/` exit-code expectation per `keboola.datadirtest` convention (an `expected-code` file containing `1`, or the framework's equivalent — see generate-vcr-tests).

- [ ] **Step 2: Full-load case (no date_from → overwrite, not incremental)**

Create `tests/functional/full_load/source/data/config.json` (placeholders; recording substitutes from `secrets.env`):

```json
{
  "parameters": {
    "base_url": "https://flexibee.example.com",
    "company": "PLACEHOLDER_COMPANY",
    "username": "PLACEHOLDER_USER",
    "#password": "PLACEHOLDER_PASSWORD",
    "ssl_verify": true,
    "evidence": "adresar",
    "detail": "full",
    "limit": 200
  }
}
```

- [ ] **Step 3: Record cassettes for both cases (substitution, sanitized)**

Follow `generate-vcr-tests` to record both against the real instance via `secrets.env` substitution (`set -a; . ./secrets.env; set +a`), with the `Authorization`-header sanitizer active. For `full_load`, assert the manifest shows `incremental: false` (no `date_from`). For `bad_credentials`, record with a deliberately wrong password (do **not** use `$PASSWORD`) so the instance returns 401 → the component maps it to exit code 1 (`UserException`). Re-run the Task 11 Step 3a leak audit over `tests/functional/` and confirm `clean` before committing.

- [ ] **Step 4: Run the full functional suite**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run pytest tests/test_functional.py -v`
Expected: PASS — all three cases (happy_path, bad_credentials, full_load).

- [ ] **Step 5: Commit**

```bash
git add tests/functional/
git commit -m "test: flexibee bad-credentials (exit 1) and full-load datadir cases"
```

---

## Task 13: Full test + lint gate and config.json sample refresh

**Files:**
- Modify: `data/config.json` (local run sample, gitignored — refresh for manual runs)

- [ ] **Step 1: Run the whole suite + lint**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee && uv run ruff check . && uv run pytest -v`
Expected: ruff clean; all unit + functional tests PASS.

- [ ] **Step 2: Build the local run sample from secrets.env (gitignored, no echo)**

`data/config.json` is gitignored. Build it from `secrets.env` via substitution so the real password is never typed or printed. Write a template and substitute:

```bash
cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee
set -a; . ./secrets.env; set +a
python3 - <<'PY'
import json, os
cfg = {"parameters": {
    "base_url": os.environ["WEBSITE"],
    "company": os.environ.get("COMPANY", os.environ.get("FIRMA", "")),
    "username": os.environ["USERNAME"],
    "#password": os.environ["PASSWORD"],
    "ssl_verify": True,
    "evidence": "faktura-vydana",
    "date_from": "7 days ago",
    "date_to": "now",
    "detail": "full",
    "limit": 200,
}}
with open("data/config.json", "w") as f:
    json.dump(cfg, f, indent=2)
print("wrote data/config.json")
PY
```

If `company`/`firma` is not a separate secret, derive it from `WEBSITE` (the segment after `/c/`) and set it manually in `data/config.json` afterward — without printing the password.

- [ ] **Step 3: Manual smoke run (optional, hits the real instance)**

Run: `cd /Users/matyasjirat/VSCodeProjects/Keboola/component-ex-flexibee/src && uv run python component.py`
Expected: writes `../data/out/tables/faktura-vydana.csv` with a header and recent rows; logs "Wrote N rows". If the real instance has sparse data, fall back to the public demo for shape confirmation.

- [ ] **Step 4: Commit (code/tests only; data/ is gitignored)**

```bash
git add -A
git commit -m "chore: flexibee full test+lint gate green" || echo "nothing to commit"
```

---

## Task 14: Deploy & validate in CF test project (kbagent)

**Files:** none (platform operation)

- [ ] **Step 1: Register & configure via kbagent**

Use `kbagent` to register `keboola.ex-flexibee` in the CF test project and create a config with two rows — `faktura-vydana` with `date_from="30 days ago"`, and `adresar` full load. Use the two-step dry-run → confirm → apply flow. For the connection, prefer the **real instance** from `secrets.env` (`WEBSITE`/`USERNAME`/`PASSWORD`); enter `#password` through kbagent's encryption/secure flow so it is stored encrypted and never printed in logs or command output. If putting real credentials in the shared CF test project is undesirable, fall back to the public demo connection. Do not echo the password.

- [ ] **Step 2: Run the configuration**

Trigger a run via kbagent. Expected end-to-end: two Storage tables created — `faktura-vydana` (dozens–hundreds of rows for a 30-day window, `incremental` upsert on `id`) and `adresar` (a few hundred rows, overwrite).

- [ ] **Step 3: Verify output**

Confirm both tables exist with primary key `id` and flattened reference columns (e.g. `mena_ref`, `mena_showAs`). Spot-check row counts are plausible.

---

## Self-Review notes (for the executor)

- **Spec coverage:** connection/auth (T7, T9), config rows (T10), rolling window on `lastUpdate` (T3, T8, T9), path-filter trap guard (T2 + T11 incremental cassette), pagination (T5), flattening (T4), detail=full default (T7), sync actions (T6, T9, T10), datadir+VCR tests with sanitizers (T11–T12), kbagent deploy (T14). Deletes-not-captured and Changes-API-v2 are documented non-goals in the spec.
- **Type consistency:** `FlexiBeeClient` methods (`build_evidence_path`, `build_lastupdate_wql`, `flatten_record`, `iter_records`, `list_evidences`, `test_connection`) and `Configuration` fields are referenced consistently across `component.py` and tests.
- **Known UI coupling follow-up (flagged in T9):** sync actions instantiate `Configuration`, which requires `evidence`. `component-build-ui` may need `evidence` optional for the `listEvidences` action to run before a selection exists.
