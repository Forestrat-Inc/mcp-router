"""Alembic environment for mcp-router.

Own greenfield chain (schema mcp_router). Coexists with every other AIMS
service's Flyway/Alembic state via schema isolation.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from app.config import settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `%` is ConfigParser's interpolation marker. URL-encoded chars in Azure PG
# passwords (e.g. `%21` for `!`) explode ConfigParser at boot unless we double
# the `%`. SQLAlchemy itself sees the literal — same fix as aims-rule-engine.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    """Autogenerate should never diff foreign-schema tables."""
    if type_ == "table":
        return obj.schema == settings.DB_SCHEMA
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        include_object=include_object,
        version_table_schema=settings.DB_SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Chicken-and-egg: version_table_schema= tells Alembic to put its
        # `alembic_version` bookkeeping row in our schema, but Alembic tries
        # to CREATE that table before running any migration — so the schema
        # must already exist. Create it here as a no-op safety net;
        # 0001_initial also CREATEs it defensively.
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{settings.DB_SCHEMA}"'))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_object=include_object,
            version_table_schema=settings.DB_SCHEMA,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
