"""Client for the ABRA Flexi (FlexiBee) REST API."""

from __future__ import annotations

from datetime import datetime


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
