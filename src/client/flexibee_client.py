"""Client for the ABRA Flexi (FlexiBee) REST API."""

from __future__ import annotations

import json
from datetime import datetime

import requests
from keboola.http_client import HttpClient


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
        self._http = HttpClient(
            base_url=f"{self.base_url}/",
            auth=(self.username, self.password),
            max_retries=5,
            backoff_factor=0.5,
            status_forcelist=(500, 502, 503, 504),
        )

    def build_evidence_path(self, evidence: str, wql: str | None) -> str:
        """Build the relative endpoint path for an evidence list call.

        With a WQL expression the filter is embedded in the path as `(<wql>)`.
        """
        if wql:
            return f"c/{self.company}/{evidence}/({wql}).json"
        return f"c/{self.company}/{evidence}.json"

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
            try:
                data = self._http.get(endpoint_path=endpoint, params=params, verify=self.ssl_verify)
            except requests.RequestException as exc:
                raise FlexiBeeClientError(f"Request to evidence '{evidence}' failed: {exc}") from exc
            body = data.get("winstrom", {})
            page = body.get(evidence, [])
            if not page:
                break
            for record in page:
                yield self.flatten_record(record)
            if len(page) < limit:
                break
            start += limit
            first = False

    def list_evidences(self) -> list[tuple[str, str]]:
        """Return (evidencePath, evidenceName) pairs for the connected company."""
        endpoint = f"c/{self.company}/evidence-list.json"
        try:
            data = self._http.get(endpoint_path=endpoint, verify=self.ssl_verify)
        except requests.RequestException as exc:
            raise FlexiBeeClientError(f"Could not list evidences: {exc}") from exc
        evidences = data.get("evidences", {}).get("evidence", [])
        return [(e.get("evidencePath", ""), e.get("evidenceName", "")) for e in evidences]

    def test_connection(self) -> None:
        """Hit evidence-list to confirm auth/host. Raises FlexiBeeClientError on failure."""
        try:
            self.list_evidences()
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as a connection failure
            raise FlexiBeeClientError(f"Could not connect to ABRA Flexi: {exc}") from exc
