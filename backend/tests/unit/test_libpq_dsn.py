"""The SQLAlchemy URL, converted for the driver that speaks LISTEN/NOTIFY."""

from __future__ import annotations

import pytest
from psycopg.conninfo import conninfo_to_dict

from app.core.config import Settings
from app.db.session import libpq_dsn

SCHEMA_PINNED = (
    "postgresql+psycopg://user:p%40ss@127.0.0.1:5432/ai_coscientist_gui"
    "?options=-csearch_path%3Dtest_abcd1234"
)


def dsn_for(url: str) -> str:
    return libpq_dsn(Settings(DATABASE_URL=url))


def test_drops_the_driver_from_the_scheme():
    assert dsn_for(SCHEMA_PINNED).startswith("postgresql://user:p%40ss@127.0.0.1:5432/")


def test_keeps_everything_libpq_needs_including_the_test_schema():
    """The conversion is not cosmetic: the listener has to land in the same schema.

    Tests run against a throwaway `test_<hex>` schema pinned by the URL's `options`. A
    listener that lost it would report `public` as its schema and quietly filter out every
    notification the test produced.
    """
    parsed = conninfo_to_dict(dsn_for(SCHEMA_PINNED))

    assert parsed["dbname"] == "ai_coscientist_gui"
    assert parsed["host"] == "127.0.0.1"
    assert parsed["port"] == "5432"
    assert parsed["user"] == "user"
    assert parsed["password"] == "p@ss"
    assert parsed["options"] == "-csearch_path=test_abcd1234"


def test_a_url_without_a_driver_suffix_is_already_fine():
    url = "postgresql://user:pass@localhost:5432/db"
    assert dsn_for(url) == url


def test_refuses_a_url_that_is_not_postgres():
    with pytest.raises(ValueError, match="Not a PostgreSQL URL"):
        dsn_for("mysql+pymysql://user:pass@localhost/db")
