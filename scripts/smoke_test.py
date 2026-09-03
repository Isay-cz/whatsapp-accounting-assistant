#!/usr/bin/env python3
"""Prueba de humo del despliegue: webhook público + flujo completo, en el
entorno real.

Qué cubre que las pruebas de `api/tests/` no cubren:

- El **camino de red de verdad**. `test_ticket_lifecycle.py` y
  `test_live_integration.py` entran por ASGI, dentro del proceso. Aquí los
  requests entran por HTTPS público, pasan por el reverse proxy y llegan al
  contenedor desplegado — que es lo único que prueba que Meta podría
  entregarle un webhook.
- La **configuración desplegada**. Un `META_APP_SECRET` vacío, un alias de red
  equivocado o una ruta de proxy faltante son invisibles para pytest y
  rompen el bot en producción.

Qué NO cubre: Meta. La salida va a `meta_sink.py` (ver `setup`), así que esto
verifica lo que el bot *habría* mandado, no que WhatsApp lo entregue.

Corre **en la VM**, con python3 del sistema (solo stdlib) y acceso al socket
de Docker:

    python3 smoke_test.py all          # setup + run + teardown
    python3 smoke_test.py setup        # levanta el sink y repunta el bot
    python3 smoke_test.py run          # solo las verificaciones
    python3 smoke_test.py teardown     # restaura el .env y quita el sink

Deja rastro a propósito (es la evidencia de la corrida): filas en
`raw_messages` y `ticket_creations` con `wamid` `wamid.smoke.<run>.*`, y **un
ticket real** en el sistema de tickets — con marcador de prueba, prioridad
baja y sin cliente, para que no encabece la cola de nadie ni se cuelgue del
historial de un cliente real. Gasta ~US$0.001 de DeepSeek por corrida.

Pensado para una VM de prueba: asume que nadie más le está mandando mensajes
al bot mientras corre (limpia las llaves de Redis del teléfono que usa).
"""

import argparse
import hashlib
import hmac
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

# -- Constantes del contrato que se está verificando -------------------------
# Copiadas a propósito de services/conversation/orchestrator.py en vez de
# importadas: este script corre fuera del contenedor, sin el código de la app
# a la mano. Si un texto cambia allá y no aquí, la prueba falla ruidosamente,
# que es el comportamiento correcto para una prueba de contrato.
ASK_CLIENT_TEXT = "¿A qué cliente corresponde? Escribe el nombre."
PICK_CLIENT_TEXT = "¿Cuál de estos clientes?"
ASK_PRIORITY_TEXT = "¿Qué prioridad tiene?"
NO_CLIENT_OPTION_ID = "__sin_cliente__"
TICKET_CONFIRMATION_PREFIX = "Ticket #"

MARKER = "PRUEBA DE DESPLIEGUE DEL BOT — ignorar, se puede cerrar"

SINK_ENV_VAR = "META_GRAPH_BASE_URL"
ENV_BACKUP_SUFFIX = ".antes-del-smoke"


# ===========================================================================
# Utilidades
# ===========================================================================

class Colors:
    OK = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    END = "\033[0m"

    @classmethod
    def disable(cls):
        for name in ("OK", "FAIL", "WARN", "DIM", "BOLD", "END"):
            setattr(cls, name, "")


if not sys.stdout.isatty():
    Colors.disable()


class SmokeError(Exception):
    """Falla que impide seguir (preflight, infraestructura ausente)."""


class Report:
    """Acumula los resultados para poder imprimir un resumen al final en vez
    de obligar a leer el scrollback completo."""

    def __init__(self):
        self.checks: list[tuple[str, bool, str]] = []
        self.notes: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, ok, detail))
        mark = f"{Colors.OK}✓{Colors.END}" if ok else f"{Colors.FAIL}✗{Colors.END}"
        line = f"  {mark} {name}"
        if detail:
            line += f"  {Colors.DIM}{detail}{Colors.END}"
        print(line, flush=True)
        return ok

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"  {Colors.DIM}· {text}{Colors.END}", flush=True)

    @property
    def failed(self) -> list[str]:
        return [n for n, ok, _ in self.checks if not ok]

    def summary(self) -> int:
        print()
        print(f"{Colors.BOLD}{'=' * 70}{Colors.END}")
        total, bad = len(self.checks), len(self.failed)
        if bad == 0:
            print(f"{Colors.OK}{Colors.BOLD}TODO BIEN — {total}/{total} verificaciones{Colors.END}")
        else:
            print(f"{Colors.FAIL}{Colors.BOLD}FALLARON {bad} de {total} verificaciones{Colors.END}")
            for name in self.failed:
                print(f"  {Colors.FAIL}✗{Colors.END} {name}")
        if self.notes:
            print()
            print(f"{Colors.BOLD}Rastro de la corrida:{Colors.END}")
            for note in self.notes:
                print(f"  · {note}")
        print(f"{Colors.BOLD}{'=' * 70}{Colors.END}")
        return 1 if bad else 0


def section(title: str) -> None:
    print()
    print(f"{Colors.BOLD}{title}{Colors.END}")


