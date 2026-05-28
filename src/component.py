"""ABRA Flexi (FlexiBee) extractor component."""

import csv
import logging

from keboola.component import ComponentBase, UserException
from keboola.component.base import sync_action
from keboola.component.dao import BaseType, ColumnDefinition
from keboola.component.sync_actions import SelectElement, ValidationResult
from keboola.vcr import DefaultSanitizer

from client.flexibee_client import FlexiBeeClient, FlexiBeeClientError
from configuration import Configuration

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

    def _build_client(self, cfg: Configuration) -> FlexiBeeClient:
        return FlexiBeeClient(
            base_url=cfg.base_url,
            company=cfg.company,
            username=cfg.username,
            password=cfg.password,
            ssl_verify=cfg.ssl_verify,
        )

    def run(self):
        cfg = Configuration(**self.configuration.parameters)
        if not cfg.evidence:
            raise UserException("No evidence type selected. Choose an evidence type for this row.")
        client = self._build_client(cfg)

        date_from, date_to = cfg.resolve_window()
        wql_parts = []
        window_wql = client.build_lastupdate_wql(date_from, date_to)
        if window_wql:
            wql_parts.append(window_wql)
        if cfg.custom_filter:
            wql_parts.append(cfg.custom_filter)
        wql = " and ".join(wql_parts) if wql_parts else None

        incremental = date_from is not None
        logging.info("Extracting evidence '%s' (incremental=%s)", cfg.evidence, incremental)

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
        logging.info("Wrote %d rows for evidence '%s'", len(records), cfg.evidence)

    @sync_action("testConnection")
    def test_connection(self) -> ValidationResult:
        cfg = Configuration(**self.configuration.parameters)
        client = self._build_client(cfg)
        try:
            client.test_connection()
        except FlexiBeeClientError as exc:
            raise UserException(str(exc))
        return ValidationResult("Connection successful.")

    @sync_action("listEvidences")
    def list_evidences(self) -> list[SelectElement]:
        cfg = Configuration(**self.configuration.parameters)
        client = self._build_client(cfg)
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
