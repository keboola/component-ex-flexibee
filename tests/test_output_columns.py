"""Unit tests for `_resolve_output_columns` -- the Full/Incremental schema-stability guard.

FlexiBee omits null fields per record, including the `_ref`/`_showAs` siblings of
empty relations. A header built from the fetched-record union therefore shrinks
whenever a narrower result set (e.g. a tight incremental Date window) happens to
carry fewer populated optional fields than an earlier full run -- and then fails
to load into the wider table the full run created. `_resolve_output_columns`
anchors `detail=full` output to the evidence's declared metadata schema instead,
so the header is identical regardless of which fields any particular batch of
records happens to populate. `detail=summary` has no metadata to anchor to, so
`_summary_anchor_columns` supplies the same kind of run-independent anchor from
an unfiltered probe (see the second half of this file).
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from client.flexibee_client import EvidenceSchema, FlexiBeeClientError
from component import _resolve_output_columns, _summary_anchor_columns
from configuration import Configuration

_BASE_PARAMS = {
    "base_url": "https://demo.flexibee.eu",
    "company": "demo",
    "username": "winstrom",
    "#password": "winstrom",
}

# ucetni-denik-like evidence: an optional relation (`stredisko`) that may or may
# not be populated on any given record, plus a `select` field (`modulK`) whose
# `_showAs` sibling is likewise only emitted when the value is non-empty.
_SCHEMA = EvidenceSchema(
    types={
        "idUcetniDenik": "integer",
        "doklad": "string",
        "stredisko": "relation",
        "stredisko_ref": "string",
        "stredisko_showAs": "string",
        "modulK": "select",
        "modulK_showAs": "string",
    },
    id_column=None,
    key_candidates=("idUcetniDenik",),
)


def _cfg(evidence: str = "ucetni-denik", **params) -> Configuration:
    return Configuration(**_BASE_PARAMS, evidence=evidence, **params)


# ---------------------------------------------------------------------------
# detail=full: header anchored to the evidence metadata, not the record sample
# ---------------------------------------------------------------------------


def test_full_detail_header_identical_whether_or_not_optional_relation_is_populated():
    # Full→Incremental regression guard: a run whose records happen to populate the
    # optional `stredisko` relation (emitting `_ref`/`_showAs`) and a run whose records
    # leave it empty (so FlexiBee omits those siblings entirely) must produce the exact
    # same output header -- otherwise an incremental load fails against a wider table
    # a full run already created.
    cfg = _cfg()

    records_with_relation_populated = [
        {
            "idUcetniDenik": "1",
            "doklad": "D1",
            "stredisko": "code:100",
            "stredisko_ref": "/c/demo/stredisko/1.json",
            "stredisko_showAs": "100: Central",
            "modulK": "modulUcetni.FAV",
            "modulK_showAs": "Faktury vydané",
        }
    ]
    records_with_relation_empty = [
        {
            "idUcetniDenik": "2",
            "doklad": "D2",
            "modulK": "modulUcetni.FAV",
            "modulK_showAs": "Faktury vydané",
            # no stredisko / stredisko_ref / stredisko_showAs at all -- FlexiBee omits
            # empty relation siblings from the record entirely.
        }
    ]

    header_populated, dropped_populated, anchored_populated = _resolve_output_columns(
        cfg, _SCHEMA, records_with_relation_populated, ""
    )
    header_empty, dropped_empty, anchored_empty = _resolve_output_columns(cfg, _SCHEMA, records_with_relation_empty, "")

    assert header_populated == header_empty
    assert header_populated == _SCHEMA.columns
    assert dropped_populated == []
    assert dropped_empty == []
    assert anchored_populated is True
    assert anchored_empty is True


def test_full_detail_drops_observed_columns_not_in_schema():
    cfg = _cfg()
    records = [
        {
            "idUcetniDenik": "1",
            "doklad": "D1",
            "idDokl_evidencePath": "faktura-vydana",  # undeclared annotation column
        }
    ]

    header, dropped, anchored = _resolve_output_columns(cfg, _SCHEMA, records, "")

    assert header == _SCHEMA.columns
    assert "idDokl_evidencePath" not in header
    assert dropped == ["idDokl_evidencePath"]
    assert anchored is True


def test_full_detail_falls_back_to_observed_union_when_no_metadata():
    """`schema.columns` empty (metadata call unavailable) -> observed union, un-anchored."""
    cfg = _cfg()
    records = [
        {"idUcetniDenik": "1", "doklad": "D1"},
        {"idUcetniDenik": "2", "doklad": "D2", "stredisko": "code:100"},
    ]

    header, dropped, anchored = _resolve_output_columns(cfg, EvidenceSchema(), records, "")

    assert header == ["idUcetniDenik", "doklad", "stredisko"]
    assert dropped == []
    assert anchored is False


# ---------------------------------------------------------------------------
# detail=custom: header anchored to the requested projection
# ---------------------------------------------------------------------------


def test_custom_detail_header_is_the_requested_projection_regardless_of_records():
    cfg = _cfg(detail="custom", custom_fields="doklad,idUcetniDenik")

    # Records happen to carry an extra field beyond the requested projection --
    # the header must still be exactly what was requested.
    records = [{"doklad": "D1", "idUcetniDenik": "1", "stredisko": "code:100"}]

    header, dropped, anchored = _resolve_output_columns(cfg, _SCHEMA, records, "doklad,idUcetniDenik")

    assert header == ["doklad", "idUcetniDenik"]
    assert dropped == ["stredisko"]
    assert anchored is True


def test_custom_detail_header_matches_projection_even_when_records_miss_fields():
    cfg = _cfg(detail="custom", custom_fields="doklad,idUcetniDenik,stredisko")
    records = [{"doklad": "D1", "idUcetniDenik": "1"}]  # stredisko omitted on this record

    header, dropped, anchored = _resolve_output_columns(cfg, _SCHEMA, records, "doklad,idUcetniDenik,stredisko")

    assert header == ["doklad", "idUcetniDenik", "stredisko"]
    assert dropped == []
    assert anchored is True


def test_custom_detail_falls_back_to_observed_union_when_projection_empty():
    """An empty custom projection sends a bare `custom:` -- no anchor, observed union."""
    cfg = _cfg(detail="custom", custom_fields="")
    records = [{"doklad": "D1", "idUcetniDenik": "1"}]

    header, dropped, anchored = _resolve_output_columns(cfg, _SCHEMA, records, "")

    assert header == ["doklad", "idUcetniDenik"]
    assert dropped == []
    assert anchored is False


# ---------------------------------------------------------------------------
# detail=summary: header anchored to the unfiltered probe (summary_anchor), when available
# ---------------------------------------------------------------------------


def test_summary_detail_header_is_anchor_when_records_are_narrower():
    cfg = _cfg(detail="summary")
    anchor = ["a", "b", "c"]
    records = [{"a": "1", "b": "2"}]  # this run's records only expose a, b

    header, dropped, anchored = _resolve_output_columns(cfg, _SCHEMA, records, "", summary_anchor=anchor)

    assert header == anchor
    assert dropped == []
    assert anchored is True


def test_summary_detail_drops_observed_columns_not_in_anchor():
    cfg = _cfg(detail="summary")
    anchor = ["a", "b"]
    records = [{"a": "1", "b": "2", "extra": "x"}]  # extra beyond the anchor

    header, dropped, anchored = _resolve_output_columns(cfg, _SCHEMA, records, "", summary_anchor=anchor)

    assert header == anchor
    assert dropped == ["extra"]
    assert anchored is True


def test_summary_detail_falls_back_to_observed_union_without_anchor():
    cfg = _cfg(detail="summary")
    records = [{"a": "1"}, {"a": "1", "b": "2"}]

    header, dropped, anchored = _resolve_output_columns(cfg, _SCHEMA, records, "", summary_anchor=None)

    assert header == ["a", "b"]
    assert dropped == []
    assert anchored is False


# ---------------------------------------------------------------------------
# _summary_anchor_columns: the run-independent anchor source for detail=summary
# ---------------------------------------------------------------------------


class _StubProbeClient:
    """Client stand-in that records the call it received and replays fixed rows."""

    def __init__(self, probe_rows):
        self.calls: list[dict] = []
        self._probe_rows = probe_rows

    def iter_records(self, evidence, *, wql, detail, limit):
        self.calls.append({"evidence": evidence, "wql": wql, "detail": detail, "limit": limit})
        return iter(self._probe_rows)


class _StubErrorClient:
    """Client stand-in whose probe call raises `FlexiBeeClientError`."""

    def iter_records(self, evidence, *, wql, detail, limit):
        raise FlexiBeeClientError("evidence temporarily unavailable")


def test_summary_anchor_returns_none_for_non_summary_detail():
    cfg = _cfg(detail="full")

    assert _summary_anchor_columns(cfg, None, None, [{"a": "1"}]) is None
    assert _summary_anchor_columns(cfg, None, "some wql", [{"a": "1"}]) is None


def test_summary_anchor_unfiltered_run_uses_the_records_own_union():
    """`wql is None` -- this run's records already cover every record; no extra call."""
    cfg = _cfg(detail="summary")
    records = [{"a": "1"}, {"a": "1", "b": "2"}]

    result = _summary_anchor_columns(cfg, None, None, records)

    assert result == ["a", "b"]


