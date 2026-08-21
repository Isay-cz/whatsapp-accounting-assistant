import asyncio

# Registro de tasks fire-and-forget para que no las recoja el garbage
# collector a medio watch — deben sobrevivir más allá del ciclo
# request/response que las crea (por eso son asyncio.create_task, no
# BackgroundTasks de FastAPI).
_live_tasks: set[asyncio.Task] = set()


def track(task: asyncio.Task) -> None:
    _live_tasks.add(task)
    task.add_done_callback(_live_tasks.discard)


def cancel_all() -> None:
    """Cancela los watchers que sigan vivos. Lo usan las pruebas: un watcher
    que despierta después de que el test cerró su sesión de base de datos se
    queda colgado sobre una conexión que ya nadie va a atender.

    Es tolerante a propósito: cada test corre en su propio event loop, así
    que en el registro pueden quedar tasks de un loop ya cerrado — esas ni
    se pueden cancelar ni hace falta, solo se sacan del registro."""
    for task in list(_live_tasks):
        if not task.done():
            try:
                task.cancel()
            except RuntimeError:
                pass  # su event loop ya no existe
        _live_tasks.discard(task)
