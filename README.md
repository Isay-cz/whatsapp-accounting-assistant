# WhatsApp Accounting Assistant

Bot que recibe mensajes de clientes reenviados por trabajadores de un despacho contable vía WhatsApp, extrae un título con un LLM, confirma cliente y prioridad, y crea el ticket en **CGHO Sistema de Tickets** (repo hermano, gestión de tickets/clientes/departamentos). Este repo es una capa delgada de captura + extracción — no gestiona tickets ni tiene interfaz propia; ver `CLAUDE.md` para el detalle completo de alcance y decisiones de arquitectura.

---

## Contexto

Los trabajadores reenvían al bot los mensajes que reciben de clientes por WhatsApp. El bot agrupa esos mensajes en bloques (debounce), extrae un título con un LLM, confirma cliente y prioridad por WhatsApp, y crea el ticket vía la API del sistema de tickets. Objetivo estrecho y deliberado: **recibir → agrupar → extraer → confirmar → crear ticket**.

---

## Stack

| Capa | Tecnología |
|---|---|
| Mensajería | WhatsApp Cloud API (Meta) directa — no Twilio |
| Backend | FastAPI (async) + SQLAlchemy 2.x + Alembic |
| Base de datos | PostgreSQL — compartido a nivel de proceso con CGHO Sistema de Tickets, base de datos y rol propios |
| Buffer / sesiones | Redis — compartido a nivel de proceso, namespaced con prefijo `bot:` |
| LLM | DeepSeek (`deepseek-v4-flash`), detrás de una interfaz swappable |
| Deploy | Docker Compose, stack independiente del sistema de tickets |

---

## Arquitectura del sistema

```
WhatsApp (cliente)
      │
      ▼
Trabajador reenvía mensaje
      │
      ▼
Meta Cloud API ──► POST /webhook
                       │
                       ├─ Firma X-Hub-Signature-256
                       ├─ Whitelist (tabla workers)
                       ├─ Guardado raw_message (idempotente por wamid)
                       │
                       ▼
                Buffer en Redis (debounce 30-60s)
                       │
                       ▼
                Extracción de título (DeepSeek, con fallback determinista)
                       │
                       ├─► ¿Cliente? → búsqueda en sistema de tickets → desambiguación
                       ├─► ¿Prioridad? → botones alta/media/baja
                       │
                       ▼
                POST /tickets (sistema de tickets)
                       │
                       └─► Confirmación al trabajador por WhatsApp
```

No hay interfaz de gestión ni dashboard en este repo — viven en el sistema de tickets.

---

## Modelo de datos

Dos tablas únicamente. `Client`, `Department` y `Ticket` se eliminaron del modelo local (esos datos viven en el sistema de tickets).

```
workers ──► raw_messages
```