def sh(*args: str, check: bool = True, cwd: Path | None = None) -> str:
    """Ejecuta un comando y devuelve stdout. Sin shell: los argumentos van
    tal cual, así que un nombre de contenedor o una consulta SQL con
    espacios no necesita comillas.

    `cwd` importa para `docker compose`: sin `-f`, busca el archivo de
    compose y el `.env` en el directorio de trabajo, no en el que le pase
    `--project-directory`."""
    proc = subprocess.run(
        args, capture_output=True, text=True, timeout=300,
        cwd=str(cwd) if cwd else None,
    )
    if check and proc.returncode != 0:
        raise SmokeError(
            f"Falló `{' '.join(args)}`\n  stdout: {proc.stdout.strip()}\n"
            f"  stderr: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def read_env(path: Path) -> dict[str, str]:
    """Lee un .env de host (el que alimenta la interpolación de Compose).
    No es un parser completo: no hay variables multilínea en estos archivos."""
    values: dict[str, str] = {}
    if not path.exists():
        raise SmokeError(f"No existe {path}")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def set_env_var(path: Path, key: str, value: str) -> None:
    """Reescribe (o agrega) una variable del .env conservando comentarios y
    orden — el archivo es documentación además de configuración."""
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")


def find_container(*needles: str) -> str:
    """Nombre del contenedor que contiene todos los fragmentos dados. Se busca
    por nombre y no por servicio de Compose porque los dos stacks tienen un
    servicio `api` y este script corre fuera de los dos proyectos."""
    names = sh("docker", "ps", "--format", "{{.Names}}").splitlines()
    matches = [n for n in names if all(x in n for x in needles)]
    if not matches:
        raise SmokeError(
            f"No hay contenedor corriendo que contenga {needles}. "
            f"Contenedores activos: {', '.join(names) or '(ninguno)'}"
        )
    return matches[0]


class HttpResult:
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body

    def json(self):
        return json.loads(self.body)


def http(
    method: str, url: str, *, data: bytes | None = None, headers: dict | None = None
) -> HttpResult:
    """Request que devuelve el status en vez de lanzar en 4xx/5xx: aquí un 403
    suele ser el resultado *esperado*."""
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return HttpResult(response.status, response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return HttpResult(exc.code, exc.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        raise SmokeError(f"No se pudo alcanzar {url}: {exc.reason}") from exc
    except OSError as exc:
        # Un puerto publicado por Docker acepta la conexión (docker-proxy ya
        # escucha) antes de que el servidor de adentro esté listo, así que el
        # primer request se cae con ConnectionResetError en vez de URLError.
        # Se normaliza a SmokeError para que quien espera pueda reintentar.
        raise SmokeError(f"No se pudo alcanzar {url}: {exc}") from exc


def wait_for(predicate, *, timeout: float, interval: float = 1.0):
    """Espera activa por un efecto asíncrono (cierre del buffer, respuesta de
    DeepSeek, creación del ticket). Devuelve lo que regrese el predicado, o
    None si se agotó el tiempo."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


# ===========================================================================
# Entorno desplegado
# ===========================================================================

class Deployment:
    """Todo lo que este script sabe de la VM: contenedores, .env y accesos
    directos a Postgres y Redis."""

    def __init__(self, args):
        self.bot_dir = Path(args.bot_dir).expanduser()
        self.ops_dir = Path(args.ops_dir).expanduser()
        self.bot_env_path = self.bot_dir / ".env"
        self.ops_env_path = self.ops_dir / ".env"
        self.bot_env = read_env(self.bot_env_path)
        self.ops_env = read_env(self.ops_env_path)

        self.postgres = find_container("postgres")
        self.redis = find_container("redis")
        self.bot = find_container("whatsapp", "api")

        self.sink_name = args.sink_name
        self.sink_port = args.sink_port
        self.sink_url = f"http://127.0.0.1:{args.sink_port}"

        host = args.base_url or f"https://{self.ops_env.get('CADDY_HOSTNAME', '')}"
        if host in ("https://", ""):
            raise SmokeError(
                "No hay hostname público: define CADDY_HOSTNAME en el .env de "
                "cgho-ops o pasa --base-url"
            )
        self.base_url = host.rstrip("/")
        self.prefix = "/" + args.prefix.strip("/") if args.prefix.strip("/") else ""
        self.webhook_url = f"{self.base_url}{self.prefix}/webhook"

    # -- Postgres -------------------------------------------------------

    def psql(self, database: str, sql: str) -> list[list[str]]:
        """Consulta cruda contra el Postgres compartido.

        Que este script lea las dos bases no contradice la decisión #1 del
        CLAUDE.md: la que no puede cruzarlas es *la aplicación*. Una prueba de
        despliegue tiene que poder verificar los dos lados, y lo hace en modo
        solo lectura, desde afuera de los dos procesos.
        """
        out = sh(
            "docker", "exec", "-i", self.postgres,
            "psql", "-U", self.ops_env["POSTGRES_USER"], "-d", database,
            "-tAF", "\x1f", "-c", sql,
        )
        return [line.split("\x1f") for line in out.splitlines() if line]

    def bot_db(self, sql: str) -> list[list[str]]:
        return self.psql("bot_db", sql)

    def tickets_db(self, sql: str) -> list[list[str]]:
        return self.psql(self.ops_env["POSTGRES_DB"], sql)

    # -- Redis ----------------------------------------------------------

    def redis_cli(self, *args: str) -> str:
        return sh("docker", "exec", self.redis, "redis-cli", *args)

    def clear_phone_state(self, phone: str) -> None:
        """Borra el estado de Redis del teléfono de prueba. Solo las llaves de
        ese número: un `flushdb` tumbaría los buffers en curso del bot."""
        self.redis_cli(
            "DEL",
            f"bot:buffer:{phone}",
            f"bot:markers:buffer:{phone}",
            f"bot:locks:buffer:{phone}",
            f"bot:session:{phone}",
            f"bot:locks:session:{phone}:awaiting_client",
            f"bot:locks:session:{phone}:awaiting_priority",
        )

    def session_state(self, phone: str) -> dict | None:
        raw = self.redis_cli("GET", f"bot:session:{phone}")
        return json.loads(raw) if raw else None

    # -- Sink -----------------------------------------------------------

    def sink_sent(self, since: int = 0) -> list[dict]:
        return http("GET", f"{self.sink_url}/_sent?{urlencode({'since': since})}").json()["sent"]

    def sink_reset(self) -> None:
        http("POST", f"{self.sink_url}/_reset", data=b"")

    def sink_running(self) -> bool:
        names = sh("docker", "ps", "--format", "{{.Names}}").splitlines()
        return self.sink_name in names

    # -- Webhook --------------------------------------------------------

    def post_webhook(self, payload: dict, *, sign: bool = True, tamper: bool = False):
        """Manda un webhook firmado como lo firma Meta: HMAC-SHA256 del cuerpo
        crudo con el App Secret. El cuerpo se serializa UNA vez y se firma ese
        mismo `bytes` — firmar un re-serializado daría una firma válida para
        un cuerpo distinto al enviado."""
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if sign:
            secret = self.bot_env.get("META_APP_SECRET", "")
            digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Hub-Signature-256"] = f"sha256={digest}"
        if tamper:
            # Firma válida, cuerpo alterado después de firmar — el ataque real
            # contra el que sirve el HMAC.
            body = body.replace(b'"text"', b'"txet"', 1)
        return http("POST", self.webhook_url, data=body, headers=headers)


# ===========================================================================
# Payloads de Cloud API
# ===========================================================================

def text_payload(text: str, *, wamid: str, from_number: str) -> dict:
    """Misma forma que manda Cloud API — ver docs/whatsapp-webhook-reference.md."""
    return _envelope(
        from_number,
        {
            "id": wamid,
            "from": from_number,
            "timestamp": str(int(time.time())),
            "type": "text",
            "text": {"body": text},
        },
    )


def interactive_payload(
    reply_id: str, *, wamid: str, from_number: str, reply_type: str, title: str
) -> dict:
    return _envelope(
        from_number,
        {
            "id": wamid,
            "from": from_number,
            "timestamp": str(int(time.time())),
            "type": "interactive",
            "interactive": {
                "type": reply_type,
                reply_type: {"id": reply_id, "title": title},
            },
        },
    )


def _envelope(from_number: str, message: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba_id_smoke",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "0000000000",
                                "phone_number_id": "smoke-test",
                            },
                            "contacts": [
                                {"profile": {"name": "Prueba de humo"}, "wa_id": from_number}
                            ],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


# ===========================================================================
# setup / teardown
# ===========================================================================

def cmd_setup(dep: Deployment, args) -> int:
    section("SETUP — sink de Meta y repunte del bot")

    sink_file = Path(__file__).resolve().parent / "meta_sink.py"
    if not sink_file.exists():
        raise SmokeError(f"Falta {sink_file} (debe viajar junto a este script)")

    image = sh("docker", "inspect", "-f", "{{.Config.Image}}", dep.bot)
    sh("docker", "rm", "-f", dep.sink_name, check=False)
    # Se reusa la imagen del bot para no bajar una segunda imagen a la VM; lo
    # único que se necesita de ella es el intérprete de Python. `--entrypoint`
    # es obligatorio: el entrypoint del bot corre `alembic upgrade head`.
    sh(
        "docker", "run", "-d",
        "--name", dep.sink_name,
        "--network", "cgho_net",
        # Publicado solo en loopback: lo lee este script desde el host, no
        # tiene por qué ser alcanzable desde fuera de la VM.
        "-p", f"127.0.0.1:{dep.sink_port}:{dep.sink_port}",
        "-v", f"{sink_file}:/meta_sink.py:ro",
        "--entrypoint", "python",
        image,
        "/meta_sink.py", "--port", str(dep.sink_port),
    )
    print(f"  sink `{dep.sink_name}` levantado sobre {image}")

    if not wait_for(lambda: _sink_healthy(dep), timeout=30):
        raise SmokeError(f"El sink no respondió en {dep.sink_url}/_health")
    print(f"  sink respondiendo en {dep.sink_url}")

    # -- .env del bot ---------------------------------------------------
    backup = dep.bot_env_path.with_suffix(dep.bot_env_path.suffix + ENV_BACKUP_SUFFIX)
    if not backup.exists():
        backup.write_text(dep.bot_env_path.read_text())
        print(f"  respaldo del .env en {backup.name}")

    changes: list[str] = []

    # Sin estos dos, el webhook no es verificable NI seguro: con un App Secret
    # vacío, cualquiera puede calcular el HMAC y mandarle mensajes al bot.
    # Se generan valores reales en vez de aceptar los vacíos.
    for key, note in (
        ("META_APP_SECRET", "llave del HMAC de X-Hub-Signature-256"),
        ("META_VERIFY_TOKEN", "handshake GET /webhook"),
    ):
        if not dep.bot_env.get(key):
            value = secrets.token_hex(24)
            set_env_var(dep.bot_env_path, key, value)
            dep.bot_env[key] = value
            changes.append(f"{key} estaba vacío → se generó uno ({note})")

    # Solo cosmético: con el phone_number_id vacío la URL saliente queda con
    # doble diagonal. El sink la aceptaría igual, pero cuesta más leer el log.
    for key, placeholder in (
        ("META_PHONE_NUMBER_ID", "smoke-test"),
        ("META_ACCESS_TOKEN", "smoke-test-token"),
    ):
        if not dep.bot_env.get(key):
            set_env_var(dep.bot_env_path, key, placeholder)
            dep.bot_env[key] = placeholder
            changes.append(f"{key} estaba vacío → placeholder `{placeholder}`")

    sink_target = f"http://{dep.sink_name}:{dep.sink_port}"
    if dep.bot_env.get(SINK_ENV_VAR) != sink_target:
        set_env_var(dep.bot_env_path, SINK_ENV_VAR, sink_target)
        dep.bot_env[SINK_ENV_VAR] = sink_target
        changes.append(f"{SINK_ENV_VAR} → {sink_target}")

    for change in changes:
        print(f"  {Colors.WARN}·{Colors.END} {change}")

    print("  recreando el contenedor del bot para que tome el entorno nuevo…")
    sh("docker", "compose", "up", "-d", cwd=dep.bot_dir)
    dep.bot = find_container("whatsapp", "api")

    if not wait_for(
        lambda: SINK_ENV_VAR in sh("docker", "exec", dep.bot, "printenv", check=False),
        timeout=90,
    ):
        raise SmokeError("El bot no arrancó con META_GRAPH_BASE_URL en su entorno")
    print(f"  {Colors.OK}listo{Colors.END} — el bot manda su salida al sink")
    return 0


def cmd_teardown(dep: Deployment, args) -> int:
    section("TEARDOWN — restaurando el estado anterior")

    backup = dep.bot_env_path.with_suffix(dep.bot_env_path.suffix + ENV_BACKUP_SUFFIX)
    if backup.exists():
        dep.bot_env_path.write_text(backup.read_text())
        backup.unlink()
        print("  .env restaurado desde el respaldo")
        sh("docker", "compose", "up", "-d", cwd=dep.bot_dir)
        print("  bot recreado con la configuración original")
    else:
        print("  no había respaldo del .env — nada que restaurar")

    sh("docker", "rm", "-f", dep.sink_name, check=False)
    print(f"  sink `{dep.sink_name}` eliminado")
    return 0


# ===========================================================================
# Verificaciones
# ===========================================================================

def preflight(dep: Deployment, rep: Report) -> dict:
    section("0 · PREFLIGHT — el despliegue está en pie")

    rep.check("Contenedor de Postgres corriendo", True, dep.postgres)
    rep.check("Contenedor de Redis corriendo", True, dep.redis)
    rep.check("Contenedor del bot corriendo", True, dep.bot)
    rep.check("Contenedor del sistema de tickets corriendo", True, find_container("ops", "api"))

    ok_sink = dep.sink_running()
    rep.check(
        f"Sink de Meta (`{dep.sink_name}`) corriendo", ok_sink,
        "" if ok_sink else "corre `smoke_test.py setup` primero",
    )
    if not ok_sink:
        raise SmokeError("Sin el sink no se puede probar el flujo completo")

    env = sh("docker", "exec", dep.bot, "printenv")
    env_vars = dict(
        line.partition("=")[::2] for line in env.splitlines() if "=" in line
    )
    rep.check(
        "El bot apunta su salida al sink",
        env_vars.get(SINK_ENV_VAR, "").startswith(f"http://{dep.sink_name}"),
        env_vars.get(SINK_ENV_VAR, "(sin definir)"),
    )
    rep.check(
        "META_APP_SECRET no está vacío en el contenedor",
        bool(env_vars.get("META_APP_SECRET")),
        "un secreto vacío hace el HMAC calculable por cualquiera",
    )
    rep.check(
        "META_VERIFY_TOKEN no está vacío en el contenedor",
        bool(env_vars.get("META_VERIFY_TOKEN")),
    )

    # La variable la inyecta Compose siempre, así que verla en el entorno no
    # prueba que la imagen la lea: una imagen anterior al cambio la ignoraría
    # y seguiría llamando a graph.facebook.com. Sin esta verificación, eso se
    # manifestaría mucho después como un timeout confuso esperando al sink.
    supports = sh(
        "docker", "exec", dep.bot, "grep", "-c", "meta_graph_base_url", "config.py",
        check=False,
    )
    rep.check(
        "La imagen desplegada lee META_GRAPH_BASE_URL",
        supports.isdigit() and int(supports) > 0,
        "" if supports.isdigit() and int(supports) > 0 else
        "imagen anterior al cambio: `docker compose pull && docker compose up -d`",
    )

    # El proxy: que `{prefix}/health` responda prueba que Caddy rutea a ESTE
    # contenedor y no al del sistema de tickets (que también expone /health).
    # La distinción de verdad la hace el handshake, más abajo.
    health = http("GET", f"{dep.base_url}{dep.prefix}/health")
    ok_route = health.status == 200
    rep.check(
        f"El proxy rutea {dep.prefix}/ hacia el bot", ok_route,
        f"HTTP {health.status}",
    )
    if not ok_route:
        raise SmokeError(
            f"{dep.base_url}{dep.prefix}/health devolvió {health.status}.\n"
            "  Falta la ruta en caddy/Caddyfile.prod del stack de cgho-ops:\n\n"
            "      handle /bot/* {\n"
            "          uri strip_prefix /bot\n"
            "          reverse_proxy bot-api:8000\n"
            "      }\n\n"
            "  y el alias `bot-api` en el docker-compose del bot. Después:\n"
            "      docker exec cgho-ops-caddy-1 caddy reload --config /etc/caddy/Caddyfile\n"
            "      (cd ~/whatsapp-accounting-assistant && docker compose up -d)"
        )

    worker = dep.bot_db(
        "SELECT phone_number, name, external_user_id, id FROM workers "
        "WHERE is_active AND external_user_id IS NOT NULL "
        "ORDER BY created_at LIMIT 1"
    )
    ok_worker = bool(worker)
    rep.check(
        "Hay un trabajador activo con external_user_id", ok_worker,
        f"{worker[0][1]} / {worker[0][0]}" if ok_worker else
        "revisa el poll contra GET /internal/workers (decisión #15)",
    )
    if not ok_worker:
        raise SmokeError("Sin trabajador en la whitelist no hay flujo que probar")

    phone, name, external_user_id, worker_id = worker[0]
    return {
        "phone": phone, "name": name,
        "external_user_id": external_user_id, "worker_id": worker_id,
    }


def phase_connectivity(dep: Deployment, rep: Report, worker: dict) -> None:
    """Todo lo que se puede verificar sin efectos secundarios. Se usa un
    número NO autorizado para los POST firmados: así ninguna de estas
    verificaciones mete mensajes al buffer del trabajador real."""
    section("1 · CONECTIVIDAD — el webhook público responde como debe")

    verify_token = dep.bot_env["META_VERIFY_TOKEN"]
    challenge = f"smoke-{secrets.token_hex(4)}"

    def handshake(token: str, with_challenge: bool = True) -> HttpResult:
        params = {"hub.mode": "subscribe", "hub.verify_token": token}
        if with_challenge:
            params["hub.challenge"] = challenge
        return http("GET", f"{dep.webhook_url}?{urlencode(params)}")

    ok = handshake(verify_token)
    rep.check(
        "GET /webhook con el verify token correcto devuelve el challenge",
        ok.status == 200 and ok.body == challenge,
        f"HTTP {ok.status} body={ok.body[:40]!r}",
    )
    rep.note(f"URL de webhook para el dashboard de Meta: {dep.webhook_url}")

    bad = handshake("token-equivocado-" + secrets.token_hex(4))
    rep.check(
        "GET /webhook con un verify token equivocado devuelve 403",
        bad.status == 403, f"HTTP {bad.status}",
    )

    empty = handshake("")
    rep.check(
        "GET /webhook con verify token vacío devuelve 403",
        empty.status == 403,
        f"HTTP {empty.status} — si pasa, META_VERIFY_TOKEN está vacío en el contenedor",
    )

    no_challenge = handshake(verify_token, with_challenge=False)
    rep.check(
        "GET /webhook sin hub.challenge devuelve 403",
        no_challenge.status == 403, f"HTTP {no_challenge.status}",
    )

    # -- Firma ----------------------------------------------------------
    stranger = _unauthorized_number(dep)
    probe = text_payload("hola", wamid=f"wamid.smoke.probe.{secrets.token_hex(4)}",
                         from_number=stranger)

    unsigned = dep.post_webhook(probe, sign=False)
    rep.check(
        "POST /webhook sin firma devuelve 403",
        unsigned.status == 403, f"HTTP {unsigned.status}",
    )

    forged = http(
        "POST", dep.webhook_url, data=json.dumps(probe).encode(),
        headers={"Content-Type": "application/json",
                 "X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    rep.check(
        "POST /webhook con firma inválida devuelve 403",
        forged.status == 403, f"HTTP {forged.status}",
    )

    tampered = dep.post_webhook(probe, tamper=True)
    rep.check(
        "POST /webhook con el cuerpo alterado después de firmar devuelve 403",
        tampered.status == 403, f"HTTP {tampered.status}",
    )

    # -- Whitelist ------------------------------------------------------
    signed = dep.post_webhook(probe)
    rep.check(
        "POST /webhook firmado de un número NO autorizado devuelve 200",
        signed.status == 200,
        f"HTTP {signed.status} — Meta reintenta 7 días si no recibe 200",
    )
    rows = dep.bot_db(
        "SELECT count(*) FROM raw_messages r JOIN workers w ON w.id = r.worker_id "
        f"WHERE right(w.phone_number, 10) = right('{stranger}', 10)"
    )
    rep.check(
        "…y no dejó nada en raw_messages",
        rows[0][0] == "0",
        f"{rows[0][0]} filas para {stranger}",
    )


def _unauthorized_number(dep: Deployment) -> str:
    """Un número que garantizadamente no cae en la whitelist. La comparación
    real es por los últimos 10 dígitos (decisión #18), así que no basta con
    inventar un número: hay que verificarlo contra la tabla."""
    taken = {row[0] for row in dep.bot_db("SELECT right(phone_number, 10) FROM workers")}
    for _ in range(50):
        candidate = "52" + "".join(secrets.choice("0123456789") for _ in range(10))
        if candidate[-10:] not in taken:
            return candidate
    raise SmokeError("No se pudo generar un número fuera de la whitelist")


def phase_flow(dep: Deployment, rep: Report, worker: dict, args) -> None:
    section("2 · FLUJO COMPLETO — de webhook a ticket real")

    phone = worker["phone"]
    run_id = secrets.token_hex(4)
    wamid = lambda suffix: f"wamid.smoke.{run_id}.{suffix}"  # noqa: E731

    dep.clear_phone_state(phone)
    dep.sink_reset()
    print(f"  {Colors.DIM}trabajador: {worker['name']} ({phone}) · corrida {run_id}{Colors.END}")

    messages = [
        MARKER,
        "te reenvío lo del cliente: pide su constancia de situación fiscal actualizada",
        "dice que la necesita antes del viernes",
    ]

    # -- 1. Ingesta ------------------------------------------------------
    for i, text in enumerate(messages):
        response = dep.post_webhook(text_payload(text, wamid=wamid(i), from_number=phone))
        if response.status != 200:
            rep.check(f"Mensaje {i + 1} aceptado", False, f"HTTP {response.status}")
            return
        time.sleep(1)
    rep.check(f"Los {len(messages)} mensajes entraron por HTTPS con firma válida", True)

    # El reintento de Meta: mismo wamid, ya procesado. Se prueba aquí y no en
    # la fase 1 a propósito — hacerlo aparte abriría una segunda ventana de
    # debounce para este teléfono y las dos se estorbarían.
    repeat = dep.post_webhook(text_payload(messages[0], wamid=wamid(0), from_number=phone))
    rep.check("El reintento con el mismo wamid devuelve 200", repeat.status == 200)

    rows = dep.bot_db(
        f"SELECT count(*) FROM raw_messages WHERE wamid LIKE 'wamid.smoke.{run_id}.%'"
    )
    rep.check(
        "Quedaron exactamente 3 filas en raw_messages (el duplicado se descartó)",
        rows[0][0] == "3", f"{rows[0][0]} filas",
    )

    # -- 2. Cierre del buffer + DeepSeek ---------------------------------
    print(f"  {Colors.DIM}esperando el cierre de la ventana de debounce y a DeepSeek…{Colors.END}")
    asked = wait_for(
        lambda: _sink_text(dep, ASK_CLIENT_TEXT), timeout=args.buffer_timeout
    )
    if not rep.check(
        "El bot cerró el buffer y preguntó por el cliente", bool(asked),
        "" if asked else f"nada en el sink tras {args.buffer_timeout}s — revisa "
                         f"`docker logs {dep.bot}`",
    ):
        return

    state = dep.session_state(phone)
    title = (state or {}).get("title", "")
    rep.check("La sesión tiene un título extraído", bool(title), repr(title))
    rep.note(f"Título generado: {title!r}")
    if state and state.get("entities"):
        rep.note(f"Entidades: {state['entities']}")

    # -- 3. Cliente ------------------------------------------------------
    query = _client_query(dep)
    dep.post_webhook(text_payload(query, wamid=wamid("cliente"), from_number=phone))
    print(f"  {Colors.DIM}respondiendo cliente: {query!r}{Colors.END}")

    outcome = wait_for(
        lambda: _sink_text(dep, PICK_CLIENT_TEXT) or _sink_text(dep, ASK_PRIORITY_TEXT),
        timeout=args.step_timeout,
    )
    if not rep.check(
        "La búsqueda de clientes contra /internal respondió", bool(outcome),
        "" if outcome else "ni lista de clientes ni pregunta de prioridad",
    ):
        return

    picker = _sink_text(dep, PICK_CLIENT_TEXT)
    if picker:
        options = picker["options"]
        rep.check(
            "La lista de clientes trae 'Sin cliente' y no pasa de 10 opciones",
            any(o["id"] == NO_CLIENT_OPTION_ID for o in options) and len(options) <= 10,
            f"{len(options)} opciones · tipo {picker['interactive_type']}",
        )
        # Se escoge "Sin cliente" a propósito: un ticket de prueba no debe
        # colgarse del historial de un cliente real.
        dep.post_webhook(interactive_payload(
            NO_CLIENT_OPTION_ID, wamid=wamid("sincliente"), from_number=phone,
            reply_type="list_reply" if picker["interactive_type"] == "list" else "button_reply",
            title="Sin cliente",
        ))
    else:
        rep.note(f"La búsqueda de {query!r} no dio coincidencias: se pasó directo a prioridad")

    # -- 4. Prioridad ----------------------------------------------------
    priority = wait_for(lambda: _sink_text(dep, ASK_PRIORITY_TEXT), timeout=args.step_timeout)
    if not rep.check("El bot preguntó la prioridad", bool(priority)):
        return
    rep.check(
        "Los botones de prioridad son alta/media/baja",
        [o["id"] for o in priority["options"]] == ["alta", "media", "baja"],
        str([o["id"] for o in priority["options"]]),
    )

    dep.post_webhook(interactive_payload(
        "baja", wamid=wamid("prioridad"), from_number=phone,
        reply_type="button_reply", title="Baja",
    ))

    # -- 5. Ticket -------------------------------------------------------
    print(f"  {Colors.DIM}esperando la creación del ticket…{Colors.END}")
    confirmation = wait_for(
        lambda: _sink_prefix(dep, TICKET_CONFIRMATION_PREFIX), timeout=args.step_timeout
    )
    if not rep.check(
        "El bot confirmó con el número de ticket", bool(confirmation),
        "" if confirmation else "el POST /internal/tickets no llegó a buen término",
    ):
        _report_failed_creation(dep, rep, worker)
        return

    rep.check(
        "La confirmación usa ticket_number, no el UUID",
        confirmation["body"].startswith(TICKET_CONFIRMATION_PREFIX),
        repr(confirmation["body"]),
    )
    number = "".join(c for c in confirmation["body"] if c.isdigit())

    rep.check(
        "La sesión de Redis se limpió después de crear el ticket",
        dep.session_state(phone) is None,
    )

    # -- 6. Bitácora local ------------------------------------------------
    log = dep.bot_db(
        "SELECT status, priority, client_id, ticket_number, external_ticket_id, title "
        f"FROM ticket_creations WHERE ticket_number = {number}"
    )
    if not rep.check("Se registró el intento en ticket_creations", bool(log)):
        return
    status, log_priority, client_id, log_number, external_id, log_title = log[0]
    rep.check("…con status=created", status == "created", status)
    rep.check("…con la prioridad que se eligió", log_priority == "baja", log_priority)
    rep.check("…sin cliente asignado", client_id == "", client_id or "(vacío)")

    linked = dep.bot_db(
        "SELECT count(*) FROM raw_messages r JOIN ticket_creations t "
        f"ON t.id = r.ticket_creation_id WHERE t.ticket_number = {number}"
    )
    rep.check(
        "Los 3 raw_messages del bloque quedaron vinculados al intento",
        linked[0][0] == "3", f"{linked[0][0]} vinculados",
    )

    # -- 7. El otro lado --------------------------------------------------
    # La descripción es multilínea (bloque de entidades + mensajes crudos) y
    # `psql -tA` la escupe con sus saltos de línea, que romperían el parseo
    # por filas de `Deployment.psql`. Se aplanan aquí, no allá: es la única
    # columna de este script que los trae.
    ticket = dep.tickets_db(
        "SELECT title, priority, status, created_by::text, client_id::text, "
        "replace(replace(description, chr(10), ' ⏎ '), chr(13), '') "
        f"FROM tickets WHERE ticket_number = {number}"
    )
    if not rep.check(
        "El ticket existe de verdad en la base del sistema de tickets", bool(ticket),
        f"#{number}",
    ):
        return
    t_title, t_priority, t_status, t_created_by, t_client, t_description = ticket[0]

    rep.check("El título del ticket es el que mandó el bot", t_title == log_title, t_title)
    rep.check("La prioridad llegó como 'baja'", t_priority == "baja", t_priority)
    rep.check(
        "created_by es el trabajador real, no un actor inventado",
        t_created_by == worker["external_user_id"], t_created_by,
    )
    rep.check("El ticket quedó sin cliente", t_client == "", t_client or "(null)")
    rep.check(
        "La descripción trae el texto crudo del trabajador",
        all(m in t_description for m in messages),
        f"{len(t_description)} caracteres",
    )

    # El bot nunca manda department_id: lo deriva el sistema de tickets a
    # partir de created_by (decisión #8). Esto verifica que esa derivación
    # ocurrió del otro lado.
    departments = dep.tickets_db(
        "SELECT count(*) FROM ticket_departments td JOIN tickets t ON t.id = td.ticket_id "
        f"WHERE t.ticket_number = {number}"
    )
    rep.check(
        "El sistema de tickets derivó el departamento por su cuenta",
        departments and int(departments[0][0]) >= 1,
        f"{departments[0][0]} departamento(s)" if departments else "0",
    )

    rep.note(f"Ticket real creado: #{number} ({t_status}, prioridad {t_priority}, sin cliente)")
    rep.note(f"raw_messages / ticket_creations: wamid.smoke.{run_id}.*")
    rep.note(f"UUID del ticket del otro lado: {external_id}")


def _report_failed_creation(dep: Deployment, rep: Report, worker: dict) -> None:
    """La bitácora registra también los intentos fallidos (decisión #20) — si
    el ticket no salió, el error está ahí y es lo más útil que se puede
    imprimir."""
    failed = dep.bot_db(
        # Un traceback de httpx trae saltos de línea — mismo aplanado que la
        # descripción del ticket, por la misma razón.
        "SELECT replace(replace(error, chr(10), ' ⏎ '), chr(13), ''), created_at "
        "FROM ticket_creations "
        f"WHERE worker_id = '{worker['worker_id']}' AND status = 'failed' "
        "ORDER BY created_at DESC LIMIT 1"
    )
    if failed:
        rep.note(f"Último fallo registrado en ticket_creations: {failed[0][0]}")


def _sink_healthy(dep: Deployment) -> bool:
    """El sink recién levantado rechaza los primeros requests; aquí un fallo de
    conexión es "todavía no", no un error."""
    try:
        return http("GET", f"{dep.sink_url}/_health").status == 200
    except SmokeError:
        return False


def _sink_text(dep: Deployment, body: str) -> dict | None:
    """Último mensaje saliente cuyo texto visible sea exactamente `body`."""
    for entry in reversed(dep.sink_sent()):
        if entry.get("body") == body:
            return entry
    return None


def _sink_prefix(dep: Deployment, prefix: str) -> dict | None:
    for entry in reversed(dep.sink_sent()):
        if (entry.get("body") or "").startswith(prefix):
            return entry
    return None


def _client_query(dep: Deployment) -> str:
    """Primera palabra de un cliente que existe de verdad. Buscar una palabra
    cualquiera dejaría la rama de "escoger cliente" sin ejercitar y la prueba
    pasaría cubriendo menos de lo que parece."""
    rows = dep.tickets_db("SELECT name FROM clients ORDER BY created_at LIMIT 1")
    return rows[0][0].split()[0] if rows and rows[0][0] else "Interno"


# ===========================================================================
# main
# ===========================================================================

def cmd_run(dep: Deployment, args) -> int:
    rep = Report()
    print(f"{Colors.BOLD}Prueba de humo del despliegue{Colors.END}")
    print(f"  webhook: {dep.webhook_url}")

    worker = preflight(dep, rep)
    phase_connectivity(dep, rep, worker)
    if not args.skip_flow:
        phase_flow(dep, rep, worker, args)
    else:
        print(f"\n{Colors.WARN}Fase de flujo omitida (--skip-flow){Colors.END}")

    return rep.summary()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command", choices=["setup", "run", "teardown", "all"],
        help="all = setup + run + teardown",
    )
    parser.add_argument("--bot-dir", default="~/whatsapp-accounting-assistant")
    parser.add_argument("--ops-dir", default="~/cgho-ops")
    parser.add_argument(
        "--base-url", default=None,
        help="por defecto https://$CADDY_HOSTNAME del .env de cgho-ops",
    )
    parser.add_argument(
        "--prefix", default="bot", help="prefijo de ruta del proxy hacia el bot",
    )
    parser.add_argument("--sink-name", default="bot-meta-sink")
    parser.add_argument("--sink-port", type=int, default=8099)
    parser.add_argument(
        "--buffer-timeout", type=int, default=150,
        help="espera máxima al cierre del debounce + DeepSeek",
    )
    parser.add_argument(
        "--step-timeout", type=int, default=90,
        help="espera máxima por cada respuesta del bot",
    )
    parser.add_argument(
        "--skip-flow", action="store_true",
        help="solo conectividad: no llama a DeepSeek ni crea tickets",
    )
    parser.add_argument(
        "--keep", action="store_true",
        help="con `all`, deja el sink y el .env repuntado al terminar",
    )
    args = parser.parse_args()

    try:
        dep = Deployment(args)
        if args.command == "setup":
            return cmd_setup(dep, args)
        if args.command == "teardown":
            return cmd_teardown(dep, args)
        if args.command == "run":
            return cmd_run(dep, args)

        cmd_setup(dep, args)
        try:
            return cmd_run(dep, args)
        finally:
            if args.keep:
                print(f"\n{Colors.WARN}--keep: el sink sigue arriba y el .env "
                      f"sigue repuntado. `teardown` lo revierte.{Colors.END}")
            else:
                cmd_teardown(dep, args)
    except SmokeError as exc:
        print(f"\n{Colors.FAIL}ERROR:{Colors.END} {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"\n{Colors.WARN}Interrumpido. Estado del sink y del .env sin "
              f"revertir — corre `teardown`.{Colors.END}", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