def test_summary_anchor_unfiltered_run_with_no_records_returns_none():
    cfg = _cfg(detail="summary")

    assert _summary_anchor_columns(cfg, None, None, []) is None


def test_summary_anchor_filtered_run_probes_unfiltered_and_ignores_narrow_records():
    """`wql` set -- probes `wql=None` and returns the PROBE's union, not the narrow records'."""
    cfg = _cfg(detail="summary", evidence="faktura-vydana")
    wide_probe_rows = [{"a": "1", "b": "2", "c": "3"}]
    narrow_records = [{"a": "1"}]  # this run's (filtered) records only expose "a"
    client = _StubProbeClient(wide_probe_rows)

    result = _summary_anchor_columns(cfg, client, "lastUpdate gt '2026-05-27T00:00:00+00:00'", narrow_records)

    assert result == ["a", "b", "c"]
    assert client.calls == [
        {"evidence": "faktura-vydana", "wql": None, "detail": "summary", "limit": cfg.limit}
    ]


def test_summary_anchor_probe_is_bounded_by_limit():
    """A probe result wider than `cfg.limit` is only read up to `cfg.limit` rows (islice)."""
    cfg = _cfg(detail="summary", limit=3)
    consumed: list[int] = []

    def _unbounded_rows():
        for i in range(10):
            consumed.append(i)
            yield {f"col{i}": "v"}

    class _StubUnbounded:
        def iter_records(self, evidence, *, wql, detail, limit):
            return _unbounded_rows()

    result = _summary_anchor_columns(cfg, _StubUnbounded(), "some wql", [])

    assert result == ["col0", "col1", "col2"]
    assert len(consumed) == 3


def test_summary_anchor_probe_error_logs_warning_and_returns_none(caplog):
    cfg = _cfg(detail="summary")

    with caplog.at_level(logging.WARNING):
        result = _summary_anchor_columns(cfg, _StubErrorClient(), "some wql", [{"a": "1"}])

    assert result is None
    assert any("probe" in r.getMessage().lower() for r in caplog.records)