- `workers`: whitelist de trabajadores. Se mantiene sola: un task en segundo plano hace poll de `GET /internal/workers` del sistema de tickets cada `WORKER_SYNC_INTERVAL_SECONDS` y reconcilia la tabla completa (ver CLAUDE.md, decisión #15). `external_user_id` es una referencia externa (el UUID de `users` del sistema de tickets, sin FK real) que se manda como `created_by` al crear un ticket. `phone_number` se guarda normalizado a solo dígitos (decisión #18).
- `raw_messages`: payload crudo del webhook, idempotente por `wamid`. `external_ticket_id` referencia (sin FK) el ticket creado del otro lado.

---

## Estructura del repositorio

```
api/
├── routes/webhook.py          # dispatcher Cloud API — GET (verify) + POST /webhook
├── models/                    # orm.py (Worker, RawMessage), schemas.py
├── services/
│   ├── meta/                   # cliente Graph API: texto, listas, botones
│   ├── llm/                    # extracción — interfaz única, proveedor intercambiable
│   ├── buffer/                 # debounce genérico sobre Redis
│   ├── conversation/           # orquesta cliente/prioridad/creación de ticket
│   ├── ticket_system/          # cliente HTTP hacia la API del sistema de tickets
│   └── alerts/                 # notificador de eventos de alerta de Meta
├── core/security.py            # firma X-Hub-Signature-256 + token interno saliente
└── tests/                      # pytest — ver sección Pruebas
db/migrations/                  # Alembic
docker-compose.yml              # solo `api`, se une a la red externa `cgho_net`
docker-compose.test.yml         # postgres/redis desechables, solo para pytest
docs/
├── whatsapp-webhook-reference.md
└── bot-diseno-flujo-cliente.md
CLAUDE.md
```

---

## Setup local

### Prerrequisitos
- Docker y Docker Compose
- Python 3.12+
- Cuenta de Meta Business Manager con número de WhatsApp Cloud API configurado
- El stack de CGHO Sistema de Tickets levantado: es el dueño de la red Docker `cgho_net` y la crea al arrancar, además de proveer Postgres y Redis. Si no está arriba, `docker compose up` de este repo falla con `network cgho_net not found`.
- La base `bot_db` y el rol `bot_role` creados en ese Postgres (script `ops/bot-db-bootstrap.sql` del repo del sistema de tickets)

### 1. Variables de entorno

```bash
cp .env.example .env
# Editar .env con las credenciales reales (Meta, DeepSeek, INTERNAL_API_TOKEN)
```

### 2. Levantar el servicio

```bash
docker compose up
```

### 3. Correr migraciones

```bash
docker compose exec api alembic upgrade head
```

### 4. Agregar un worker de prueba

```bash
docker compose exec api python -c "
import asyncio
from database import AsyncSessionLocal
from models.orm import Worker

async def main():
    async with AsyncSessionLocal() as db:
        db.add(Worker(phone_number='+521XXXXXXXXXX', name='Tu nombre', is_active=True))
        await db.commit()

asyncio.run(main())
"
```

### 5. Registrar el webhook en Meta

En Meta for Developers → tu app → WhatsApp → Configuration, apunta el webhook a `https://tu-dominio/webhook` con el `META_VERIFY_TOKEN` configurado en `.env`.

---

## Pruebas

Requieren un Postgres y un Redis desechables (no el stack compartido de producción):

```bash
docker compose -f docker-compose.test.yml up -d
cd api
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql://bot_test:bot_test@localhost:55432/bot_test
export REDIS_URL=redis://localhost:56379/1
alembic upgrade head
pytest
```

Los clientes de `services/meta`, `services/ticket_system` y `services/llm` se prueban con mocks — nunca contra Graph API, el sistema de tickets real o DeepSeek. `services/buffer` se prueba contra Redis real (necesita TTL real, no tiene sentido simularlo). La conexión real contra el stack de `cgho-ops` queda fuera de esta rebanada — ver `CLAUDE.md`, Pendientes.

---

## Variables de entorno

Ver `.env.example` para la lista completa. Se inyectan por Docker Compose (`environment:`) — nunca `env_file` dentro de `Settings` de pydantic ni `load_dotenv()` en otro lado.

| Variable | Descripción |
|---|---|
| `DATABASE_URL` | Conexión a Postgres (base y rol propios del bot) |
| `REDIS_URL` | Conexión a Redis compartido (namespaced `bot:`) |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` | Extracción de título |
| `META_APP_SECRET` / `META_VERIFY_TOKEN` / `META_ACCESS_TOKEN` / `META_PHONE_NUMBER_ID` | WhatsApp Cloud API |
| `INTERNAL_API_TOKEN` | Token compartido con el sistema de tickets — solo saliente |
| `TICKET_SYSTEM_BASE_URL` | Base URL de la API del sistema de tickets. Es `http://tickets-api:8000`, **no** `http://api:8000`: este stack también nombra `api` a su propio servicio y ese nombre resuelve al contenedor local |
| `WORKER_SYNC_INTERVAL_SECONDS` | Cada cuánto se refresca la whitelist (default 300) |

---

## Fases

Ver `CLAUDE.md` para el detalle de fases (0–4), decisiones de arquitectura y pendientes que bloquean piezas específicas.

---

*Ver `CLAUDE.md` para contexto completo, decisiones ya tomadas y qué no reabrir sin discusión explícita.*
