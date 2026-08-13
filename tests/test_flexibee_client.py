from datetime import datetime
from unittest import mock

from urllib3.util.retry import Retry

from client.flexibee_client import FlexiBeeClient, FlexiBeeClientError


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


def test_build_date_wql_both_bounds():
    c = _client()
    wql = c.build_date_wql(
        "lastUpdate",
        datetime(2026, 5, 1, 0, 0, 0),
        datetime(2026, 5, 27, 23, 59, 59),
    )
    assert wql == ("lastUpdate gte '2026-05-01T00:00:00+00:00' and lastUpdate lte '2026-05-27T23:59:59+00:00'")


def test_build_date_wql_from_only():
    c = _client()
    wql = c.build_date_wql("lastUpdate", datetime(2026, 5, 1, 0, 0, 0), None)
    assert wql == "lastUpdate gte '2026-05-01T00:00:00+00:00'"


def test_build_date_wql_none_returns_none():
    c = _client()
    assert c.build_date_wql("lastUpdate", None, None) is None


def test_build_date_wql_honours_custom_field():
    # The window column is configurable (the "Date field" picker), not hardcoded.
    c = _client()
    wql = c.build_date_wql("datVyst", datetime(2026, 5, 1, 0, 0, 0), None)
    assert wql == "datVyst gte '2026-05-01T00:00:00+00:00'"


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

    c._http.get = mock.Mock(side_effect=[page1, page2, page3])

    rows = list(c.iter_records("faktura-vydana", wql=None, detail="full", limit=2))

    assert [r["id"] for r in rows] == ["1", "2", "3"]
    # First call requests add-row-count and start=0; second start=2.
    first_params = c._http.get.call_args_list[0].kwargs["params"]
    second_params = c._http.get.call_args_list[1].kwargs["params"]
    assert first_params["start"] == 0
    assert first_params["limit"] == 2
    assert first_params["detail"] == "full"
    assert second_params["start"] == 2
    # Every request must carry the bounded (connect, read) timeout.
    assert c._http.get.call_args_list[0].kwargs["timeout"] == FlexiBeeClient._HTTP_TIMEOUT


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
    c._http.get = mock.Mock(return_value=body)

    result = c.list_evidences()
    assert result == [
        ("faktura-vydana", "Vydane faktury"),
        ("adresar", "Adresy firem"),
    ]


def test_iter_records_wraps_http_error():
    import requests

    c = _client()
    c._http.get = mock.Mock(side_effect=requests.HTTPError("401 Unauthorized"))
    try:
        list(c.iter_records("faktura-vydana", wql=None))
    except FlexiBeeClientError as e:
        assert "faktura-vydana" in str(e)
    else:
        raise AssertionError("expected FlexiBeeClientError")


def test_iter_records_wraps_timeout():
    import requests

    c = _client()
    c._http.get = mock.Mock(side_effect=requests.Timeout("Read timed out"))
    try:
        list(c.iter_records("adresar", wql=None))
    except FlexiBeeClientError as e:
        assert "adresar" in str(e)
    else:
        raise AssertionError("expected FlexiBeeClientError on timeout")


def test_get_evidence_schema_returns_name_to_type_map():
    c = _client()
    body = {
        "properties": {
            "property": [
                {"propertyName": "id", "type": "integer", "inId": "true"},
                {"propertyName": "kod", "type": "string"},
                {"propertyName": "sumOsv", "type": "numeric"},
                {"propertyName": "datObj", "type": "date"},
                {"propertyName": "lastUpdate", "type": "datetime"},
                {"propertyName": "postovniShodna", "type": "logic"},
                {"propertyName": "zamekK", "type": "select"},
            ]
        }
    }
    c._http.get = mock.Mock(return_value=body)
    types = c.get_evidence_schema("faktura-vydana").types
    assert types["id"] == "integer"
    assert types["kod"] == "string"
    assert types["sumOsv"] == "numeric"
    assert types["datObj"] == "date"
    assert types["lastUpdate"] == "datetime"
    assert types["postovniShodna"] == "logic"
    assert types["zamekK"] == "select"


def test_get_evidence_schema_expands_relation_siblings():
    c = _client()
    body = {
        "properties": {
            "property": [
                {"propertyName": "mena", "type": "relation"},
            ]
        }
    }
    c._http.get = mock.Mock(return_value=body)
    types = c.get_evidence_schema("faktura-vydana").types
    # The flattener emits `mena`, `mena_ref`, `mena_showAs`; all three must be typed.
    assert types == {"mena": "relation", "mena_ref": "string", "mena_showAs": "string"}


