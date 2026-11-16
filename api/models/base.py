from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base declarativa de los modelos.

    Vive aquí y no en `database.py` para que Alembic pueda cargar los modelos
    sin construir `Settings` ni el engine: migrar solo necesita `DATABASE_URL`
    (`db/migrations/env.py`), no los secretos de Meta que el arranque de la
    aplicación exige."""
