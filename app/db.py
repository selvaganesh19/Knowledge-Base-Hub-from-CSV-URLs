"""Database engine, session factory, and the FastAPI session dependency.

SQLite is configured for WAL mode with a generous busy timeout. The harvest job
is written so that only one coroutine ever writes at a time, so contention is
unlikely by construction - but a running job and an incoming HTTP request can
still overlap, and WAL plus the timeout is what keeps that overlap from raising
"database is locked" at the user.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Create any missing tables, then bring existing ones up to date."""
    from app import models  # noqa: F401  (registers mappers on Base.metadata)

    Base.metadata.create_all(bind=engine)
    sync_columns()


#: How a SQLAlchemy column type maps onto a SQLite ADD COLUMN clause. SQLite only
#: accepts a limited set of types in ALTER TABLE, and none of the constraints that
#: are fine at CREATE TABLE time, so the type is translated rather than emitted.
_SQLITE_TYPES = {
    "INTEGER": "INTEGER",
    "FLOAT": "FLOAT",
    "BOOLEAN": "INTEGER",
    "VARCHAR": "VARCHAR",
    "TEXT": "TEXT",
    "DATETIME": "DATETIME",
    "JSON": "JSON",
}


def sync_columns() -> list[str]:
    """Add columns that the models define but an existing table does not have.

    `create_all` creates missing *tables* and silently ignores missing *columns*,
    so a database created before a field was added keeps working right up until
    something queries the new column and gets "no such column". This walks the
    model metadata against the live schema and issues the ALTERs.

    Deliberately not Alembic: there is one deployment, one developer and no
    migration history to preserve. A nullable column with a server default is
    exactly the case Alembic would generate a trivial autogenerate revision for,
    and this does it in thirty lines with no new dependency.

    Returns the list of added columns, for the startup log.
    """
    from sqlalchemy import inspect, text

    added: list[str] = []
    inspector = inspect(engine)

    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue

        existing = {column["name"] for column in inspector.get_columns(table.name)}

        for column in table.columns:
            if column.name in existing:
                continue

            type_name = _SQLITE_TYPES.get(type(column.type).__name__.upper(), "TEXT")
            # A NOT NULL column cannot be added to a table with rows unless it has
            # a default, and even then SQLite refuses a non-constant one. New
            # columns are added nullable and the model default fills them in on
            # the next write.
            clause = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {type_name}'
            if column.default is not None and getattr(column.default, "is_scalar", False):
                value = column.default.arg
                if isinstance(value, bool):
                    value = int(value)
                literal = f"'{value}'" if isinstance(value, str) else value
                clause += f" DEFAULT {literal}"

            with engine.begin() as connection:
                connection.execute(text(clause))
                _backfill(connection, table.name, column.name)
            added.append(f"{table.name}.{column.name}")

    if added:
        logger.info("schema upgraded with %d new column(s): %s", len(added), ", ".join(added))
    return added


#: Columns whose value can be derived from data that already existed, so an upgraded
#: database does not show a screen of blanks where the old rows have perfectly good
#: answers. Each entry is (columns the statement reads, the statement) and is only
#: run for a column that was just added.
#:
#: The column list is explicit because a database old enough to be missing
#: `crawl_status` may also predate `error` or `fetched_at`. Referring to a column
#: that is not there would abort the whole upgrade, so the entry is skipped instead.
_BACKFILL_SQL: dict[str, tuple[tuple[str, ...], str]] = {
    # A row with text but no error was a good harvest; text with an error is thin.
    # A 2xx carrying no text at all is the JavaScript-shell case, which the new code
    # calls PARTIAL, so the reconstruction has to agree with it. The finer
    # distinctions - SKIPPED for robots.txt, the browser-unavailable note - are not
    # recoverable from the old schema and are not invented here.
    "harvested_urls.crawl_status": (
        ("http_status", "text_content", "error", "fetched_at"),
        """
        UPDATE harvested_urls
           SET crawl_status = CASE
                 WHEN text_content IS NOT NULL AND text_content != '' AND (error IS NULL OR error = '')
                      THEN 'SUCCESS'
                 WHEN text_content IS NOT NULL AND text_content != '' THEN 'PARTIAL'
                 WHEN http_status IN (401, 403, 429, 451) THEN 'BLOCKED'
                 WHEN error LIKE '%bot protection%' THEN 'BLOCKED'
                 WHEN error LIKE '%robots.txt%' THEN 'SKIPPED'
                 -- No status and no reason is the only real "never tried" case. A
                 -- failed connection also has no status, but it has a reason, and
                 -- calling that PENDING would show a crawl that happened as queued.
                 WHEN http_status IS NULL AND (error IS NULL OR error = '') THEN 'PENDING'
                 WHEN http_status IS NULL THEN 'FAILED'
                 WHEN http_status < 400 THEN 'PARTIAL'
                 ELSE 'FAILED'
               END
         WHERE fetched_at IS NOT NULL
        """,
    ),
    # Rows already carry the text; counting it is cheaper than leaving the column at
    # zero and making every list request compute it.
    "harvested_urls.char_count": (
        ("text_content",),
        """
        UPDATE harvested_urls SET char_count = COALESCE(LENGTH(text_content), 0)
         WHERE char_count IS NULL OR char_count = 0
        """,
    ),
}


def _table_columns(connection, table_name: str) -> set[str]:
    from sqlalchemy import inspect

    return {column["name"] for column in inspect(connection).get_columns(table_name)}


def _backfill(connection, table_name: str, column_name: str) -> None:
    """Populate a freshly added column from data already on the row.

    Only ever runs for a column that was just created, so it cannot overwrite a
    value the application has since written.
    """
    from sqlalchemy import text

    entry = _BACKFILL_SQL.get(f"{table_name}.{column_name}")
    if entry is not None:
        required, statement = entry
        missing = [name for name in required if name not in _table_columns(connection, table_name)]
        if missing:
            # A table old enough to lack one of these has no history worth
            # reconstructing, and referencing the column would abort the upgrade.
            logger.debug(
                "skipping backfill of %s.%s: table has no %s",
                table_name,
                column_name,
                ", ".join(missing),
            )
        else:
            connection.execute(text(statement))
            logger.info("backfilled %s.%s for existing rows", table_name, column_name)

    if (table_name, column_name) == ("harvested_urls", "domain"):
        _backfill_domain(connection)


def _backfill_domain(connection) -> None:
    """Derive the domain for existing rows.

    In Python rather than SQL because the rule - strip a leading `www.`, lowercase -
    lives in url_guard and duplicating it as SQL would give two implementations that
    can disagree.
    """
    from sqlalchemy import text

    from app.services.url_guard import domain_of

    rows = connection.execute(
        text("SELECT id, url FROM harvested_urls WHERE domain IS NULL OR domain = ''")
    ).all()
    if not rows:
        return

    connection.execute(
        text("UPDATE harvested_urls SET domain = :domain WHERE id = :id"),
        [{"id": row_id, "domain": domain_of(row_url)} for row_id, row_url in rows],
    )
    logger.info("backfilled domain for %d row(s)", len(rows))


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
