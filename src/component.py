"""ABRA Flexi (FlexiBee) extractor component."""

import csv
import itertools
import logging
import re

from keboola.component import ComponentBase, UserException
from keboola.component.base import sync_action
from keboola.component.dao import BaseType, ColumnDefinition
from keboola.component.sync_actions import SelectElement, ValidationResult
from keboola.vcr import DefaultSanitizer

from client.flexibee_client import (
    EvidenceSchema,
    FlexiBeeClient,
    FlexiBeeClientError,
    looks_like_key_column,
)
from client.ssh_tunnel import open_tunnel
from configuration import Configuration, PrimaryKeyMode

# FlexiBee property types offered as the Date Start / Date End window column.
_DATE_PROPERTY_TYPES = frozenset({"date", "datetime"})

# Fallback date column when the user leaves the "Date field" empty. `lastUpdate`
# is the record-modification timestamp present on every evidence.
_DEFAULT_DATE_FIELD = "lastUpdate"

# FlexiBee `type` (from /properties.json) → Keboola Storage base type.
# `select` and `relation` map to STRING: enum values and FK identifiers are
# textual in practice. Unknown types also fall back to STRING.
_FLEXIBEE_TO_BASE_TYPE: dict[str, BaseType] = {
    "integer": BaseType.integer,
    "numeric": BaseType.numeric,
    "date": BaseType.date,
    "datetime": BaseType.timestamp,
    "logic": BaseType.boolean,
    "string": BaseType.string,
    "select": BaseType.string,
    "relation": BaseType.string,
}


# Values FlexiBee returns for the virtual `id` field of derived evidences: the
# column is emitted when explicitly requested but carries no record identity, so
# it must never become a primary key (every row would collapse into one).
_PLACEHOLDER_ID_VALUES = frozenset({"", "-1", "0"})


# FlexiBee returns whole-day `date` values with a UTC offset appended and no time
# part — `2025-01-01+01:00` (`+02:00` under DST). A column declared DATE cannot
# load that; Storage rejects the whole file with
# `Date '2025-01-01+01:00' is not recognized`. The offset carries no information
# for a whole-day value, so it is dropped and the plain calendar date is written.
# `datetime` values are left alone: they are full ISO-8601 timestamps
# (`2025-01-15T21:52:24.742+01:00`) that Storage accepts as-is.
_DATE_WITH_OFFSET = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[+-]\d{2}:?\d{2}|Z)$")


def _normalize_date_value(value):
    """Strip the trailing UTC offset from a whole-day FlexiBee `date` value.

    Anything that is not a bare `YYYY-MM-DD` plus offset — an empty string, a
    full timestamp, a non-string — is returned untouched.
    """
    if not isinstance(value, str):
        return value
    match = _DATE_WITH_OFFSET.match(value.strip())
    return match.group(1) if match else value


def _normalize_date_columns(records: list[dict], property_types: dict[str, str]) -> int:
    """Make `date`-typed columns loadable, in place. Returns the number of values fixed.

    Scoped deliberately to the columns FlexiBee declares as `date`, which are
    exactly the ones written with a DATE data type. Columns that fall back to
    STRING because no metadata was available are left untouched, so the untyped
    output path is unchanged.
    """
    date_columns = [col for col, typ in property_types.items() if typ == "date"]
    if not date_columns:
        return 0
    fixed = 0
    for record in records:
        for col in date_columns:
            value = record.get(col)
            normalized = _normalize_date_value(value)
            if normalized != value:
                record[col] = normalized
                fixed += 1
    return fixed


def _date_keys_needing_reload(primary_key: list[str], property_types: dict[str, str]) -> list[str]:
    """Primary-key columns whose stored form changed when the offset was stripped.

    Those rows no longer match what earlier runs wrote, so an incremental upsert
    inserts them again instead of updating. Empty list = nothing to warn about.
    """
    return [col for col in primary_key if property_types.get(col) == "date"]


def _build_typed_schema(
    columns: list[str],
    property_types: dict[str, str],
    primary_key: list[str] | None = None,
) -> dict[str, ColumnDefinition]:
    """Map observed CSV columns onto ColumnDefinitions using FlexiBee property types.

    Columns absent from `property_types` (or with unrecognized types) fall back
    to STRING — keeps the output safe when FlexiBee adds new fields without us
    updating the type map.
    """
    key_columns = set(primary_key or [])
    schema: dict[str, ColumnDefinition] = {}
    for col in columns:
        typ = property_types.get(col)
        builder = _FLEXIBEE_TO_BASE_TYPE.get(typ) if typ else None
        data_types = builder() if builder else BaseType.string()
        schema[col] = ColumnDefinition(
            data_types=data_types,
            primary_key=(col in key_columns),
            nullable=(col not in key_columns),
        )
    return schema


