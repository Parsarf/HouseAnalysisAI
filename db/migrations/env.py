from alembic import context
from sqlalchemy import engine_from_config, pool

from common.db import Base
from common.settings import settings
from db import models as _db_models  # noqa: F401 - register ORM metadata for alembic
from identity import models as _identity_models  # noqa: F401 - register ORM metadata for alembic

config = context.config
target_metadata = Base.metadata


def include_object(object_, name, type_, reflected, compare_to):
    # The initial SQL schema intentionally leaves defaulted columns nullable;
    # matched columns are still structurally compared by PostgreSQL itself.
    # Keep unmatched metadata objects visible so missing tables/columns/indexes
    # remain migration drift, while avoiding false positives from inference of
    # Python annotations versus SQL defaults.
    return not (type_ == "column" and reflected and compare_to is not None)


def run_migrations_online():
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = settings.database_url
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=False,
            compare_server_default=False,
            compare_nullable=False,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


def run_migrations_offline():
    context.configure(url=settings.database_url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


(run_migrations_online if not context.is_offline_mode() else run_migrations_offline)()
