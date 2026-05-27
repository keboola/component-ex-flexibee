import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from configuration import Configuration  # noqa: E402

BASE = {
    "base_url": "https://demo.flexibee.eu",
    "company": "demo",
    "username": "winstrom",
    "#password": "winstrom",
    "evidence": "faktura-vydana",
}


def test_minimal_config_valid():
    cfg = Configuration(**BASE)
    assert cfg.base_url == "https://demo.flexibee.eu"
    assert cfg.company == "demo"
    assert cfg.password == "winstrom"
    assert cfg.evidence == "faktura-vydana"
    assert cfg.detail == "full"
    assert cfg.ssl_verify is True
    assert cfg.limit == 200


def test_missing_required_field_raises_user_exception():
    from keboola.component import UserException

    data = dict(BASE)
    del data["company"]
    with pytest.raises(UserException):
        Configuration(**data)


def test_password_alias_mapping():
    cfg = Configuration(**BASE)
    assert cfg.password == "winstrom"


from datetime import datetime  # noqa: E402


def test_resolve_window_both_empty_returns_none():
    cfg = Configuration(**BASE)
    date_from, date_to = cfg.resolve_window()
    assert date_from is None
    assert date_to is None


def test_resolve_window_absolute_dates():
    data = dict(BASE, date_from="2026-05-01", date_to="2026-05-27")
    cfg = Configuration(**data)
    date_from, date_to = cfg.resolve_window()
    assert isinstance(date_from, datetime)
    assert isinstance(date_to, datetime)
    assert date_from < date_to


def test_resolve_window_invalid_from_raises_user_exception():
    from keboola.component import UserException

    data = dict(BASE, date_from="not a date", date_to="today")
    cfg = Configuration(**data)
    with pytest.raises(UserException):
        cfg.resolve_window()
