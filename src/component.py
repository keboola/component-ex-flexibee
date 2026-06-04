"""ABRA Flexi (FlexiBee) extractor component."""

import csv
import logging
from datetime import UTC, datetime

from keboola.component import ComponentBase, UserException
from keboola.component.base import sync_action
from keboola.component.dao import BaseType, ColumnDefinition
from keboola.component.sync_actions import SelectElement, ValidationResult
from keboola.vcr import DefaultSanitizer

from client.flexibee_client import FlexiBeeClient, FlexiBeeClientError
from client.ssh_tunnel import open_tunnel
from configuration import Configuration

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


def _build_typed_schema(
    columns: list[str],
    property_types: dict[str, str],
) -> dict[str, ColumnDefinition]:
    """Map observed CSV columns onto ColumnDefinitions using FlexiBee property types.

    Columns absent from `property_types` (or with unrecognized types) fall back
    to STRING — keeps the output safe when FlexiBee adds new fields without us
    updating the type map.
    """
    schema: dict[str, ColumnDefinition] = {}
    for col in columns:
        typ = property_types.get(col)
        builder = _FLEXIBEE_TO_BASE_TYPE.get(typ) if typ else None
        data_types = builder() if builder else BaseType.string()
        schema[col] = ColumnDefinition(
            data_types=data_types,
            primary_key=(col == "id"),
            nullable=(col != "id"),
        )
    return schema


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
        """Resolve the (date_from, date_to) ``lastUpdate`` window for this run.

        Incremental: the lower bound is the ``last_run`` watermark from ``state.json``;
        on the first run (no watermark) it falls back to the configured Date from as a
        seed, or to full history. There is no upper bound — incremental always fetches
        up to "now". Full load: the explicit manual Date from/to window.
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
        wql_parts = []
        window_wql = client.build_lastupdate_wql(date_from, date_to)
        if window_wql:
            wql_parts.append(window_wql)
        if cfg.custom_filter:
            wql_parts.append(cfg.custom_filter)
        wql = " and ".join(wql_parts) if wql_parts else None

        incremental = cfg.incremental
        logging.info("Extracting evidence '%s' (load_type=%s)", cfg.evidence, cfg.load_type.value)

        # Native-type schema: properties.json is the source of truth for each column's
        # FlexiBee type. We fetch it best-effort — if the call fails we degrade to an
        # untyped (all-STRING) schema rather than failing the run, since the data is
        # already in hand by the time we'd write the manifest.
        property_types: dict[str, str] = {}
        try:
            property_types = client.get_evidence_properties(cfg.evidence)
        except Exception as exc:  # noqa: BLE001 - native types are best-effort; STRING fallback is safe
            logging.warning("Skipping native types for '%s': %s", cfg.evidence, exc)

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
                    custom_fields=cfg.custom_fields,
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

        if records and "id" not in seen:
            raise UserException(
                f"FlexiBee evidence '{cfg.evidence}' returned records without an 'id' field; "
                "cannot use it as primary key."
            )
        if not columns:
            columns = ["id"]

        table = self.create_out_table_definition(
            f"{cfg.evidence}.csv",
            primary_key=["id"],
            incremental=incremental,
            schema=_build_typed_schema(columns, property_types),
            has_header=True,
        )

        # Header-ful CSV: the first row holds the column names (self-documenting).
        # has_header=True makes the manifest agree so the header isn't ingested as data.
        with open(table.full_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow(record)

        if not records:
            logging.warning("No records returned for evidence '%s'", cfg.evidence)

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
