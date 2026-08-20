import httpx

from config import Settings
from core.security import internal_auth_header
from models.schemas import ClientSearchResult, CreatedTicket, WorkerSync


class TicketSystemClient:
    """Cliente HTTP hacia la API de CGHO Sistema de Tickets.

    Todo va contra el grupo `/internal`, que se autentica con el token de
    servicio compartido y no con el JWT de un usuario humano. Ese token
    autentica la *llamada*; el *actor* de lo que se escribe se declara
    explícitamente en cada request (`created_by`).
    """

    def __init__(self, base_url: str, internal_api_token: str):
        self._base_url = base_url.rstrip("/")
        self._headers = internal_auth_header(internal_api_token)

    async def search_clients(self, query: str) -> list[ClientSearchResult]:
        """Máximo 9 coincidencias — el décimo lugar de la lista interactiva de
        WhatsApp lo ocupa la opción fija "Sin cliente" que arma el orquestador."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{self._base_url}/internal/clients/search",
                params={"q": query},
                headers=self._headers,
            )
            response.raise_for_status()
            return [
                ClientSearchResult(**item) for item in response.json()["matches"]
            ]

    async def create_ticket(
        self,
        title: str,
        description: str,
        priority: str,
        created_by: str,
        client_id: str | None,
    ) -> CreatedTicket:
        """El departamento no se manda: lo deriva el sistema de tickets a
        partir de `created_by` (ver CLAUDE.md, decisión #8)."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._base_url}/internal/tickets",
                json={
                    "title": title,
                    "description": description,
                    "priority": priority,
                    "created_by": created_by,
                    "client_id": client_id,
                },
                headers=self._headers,
            )
            response.raise_for_status()
            return CreatedTicket(**response.json())

    async def list_workers(self) -> list[WorkerSync]:
        """Roster completo de trabajadores con teléfono, para el poll de la
        whitelist (ver CLAUDE.md, decisión #15)."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{self._base_url}/internal/workers",
                headers=self._headers,
            )
            response.raise_for_status()
            return [WorkerSync(**item) for item in response.json()["workers"]]


def get_ticket_system_client(settings: Settings) -> TicketSystemClient:
    return TicketSystemClient(
        base_url=settings.ticket_system_base_url,
        internal_api_token=settings.internal_api_token,
    )
