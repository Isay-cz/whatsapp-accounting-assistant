import httpx

from config import Settings

GRAPH_BASE_URL = "https://graph.facebook.com"

# Límites de los mensajes interactivos, según la documentación de Cloud API.
# Con un texto más largo Meta rechaza el mensaje completo, así que quien arma
# las opciones recorta antes (`truncate`) y este cliente se niega a mandar lo
# que no cabe: que falle en las pruebas y no contra Meta.
BUTTON_TITLE_MAX = 20
LIST_ROW_TITLE_MAX = 24
LIST_ROW_DESCRIPTION_MAX = 72


def truncate(text: str, limit: int) -> str:
    """Recorta `text` a `limit` caracteres; si recortó, termina en «…»."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class MetaClient:
    """Cliente async de WhatsApp Cloud API (Graph API) — texto y mensajes
    interactivos (listas, botones). Reemplaza al cliente Twilio anterior,
    que estaba roto (importaba `settings` de un módulo que solo exporta
    `get_settings()`, con atributos en mayúsculas que no existen)."""

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        api_version: str,
        base_url: str = GRAPH_BASE_URL,
    ):
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._api_version = api_version
        # `base_url` existe solo para poder apuntar la salida a un receptor
        # local en pruebas de despliegue (scripts/meta_sink.py), donde todavía
        # no hay número de Cloud API verificado. El default es Meta.
        self._base_url = base_url.rstrip("/")

    @property
    def _url(self) -> str:
        return f"{self._base_url}/{self._api_version}/{self._phone_number_id}/messages"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    async def send_text(self, to: str, body: str) -> None:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        await self._post(payload)

    async def send_buttons(self, to: str, body: str, options: list[dict]) -> None:
        """`options`: 1 a 3 dicts `{"id": ..., "title": ...}`. El `id` es el
        valor real (p. ej. la prioridad) que llega en `button_reply.id` —
        nunca se parsea el texto libre de la respuesta."""
        if not 1 <= len(options) <= 3:
            raise ValueError("send_buttons soporta entre 1 y 3 opciones")
        for opt in options:
            if len(opt["title"]) > BUTTON_TITLE_MAX:
                raise ValueError(
                    f"El título de un botón admite {BUTTON_TITLE_MAX} caracteres: {opt['title']!r}"
                )
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": opt["id"], "title": opt["title"]}}
                        for opt in options
                    ]
                },
            },
        }
        await self._post(payload)

    async def send_list(self, to: str, body: str, options: list[dict], button_label: str = "Elegir") -> None:
        """`options`: 1 a 10 dicts `{"id": ..., "title": ...}`, con una
        `description` opcional que WhatsApp pinta debajo del título. El `id`
        es el UUID real (p. ej. el cliente) que llega en `list_reply.id`."""
        if not 1 <= len(options) <= 10:
            raise ValueError("send_list soporta entre 1 y 10 opciones")
        rows = []
        for opt in options:
            if len(opt["title"]) > LIST_ROW_TITLE_MAX:
                raise ValueError(
                    f"El título de una fila admite {LIST_ROW_TITLE_MAX} caracteres: {opt['title']!r}"
                )
            row = {"id": opt["id"], "title": opt["title"]}
            description = opt.get("description")
            if description:
                if len(description) > LIST_ROW_DESCRIPTION_MAX:
                    raise ValueError(
                        f"La descripción de una fila admite {LIST_ROW_DESCRIPTION_MAX} "
                        f"caracteres: {description!r}"
                    )
                row["description"] = description
            rows.append(row)
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body},
                "action": {
                    "button": button_label,
                    "sections": [{"rows": rows}],
                },
            },
        }
        await self._post(payload)

    async def _post(self, payload: dict) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(self._url, json=payload, headers=self._headers)
            response.raise_for_status()


def get_meta_client(settings: Settings) -> MetaClient:
    return MetaClient(
        access_token=settings.meta_access_token,
        phone_number_id=settings.meta_phone_number_id,
        api_version=settings.meta_api_version,
        base_url=settings.meta_graph_base_url,
    )
