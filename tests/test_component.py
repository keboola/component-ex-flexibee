"""Unit tests for Component extraction behavior."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from client.flexibee_client import EvidenceSchema
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