def test_get_evidence_schema_reads_key_from_in_id_flag():
    c = _client()
    body = {
        "properties": {
            "property": [
                {"propertyName": "id", "type": "integer", "inId": "true"},
                {"propertyName": "kod", "type": "string"},
            ]
        }
    }
    c._http.get = mock.Mock(return_value=body)
    schema = c.get_evidence_schema("faktura-vydana")
    assert schema.id_column == "id"
    assert schema.key_candidates == ("id",)


def test_get_evidence_schema_collects_key_candidates_in_declaration_order():
    # Derived evidences (ucetni-denik) flag no inId; their own key column comes
    # first, ahead of relational id* columns that are not unique per record.
    c = _client()
    body = {
        "properties": {
            "property": [
                {"propertyName": "idUcetniDenik", "type": "integer"},
                {"propertyName": "doklad", "type": "string"},
                {"propertyName": "idDokl", "type": "integer"},
                {"propertyName": "idPolozek", "type": "array"},
            ]
        }
    }
    c._http.get = mock.Mock(return_value=body)
    schema = c.get_evidence_schema("ucetni-denik")
    assert schema.id_column is None
    assert schema.key_candidates == ("idUcetniDenik", "idDokl")
    assert schema.columns == ["idUcetniDenik", "doklad", "idDokl", "idPolozek"]


def test_get_evidence_schema_ignores_id_prefixed_non_key_columns():
    # Only `id` or `id<Capital>...` columns are keys. A numeric field that merely
    # starts with the letters "id" (e.g. `idealniStav`) must NOT become a candidate.
    c = _client()
    body = {
        "properties": {
            "property": [
                {"propertyName": "idUcetniDenik", "type": "integer"},
                {"propertyName": "idealniStav", "type": "numeric"},
                {"propertyName": "identifikator", "type": "string"},
            ]
        }
    }
    c._http.get = mock.Mock(return_value=body)
    schema = c.get_evidence_schema("ucetni-denik")
    assert schema.key_candidates == ("idUcetniDenik",)


def test_get_evidence_schema_wraps_http_error():
    import requests

    c = _client()
    c._http.get = mock.Mock(side_effect=requests.HTTPError("500"))
    try:
        c.get_evidence_schema("faktura-vydana")
    except FlexiBeeClientError as e:
        assert "faktura-vydana" in str(e)
    else:
        raise AssertionError("expected FlexiBeeClientError")


def test_test_connection_raises_on_http_error():
    c = _client()
    c._http.get = mock.Mock(side_effect=Exception("401 Unauthorized"))
    try:
        c.test_connection()
    except FlexiBeeClientError as e:
        assert "connect" in str(e).lower() or "401" in str(e)
    else:
        raise AssertionError("expected FlexiBeeClientError")


def test_build_date_wql_bounds_are_inclusive():
    """Regression [SUPPORT-17334]: the window must include records ON the bounds.

    The Date Start / Date End fields read as "from this date" / "to this date", so
    a record dated exactly on a bound belongs in the result. The exclusive
    `gt` / `lt` this used to emit dropped them — on accounting evidences that
    silently lost the whole batch of entries dated the first day of the period.
    """
    c = _client()
    wql = c.build_date_wql(
        "datUcto",
        datetime(2025, 1, 1, 0, 0, 0),
        datetime(2025, 12, 31, 23, 59, 59),
    )
    assert wql == ("datUcto gte '2025-01-01T00:00:00+00:00' and datUcto lte '2025-12-31T23:59:59+00:00'")
    # The exclusive operators must not come back.
    assert " gt " not in wql
    assert " lt " not in wql


def test_build_date_wql_uses_operators_the_api_accepts():
    """ABRA Flexi accepts `gte` / `lte`; the short `ge` / `le` forms are rejected."""
    c = _client()
    wql = c.build_date_wql("lastUpdate", datetime(2026, 5, 1), datetime(2026, 5, 2))
    assert " ge " not in wql
    assert " le " not in wql
    assert "gte" in wql and "lte" in wql


def test_rate_limit_and_transient_statuses_are_retried():
    """Regression [SUPPORT-17334]: a throttled request must be retried, not surfaced.

    429 responses were failing the column / date-field sync actions outright
    because 429 was absent from the retry list.
    """
    retry = FlexiBeeClient._build_retry()
    assert 429 in retry.status_forcelist
    assert 408 in retry.status_forcelist
    # The pre-existing transient statuses stay covered.
    for status in (500, 502, 503, 504):
        assert status in retry.status_forcelist


