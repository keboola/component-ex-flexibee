"""Unit tests for `_resolve_output_columns` -- the Full/Incremental schema-stability guard.

FlexiBee omits null fields per record, including the `_ref`/`_showAs` siblings of
empty relations. A header built from the fetched-record union therefore shrinks
whenever a narrower result set (e.g. a tight incremental Date window) happens to
carry fewer populated optional fields than an earlier full run -- and then fails
to load into the wider table the full run created. `_resolve_output_columns`
anchors `detail=full` output to the evidence's declared metadata schema instead,
so the header is identical regardless of which fields any particular batch of
records happens to populate.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from client.flexibee_client import EvidenceSchema
from component import _resolve_output_columns
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

    header_populated, dropped_populated = _resolve_output_columns(cfg, _SCHEMA, records_with_relation_populated, "")
    header_empty, dropped_empty = _resolve_output_columns(cfg, _SCHEMA, records_with_relation_empty, "")

    assert header_populated == header_empty
    assert header_populated == _SCHEMA.columns
    assert dropped_populated == []
    assert dropped_empty == []


def test_full_detail_drops_observed_columns_not_in_schema():
    cfg = _cfg()
    records = [
        {
            "idUcetniDenik": "1",
            "doklad": "D1",
            "idDokl_evidencePath": "faktura-vydana",  # undeclared annotation column
        }
    ]

    header, dropped = _resolve_output_columns(cfg, _SCHEMA, records, "")

    assert header == _SCHEMA.columns
    assert "idDokl_evidencePath" not in header
    assert dropped == ["idDokl_evidencePath"]


# ---------------------------------------------------------------------------
# detail=custom: header anchored to the requested projection
# ---------------------------------------------------------------------------


def test_custom_detail_header_is_the_requested_projection_regardless_of_records():
    cfg = _cfg(detail="custom", custom_fields="doklad,idUcetniDenik")

    # Records happen to carry an extra field beyond the requested projection --
    # the header must still be exactly what was requested.
    records = [{"doklad": "D1", "idUcetniDenik": "1", "stredisko": "code:100"}]

    header, dropped = _resolve_output_columns(cfg, _SCHEMA, records, "doklad,idUcetniDenik")

    assert header == ["doklad", "idUcetniDenik"]
    assert dropped == ["stredisko"]


def test_custom_detail_header_matches_projection_even_when_records_miss_fields():
    cfg = _cfg(detail="custom", custom_fields="doklad,idUcetniDenik,stredisko")
    records = [{"doklad": "D1", "idUcetniDenik": "1"}]  # stredisko omitted on this record

    header, dropped = _resolve_output_columns(cfg, _SCHEMA, records, "doklad,idUcetniDenik,stredisko")

    assert header == ["doklad", "idUcetniDenik", "stredisko"]
    assert dropped == []


# ---------------------------------------------------------------------------
# No metadata available: fall back to the observed record-union
# ---------------------------------------------------------------------------


def test_no_metadata_falls_back_to_observed_column_union():
    cfg = _cfg()
    records = [
        {"idUcetniDenik": "1", "doklad": "D1"},
        {"idUcetniDenik": "2", "doklad": "D2", "stredisko": "code:100"},
    ]

    header, dropped = _resolve_output_columns(cfg, EvidenceSchema(), records, "")

    assert header == ["idUcetniDenik", "doklad", "stredisko"]
    assert dropped == []
