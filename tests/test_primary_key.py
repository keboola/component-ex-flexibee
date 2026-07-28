"""Unit tests for primary key resolution across evidence shapes.

Model: the `primary_key` config field is a single creatable list. Empty => the key
is auto-detected from the evidence metadata (with a uniqueness safety-check);
non-empty => those columns are used verbatim.
"""

import sys
from pathlib import Path

import pytest
from keboola.component import UserException

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from client.flexibee_client import EvidenceSchema
from component import _custom_fields_with_key, _resolve_primary_key
from configuration import Configuration

_BASE_PARAMS = {
    "base_url": "https://demo.flexibee.eu",
    "company": "demo",
    "username": "winstrom",
    "#password": "winstrom",
}

# faktura-vydana: standard evidence, `id` flagged with inId.
_STANDARD = EvidenceSchema(
    types={"id": "integer", "kod": "string"},
    id_column="id",
    key_candidates=("id",),
)
# ucetni-denik: derived evidence, no inId; own key first, relational idDokl second.
_DERIVED = EvidenceSchema(
    types={"idUcetniDenik": "integer", "doklad": "string", "idDokl": "integer"},
    id_column=None,
    key_candidates=("idUcetniDenik", "idDokl"),
)
# rozvaha-po-uctech: report view, no identifier column at all.
_KEYLESS = EvidenceSchema(types={"ucet": "string", "mena": "relation"})


def _cfg(evidence: str, **params) -> Configuration:
    return Configuration(**_BASE_PARAMS, evidence=evidence, **params)


# ---------------------------------------------------------------------------
# Auto-detection (primary_key left empty)
# ---------------------------------------------------------------------------


def test_auto_uses_in_id_column_on_standard_evidence():
    cfg = _cfg("faktura-vydana")
    records = [{"id": "1", "kod": "FV1"}]
    assert _resolve_primary_key(cfg, ["id", "kod"], _STANDARD, records) == ["id"]


def test_auto_uses_evidence_specific_key_on_derived_evidence():
    cfg = _cfg("ucetni-denik")
    records = [{"idUcetniDenik": "2147521808", "doklad": "00000005/16", "idDokl": "15173"}]
    columns = ["idUcetniDenik", "doklad", "idDokl"]
    assert _resolve_primary_key(cfg, columns, _DERIVED, records) == ["idUcetniDenik"]


def test_auto_skips_placeholder_id_returned_by_derived_evidence():
    # Derived evidences answer a `custom:id,...` request with `id = -1` on every
    # row; keying on it would collapse the whole table into a single record.
    cfg = _cfg("ucetni-denik")
    records = [{"id": "-1", "idUcetniDenik": "2147521808"}, {"id": "-1", "idUcetniDenik": "2147521809"}]
    schema = EvidenceSchema(
        types={"id": "integer", "idUcetniDenik": "integer"},
        key_candidates=("id", "idUcetniDenik"),
    )
    assert _resolve_primary_key(cfg, ["id", "idUcetniDenik"], schema, records) == ["idUcetniDenik"]


def test_auto_prefers_unique_candidate_over_non_unique_earlier_one():
    # Guards against silent row-collapse: if the first id* candidate is NOT unique
    # across the fetched records (e.g. a relational idDokl shared by many lines),
    # auto must skip it and pick a candidate that actually identifies a record.
    cfg = _cfg("ucetni-denik")
    schema = EvidenceSchema(
        types={"idDokl": "integer", "idUcetniDenik": "integer"},
        key_candidates=("idDokl", "idUcetniDenik"),
    )
    records = [
        {"idDokl": "15173", "idUcetniDenik": "2147521808"},
        {"idDokl": "15173", "idUcetniDenik": "2147521809"},
    ]
    assert _resolve_primary_key(cfg, ["idDokl", "idUcetniDenik"], schema, records) == ["idUcetniDenik"]


def test_auto_returns_no_key_when_no_candidate_is_unique():
    # Rather than silently upserting rows onto a non-unique key, drop the key.
    cfg = _cfg("ucetni-denik")
    schema = EvidenceSchema(types={"idDokl": "integer"}, key_candidates=("idDokl",))
    records = [{"idDokl": "15173"}, {"idDokl": "15173"}]
    assert _resolve_primary_key(cfg, ["idDokl"], schema, records) == []


def test_auto_falls_back_to_id_column_when_properties_are_unavailable():
    cfg = _cfg("faktura-vydana")
    records = [{"id": "1", "kod": "FV1"}]
    assert _resolve_primary_key(cfg, ["id", "kod"], EvidenceSchema(), records) == ["id"]


def test_auto_returns_no_primary_key_for_report_evidence():
    cfg = _cfg("rozvaha-po-uctech")
    records = [{"ucet": "311", "mena": "code:CZK"}]
    assert _resolve_primary_key(cfg, ["ucet", "mena"], _KEYLESS, records) == []


def test_auto_resolves_from_properties_when_no_records_were_returned():
    cfg = _cfg("ucetni-denik")
    assert _resolve_primary_key(cfg, [], _DERIVED, []) == ["idUcetniDenik"]


# ---------------------------------------------------------------------------
# Explicit columns (primary_key filled)
# ---------------------------------------------------------------------------


def test_explicit_key_uses_selected_columns():
    cfg = _cfg("rozvaha-po-uctech", primary_key=["ucet", "mena"])
    records = [{"ucet": "311", "mena": "code:CZK"}]
    assert _resolve_primary_key(cfg, ["ucet", "mena"], _KEYLESS, records) == ["ucet", "mena"]


def test_explicit_key_rejects_unknown_column():
    cfg = _cfg("rozvaha-po-uctech", primary_key=["nonexistent"])
    with pytest.raises(UserException, match="nonexistent"):
        _resolve_primary_key(cfg, ["ucet", "mena"], _KEYLESS, [{"ucet": "311"}])


def test_explicit_key_is_kept_even_when_not_unique():
    # The user chose these columns; respect them (a warning is logged) rather than
    # silently overriding — only auto-detection self-protects.
    cfg = _cfg("ucetni-denik", primary_key=["idDokl"])
    records = [{"idDokl": "15173"}, {"idDokl": "15173"}]
    assert _resolve_primary_key(cfg, ["idDokl"], _DERIVED, records) == ["idDokl"]


def test_explicit_key_accepts_comma_separated_string():
    cfg = _cfg("rozvaha-po-uctech", primary_key="ucet, mena")
    assert cfg.primary_key == ["ucet", "mena"]


# ---------------------------------------------------------------------------
# custom:<fields> projection must retain the key column
# ---------------------------------------------------------------------------


def test_custom_fields_gain_the_auto_detected_key_column():
    cfg = _cfg("ucetni-denik", detail="custom", custom_fields="doklad,datVyst")
    assert _custom_fields_with_key(cfg, _DERIVED) == "doklad,datVyst,idUcetniDenik"


def test_custom_fields_gain_the_user_selected_key_columns():
    cfg = _cfg(
        "rozvaha-po-uctech",
        detail="custom",
        custom_fields="ucet",
        primary_key=["ucet", "mena"],
    )
    assert _custom_fields_with_key(cfg, _KEYLESS) == "ucet,mena"


def test_custom_fields_are_untouched_without_custom_detail():
    cfg = _cfg("ucetni-denik")
    assert _custom_fields_with_key(cfg, _DERIVED) == ""
