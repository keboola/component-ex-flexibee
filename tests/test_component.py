"""Unit tests for Component._resolve_extraction_window."""

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from component import Component
from configuration import Configuration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_PARAMS = {
    "base_url": "https://demo.flexibee.eu",
    "company": "demo",
    "username": "winstrom",
    "#password": "winstrom",
    "evidence": "faktura-vydana",
}


def _make_datadir(state: dict, params: dict | None = None) -> str:
    """Create a temporary KBC data directory with config.json and state.json.

    Returns the path to the data directory (the value for KBC_DATADIR).
    The caller is responsible for cleaning up; use as a context manager if needed.
    """
    tmp = tempfile.mkdtemp(prefix="test_component_")
    data_dir = Path(tmp)
    (data_dir / "in").mkdir()
    (data_dir / "out" / "tables").mkdir(parents=True)

    cfg_params = dict(_BASE_PARAMS, **(params or {}))
    config = {"action": "run", "parameters": cfg_params}
    (data_dir / "config.json").write_text(json.dumps(config))
    (data_dir / "in" / "state.json").write_text(json.dumps(state))

    return str(data_dir)


def _component(data_dir: str) -> Component:
    """Instantiate Component with KBC_DATADIR pointed at data_dir."""
    old = os.environ.get("KBC_DATADIR")
    os.environ["KBC_DATADIR"] = data_dir
    try:
        return Component()
    finally:
        if old is None:
            os.environ.pop("KBC_DATADIR", None)
        else:
            os.environ["KBC_DATADIR"] = old


# ---------------------------------------------------------------------------
# _resolve_extraction_window tests
# ---------------------------------------------------------------------------


def test_resolve_window_incremental_with_watermark():
    """Incremental + state has last_run → returns (parsed datetime, None)."""
    watermark = "2026-05-27T00:00:00+00:00"
    data_dir = _make_datadir({"last_run": watermark})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(**dict(_BASE_PARAMS, load_type="incremental_load"))
        date_from, date_to = comp._resolve_extraction_window(cfg)

        assert date_from == datetime.fromisoformat(watermark)
        assert date_to is None
    finally:
        import shutil

        shutil.rmtree(data_dir, ignore_errors=True)


def test_resolve_window_incremental_empty_state_with_date_from():
    """Incremental + empty state + date_from set → returns (seed datetime, None)."""
    data_dir = _make_datadir({}, params={"date_from": "2026-01-01"})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(**dict(_BASE_PARAMS, load_type="incremental_load", date_from="2026-01-01"))
        date_from, date_to = comp._resolve_extraction_window(cfg)

        assert isinstance(date_from, datetime)
        assert date_from.year == 2026
        assert date_from.month == 1
        assert date_to is None
    finally:
        import shutil

        shutil.rmtree(data_dir, ignore_errors=True)


def test_resolve_window_incremental_empty_state_no_date_from(caplog):
    """Incremental + empty state + no date_from → returns (None, None) with info log."""
    import logging

    data_dir = _make_datadir({})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(**dict(_BASE_PARAMS, load_type="incremental_load"))

        with caplog.at_level(logging.INFO):
            date_from, date_to = comp._resolve_extraction_window(cfg)

        assert date_from is None
        assert date_to is None
        assert any("first incremental run" in r.message for r in caplog.records)
    finally:
        import shutil

        shutil.rmtree(data_dir, ignore_errors=True)


def test_run_extraction_no_records_writes_no_table(caplog):
    """Empty result → no CSV/manifest (existing table untouched) but the watermark advances."""
    import logging
    import shutil
    from datetime import UTC

    from client.flexibee_client import EvidenceSchema

    data_dir = _make_datadir({"last_run": "2026-07-01T00:00:00+00:00"})
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(**dict(_BASE_PARAMS, load_type="incremental_load"))

        client = type(
            "StubClient",
            (),
            {
                "build_date_wql": staticmethod(lambda *_: None),
                "get_evidence_schema": staticmethod(lambda _: EvidenceSchema(types={"id": "integer"}, id_column="id")),
                "iter_records": staticmethod(lambda *args, **kwargs: iter(())),
            },
        )()

        with caplog.at_level(logging.WARNING):
            comp._run_extraction(cfg, client, datetime(2026, 7, 27, tzinfo=UTC))

        assert list((Path(data_dir) / "out" / "tables").iterdir()) == []
        assert json.loads((Path(data_dir) / "out" / "state.json").read_text())["last_run"].startswith("2026-07-27")
        assert any("reset the configuration state" in r.getMessage() for r in caplog.records)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_resolve_window_full_load_delegates_to_resolve_window():
    """Full load → delegates to cfg.resolve_window() (uses date_from/to, returns both bounds)."""
    data_dir = _make_datadir(
        {},
        params={"date_from": "2026-05-01", "date_to": "2026-05-27"},
    )
    os.environ["KBC_DATADIR"] = data_dir

    try:
        comp = Component()
        cfg = Configuration(
            **dict(
                _BASE_PARAMS,
                load_type="full_load",
                date_from="2026-05-01",
                date_to="2026-05-27",
            )
        )

        # Full load must NOT call get_state_file; patch it to confirm.
        with patch.object(comp, "get_state_file", side_effect=AssertionError("should not be called")):
            date_from, date_to = comp._resolve_extraction_window(cfg)

        assert isinstance(date_from, datetime)
        assert isinstance(date_to, datetime)
        assert date_from < date_to
    finally:
        import shutil

        shutil.rmtree(data_dir, ignore_errors=True)
