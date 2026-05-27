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