def test_retry_after_is_bounded():
    """A server-sent `Retry-After` must not be able to hang a sync action.

    urllib3 honours `Retry-After` in preference to the backoff schedule and allows
    it up to 6 hours by default, so adding 429 to the forcelist would otherwise let
    one throttled response stall the UI far past its patience.
    """
    retry = FlexiBeeClient._build_retry()
    assert retry.respect_retry_after_header is True
    assert retry.retry_after_max == 4
    assert retry.retry_after_max < Retry().retry_after_max  # well under urllib3's 21600 s default
    # A generous server value is clamped, not obeyed.
    assert retry.parse_retry_after("600") == 4
    assert retry.parse_retry_after("2") == 2


def test_connect_retries_stay_lower_than_status_retries():
    """An unreachable host must still fail fast; only throttling gets the extra attempts."""
    retry = FlexiBeeClient._build_retry()
    assert retry.status == 5
    assert retry.connect == 3
    assert retry.read == 3
    assert retry.backoff_factor == 0.5
    assert retry.backoff_max == 8


def test_retry_policy_is_installed_on_both_schemes():
    """The custom Retry must actually reach the session HttpClient builds per request."""
    c = _client()
    session = c._http._requests_retry_session()
    for scheme in ("http://", "https://"):
        retry = session.get_adapter(scheme).max_retries
        assert retry.status == 5, scheme
        assert 429 in retry.status_forcelist, scheme
        assert retry.retry_after_max == 4, scheme


def test_rate_limit_error_message_explains_throttling():
    """A 429 that outlives the retries gets an actionable explanation, not a bare code."""
    import requests

    response = mock.Mock(status_code=429)
    exc = requests.HTTPError("429 Client Error: Too Many Requests for url: https://example.invalid", response=response)

    c = _client()
    with mock.patch.object(c._http, "get", side_effect=exc):
        try:
            c.get_evidence_schema("ucetni-denik")
        except FlexiBeeClientError as err:
            message = str(err)
        else:
            raise AssertionError("expected FlexiBeeClientError")

    assert "rate limiting" in message
    assert "ucetni-denik" in message


def test_rate_limit_hint_not_added_to_unrelated_errors():
    """A URL that merely contains "429" must not be reported as rate limiting.

    The rendered message embeds the request URL, so a substring test on "429"
    would mislabel any unrelated failure whose path or identifiers contain it.
    """
    import requests

    from client.flexibee_client import _describe_request_error

    response = mock.Mock(status_code=500)
    exc = requests.HTTPError(
        "500 Server Error: Internal Server Error for url: "
        "https://demo.flexibee.eu/c/demo/faktura-vydana/(id%20eq%20429).json",
        response=response,
    )
    assert "rate limiting" not in _describe_request_error(exc)


def test_rate_limit_hint_added_when_retries_are_exhausted():
    """requests.RetryError carries no `.response`; the signal is only in the text."""
    import requests

    from client.flexibee_client import _describe_request_error

    exc = requests.exceptions.RetryError(
        "HTTPSConnectionPool(host='demo.flexibee.eu', port=443): Max retries exceeded "
        "with url: /c/demo/ucetni-denik/properties.json (Caused by "
        "ResponseError('too many 429 error responses'))"
    )
    assert getattr(exc, "response", None) is None
    assert "rate limiting" in _describe_request_error(exc)


def test_date_typed_window_field_keeps_the_timestamp_format():
    """A `date`-typed window column (the reported config used `datUcto`) is windowed
    with the same full-timestamp value format as a `datetime` column.

    Verified against a live instance: a record stored as `2019-12-23+01:00` IS
    matched by `datVyst gte '2019-12-23T00:00:00+00:00' and datVyst lte
    '2019-12-23T00:00:00+00:00'` (same 4 rows as `eq`), i.e. the server compares
    the calendar date and the hardcoded `+00:00` offset does not shift a whole-day
    value out of the window on an instance running ahead of UTC. The API rejects
    date-only values, so the time component must stay.
    """
    c = _client()
    wql = c.build_date_wql("datUcto", datetime(2025, 1, 1, 0, 0, 0), datetime(2025, 1, 1, 0, 0, 0))
    assert wql == ("datUcto gte '2025-01-01T00:00:00+00:00' and datUcto lte '2025-01-01T00:00:00+00:00'")
    # Date-only values are rejected by the API — the time part must not be dropped.
    assert "T00:00:00" in wql
