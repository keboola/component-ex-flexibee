"""ABRA Flexi (FlexiBee) extractor component."""

import csv
import logging
from datetime import UTC, datetime

from keboola.component import ComponentBase, UserException
from keboola.component.base import sync_action
from keboola.component.dao import BaseType, ColumnDefinition
from keboola.component.sync_actions import SelectElement, ValidationResult
from keboola.vcr import DefaultSanitizer

from client.flexibee_client import EvidenceSchema, FlexiBeeClient, FlexiBeeClientError
from client.ssh_tunnel import open_tunnel
from configuration import Configuration

# FlexiBee property types that can serve as the date window / watermark column.
_DATE_PROPERTY_TYPES = frozenset({"date", "datetime"})

# Fallback date column when the user leaves the "Date field" empty. `lastUpdate`
# is the record-modification timestamp present on every evidence.
_DEFAULT_DATE_FIELD = "lastUpdate"

# state.json key holding the ISO-8601 UTC timestamp of the last successful run.
# Used as the `lastUpdate` lower bound for the next incremental extraction.
_STATE_LAST_RUN = "last_run"

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
        # Auto mode: request the evidence's own key so it survives the custom
        # projection. `id` on a derived evidence comes back as a placeholder on
        # every row, so prefer the inId column, else the first id* candidate.
        auto_key = schema.id_column or next(iter(schema.key_candidates), None)
        needed = [auto_key] if auto_key else []

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
    candidates: list[str] = []
    for candidate in (schema.id_column, "id", *schema.key_candidates):
        if not candidate or candidate not in available or candidate in candidates:
            continue
        if records and _is_placeholder_column(candidate, records):
            logging.warning("Column '%s' holds no record identity in this evidence; skipping it.", candidate)
            continue
        candidates.append(candidate)

    # Prefer the first candidate that is unique across the fetched records. Without
    # records (metadata-only resolution) uniqueness cannot be checked, so the first
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
        # Capture the watermark BEFORE fetching so any records modified during the
        # run are re-fetched next time rather than silently skipped. Persisted to
        # state.json only after a successful write (see _run_extraction).
        run_started_at = datetime.now(UTC)
        # Open the SSH tunnel when configured and enabled; it is a no-op
        # context manager otherwise so the direct-connection path is unchanged.
        with open_tunnel(cfg.ssh_tunnel, cfg.base_url) as (tunnel_base_url, tunnel_original_host):
            client = self._build_client(cfg, tunnel_base_url, tunnel_original_host)
            self._run_extraction(cfg, client, run_started_at)

    def _resolve_extraction_window(self, cfg: Configuration) -> tuple[datetime | None, datetime | None]:
        """Resolve the (date_from, date_to) window (on ``cfg.date_field``) for this run.

        Incremental: the lower bound is the ``last_run`` watermark from ``state.json``;
        on the first run (no watermark) it falls back to the configured Date Start as a
        seed, or to full history. There is no upper bound — incremental always fetches
        up to "now". Full load: the explicit manual Date Start / Date End window.
        """
        if not cfg.incremental:
            return cfg.resolve_window()

        state = self.get_state_file() or {}
        last_run = state.get(_STATE_LAST_RUN)
        if last_run:
            try:
                return datetime.fromisoformat(last_run), None
            except (TypeError, ValueError):
                logging.warning("Ignoring unparsable last_run watermark in state: %r", last_run)

        seed_from, _ = cfg.resolve_window()
        if seed_from is None:
            logging.info("No state watermark and no Date from set — first incremental run extracts full history.")
        return seed_from, None

    def _run_extraction(self, cfg: Configuration, client: FlexiBeeClient, run_started_at: datetime) -> None:
        """Execute the evidence extraction with a ready client.

        Separated from ``run()`` so the SSH tunnel context manager wraps
        the entire extraction without ``run()`` becoming too deeply nested.
        """

        date_from, date_to = self._resolve_extraction_window(cfg)
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

        # Buffer records so we can compute the full column union before writing.
        # FlexiBee records have varying keys (e.g. `external-ids` appears only on some),
        # so a fixed first-row header would silently drop later fields. Evidence sizes
        # are modest (thousands of flat rows), so buffering is acceptable for v1.
        try:
            records = list(
                client.iter_records(
                    cfg.evidence,
                    wql=wql,
                    detail=cfg.detail,
                    custom_fields=_custom_fields_with_key(cfg, evidence_schema),
                    limit=cfg.limit,
                )
            )
        except FlexiBeeClientError as exc:
            raise UserException(str(exc))

        columns: list[str] = []
        seen: set[str] = set()
        for record in records:
            for key in record:
                if key not in seen:
                    seen.add(key)
                    columns.append(key)

        if not records:
            # Without records there is no column union, and a header built from the key
            # alone would not match the columns of an already loaded table (output mapping
            # rejects it). Skipping the output leaves the existing table untouched.
            logging.warning("No records returned for evidence '%s'; the output table is left unchanged.", cfg.evidence)
            if incremental and date_from:
                logging.warning(
                    "Nothing has changed in '%s' since the last run (%s > %s). To load the evidence from "
                    "scratch — for example after deleting the output table — reset the configuration state "
                    "(RAW configuration editor, Update State tab: {}) or run it once as full load.",
                    cfg.evidence,
                    date_field,
                    date_from.isoformat(),
                )
            self.write_state_file({_STATE_LAST_RUN: run_started_at.isoformat()})
            return

        primary_key = _resolve_primary_key(cfg, columns, evidence_schema, records)
        if incremental and not primary_key:
            logging.warning(
                "Incremental load without a primary key appends rows on every run; "
                "re-fetched records will be duplicated in the table."
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
        # Advance the watermark only after a successful write — a failed run keeps the
        # old watermark and is safely retried. Written on every load type so switching
        # full → incremental later picks up cleanly from this run's start time.
        self.write_state_file({_STATE_LAST_RUN: run_started_at.isoformat()})
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
        # `lastUpdate` is the default watermark column and exists on every evidence;
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
