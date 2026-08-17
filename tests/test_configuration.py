from datetime import datetime

import pytest

from configuration import Configuration

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


def test_resolve_window_to_only_warns_and_returns_none(caplog):
    import logging

    data = dict(BASE, date_to="2026-05-27")
    cfg = Configuration(**data)
    with caplog.at_level(logging.WARNING):
        date_from, date_to = cfg.resolve_window()
    assert date_from is None
    assert date_to is None
    assert any("date_to is set but date_from is empty" in record.message for record in caplog.records)


def test_limit_must_be_positive():
    from keboola.component import UserException

    data = dict(BASE, limit=0)
    with pytest.raises(UserException):
        Configuration(**data)


# --- LoadType / incremental field tests ---


def test_default_load_type_is_incremental():
    """Default load_type is incremental_load and incremental property is True."""
    from configuration import LoadType

    cfg = Configuration(**BASE)
    assert cfg.load_type == LoadType.incremental_load
    assert cfg.incremental is True


def test_full_load_type_makes_incremental_false():
    """Explicitly setting full_load makes incremental False."""
    from configuration import LoadType

    cfg = Configuration(**dict(BASE, load_type="full_load"))
    assert cfg.load_type == LoadType.full_load
    assert cfg.incremental is False


def test_invalid_load_type_raises_user_exception():
    """An unrecognized load_type value raises UserException."""
    from keboola.component import UserException

    with pytest.raises(UserException):
        Configuration(**dict(BASE, load_type="banana"))


def test_empty_date_to_means_no_upper_bound():
    """Regression [SUPPORT-17334]: an empty Date End must not silently cap at "now".

    The customer saw records missing and could not tell why: the field is empty, so
    it reads as "no limit", but it used to resolve to the run time and drop every
    future-dated record. Documents are routinely dated ahead (due dates, planned
    entries), so those rows just vanished.
    """
    cfg = Configuration(**dict(BASE, date_from="2025-01-01"))
    date_from, date_to = cfg.resolve_window()
    assert isinstance(date_from, datetime)
    assert date_to is None


def test_today_as_date_to_still_caps_at_the_current_moment():
    """The old behaviour stays available — it just has to be asked for explicitly."""
    cfg = Configuration(**dict(BASE, date_from="2025-01-01", date_to="today"))
    date_from, date_to = cfg.resolve_window()
    assert isinstance(date_to, datetime)
    assert date_from < date_to


def test_future_date_from_is_valid_when_unbounded():
    """ "From 2030 onwards" is a sensible window and must not be rejected.

    Resolving the lower bound against "now" (as the old code did) raises
    "start_date cannot exceed end_date" for a future date_from, so the bound is
    resolved by pairing it with itself instead.
    """
    cfg = Configuration(**dict(BASE, date_from="2030-01-01"))
    date_from, date_to = cfg.resolve_window()
    assert date_from == datetime(2030, 1, 1)
    assert date_to is None


def test_relative_date_from_still_resolves_when_unbounded():
    cfg = Configuration(**dict(BASE, date_from="5 days ago"))
    date_from, date_to = cfg.resolve_window()
    assert isinstance(date_from, datetime)
    assert date_from < datetime.now()
    assert date_to is None


def test_invalid_date_from_still_raises_when_unbounded():
    """The error path must survive the unbounded branch, and name the bound clearly."""
    from keboola.component import UserException

    with pytest.raises(UserException) as err:
        Configuration(**dict(BASE, date_from="not a date")).resolve_window()
    assert "(unbounded)" in str(err.value)