def _unreachable_count(total: int | None, reachable: int | None) -> int | None:
    """How many records a date window can never return, from two counts.

    `total` is every record (within the same non-date filter), `reachable` is those
    whose window column holds a value. The difference is the records with an empty
    window column, which no range predicate can match. Returns None when either
    count is unavailable, so callers can stay silent instead of reporting a guess.
    """
    if total is None or reachable is None:
        return None
    return max(0, total - reachable)


def _warn_unreachable_records(client: FlexiBeeClient, cfg: Configuration, date_field: str) -> int | None:
    """Warn when records are invisible to the date window because the column is empty.

    ABRA Flexi leaves `lastUpdate` unset on some records — a known quirk of the
    source system rather than something the component can correct. Because the API
    has no null test, such records cannot be brought into a windowed result at all;
    the only way to extract them is a run with no Date Start. Silently returning
    fewer rows than the evidence holds is the worst outcome, so the run says how
    many records are affected and what to do about it.

    Best-effort: two lightweight counting calls. Any failure is logged at debug and
    the run continues — this is a diagnostic, never a reason to fail an extraction.
    """
    try:
        presence = client.build_field_present_wql(date_field)
        # BOTH sides must be parenthesized. The presence expression contains `or`,
        # and the user's filter may too, so leaving either bare lets an `or` escape
        # the intended AND. Measured against a live instance with a custom filter
        # containing `or`: the bare form counted 22,927 reachable of 22,931 total
        # (reporting 4 unreachable) where the parenthesized form counts 13,319
        # (9,612 unreachable) — i.e. the bug would under-report by three orders of
        # magnitude, silently defeating the warning this function exists to emit.
        reachable_wql = f"({presence}) and ({cfg.custom_filter})" if cfg.custom_filter else presence
        total = client.count_records(cfg.evidence, cfg.custom_filter or None)
        reachable = client.count_records(cfg.evidence, reachable_wql)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail the run
        logging.debug("Could not check for records with an empty '%s': %s", date_field, exc)
        return None

    unreachable = _unreachable_count(total, reachable)
    if not unreachable:
        return unreachable

    logging.warning(
        "%d of %d records in evidence '%s' have an empty '%s' and therefore CANNOT be returned by any "
        "date window on that column — they are missing from this run's output. This is an ABRA Flexi "
        "data quirk (the column is sometimes left unset) and the API offers no way to filter for empty "
        "values. To extract them, clear Date Start to run without a window, or pick a Date field that is "
        "always populated on this evidence.",
        unreachable,
        total,
        cfg.evidence,
        date_field,
    )
    return unreachable


def _is_placeholder_column(column: str, records: list[dict]) -> bool:
    """True when every record carries a placeholder value in `column`."""
    return all(str(record.get(column, "")).strip() in _PLACEHOLDER_ID_VALUES for record in records)


def _is_unique(key: list[str], records: list[dict]) -> bool:
    """True when `key` takes a distinct value combination on every record."""
    seen = {tuple(str(record.get(col, "")) for col in key) for record in records}
    return len(seen) == len(records)


def _custom_fields_with_key(cfg: Configuration, schema: EvidenceSchema) -> str:
    """Return the custom field list extended with the columns the key needs.

    A `custom:` detail returns only the listed fields, so a key column left out
    of the list would be missing from the output and the table would lose its
    primary key.
    """
    if not cfg.custom_fields:
        return cfg.custom_fields

    if cfg.primary_key:
        needed = list(cfg.primary_key)
    else:
        # Auto mode: request EVERY declared key candidate (inId column + id* keys), so
        # whichever one _resolve_primary_key settles on survives the custom projection.
        # Requesting only the first would drop the key entirely when that column turns
        # out non-unique and the resolver falls through to a candidate that was never
        # fetched. The bare `id` is left out on purpose — on a derived evidence it comes
        # back as a `-1` placeholder (never chosen as key, only noise), and on a standard
        # evidence it is already carried here as `id_column`.
        needed = []
        for candidate in (schema.id_column, *schema.key_candidates):
            if candidate and candidate not in needed:
                needed.append(candidate)

    fields = [part.strip() for part in cfg.custom_fields.split(",") if part.strip()]
    fields.extend(col for col in needed if col not in fields)
    return ",".join(fields)


