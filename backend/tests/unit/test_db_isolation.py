"""The database engine must follow the Settings it is given.

Before this, `db/session.py` built an Engine at import time from the ambient .env, so a
Settings override was silently ignored and every test — including ones that delete rows —
ran against the owner's live database.
"""

from app.core.config import Settings
from app.db import session as session_module
from app.db.session import get_engine, get_session_factory

OVERRIDE_URL = "postgresql+psycopg://someone:secret@127.0.0.1:5432/override_db"
OTHER_URL = "postgresql+psycopg://someone:secret@127.0.0.1:5432/other_db"


def test_engine_respects_settings_override():
    engine = get_engine(Settings(DATABASE_URL=OVERRIDE_URL))

    assert engine.url.database == "override_db"


def test_engines_are_cached_per_url():
    first = get_engine(Settings(DATABASE_URL=OVERRIDE_URL))
    same = get_engine(Settings(DATABASE_URL=OVERRIDE_URL))
    different = get_engine(Settings(DATABASE_URL=OTHER_URL))

    assert first is same
    assert first is not different


def test_session_factory_is_bound_to_the_matching_engine():
    settings = Settings(DATABASE_URL=OVERRIDE_URL)

    factory = get_session_factory(settings)

    assert factory.kw["bind"] is get_engine(settings)


def test_module_exposes_no_import_time_engine():
    """Importing the module must not connect to — or even name — a database."""
    assert not hasattr(session_module, "engine")
    assert not hasattr(session_module, "SessionLocal")
