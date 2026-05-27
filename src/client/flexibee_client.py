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
