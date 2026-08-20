from sqlalchemy import select

from database import AsyncSessionLocal
from models.orm import Worker


async def lookup_active_worker_by_phone(phone: str) -> Worker | None:
    """Usado por el flujo conversacional para resolver el `Worker` (y sus
    referencias externas) en el momento de cerrar el buffer o recuperar
    watchers tras un restart — nunca se arrastra el objeto ORM a través de
    la ventana de debounce, se re-consulta fresco cada vez."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Worker).where(Worker.phone_number == phone, Worker.is_active == True)
        )
        return result.scalar_one_or_none()
