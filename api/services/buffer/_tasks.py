import asyncio

# Registro de tasks fire-and-forget para que no las recoja el garbage
# collector a medio watch — deben sobrevivir más allá del ciclo
# request/response que las crea (por eso son asyncio.create_task, no
# BackgroundTasks de FastAPI).
_live_tasks: set[asyncio.Task] = set()


def track(task: asyncio.Task) -> None:
    _live_tasks.add(task)
    task.add_done_callback(_live_tasks.discard)
