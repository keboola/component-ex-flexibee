from datetime import datetime
from unittest import mock

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


def test_build_lastupdate_wql_both_bounds():
    c = _client()
    wql = c.build_lastupdate_wql(
        datetime(2026, 5, 1, 0, 0, 0),
        datetime(2026, 5, 27, 23, 59, 59),
    )
    assert wql == ("lastUpdate gt '2026-05-01T00:00:00+00:00' and lastUpdate lt '2026-05-27T23:59:59+00:00'")


def test_build_lastupdate_wql_from_only():
    c = _client()
    wql = c.build_lastupdate_wql(datetime(2026, 5, 1, 0, 0, 0), None)
    assert wql == "lastUpdate gt '2026-05-01T00:00:00+00:00'"


def test_build_lastupdate_wql_none_returns_none():
    c = _client()
    assert c.build_lastupdate_wql(None, None) is None


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
