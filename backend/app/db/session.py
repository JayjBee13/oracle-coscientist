from collections.abc import Generator
from functools import lru_cache
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings

SettingsDep = Annotated[Settings, Depends(get_settings)]


_created_engines: list[Engine] = []


@lru_cache(maxsize=8)
def _engine_for_url(database_url: str) -> Engine:
    engine = create_engine(database_url, pool_pre_ping=True)
    _created_engines.append(engine)
    return engine


@lru_cache(maxsize=8)
def _session_factory_for_url(database_url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=_engine_for_url(database_url), autoflush=False, autocommit=False)


def get_engine(settings: Settings | None = None) -> Engine:
    """Engine for these settings, created on first use and cached by URL.

    Nothing is built at import time: tests point Settings at a throwaway schema, and an
    import-time engine would bind to the ambient .env before they get the chance.
    """
    return _engine_for_url((settings or get_settings()).database_url)


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    return _session_factory_for_url((settings or get_settings()).database_url)


def reset_engine_cache() -> None:
    """Dispose every cached engine and forget it. Used when tests repoint the database."""
    while _created_engines:
        _created_engines.pop().dispose()
    _engine_for_url.cache_clear()
    _session_factory_for_url.cache_clear()


def get_db(settings: SettingsDep) -> Generator[Session, None, None]:
    with get_session_factory(settings)() as session:
        yield session


def libpq_dsn(settings: Settings | None = None) -> str:
    """The same database, addressed the way libpq wants it.

    SQLAlchemy names its driver in the scheme (`postgresql+psycopg://`); libpq — which is
    what `psycopg.AsyncConnection.connect` speaks, and what the LISTEN/NOTIFY listener
    needs — rejects that. Everything else about the URL is already in libpq's URI form,
    including the `options=-csearch_path=...` the test suite pins its throwaway schema
    with, so dropping the driver suffix is the whole conversion.
    """
    parts = urlsplit((settings or get_settings()).database_url)
    scheme = parts.scheme.split("+", 1)[0]
    if scheme not in {"postgresql", "postgres"}:
        raise ValueError(f"Not a PostgreSQL URL: {parts.scheme!r}")
    return urlunsplit(parts._replace(scheme=scheme))
