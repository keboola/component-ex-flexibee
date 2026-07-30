"""Unit tests for Component extraction behavior."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from keboola.component import UserException

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from client.flexibee_client import EvidenceSchema, FlexiBeeClientError
from component import Component
from configuration import Configuration

_BASE_PARAMS = {
    "base_url": "https://demo.flexibee.eu",
    "company": "demo",
    "username": "winstrom",
    "#password": "winstrom",
    "evidence": "faktura-vydana",
}


def _make_datadir(params: dict | None = None) -> str:
    """Create a temporary KBC data directory with config.json. Caller cleans up."""
    tmp = tempfile.mkdtemp(prefix="test_component_")
    data_dir = Path(tmp)
    (data_dir / "in").mkdir()
    (data_dir / "out" / "tables").mkdir(parents=True)

    cfg_params = dict(_BASE_PARAMS, **(params or {}))
    config = {"action": "run", "parameters": cfg_params}
    (data_dir / "config.json").write_text(json.dumps(config))
    (data_dir / "in" / "state.json").write_text("{}")

    return str(data_dir)


class _StubClient:
    """Minimal client stand-in for the no-records extraction path."""

    @staticmethod
    def build_date_wql(*_):
        return None

    @staticmethod
    def get_evidence_schema(_):
        return EvidenceSchema(types={"id": "integer"}, id_column="id")

    @staticmethod
    def iter_records(*_args, **_kwargs):
        return iter(())


def test_run_extraction_no_records_writes_no_table():
    """Empty result → no CSV/manifest written; the existing Storage table is left untouched.

    With the watermark removed there is no state file to advance either — the run
    simply exits cleanly, leaving Storage as-is.
    """
    data_dir = _make_datadir({"load_type": "incremental_load"})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(**dict(_BASE_PARAMS, load_type="incremental_load"))

        comp._run_extraction(cfg, _StubClient())

        assert list((Path(data_dir) / "out" / "tables").iterdir()) == []
        assert not (Path(data_dir) / "out" / "state.json").exists()
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_run_extraction_full_load_with_no_records_writes_empty_table():
    """An empty full load replaces the existing table with an empty output."""
    data_dir = _make_datadir({"load_type": "full_load"})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(**dict(_BASE_PARAMS, load_type="full_load"))

        comp._run_extraction(cfg, _StubClient())

        output = Path(data_dir) / "out" / "tables" / "faktura-vydana.csv"
        assert output.read_text() == "id\n"
        assert output.with_suffix(".csv.manifest").exists()
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


class _StubClientNoMetadata:
    """Client stand-in whose metadata call yields nothing (e.g. a transient tunnel failure)."""

    @staticmethod
    def build_date_wql(*_):
        return None

    @staticmethod
    def get_evidence_schema(_):
        return EvidenceSchema()

    @staticmethod
    def iter_records(*_args, **_kwargs):
        return iter(())


def test_run_extraction_full_load_no_records_no_metadata_fails_fast():
    """Full load with neither metadata nor records → raise instead of shrinking the table.

    A full load overwrites, so writing an `id`-only table when the column set is unknown
    (the metadata call failed AND the window returned nothing) would replace the existing
    Storage table with a single column. Fail instead so the transient failure is visible.
    """
    data_dir = _make_datadir({"load_type": "full_load"})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(**dict(_BASE_PARAMS, load_type="full_load"))

        with pytest.raises(UserException, match="Could not determine any output columns"):
            comp._run_extraction(cfg, _StubClientNoMetadata())

        assert list((Path(data_dir) / "out" / "tables").iterdir()) == []
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


class _StubSummaryProbeClient:
    """Distinguishes the unfiltered probe (`wql=None`) from the actual windowed fetch.

    `detail=summary` has no metadata anchor, so `_run_extraction` probes the evidence
    unfiltered to build a run-independent header; the probe's column set must win over
    the (narrower) windowed fetch's own columns.
    """

    def __init__(self, wide_probe_rows: list[dict], narrow_window_rows: list[dict]):
        self._wide_probe_rows = wide_probe_rows
        self._narrow_window_rows = narrow_window_rows

    @staticmethod
    def build_date_wql(*_):
        return "lastUpdate gt '2026-05-27T00:00:00+00:00'"

    @staticmethod
    def get_evidence_schema(_):
        # `summary` has no /properties.json anchor either way.
        return EvidenceSchema()

    def iter_records(self, _evidence, wql=None, detail="full", custom_fields=None, limit=200):  # noqa: ARG002
        if wql is None:
            return iter(self._wide_probe_rows)
        return iter(self._narrow_window_rows)


def test_run_extraction_summary_incremental_probe_wide_columns_writes_stable_header():
    """summary + incremental + date window: the unfiltered probe's columns become the
    header, even though the windowed fetch itself only returns a narrower record."""
    data_dir = _make_datadir({"load_type": "incremental_load", "detail": "summary", "date_from": "2026-05-27"})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(
            **dict(_BASE_PARAMS, load_type="incremental_load", detail="summary", date_from="2026-05-27")
        )
        client = _StubSummaryProbeClient(
            wide_probe_rows=[{"a": "1", "b": "2", "c": "3"}],
            narrow_window_rows=[{"a": "10"}],
        )

        comp._run_extraction(cfg, client)

        output = Path(data_dir) / "out" / "tables" / "faktura-vydana.csv"
        header = output.read_text().splitlines()[0]
        assert header == "a,b,c"
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


class _StubSummaryProbeErrorClient:
    """The unfiltered probe call fails; the actual windowed fetch still returns records."""

    @staticmethod
    def build_date_wql(*_):
        return "lastUpdate gt '2026-05-27T00:00:00+00:00'"

    @staticmethod
    def get_evidence_schema(_):
        return EvidenceSchema()

    @staticmethod
    def iter_records(_evidence, wql=None, detail="full", custom_fields=None, limit=200):  # noqa: ARG004
        if wql is None:
            raise FlexiBeeClientError("evidence temporarily unavailable")
        return iter([{"a": "1"}])


def test_run_extraction_summary_incremental_probe_failure_fails_fast():
    """summary + incremental: an unfiltered-probe failure must not silently write an
    un-anchored (run-dependent) header under an incremental load."""
    data_dir = _make_datadir({"load_type": "incremental_load", "detail": "summary", "date_from": "2026-05-27"})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(
            **dict(_BASE_PARAMS, load_type="incremental_load", detail="summary", date_from="2026-05-27")
        )

        with pytest.raises(UserException, match="Could not build a stable set of output columns"):
            comp._run_extraction(cfg, _StubSummaryProbeErrorClient())

        assert list((Path(data_dir) / "out" / "tables").iterdir()) == []
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


class _StubClientNoMetadataWithRecords:
    """Metadata call yields nothing, but the window fetch itself returns records."""

    @staticmethod
    def build_date_wql(*_):
        return None

    @staticmethod
    def get_evidence_schema(_):
        return EvidenceSchema()

    @staticmethod
    def iter_records(*_args, **_kwargs):
        return iter([{"id": "1", "kod": "A"}, {"id": "2", "kod": "B"}])


def test_run_extraction_full_detail_incremental_no_metadata_with_records_fails_fast():
    """full + incremental, metadata unavailable: the observed union from THIS run's
    records is un-anchored, so an incremental load must refuse rather than risk an
    upsert against a table a wider run created."""
    data_dir = _make_datadir({"load_type": "incremental_load"})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(**dict(_BASE_PARAMS, load_type="incremental_load"))

        with pytest.raises(UserException, match="Could not build a stable set of output columns"):
            comp._run_extraction(cfg, _StubClientNoMetadataWithRecords())

        assert list((Path(data_dir) / "out" / "tables").iterdir()) == []
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_run_extraction_full_detail_full_load_no_metadata_with_records_writes_observed_union():
    """full + FULL LOAD, metadata unavailable: a full load overwrites the table anyway,
    so the observed-record union is written instead of failing."""
    data_dir = _make_datadir({"load_type": "full_load"})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(**dict(_BASE_PARAMS, load_type="full_load"))

        comp._run_extraction(cfg, _StubClientNoMetadataWithRecords())

        output = Path(data_dir) / "out" / "tables" / "faktura-vydana.csv"
        lines = output.read_text().splitlines()
        assert lines[0] == "id,kod"
        assert len(lines) == 3  # header + 2 data rows
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_run_raises_when_custom_detail_has_no_custom_fields():
    """`detail=custom` with no `custom_fields` is rejected in `run()`, before any network
    call -- an empty projection would produce an unstable (run-dependent) header."""
    data_dir = _make_datadir({"detail": "custom", "custom_fields": ""})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()

        with pytest.raises(UserException, match="no custom fields were provided"):
            comp.run()
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_run_extraction_empty_incremental_window_logs_reload_hint(caplog):
    """Empty incremental result within a Date Start window → hint how to reload from scratch.

    Preserves the user-facing guidance from the earlier watermark-era hint, adapted
    to the stateless model (widen/clear Date Start or run full load, not "reset state").
    """
    import logging

    data_dir = _make_datadir({"load_type": "incremental_load", "date_from": "2026-01-01"})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(**dict(_BASE_PARAMS, load_type="incremental_load", date_from="2026-01-01"))

        with caplog.at_level(logging.WARNING):
            comp._run_extraction(cfg, _StubClient())

        assert list((Path(data_dir) / "out" / "tables").iterdir()) == []
        assert any("reload it from scratch" in r.getMessage() for r in caplog.records)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


class _StubSchemaClient:
    """Client stand-in for `getDateFields`: only `get_evidence_schema` is exercised."""

    def __init__(self, types: dict[str, str]):
        self._types = types

    def get_evidence_schema(self, _evidence):
        return EvidenceSchema(types=self._types)


def test_get_date_fields_returns_only_date_and_datetime_columns():
    """`getDateFields` filters the evidence schema down to date/datetime columns only.

    Non-date columns (`kod`: string, `id`: integer) must not be offered as Date
    field candidates; `lastUpdate` is already a date-ish column here so it should
    come through the normal filtering path (not the `lastUpdate`-missing fallback).
    """
    data_dir = _make_datadir()
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        stub = _StubSchemaClient(
            types={
                "lastUpdate": "datetime",
                "datVyst": "date",
                "kod": "string",
                "id": "integer",
            }
        )
        comp._build_client = lambda *args, **kwargs: stub

        result = comp.get_date_fields()

        values = [el.value for el in result]
        assert set(values) == {"lastUpdate", "datVyst"}
        assert "kod" not in values
        assert "id" not in values
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_get_date_fields_always_offers_last_update_as_fallback():
    """`lastUpdate` is offered first even when the evidence schema never mentions it.

    Some evidences may not expose `lastUpdate` via `/properties.json` (or the
    metadata call may return an incomplete set); the picker must still surface
    it since it is the default Date field used by the extraction window.
    """
    data_dir = _make_datadir()
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        stub = _StubSchemaClient(
            types={
                "datVyst": "date",
                "kod": "string",
            }
        )
        comp._build_client = lambda *args, **kwargs: stub

        result = comp.get_date_fields()

        assert [el.value for el in result][0] == "lastUpdate"
        assert {el.value for el in result} == {"lastUpdate", "datVyst"}
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
