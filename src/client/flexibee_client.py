"""Client for the ABRA Flexi (FlexiBee) REST API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import requests
from keboola.http_client import HttpClient
from requests.adapters import HTTPAdapter
from requests_toolbelt.adapters.host_header_ssl import HostHeaderSSLAdapter
from urllib3.util.retry import Retry


class FlexiBeeClientError(Exception):
    """Raised for FlexiBee API errors that should surface to the user."""


# FlexiBee property types that can hold a record identifier.
_KEY_PROPERTY_TYPES = frozenset({"integer", "numeric"})

# Statuses worth another attempt. 429 (Too Many Requests) and 408 (Request Timeout)
# sit alongside the 5xx family: ABRA Flexi rate-limits per instance, so a burst of
# concurrent calls — several config rows refreshing their column / date-field
# pickers at once, or a run overlapping a sync action — gets one or more requests
# rejected even though the instance is healthy. Without 429 here a single throttled
# response failed the whole call, which surfaced in the UI as "Could not list date
# fields of evidence '<x>': ... 429 Too Many Requests".
_FB_RETRY_STATUSES = (408, 429, 500, 502, 503, 504)

# Retries are split by failure kind on purpose.
#
# Status retries (throttling, transient 5xx) get 5 attempts: a rate-limit burst is
# short and worth riding out. Connect/read retries stay at 3 — a host that is
# unreachable from the run environment should fail fast, and each attempt already
# costs up to the connect timeout below, so raising this only makes a dead host
# take longer to report.
_FB_STATUS_RETRIES = 5
_FB_CONNECT_RETRIES = 3

# Backoff waits: ~0, 1, 2, 4, 8 s => ~15 s of retrying in the worst case.
_FB_BACKOFF_FACTOR = 0.5
_FB_BACKOFF_MAX = 8

# Cap on a server-sent `Retry-After`. urllib3 honours that header in preference to
# the backoff schedule and by default allows it up to 6 hours (21600 s), which
# would let one throttled response hang a sync action long past the UI's patience.
# Capped low: if the instance wants minutes, the retries are better spent quickly
# and the user told to come back later (see `_describe_request_error`).
#
# Measured worst cases for the *status* path (persistent 429): ~15 s with no
# `Retry-After` (0/1/2/4/8), ~20 s when the server sends one (5 x the 4 s cap).
# Note this bounds throttling only — a host that accepts the connection and then
# stalls is bounded by the read timeout below (~4 x 60 s), not by these numbers.
_FB_RETRY_AFTER_MAX = 4


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
    # Two distinct shapes reach here, hence two checks:
    #   * a 429 that was never retried surfaces as requests.HTTPError, which
    #     carries `.response` and renders as "429 Client Error: ...";
    #   * a 429 that exhausted the retries surfaces as requests.RetryError, which
    #     has NO `.response` — the signal only survives in urllib3's message text
    #     ("Max retries exceeded ... too many 429 error responses").
    # Both patterns are matched literally rather than testing `"429" in message`,
    # because the message embeds the request URL and would false-positive on any
    # unrelated failure whose URL or identifiers happen to contain "429".
    is_rate_limited = status == 429 or "429 Client Error" in message or "too many 429" in message
    if is_rate_limited:
        message += (
            " — the ABRA Flexi instance is rate limiting this client. It was retried "
            f"{_FB_STATUS_RETRIES} times and still refused. Retry in a few minutes, "
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

        # Retry settings are deliberately NOT passed here: the policy is installed
        # below via _requests_retry_session and HttpClient's own retry arguments
        # would be inert (and misleading, since `_http.max_retries` would then read
        # as authoritative). `_build_retry` is the single source of truth.
        self._http = HttpClient(
            base_url=f"{self.base_url}/",
            auth=(self.username, self.password),
        )

        # HttpClient creates a fresh Session per request inside _request_raw →
        # _requests_retry_session, so we cannot pre-mount anything on a stored
        # session. Replacing the attribute on *this instance only* (not the class)
        # keeps the change fully scoped. Two reasons to replace it:
        #
        # 1. HttpClient builds its own urllib3 Retry from max_retries /
        #    backoff_factor / status_forcelist and exposes no way to bound
        #    `Retry-After` or to separate connect from status retries. We need
        #    both (see the retry constants above), so we install our own Retry.
        # 2. In tunnel+ssl mode the https:// adapter must be one that honours the
        #    Host header for hostname verification (RFC 6066 SNI + cert CN),
        #    because the TCP connection goes to 127.0.0.1 while the certificate
        #    is issued for the real host. Note the adapter must carry the retry
        #    policy too — mounting a bare HostHeaderSSLAdapter() would silently
        #    give the tunnelled path max_retries=0, i.e. no retries at all.
        use_host_header_adapter = bool(tunnel_original_host) and ssl_verify
        # Preserve the library's retryable-method set. urllib3's default omits POST
        # and PATCH, so dropping this would silently disable retries for any
        # non-idempotent call added later (every call site is a GET today).
        allowed_methods = self._http.allowed_methods

        def _patched_rrs(session=None):
            # MUST honour a session passed in by _request_raw: that is the one
            # carrying the auth and headers it just set. Building a fresh session
            # here instead would drop authentication on every request.
            s = session or requests.Session()
            adapter = HTTPAdapter(max_retries=self._build_retry(allowed_methods))
            s.mount("http://", adapter)
            if use_host_header_adapter:
                s.mount("https://", HostHeaderSSLAdapter(max_retries=self._build_retry(allowed_methods)))
            else:
                s.mount("https://", adapter)
            return s

        # A plain function on the instance, not a MethodType: instance attributes
        # are not descriptors, so HttpClient's `self._requests_retry_session(...)`
        # call passes only its own kwargs and no implicit self.
        self._http._requests_retry_session = _patched_rrs

    @staticmethod
    def _build_retry(allowed_methods=None) -> Retry:
        """Build the urllib3 retry policy shared by every adapter we mount.

        A fresh instance per adapter: urllib3 treats a Retry as immutable and
        derives new ones as attempts are consumed, so sharing one is harmless,
        but constructing per adapter keeps that assumption out of the picture.
        """
        kwargs = {} if allowed_methods is None else {"allowed_methods": allowed_methods}
        return Retry(
            total=_FB_STATUS_RETRIES,
            connect=_FB_CONNECT_RETRIES,
            read=_FB_CONNECT_RETRIES,
            status=_FB_STATUS_RETRIES,
            status_forcelist=_FB_RETRY_STATUSES,
            backoff_factor=_FB_BACKOFF_FACTOR,
            backoff_max=_FB_BACKOFF_MAX,
            respect_retry_after_header=True,
            retry_after_max=_FB_RETRY_AFTER_MAX,
            **kwargs,
        )

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
