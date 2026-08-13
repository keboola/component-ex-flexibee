"""Client for the ABRA Flexi (FlexiBee) REST API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import requests
from keboola.http_client import HttpClient
from requests_toolbelt.adapters.host_header_ssl import HostHeaderSSLAdapter


class FlexiBeeClientError(Exception):
    """Raised for FlexiBee API errors that should surface to the user."""


# FlexiBee property types that can hold a record identifier.
_KEY_PROPERTY_TYPES = frozenset({"integer", "numeric"})


def looks_like_key_column(name: str) -> bool:
    """True for FlexiBee id-style key columns: exactly ``id`` or ``id<Capital>…``.

    Matches ``id``, ``idUcetniDenik``, ``idDokl``; rejects incidental names such as
    ``idealniHodnota`` where ``id`` merely prefixes a lowercase word.
    """
    return name == "id" or (name.startswith("id") and len(name) > 2 and name[2].isupper())


def _describe_request_error(exc: Exception) -> str:
    """Render a failed request for the user, with a hint when it is rate limiting.

    A bare ``429 Client Error: Too Many Requests`` tells the user nothing they can
    act on. The retry schedule already rides out short bursts, so a 429 reaching
    this point means the instance is throttling harder than that — which is a
    property of the ABRA Flexi instance, not of the configuration.
    """
    message = str(exc)
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429 or "429" in message:
        message += (
            " — the ABRA Flexi instance is rate limiting this client. It was retried "
            f"{FlexiBeeClient._MAX_RETRIES} times and still refused. Retry in a few minutes, "
            "and avoid refreshing several configuration rows at the same time."
        )
    return message


@dataclass(frozen=True)
class EvidenceSchema:
    """Column metadata of one evidence, read from ``/properties.json``.

    `id_column` is the property FlexiBee flags with ``inId`` — the record key.
    Derived evidences (``ucetni-denik``, ``hlavni-kniha``, report views, …) flag
    none; most of them still carry their own key column under an evidence-specific
    name (``idUcetniDenik``, ``idObratovaPredvaha``, …), which is what
    `key_candidates` holds: the ``id``-prefixed integer properties in declaration
    order, the first one being the evidence's own key.
    """

    types: dict[str, str] = field(default_factory=dict)
    id_column: str | None = None
    key_candidates: tuple[str, ...] = ()

    @property
    def columns(self) -> list[str]:
        return list(self.types)


class FlexiBeeClient:
    """Talks to one ABRA Flexi company over HTTP Basic auth.

    All record filtering uses WQL embedded in the URL *path* inside parentheses.
    The `?filter=` query parameter is silently ignored by the API and must never be used.

    SSH tunnel + TLS
    ----------------
    When traffic is forwarded through an SSH tunnel the URL host becomes
    ``127.0.0.1:<local_port>`` — not the real server hostname.  TLS would
    fail hostname verification because the certificate is issued for the
    original host, not ``127.0.0.1``.

    Setting ``tunnel_original_host`` enables two complementary fixes:

    1. **HostHeaderSSLAdapter** is wired in via a monkey-patched
       ``_requests_retry_session`` on the ``HttpClient`` instance.  The
       adapter reads the ``Host`` request header and passes it as
       ``assert_hostname`` to urllib3, so the cert is validated against the
       real hostname instead of ``127.0.0.1``.

    2. Every ``_http.get()`` call receives ``headers={"Host": original_host}``
       so urllib3 also sends the right SNI value in the TLS Client Hello.

    When ``ssl_verify=False`` the existing behavior is unchanged — no adapter
    patching is needed because verification is disabled entirely.
    """

    # (connect, read) timeout in seconds. Bounds each HTTP attempt so an
    # unreachable or stalled host fails fast with a clear error instead of
    # hanging the whole job (e.g. when the instance is not reachable from the
    # run environment's network).
    _HTTP_TIMEOUT = (10, 60)

    # Statuses worth another attempt. 429 (Too Many Requests) and 408 (Request
    # Timeout) sit alongside the 5xx family: ABRA Flexi rate-limits per instance,
    # so a burst of concurrent calls — several config rows refreshing their column
    # / date-field pickers at once, or a run overlapping a sync action — gets one
    # or more requests rejected even though the instance is healthy. Without 429
    # here a single throttled response failed the whole call, which surfaced in
    # the UI as "Could not list date fields of evidence '<x>': ... 429 Too Many
    # Requests" and made evidence pickers unusable on busy instances.
    _RETRY_STATUSES = (408, 429, 500, 502, 503, 504)

    # 5 attempts with a 0.5 backoff factor => waits of ~0, 1, 2, 4, 8 s (~15 s of
    # retrying in the worst case). Deliberately bounded: sync actions must still
    # answer the UI well inside its timeout, so this rides out a short throttling
    # burst without turning a genuinely rate-limited instance into a hang.
    # urllib3's Retry honours a server-sent `Retry-After` header in preference to
    # the backoff schedule.
    _MAX_RETRIES = 5
    _BACKOFF_FACTOR = 0.5

    def __init__(
        self,
        base_url: str,
        company: str,
        username: str,
        password: str,
        ssl_verify: bool = True,
        tunnel_original_host: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.company = company
        self.username = username
        self.password = password
        self.ssl_verify = ssl_verify
        # When set, all requests carry a Host header pointing to this hostname so
        # TLS validation succeeds against the cert even though the actual TCP
        # connection goes to 127.0.0.1 (the local side of the SSH tunnel).
        self.tunnel_original_host = tunnel_original_host

        self._http = HttpClient(
            base_url=f"{self.base_url}/",
            auth=(self.username, self.password),
            max_retries=self._MAX_RETRIES,
            backoff_factor=self._BACKOFF_FACTOR,
            status_forcelist=self._RETRY_STATUSES,
        )

        # Patch the HttpClient instance to mount HostHeaderSSLAdapter when we
        # are in tunnel+ssl mode.  HttpClient creates a fresh Session per
        # request inside _request_raw → _requests_retry_session, so we cannot
        # pre-mount on a stored session.  Replacing the method on *this
        # instance only* (not the class) keeps the change fully scoped.
        if tunnel_original_host and ssl_verify:
            _original_rrs = self._http._requests_retry_session

            def _patched_rrs(session=None):
                # Let the original method build and configure the session
                # (attaches the retry HTTPAdapter to http:// and https://).
                s = _original_rrs(session=session)
                # Replace the plain https:// adapter with one that honours the
                # Host header for hostname verification (RFC 6066 SNI + cert CN).
                s.mount("https://", HostHeaderSSLAdapter())
                return s

            # Bind the patched version to the instance (not the class).
            import types

            self._http._requests_retry_session = types.MethodType(_patched_rrs, self._http)

    def _tunnel_headers(self) -> dict[str, str] | None:
        """Return ``{"Host": original_host}`` when a tunnel is active, else ``None``.

        Passing this to every ``_http.get()`` call ensures that:
        * the TLS Client Hello carries the correct SNI extension;
        * ``HostHeaderSSLAdapter`` reads it for ``assert_hostname``.
        When there is no tunnel the value is ``None`` and HttpClient simply
        uses no extra headers.
        """
        if self.tunnel_original_host:
            return {"Host": self.tunnel_original_host}
        return None

    def build_evidence_path(self, evidence: str, wql: str | None) -> str:
        """Build the relative endpoint path for an evidence list call.

        With a WQL expression the filter is embedded in the path as `(<wql>)`.
        """
        if wql:
            return f"c/{self.company}/{evidence}/({wql}).json"
        return f"c/{self.company}/{evidence}.json"

    _WQL_TS_FORMAT = "%Y-%m-%dT%H:%M:%S+00:00"

    def build_date_wql(
        self,
        field: str,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> str | None:
        """Build an **inclusive** WQL window over `field`. Returns None when both bounds are absent.

        `field` is the date/datetime column the window applies to, e.g.
        `lastUpdate`. Uses `gte` / `lte` and full ISO timestamps with offset (the
        API rejects date-only values).

        The bounds must be inclusive: Date Start / Date End are presented to the
        user as "from this date" / "to this date", so a record falling exactly on
        a bound belongs in the result. The exclusive `gt` / `lt` used previously
        silently dropped every record sitting on the boundary — most visibly on
        accounting evidences, where a whole batch of entries shares the first day
        of the period (opening entries dated 1 January).

        Note the operator spelling: ABRA Flexi accepts `gte` / `lte` (or the
        symbolic `>=` / `<=`), *not* `ge` / `le` — the short forms are rejected,
        which is why this used to fall back to the exclusive operators.
        """
        clauses: list[str] = []
        if date_from is not None:
            clauses.append(f"{field} gte '{date_from.strftime(self._WQL_TS_FORMAT)}'")
        if date_to is not None:
            clauses.append(f"{field} lte '{date_to.strftime(self._WQL_TS_FORMAT)}'")
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
                data = self._http.get(
                    endpoint_path=endpoint,
                    params=params,
                    headers=self._tunnel_headers(),
                    verify=self.ssl_verify,
                    timeout=self._HTTP_TIMEOUT,
                )
            except requests.RequestException as exc:
                raise FlexiBeeClientError(
                    f"Request to evidence '{evidence}' failed: {_describe_request_error(exc)}"
                ) from exc
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

    def get_evidence_schema(self, evidence: str) -> EvidenceSchema:
        """Return the column types and key metadata of one evidence.

        `relation` properties are expanded into the three flattened siblings
        (`x`, `x_ref`, `x_showAs` — all typed as `string`) to match how
        `flatten_record` emits them in record output.
        """
        endpoint = f"c/{self.company}/{evidence}/properties.json"
        try:
            data = self._http.get(
                endpoint_path=endpoint,
                headers=self._tunnel_headers(),
                verify=self.ssl_verify,
                timeout=self._HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise FlexiBeeClientError(
                f"Could not fetch properties for evidence '{evidence}': {_describe_request_error(exc)}"
            ) from exc
        properties = data.get("properties", {}).get("property", [])
        if isinstance(properties, dict):
            properties = [properties]
        types: dict[str, str] = {}
        id_column: str | None = None
        key_candidates: list[str] = []
        for prop in properties:
            name = prop.get("propertyName")
            typ = prop.get("type")
            if not name or not typ:
                continue
            types[name] = typ
            if typ == "relation":
                types[f"{name}_ref"] = "string"
                types[f"{name}_showAs"] = "string"
            elif typ == "select":
                # Enum/select fields also emit a human-readable `@showAs` sibling in
                # record output; declare it so it stays in the (stable) output schema
                # instead of being dropped as an undeclared column.
                types[f"{name}_showAs"] = "string"
            if prop.get("inId") == "true" and id_column is None:
                id_column = name
            if looks_like_key_column(name) and typ in _KEY_PROPERTY_TYPES:
                key_candidates.append(name)
        return EvidenceSchema(types=types, id_column=id_column, key_candidates=tuple(key_candidates))

    def list_evidences(self) -> list[tuple[str, str]]:
        """Return (evidencePath, evidenceName) pairs for the connected company."""
        endpoint = f"c/{self.company}/evidence-list.json"
        try:
            data = self._http.get(
                endpoint_path=endpoint,
                headers=self._tunnel_headers(),
                verify=self.ssl_verify,
                timeout=self._HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise FlexiBeeClientError(f"Could not list evidences: {_describe_request_error(exc)}") from exc
        evidences = data.get("evidences", {}).get("evidence", [])
        return [(e.get("evidencePath", ""), e.get("evidenceName", "")) for e in evidences]

    def test_connection(self) -> None:
        """Hit evidence-list to confirm auth/host. Raises FlexiBeeClientError on failure."""
        try:
            self.list_evidences()
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as a connection failure
            raise FlexiBeeClientError(f"Could not connect to ABRA Flexi: {exc}") from exc
