"""Alembic entrypoint; credentials come exclusively from application settings."""

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from app.core.config import get_settings
from app.models import Base, UTCDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(type_: str, value, autogen_context):
    if type_ == "type" and isinstance(value, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(get_settings().database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        if is_sqlite:
            # Batch replacement of a referenced table must not cascade or reject
            # its DROP. This affects only this dedicated migration connection.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            # pysqlite's legacy transaction mode does not begin for DDL. An
            # explicit BEGIN makes table rebuilds and version updates atomic.
            connection.exec_driver_sql("BEGIN")
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
                render_as_batch=is_sqlite,
                render_item=render_item,
            )
            with context.begin_transaction():
                context.run_migrations()
            if is_sqlite:
                violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError("Migration would leave invalid SQLite foreign keys.")
                connection.commit()
        finally:
            if is_sqlite:
                connection.rollback()
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
