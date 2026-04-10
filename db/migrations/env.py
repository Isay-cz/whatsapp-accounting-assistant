from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context
import sys
import os

# ── Cargar variables de entorno desde .env (para desarrollo local) ──────────
from dotenv import load_dotenv
_root_dir = os.path.join(os.path.dirname(__file__), '..', '..')
load_dotenv(os.path.join(_root_dir, '.env'))

# ── Agregar carpeta raíz y carpeta api al path ───────────────────────────────
sys.path.insert(0, os.path.abspath(_root_dir))
sys.path.insert(0, os.path.abspath(os.path.join(_root_dir, 'api')))

# Ahora sys.path apunta a /api, así que los imports NO llevan prefijo "api."
from database import Base          # noqa
from models import orm             # noqa: F401 — necesario para que alembic vea los modelos

# ── Config de Alembic ────────────────────────────────────────────────────────
config = context.config

# Inyectar DATABASE_URL desde el entorno (resuelve el error InterpolationMissing)
# Alembic necesita un driver sincrónico: reemplazar asyncpg → psycopg2
_db_url = os.environ.get("DATABASE_URL", "")
_sync_url = (
    _db_url
    .replace("postgresql+asyncpg://", "postgresql://")
    .replace("postgresql+aiosqlite://", "sqlite:///")
)
config.set_main_option("sqlalchemy.url", _sync_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
