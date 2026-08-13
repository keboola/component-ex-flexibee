"""Date-value normalization for columns FlexiBee declares as `date`.

Regression [SUPPORT-17334]: FlexiBee returns whole-day dates with a UTC offset
appended (`2025-01-01+01:00`). Written verbatim into a column declared DATE,
Storage rejects the entire file:

    Table import error: Load error: ... Date '2025-01-01+01:00' is not recognized
    Row 3, column "..."["datSplat":9]
"""

from component import _date_keys_needing_reload, _normalize_date_columns, _normalize_date_value

# Shapes taken from a live `ucetni-denik` record: `date` columns carry an offset
# and no time part, `datetime` columns are full ISO-8601 timestamps.
_UCETNI_DENIK_TYPES = {
    "idUcetniDenik": "integer",
    "lastUpdate": "datetime",
    "datUcto": "date",
    "datSplat": "date",
    "datUhr": "date",
    "popis": "string",
}


def test_strips_offset_from_whole_day_date():
    assert _normalize_date_value("2025-01-01+01:00") == "2025-01-01"
    # DST puts the instance an hour further out.
    assert _normalize_date_value("2025-07-14+02:00") == "2025-07-14"
    assert _normalize_date_value("2025-01-01Z") == "2025-01-01"
    assert _normalize_date_value("2025-01-01-05:00") == "2025-01-01"
    assert _normalize_date_value("2025-01-01+0100") == "2025-01-01"


def test_leaves_everything_else_untouched():
    # Already clean.
    assert _normalize_date_value("2025-01-01") == "2025-01-01"
    # Empty dates come back as an empty string and must stay nullable, not "".strip()ed away.
    assert _normalize_date_value("") == ""
    # A datetime is a valid ISO-8601 timestamp that Storage accepts; do not touch it.
    assert _normalize_date_value("2025-01-15T21:52:24.742+01:00") == "2025-01-15T21:52:24.742+01:00"
    # Non-strings pass through unharmed.
    assert _normalize_date_value(None) is None
    assert _normalize_date_value(17) == 17
    # Not a date at all.
    assert _normalize_date_value("code:CZK") == "code:CZK"


def test_normalizes_only_date_typed_columns():
    records = [
        {
            "idUcetniDenik": "10737418690",
            "lastUpdate": "2025-01-15T21:52:24.742+01:00",
            "datUcto": "2025-01-01+01:00",
            "datSplat": "2025-01-31+01:00",
            "datUhr": "",
            "popis": "2025-01-01+01:00",
        }
    ]

    fixed = _normalize_date_columns(records, _UCETNI_DENIK_TYPES)

    assert fixed == 2
    assert records[0]["datUcto"] == "2025-01-01"
    assert records[0]["datSplat"] == "2025-01-31"
    # Empty stays empty, the datetime keeps its offset, and a string column that
    # merely happens to look like a date is NOT rewritten.
    assert records[0]["datUhr"] == ""
    assert records[0]["lastUpdate"] == "2025-01-15T21:52:24.742+01:00"
    assert records[0]["popis"] == "2025-01-01+01:00"


def test_no_metadata_leaves_records_untouched():
    """With no property metadata (the all-STRING fallback path) nothing changes."""
    records = [{"datUcto": "2025-01-01+01:00"}]
    assert _normalize_date_columns(records, {}) == 0
    assert records[0]["datUcto"] == "2025-01-01+01:00"


def test_missing_column_is_not_invented():
    """A declared date column absent from a record (summary/custom projection) is skipped."""
    records = [{"idUcetniDenik": "1"}]
    assert _normalize_date_columns(records, _UCETNI_DENIK_TYPES) == 0
    assert records == [{"idUcetniDenik": "1"}]


def test_date_primary_key_is_flagged_for_reload():
    """A `date` column in the primary key means earlier rows no longer match.

    Stripping the offset changes the key value, so an incremental upsert inserts
    instead of updating and the table silently double-counts. Automatic key
    detection cannot produce this (it only picks `id`-prefixed integer/numeric
    columns), but a hand-set primary key can.
    """
    # Hand-set date primary key -> must be flagged.
    assert _date_keys_needing_reload(["datUcto"], _UCETNI_DENIK_TYPES) == ["datUcto"]
    # Mixed key: only the date part is affected.
    assert _date_keys_needing_reload(["idUcetniDenik", "datUcto"], _UCETNI_DENIK_TYPES) == ["datUcto"]


def test_normal_primary_keys_are_not_flagged():
    """The keys auto-detection actually produces must never trip the warning."""
    assert _date_keys_needing_reload(["idUcetniDenik"], _UCETNI_DENIK_TYPES) == []
    # A datetime key keeps its offset, so its stored form is unchanged.
    assert _date_keys_needing_reload(["lastUpdate"], _UCETNI_DENIK_TYPES) == []
    # A string column is untouched by normalization.
    assert _date_keys_needing_reload(["popis"], _UCETNI_DENIK_TYPES) == []
    # No key at all, and no metadata at all.
    assert _date_keys_needing_reload([], _UCETNI_DENIK_TYPES) == []
    assert _date_keys_needing_reload(["datUcto"], {}) == []