def _resolve_primary_key(
    cfg: Configuration,
    columns: list[str],
    schema: EvidenceSchema,
    records: list[dict],
) -> list[str]:
    """Resolve the output table primary key for one evidence.

    When the user filled the ``primary_key`` field those columns are used verbatim
    (validated against the evidence, with a warning if they are not unique across
    the fetched records). When it is empty the key is auto-detected from the
    evidence metadata: the property FlexiBee flags with ``inId`` (``id`` on standard
    evidences), otherwise the evidence's own ``id``-prefixed key column
    (``idUcetniDenik`` on ``ucetni-denik``). Among the candidates the first that is
    actually *unique* across the fetched records wins; if none is unique the table
    is loaded without a primary key rather than silently overwriting rows.
    Report-style evidences expose no identifier and also end up without a key.
    """
    available = columns or schema.columns

    if cfg.primary_key_mode == PrimaryKeyMode.none:
        return []

    # Explicit, user-selected key (creatable field). Trust the columns but warn
    # loudly if they do not actually identify a record.
    if cfg.primary_key:
        missing = [col for col in cfg.primary_key if available and col not in available]
        if missing:
            raise UserException(
                f"Primary key column(s) {', '.join(missing)} are not present in evidence '{cfg.evidence}'. "
                f"Available columns: {', '.join(available)}."
            )
        if records and not _is_unique(cfg.primary_key, records):
            logging.warning(
                "Primary key %s is not unique across the fetched records of '%s'; under incremental load "
                "rows sharing a key overwrite each other. Choose columns that are unique per record.",
                cfg.primary_key,
                cfg.evidence,
            )
        return list(cfg.primary_key)

    # Auto-detection: collect the eligible id-like candidates in priority order,
    # discarding placeholder columns (a derived evidence's `id` is -1 on every row).
    # Metadata-declared keys come first; when /properties.json was unavailable or did
    # not flag the key, fall back to id-like columns observed in the fetched data so a
    # derived evidence's own key (e.g. `idUcetniDenik`) is still found — otherwise a
    # transient metadata failure would silently load the table with no key and let an
    # incremental run append duplicate rows.
    observed_key_like = [col for col in available if looks_like_key_column(col)]
    candidates: list[str] = []
    for candidate in (schema.id_column, "id", *schema.key_candidates, *observed_key_like):
        if not candidate or candidate not in available or candidate in candidates:
            continue
        if records and _is_placeholder_column(candidate, records):
            logging.warning("Column '%s' holds no record identity in this evidence; skipping it.", candidate)
            continue
        candidates.append(candidate)

    # Prefer the first candidate that is unique across THIS RUN's fetched records —
    # not the whole Storage table, so a filtered or first run can look unique by
    # coincidence; set the Primary key explicitly to override. Without records
    # (metadata-only resolution) uniqueness cannot be checked, so the first
    # candidate — the evidence's own key by declaration order — is used.
    for candidate in candidates:
        if not records or _is_unique([candidate], records):
            logging.debug("Auto-detected primary key for evidence '%s': %s", cfg.evidence, candidate)
            return [candidate]

    if candidates:
        logging.warning(
            "No candidate key column %s is unique across the fetched records of '%s'; loading the table "
            "without a primary key to avoid silently overwriting rows. Set 'Primary key' explicitly if you "
            "know which columns identify a record.",
            candidates,
            cfg.evidence,
        )
        return []

    logging.warning(
        "Evidence '%s' exposes no identifier column, so the output table has no primary key. "
        "Set 'Primary key' to the columns that identify a record if you need incremental upsert.",
        cfg.evidence,
    )
    return []


def _observed_columns(records: list[dict]) -> list[str]:
    """Union of column names across records, in first-seen order."""
    columns: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in record:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns


def _summary_anchor_columns(
    cfg: Configuration,
    client: FlexiBeeClient,
    wql: str | None,
    records: list[dict],
) -> list[str] | None:
    """Return a run-independent column set for ``summary`` detail, or ``None`` if none.

    ``summary`` is a FlexiBee-defined projection that is *not* described by
    ``/properties.json``, so it has no metadata anchor. Like ``full`` detail it omits
    null fields per record, so a Date-filtered window can expose fewer columns than a
    wide run and then fail to load into the table that wide run built.

    The stable anchor is the column set of the evidence *unfiltered*:
    - When this run itself is unfiltered (``wql is None``) the fetched ``records`` already
      cover every record, so their union is the complete set — no extra call is made.
    - When the run is filtered, probe the unfiltered head (one page, bounded by
      ``limit``) and take its column union. Deterministic across runs (same query, same
      first page), so the header stays put even as the filtered window narrows.

    Returns ``None`` for non-``summary`` detail, when the probe yields nothing, or when
    the probe call fails — the caller then treats the header as un-anchored.
    """
    if cfg.detail != "summary":
        return None
    if wql is None:
        return _observed_columns(records) or None
    try:
        probe = list(
            itertools.islice(
                client.iter_records(cfg.evidence, wql=None, detail=cfg.detail, limit=cfg.limit),
                cfg.limit,
            )
        )
    except FlexiBeeClientError as exc:
        logging.warning(
            "Could not probe the unfiltered column set of '%s' to stabilise the summary header: %s",
            cfg.evidence,
            exc,
        )
        return None
    return _observed_columns(probe) or None


def _resolve_output_columns(
    cfg: Configuration,
    schema: EvidenceSchema,
    records: list[dict],
    custom_projection: str,
    summary_anchor: list[str] | None = None,
) -> tuple[list[str], list[str], bool]:
    """Return ``(header, dropped, anchored)`` for the output table.

    ``header`` is the output columns, ``dropped`` the columns left out to keep it stable,
    and ``anchored`` is ``True`` only when ``header`` came from a run-independent source.

    The header must be identical across runs for a given ``(evidence, detail)`` so an
    incremental (upsert) load never mismatches the columns of a table an earlier — often
    wider — run created. FlexiBee omits null fields per record, including the
    ``_ref``/``_showAs`` siblings of empty relations, so a header built from the fetched
    records alone shrinks with the result set: a narrow Date window returns fewer records,
    exposes fewer optional fields, and then fails to load into the table a full run built.

    Anchored sources (``anchored=True``):
    - ``full`` detail with metadata: the evidence schema (``/properties.json``).
    - ``custom`` detail with a projection: the requested field list — already fixed.
    - ``summary`` detail with a probe: the unfiltered column set (see
      :func:`_summary_anchor_columns`).
    Columns the run observes but the anchor does not declare are returned in ``dropped``
    (so the caller can warn) rather than allowed to destabilise the header.

    Un-anchored fallback (``anchored=False``): the observed column union of *this run's*
    records — used when metadata/projection/probe are all unavailable. It can drift
    between runs, so the caller must refuse to write it under an incremental load.
    """
    observed = _observed_columns(records)

    if cfg.detail == "custom":
        requested = [col.strip() for col in custom_projection.split(",") if col.strip()]
        if requested:
            return requested, [col for col in observed if col not in requested], True
        return observed, [], False

    if cfg.detail == "full":
        if schema.columns:
            header = list(schema.columns)
            return header, [col for col in observed if col not in header], True
        return observed, [], False

    if cfg.detail == "summary" and summary_anchor is not None:
        return summary_anchor, [col for col in observed if col not in summary_anchor], True

    return observed, [], False


# Picked up automatically by the datadirtest VCR recorder. Strips the HTTP Basic
# Authorization header (only content-type/length/accept are kept) and redacts
# password fields so no credentials are written to committed cassettes.
VCR_SANITIZERS = [
    DefaultSanitizer(additional_sensitive_fields=["#password", "password"]),
]


