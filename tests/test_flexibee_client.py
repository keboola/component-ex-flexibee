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
