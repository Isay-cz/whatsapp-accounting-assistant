import httpx

from config import Settings
from core.security import internal_auth_header
from models.schemas import ClientSearchResult


class TicketSystemClient:
    """Cliente HTTP hacia la API de CGHO Sistema de Tickets. Sus endpoints
    de búsqueda de clientes y creación de tickets no existen todavía del
    otro lado (ver CLAUDE.md, Pendientes) — este cliente se construye y se
    prueba con mocks en este repo, nunca contra el servicio real."""

    def __init__(self, base_url: str, internal_api_token: str):
        self._base_url = base_url.rstrip("/")
        self._headers = internal_auth_header(internal_api_token)

    async def search_clients(self, query: str) -> list[ClientSearchResult]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{self._base_url}/clients/search",
                params={"q": query},
                headers=self._headers,
            )
            response.raise_for_status()
            return [ClientSearchResult(**item) for item in response.json()]

    async def create_ticket(
        self,
        title: str,
        description: str,
        priority: str,
        created_by: str,
        client_id: str | None,
    ) -> str:
        """Devuelve el id del ticket creado."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._base_url}/tickets",
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
            return response.json()["id"]

    async def add_department(
        self, ticket_id: str, department_id: str, actor_id: str, note: str | None = None
    ) -> None:
        """Diseño provisional: el contrato real de cómo el sistema de
        tickets asocia un departamento a un ticket (¿mismo POST de
        creación, o llamada aparte a ticket_departments?) no está definido
        del otro lado todavía. Ver CLAUDE.md, Pendientes."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self._base_url}/tickets/{ticket_id}/departments",
                json={"department_id": department_id, "actor_id": actor_id, "note": note},
                headers=self._headers,
            )
            response.raise_for_status()


def get_ticket_system_client(settings: Settings) -> TicketSystemClient:
    return TicketSystemClient(
        base_url=settings.ticket_system_base_url,
        internal_api_token=settings.internal_api_token,
    )