class Component(ComponentBase):
    def __init__(self):
        super().__init__()

    def _build_client(
        self,
        cfg: Configuration,
        tunnel_base_url: str | None = None,
        tunnel_original_host: str | None = None,
    ) -> FlexiBeeClient:
        """Construct a :class:`FlexiBeeClient` from the parsed configuration.

        When an SSH tunnel is active the caller passes the rewritten ``base_url``
        (pointing at ``127.0.0.1:<local_port>``) as ``tunnel_base_url`` and the
        real server hostname as ``tunnel_original_host``.  Those values are
        forwarded to :class:`FlexiBeeClient` so it can configure TLS correctly.
        When no tunnel is in use both extra arguments are ``None`` and the
        behavior is identical to the previous direct-connection path.
        """
        return FlexiBeeClient(
            base_url=tunnel_base_url or cfg.base_url,
            company=cfg.company,
            username=cfg.username,
            password=cfg.password,
            ssl_verify=cfg.ssl_verify,
            tunnel_original_host=tunnel_original_host,
        )

    def run(self):
        cfg = Configuration(**self.configuration.parameters)
        if not cfg.evidence:
            raise UserException("No evidence type selected. Choose an evidence type for this row.")
        if cfg.detail == "custom" and not cfg.custom_fields.strip():
            # `detail=custom` with no projection sends a bare `custom:` to the API and the
            # header can only come from the fetched window, which drifts between runs and
            # breaks the incremental load. Reject it here rather than at parse time so the
            # sync actions (test connection, list columns) still work on a half-filled row.
            raise UserException(
                "Detail is set to 'custom' but no custom fields were provided. List the columns to "
                "extract in 'Custom fields', or switch Detail to 'full' or 'summary'. A custom "
                "projection with no fields produces an unstable column set that fails the incremental load."
            )
        # Open the SSH tunnel when configured and enabled; it is a no-op
        # context manager otherwise so the direct-connection path is unchanged.
        with open_tunnel(cfg.ssh_tunnel, cfg.base_url) as (tunnel_base_url, tunnel_original_host):
            client = self._build_client(cfg, tunnel_base_url, tunnel_original_host)
            self._run_extraction(cfg, client)

    def _run_extraction(self, cfg: Configuration, client: FlexiBeeClient) -> None:
        """Execute the evidence extraction with a ready client.

        Separated from ``run()`` so the SSH tunnel context manager wraps
        the entire extraction without ``run()`` becoming too deeply nested.

        Fetch bounds come solely from the Date Start / Date End window on the
        configured date field (there is no stateful watermark); ``load_type``
        only decides whether Storage overwrites or upserts on the primary key.
        """

        date_from, date_to = cfg.resolve_window()
        date_field = cfg.date_field or _DEFAULT_DATE_FIELD
        wql_parts = []
        window_wql = client.build_date_wql(date_field, date_from, date_to)
        if window_wql:
            wql_parts.append(window_wql)
        if cfg.custom_filter:
            wql_parts.append(cfg.custom_filter)
        wql = " and ".join(wql_parts) if wql_parts else None

        incremental = cfg.incremental
        logging.info("Extracting evidence '%s' (load_type=%s)", cfg.evidence, cfg.load_type.value)
        if date_from or date_to:
            # Log the window that was actually applied, inclusive on both ends. An
            # empty Date End resolves to the run time (documented as "Empty = up to
            # now"), which means records dated ahead of the run are outside the
            # window — worth being able to see in the job log rather than having to
            # infer it from a row count.
            logging.info(
                "Applied filter: %s from %s to %s (both inclusive)%s",
                date_field,
                date_from.isoformat() if date_from else "(unbounded)",
                date_to.isoformat() if date_to else "(unbounded)",
                # The custom filter is AND-ed into the same WQL, so leaving it out
                # here would report the effective filter as narrower than it is.
                f", AND custom filter: {cfg.custom_filter}" if cfg.custom_filter else "",
            )

        # Only meaningful when a window is actually applied — without one every
        # record is returned regardless of whether the date column is populated.
        if window_wql:
            _warn_unreachable_records(client, cfg, date_field)

        # Evidence metadata: properties.json is the source of truth for each column's
        # FlexiBee type and for the record key. We fetch it best-effort — if the call
        # fails we degrade to an untyped (all-STRING) schema and key detection from the
        # returned columns rather than failing the run.
        evidence_schema = EvidenceSchema()
        try:
            evidence_schema = client.get_evidence_schema(cfg.evidence)
        except Exception as exc:  # noqa: BLE001 - native types are best-effort; STRING fallback is safe
            logging.warning("Skipping native types for '%s': %s", cfg.evidence, exc)
        property_types = evidence_schema.types

        # Buffer records before writing: we need the whole result set both to write every
        # row and to compute the observed-column union that `_resolve_output_columns` uses
        # as the fallback header when metadata is unavailable (summary / no-metadata paths).
        # Evidence sizes are modest (thousands of flat rows), so buffering is acceptable for v1.
        custom_projection = _custom_fields_with_key(cfg, evidence_schema)
        try:
            records = list(
                client.iter_records(
                    cfg.evidence,
                    wql=wql,
                    detail=cfg.detail,
                    custom_fields=custom_projection,
                    limit=cfg.limit,
                )
            )
        except FlexiBeeClientError as exc:
            raise UserException(str(exc))

        # Must run before the table is written: `date` columns arrive with a UTC
        # offset that a DATE column cannot parse (see _normalize_date_columns).
        fixed_dates = _normalize_date_columns(records, property_types)
        if fixed_dates:
            logging.debug("Normalized %d date value(s) to YYYY-MM-DD for evidence '%s'", fixed_dates, cfg.evidence)

        if not records and incremental:
            # An incremental (upsert) run that matched nothing new: leave the existing
            # table untouched rather than writing an empty one. A full load falls through
            # so it still overwrites (empties) its table with the run's actual result.
            logging.warning("No records returned for evidence '%s'; the output table is left unchanged.", cfg.evidence)
            if date_from:
                logging.warning(
                    "Evidence '%s' matched no records with %s newer than %s. To reload it from scratch — "
                    "for example after deleting the output table — widen or clear Date Start, or run it once "
                    "as full load.",
                    cfg.evidence,
                    date_field,
                    date_from.isoformat(),
                )
            return

        # Anchor the output columns to a run-independent source (metadata for full,
        # the requested projection for custom, the unfiltered column set for summary)
        # so the schema stays identical across runs — otherwise a narrow incremental
        # window fails to load into the wider table a full run created. `summary` has no
        # metadata to anchor to, so its stable column set comes from an unfiltered probe.
        summary_anchor = _summary_anchor_columns(cfg, client, wql, records)
        columns, dropped_columns, anchored = _resolve_output_columns(
            cfg, evidence_schema, records, custom_projection, summary_anchor
        )
        if dropped_columns:
            reason = (
                "returned beyond the requested custom projection"
                if cfg.detail == "custom"
                else "not declared in the evidence metadata"
            )
            logging.warning(
                "Evidence '%s': %d column(s) %s were left out of the typed output so the schema "
                "stays stable across runs: %s",
                cfg.evidence,
                len(dropped_columns),
                reason,
                ", ".join(sorted(dropped_columns)),
            )
        if not columns:
            # No column information from either the metadata/probe or the fetched records —
            # e.g. a full load that matched nothing while /properties.json was unavailable.
            # Fail rather than write an `id`-only table, which on a full load would overwrite
            # and shrink the existing Storage table to a single column.
            raise UserException(
                f"Could not determine any output columns for evidence '{cfg.evidence}': the metadata "
                f"call returned nothing and the run fetched no records. Retry once the source is reachable, "
                f"or widen the Date Start / Date End window so at least one record is returned."
            )
        if incremental and not anchored:
            # The header could only be taken from THIS run's filtered records — a narrower
            # window would then be missing columns the existing table already has, which
            # Storage rejects on an upsert. Fail loudly with the cause instead of writing a
            # table that mismatches. A full load is exempt: it overwrites the whole table,
            # so a reshaped column set is fine there.
            if cfg.detail == "full":
                cause = "the evidence metadata (/properties.json) was unavailable"
            elif cfg.detail == "summary":
                cause = "the unfiltered column probe was unavailable"
            else:
                cause = "no stable column source was available"
            raise UserException(
                f"Could not build a stable set of output columns for evidence '{cfg.evidence}' on an "
                f"incremental load: {cause}, so the columns could only be taken from this run's filtered "
                f"result. A narrower window would then be missing columns the existing table already has "
                f"and Storage would reject the load. Retry once the source is reachable, or run the row "
                f"once as full load to rebuild the table from the current column set."
            )

        primary_key = _resolve_primary_key(cfg, columns, evidence_schema, records)
        if incremental and not primary_key:
            logging.warning(
                "Incremental load without a primary key appends rows on every run; "
                "re-fetched records will be duplicated in the table."
            )

        # The one path where stripping the offset from date values can bite an
        # existing table: if a `date` column is part of the primary key, its values
        # no longer match the rows a previous run wrote with the offset attached, so
        # an upsert inserts instead of updating and the table silently double-counts.
        # Automatic key detection cannot get here (it only picks `id`-prefixed
        # integer/numeric columns), so this requires a hand-set primary key — but if
        # it does happen, say so loudly rather than letting it pass unnoticed.
        if incremental and fixed_dates:
            renormalized_keys = _date_keys_needing_reload(primary_key, property_types)
            if renormalized_keys:
                logging.warning(
                    "Primary key column(s) %s hold date values whose UTC offset is now stripped "
                    "(e.g. '2025-01-01+01:00' is written as '2025-01-01'). Rows written by earlier "
                    "runs used the old form, so this incremental run will INSERT them again instead "
                    "of updating them. Reload this table once as a full load (or drop it and re-run) "
                    "to clear the duplicates.",
                    ", ".join(renormalized_keys),
                )

        table = self.create_out_table_definition(
            f"{cfg.evidence}.csv",
            primary_key=primary_key,
            incremental=incremental,
            schema=_build_typed_schema(columns, property_types, primary_key),
            has_header=True,
        )

        # Header-ful CSV: the first row holds the column names (self-documenting).
        # has_header=True makes the manifest agree so the header isn't ingested as data.
        with open(table.full_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow(record)

        self.write_manifest(table)
        logging.info("Wrote %d rows for evidence '%s'", len(records), cfg.evidence)

    @sync_action("testConnection")
    def test_connection(self) -> ValidationResult:
        cfg = Configuration(**self.configuration.parameters)
        # The tunnel must be open for the connection test too — the test
        # verifies reachability, which only makes sense through the tunnel
        # when the server is not directly exposed to the internet.
        with open_tunnel(cfg.ssh_tunnel, cfg.base_url) as (tunnel_base_url, tunnel_original_host):
            client = self._build_client(cfg, tunnel_base_url, tunnel_original_host)
            try:
                client.test_connection()
            except FlexiBeeClientError as exc:
                raise UserException(str(exc))
        return ValidationResult("Connection successful.")

    @sync_action("listEvidences")
    def list_evidences(self) -> list[SelectElement]:
        cfg = Configuration(**self.configuration.parameters)
        # Listing evidences also needs the tunnel — the API call to
        # evidence-list.json goes to the same protected on-prem host.
        with open_tunnel(cfg.ssh_tunnel, cfg.base_url) as (tunnel_base_url, tunnel_original_host):
            client = self._build_client(cfg, tunnel_base_url, tunnel_original_host)
            try:
                evidences = client.list_evidences()
            except Exception as exc:  # noqa: BLE001
                raise UserException(f"Could not list evidences: {exc}")
        return [SelectElement(value=path, label=f"{name} ({path})") for path, name in evidences]

    @sync_action("getEvidenceColumns")
    def get_evidence_columns(self) -> list[SelectElement]:
        """List the columns of the selected evidence, for the primary key picker."""
        cfg = Configuration(**self.configuration.parameters)
        if not cfg.evidence:
            raise UserException("Select an evidence type first.")
        with open_tunnel(cfg.ssh_tunnel, cfg.base_url) as (tunnel_base_url, tunnel_original_host):
            client = self._build_client(cfg, tunnel_base_url, tunnel_original_host)
            try:
                schema = client.get_evidence_schema(cfg.evidence)
            except Exception as exc:  # noqa: BLE001
                raise UserException(f"Could not list columns of evidence '{cfg.evidence}': {exc}")
        return [SelectElement(value=col, label=f"{col} ({typ})") for col, typ in schema.types.items()]

    @sync_action("getDateFields")
    def get_date_fields(self) -> list[SelectElement]:
        """List the date/datetime columns of the selected evidence, for the Date field picker."""
        cfg = Configuration(**self.configuration.parameters)
        if not cfg.evidence:
            raise UserException("Select an evidence type first.")
        with open_tunnel(cfg.ssh_tunnel, cfg.base_url) as (tunnel_base_url, tunnel_original_host):
            client = self._build_client(cfg, tunnel_base_url, tunnel_original_host)
            try:
                schema = client.get_evidence_schema(cfg.evidence)
            except Exception as exc:  # noqa: BLE001
                raise UserException(f"Could not list date fields of evidence '{cfg.evidence}': {exc}")
        date_fields = [
            SelectElement(value=col, label=f"{col} ({typ})")
            for col, typ in schema.types.items()
            if typ in _DATE_PROPERTY_TYPES
        ]
        # `lastUpdate` is the default date column and exists on every evidence;
        # surface it first even if the metadata call did not enumerate it.
        if not any(el.value == _DEFAULT_DATE_FIELD for el in date_fields):
            date_fields.insert(0, SelectElement(value=_DEFAULT_DATE_FIELD, label=f"{_DEFAULT_DATE_FIELD} (datetime)"))
        return date_fields


if __name__ == "__main__":
    try:
        comp = Component()
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
