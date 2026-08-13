"""Detection of records a date window can never return.

ABRA Flexi leaves date columns — notably `lastUpdate`, the component's default
window field — unset on some records. The API has no null test (`isempty` /
`isnotempty` are rejected with "Špatný formát WQL dotazu"), so a range predicate
can never match those records and they silently vanish from a windowed run.

Measured on a live instance: of 51,476 `ucetni-denik` records only 51,201 are
reachable by a range predicate on `lastUpdate` (275 have it empty), and 51,466 by
one on `datUcto` (10 empty). A filter covering the entire non-null domain
(`gte '1900-01-01' or lt '1900-01-01'`) returns the same 51,201 — confirming the
gap is null semantics, not a bound that is merely too narrow.
"""

import logging
from unittest import mock

from client.flexibee_client import FlexiBeeClient
from component import _unreachable_count, _warn_unreachable_records
from configuration import Configuration


def _client():
    return FlexiBeeClient(
        base_url="https://demo.flexibee.eu",
        company="demo",
        username="winstrom",
        password="winstrom",
    )


def _cfg(**overrides):
    params = {
        "base_url": "https://demo.flexibee.eu",
        "company": "demo",
        "username": "winstrom",
        "#password": "winstrom",
        "evidence": "ucetni-denik",
        "date_field": "lastUpdate",
        "date_from": "2025-01-01",
    }
    params.update(overrides)
    return Configuration(**params)


def test_presence_wql_covers_the_whole_non_null_domain():
    """A value is either at/after the sentinel or before it — empty is neither."""
    wql = _client().build_field_present_wql("lastUpdate")
    assert wql == ("lastUpdate gte '1900-01-01T00:00:00+00:00' or lastUpdate lt '1900-01-01T00:00:00+00:00'")
    # No null test is available; the union of two ranges is the workaround.
    assert "isempty" not in wql and "isnotempty" not in wql


def test_unreachable_count_arithmetic():
    assert _unreachable_count(51476, 51201) == 275
    assert _unreachable_count(51476, 51476) == 0
    # A reachable count above the total would mean a bad probe, not negative records.
    assert _unreachable_count(100, 120) == 0


def test_unreachable_count_is_none_when_a_count_is_missing():
    """Better to stay silent than to report a guessed number."""
    assert _unreachable_count(None, 10) is None
    assert _unreachable_count(10, None) is None
    assert _unreachable_count(None, None) is None


def test_count_records_reads_row_count_without_fetching_rows():
    c = _client()
    with mock.patch.object(c._http, "get", return_value={"winstrom": {"@rowCount": "51476"}}) as get:
        assert c.count_records("ucetni-denik", None) == 51476
    params = get.call_args.kwargs["params"]
    assert params["limit"] == 1 and params["add-row-count"] == "true"


def test_count_records_returns_none_when_row_count_absent_or_junk():
    c = _client()
    for body in ({"winstrom": {}}, {"winstrom": {"@rowCount": "not-a-number"}}, {}):
        with mock.patch.object(c._http, "get", return_value=body):
            assert c.count_records("ucetni-denik", None) is None


def test_warns_with_the_real_numbers(caplog):
    c = _client()
    with mock.patch.object(c, "count_records", side_effect=[51476, 51201]):
        with caplog.at_level(logging.WARNING):
            assert _warn_unreachable_records(c, _cfg(), "lastUpdate") == 275
    assert "275 of 51476" in caplog.text
    assert "lastUpdate" in caplog.text
    # The message must tell the user what to actually do.
    assert "clear Date Start" in caplog.text


def test_silent_when_every_record_is_reachable(caplog):
    c = _client()
    with mock.patch.object(c, "count_records", side_effect=[51476, 51476]):
        with caplog.at_level(logging.WARNING):
            assert _warn_unreachable_records(c, _cfg(), "datUcto") == 0
    assert caplog.text == ""


def test_custom_filter_containing_or_is_parenthesized():
    """BOTH sides must be wrapped, or an `or` escapes the intended AND.

    Measured against a live instance with an `or` in the custom filter: the bare
    form `(<presence>) and a or b` counted 22,927 reachable of 22,931 total and so
    reported 4 unreachable records, where the correctly parenthesized form counts
    13,319 and reports 9,612. A precedence slip here silently defeats the warning
    this whole feature exists to emit, so this test uses a filter that actually
    contains `or` — one without it cannot catch the bug.
    """
    c = _client()
    user_filter = "datUcto gte '2016-03-01T00:00:00+00:00' or datUcto gte '2025-01-01T00:00:00+00:00'"
    with mock.patch.object(c, "count_records", side_effect=[10, 10]) as counter:
        _warn_unreachable_records(c, _cfg(custom_filter=user_filter), "lastUpdate")
    total_wql = counter.call_args_list[0].args[1]
    reachable_wql = counter.call_args_list[1].args[1]

    # The total is counted within the same custom-filter context, or the difference
    # would wrongly include records the user never asked for. Alone it needs no
    # parentheses — there is no surrounding operator for an `or` to escape.
    assert total_wql == user_filter

    presence = c.build_field_present_wql("lastUpdate")
    assert reachable_wql == f"({presence}) and ({user_filter})"
    # Explicitly: neither operand may sit bare next to the AND.
    assert f"and {user_filter}" not in reachable_wql
    assert f"{presence} and" not in reachable_wql


def test_probe_failure_never_fails_the_run(caplog):
    """A diagnostic must not turn a working extraction into a failed job."""
    c = _client()
    with mock.patch.object(c, "count_records", side_effect=RuntimeError("boom")):
        with caplog.at_level(logging.WARNING):
            assert _warn_unreachable_records(c, _cfg(), "lastUpdate") is None
    assert caplog.text == ""
