#!/usr/bin/env python3
"""Receptor local que hace las veces de Graph API en entornos de prueba.

Existe por una sola razón: sin número de Cloud API verificado, todo lo que el
bot manda hacia afuera (la pregunta de cliente, los botones de prioridad, la
confirmación "Ticket #N creado") muere en un 404 contra
`https://graph.facebook.com/v21.0//messages`, y el flujo se corta justo
después de cerrar el buffer. Apuntando `META_GRAPH_BASE_URL` aquí, el flujo
completo corre de verdad en el entorno desplegado y además queda registrado
lo que el trabajador *habría* recibido, que es lo que el script de humo
verifica.

No es un mock de Meta: no valida el token, no aplica la ventana de 24h, no
reproduce códigos de error. Acepta lo que llega, responde 200 con un
`wamid` sintético y lo guarda en memoria.

Solo stdlib: corre dentro de la imagen del bot sin instalar nada.

    python meta_sink.py [--port 8099]

Endpoints propios (prefijo `_` para que nunca choquen con una ruta de Graph):
    GET    /_health          -> {"status": "ok", "count": N}
    GET    /_sent            -> {"sent": [...]}  (filtros: ?since=N&to=52...)
    POST   /_reset           -> vacía lo registrado
"""

import argparse
import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("meta_sink")

_lock = threading.Lock()
_sent: list[dict] = []


def _record(path: str, payload: dict) -> dict:
    """Aplana el payload de Graph a algo sobre lo que se pueda afirmar sin
    volver a parsear la estructura de WhatsApp en cada aserción. El payload
    íntegro se conserva en `raw` para lo que el aplanado no cubra."""
    interactive = payload.get("interactive") or {}
    action = interactive.get("action") or {}
    sections = action.get("sections") or []
    rows = sections[0].get("rows", []) if sections else []
    buttons = [b.get("reply", {}) for b in action.get("buttons", [])]

    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "path": path,
        "to": payload.get("to"),
        "type": payload.get("type"),
        # `body` es el texto visible sea cual sea el tipo: para un mensaje de
        # texto es text.body, para uno interactivo es interactive.body.text.
        # El script de humo espera por texto, no por forma.
        "body": (
            (payload.get("text") or {}).get("body")
            if payload.get("type") == "text"
            else (interactive.get("body") or {}).get("text")
        ),
        "interactive_type": interactive.get("type"),
        "options": [
            {"id": o.get("id"), "title": o.get("title")} for o in (rows or buttons)
        ],
        "raw": payload,
    }

    with _lock:
        entry["seq"] = len(_sent) + 1
        _sent.append(entry)
    return entry


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # el logging real lo hacemos abajo
        pass

    # -- helpers ---------------------------------------------------------

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return None

    # -- rutas -----------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/_health":
            with _lock:
                self._json(200, {"status": "ok", "count": len(_sent)})
            return
        if url.path == "/_sent":
            params = parse_qs(url.query)
            since = int(params.get("since", ["0"])[0])
            to = params.get("to", [None])[0]
            with _lock:
                items = [e for e in _sent if e["seq"] > since]
            if to:
                items = [e for e in items if e["to"] == to]
            self._json(200, {"sent": items})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/_reset":
            with _lock:
                _sent.clear()
            self._json(200, {"status": "reset"})
            return

        # Cualquier /vXX/<phone_number_id>/messages. No se valida la forma de
        # la ruta a propósito: si el bot arma una URL rara (p. ej. con
        # META_PHONE_NUMBER_ID vacío, que da una doble diagonal), queremos
        # verlo registrado, no rechazado con un 404 indistinguible del de
        # Meta.
        if url.path.endswith("/messages"):
            payload = self._read_json()
            if payload is None:
                self._json(400, {"error": "body no es JSON"})
                return
            entry = _record(url.path, payload)
            logger.info(
                "-> %s | %s | %s",
                entry["to"],
                entry["type"],
                (entry["body"] or "")[:70],
            )
            self._json(
                200,
                {
                    "messaging_product": "whatsapp",
                    "contacts": [{"input": entry["to"], "wa_id": entry["to"]}],
                    "messages": [{"id": f"wamid.sink.{entry['seq']}"}],
                },
            )
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logger.info("meta_sink escuchando en %s:%s", args.host, args.port)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
