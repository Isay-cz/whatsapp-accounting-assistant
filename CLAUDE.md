# CLAUDE.md — Bot de WhatsApp (CGHO Contadores)

Bot que recibe mensajes de clientes reenviados por trabajadores del despacho vía WhatsApp, extrae un título con un LLM, confirma cliente y prioridad, y crea el ticket en **CGHO Sistema de Tickets** — el sistema interno de gestión (repo separado). Este bot **no gestiona tickets, clientes ni departamentos**: esos datos y esa lógica viven exclusivamente en el sistema de tickets. Este repo es una capa delgada de captura + extracción que termina en una llamada a la API de ese sistema.

**Repo hermano:** `CGHO Sistema de Tickets`. Consultar su `CLAUDE.md` y `DB-SCHEMA.md` para todo lo relacionado a `tickets`, `clients`, `departments`, `users`, `priority`. No se duplica esa información aquí — si algo de esto cambia del otro lado, este archivo se actualiza para reflejarlo, no se asume.

## Qué hace y qué no hace

**Hace:** recibe webhooks de WhatsApp Cloud API, valida whitelist de trabajadores, agrupa mensajes en bloques (buffer/debounce), extrae un título vía LLM, pregunta cliente y prioridad por WhatsApp, crea el ticket vía API del sistema de tickets, confirma al trabajador.

**No hace** (vive en el sistema de tickets): gestión de tickets, dashboard ejecutivo, base de clientes, historial entre departamentos. La interfaz de gestión HTMX y el dashboard Plotly Dash del plan original de este bot **se eliminaron** — quedaron redundantes con las vistas Operación/Dirección del sistema de tickets.

## Stack

- **Backend:** FastAPI (async) + SQLAlchemy 2.x + Alembic
- **DB:** PostgreSQL — **mismo servidor/proceso** que el sistema de tickets, **base de datos separada** (`bot_db`) con rol propio de mínimo privilegio, sin permisos sobre las tablas del otro sistema.
- **Cache/buffer:** Redis — mismo proceso que el sistema de tickets, namespaced por prefijo de llave (`bot:*`). Usado para el buffer de mensajes (debounce 30-60s) y los timeouts de confirmación (cliente, prioridad) — mismo mecanismo de TTL para ambos casos.
- **Mensajería:** WhatsApp Cloud API directa (Meta), **no Twilio** — decisión explícita para evitar el cargo adicional de Twilio, asumiendo el trabajo extra de configuración propia.
- **LLM:** DeepSeek, modelo `deepseek-v4-flash` (el alias `deepseek-chat` se retiró el 24 de julio de 2026 — no usar ese nombre). Posible migración a otro proveedor después; la extracción debe quedar detrás de una sola interfaz/módulo para que cambiar de proveedor no toque el resto del código.
- **Deploy:** Docker Compose, **stack independiente** del sistema de tickets — ciclo de deploy propio, porque el bot itera más seguido (prompts, proveedor LLM) y no debe requerir tocar el stack de producción del sistema de tickets. Se conecta a Postgres/Redis compartidos vía red Docker externa (`cgho_net`); **no define esos servicios en su propio `docker-compose.yml`.**

## Convenciones

- **Idioma:** código, tablas, columnas, endpoints y comentarios en inglés. Mensajes al trabajador (WhatsApp) en español.
- **PKs:** UUID en toda tabla.
- **Timestamps:** `created_at` en toda tabla.
- **Migraciones:** todo cambio de esquema pasa por Alembic.
- **Config:** variables de entorno inyectadas directo por Docker Compose (`environment:` en `docker-compose.yml`) — **nunca** `env_file` dentro de `Settings` de pydantic. El `.env` vive solo en el host, para alimentar la interpolación `${VAR}` de `docker-compose.yml`; no se copia ni se monta dentro del contenedor (ver decisión #12).
- **Plan antes de código** en toda feature no trivial.
- **Parsing siempre defensivo** en el webhook (`.get()`, nunca acceso directo por llave/índice) — hay campos de payloads reales de Meta que no están en la documentación oficial.

## Decisiones de arquitectura ya tomadas (no reabrir sin discusión explícita)

1. **Nunca acceso directo a las tablas del sistema de tickets, ni viceversa.** Toda comunicación es vía API HTTP interna, autenticada con un token compartido (`INTERNAL_API_TOKEN`, variable de entorno en ambos stacks, nunca en código ni en docs). Aunque Postgres corre en el mismo proceso físico, cada sistema tiene su propio rol de base de datos sin permisos sobre las tablas del otro. El uso de `INTERNAL_API_TOKEN` es **solo saliente**: este bot lo manda como `Authorization: Bearer` al llamar a la API del sistema de tickets; no expone ningún endpoint que el sistema de tickets necesite invocar de vuelta.
2. **`tickets`, `clients`, `departments` no se modelan en este repo.** Se eliminaron los modelos `Ticket`/`Client`/`Department` heredados de la primera versión (Twilio) — esos datos viven solo en el sistema de tickets.
3. **Postgres y Redis compartidos a nivel de proceso, no de datos.** Un solo servidor de cada uno corriendo en la VPS (ahorra recursos en el CX32 de 8GB), pero bases de datos/namespaces separados — nunca queries cruzados entre los dos sistemas.
4. **Cloud API de Meta, no Twilio.** Requiere cuenta de Meta Business Manager verificada, lo cual depende de tener un sitio web activo — engancha con la resolución del dominio `cghocontadores.mx`.
5. **El nombre del cliente nunca se extrae vía LLM.** Siempre se pregunta explícitamente al trabajador después de cerrada la ventana del buffer (no antes, no mezclado con la extracción del título) — la respuesta de texto libre se manda tal cual al endpoint de búsqueda de clientes del sistema de tickets. Si no contesta antes del timeout, el ticket se crea con `client_id = null`. Nunca se asigna un cliente adivinado — una asignación automática silenciosamente equivocada es peor que un ticket temporalmente sin cliente.
6. **El LLM se llama siempre, una vez por bloque de buffer.** No hay un filtro previo que decida si "vale la pena" llamarlo. El volumen y costo (estimado en menos de US$1/mes con `deepseek-v4-flash` al volumen del despacho) no justifican la complejidad de mantener un clasificador propio, y un filtro mal calibrado tiene más riesgo de error silencioso que llamar siempre al LLM.
7. **Fallback determinista si el LLM falla.** Si la llamada de extracción falla o tarda, el título cae a los primeros ~60 caracteres del mensaje crudo. La descripción (mensajes crudos concatenados, con o sin resumen de entidades antepuesto) no depende del LLM para existir. La creación del ticket nunca queda bloqueada por una falla del LLM — la función de extracción nunca lanza excepción hacia arriba, degrada internamente.
8. **`department_id` del ticket = departamento del trabajador que reenvía.** Nunca se infiere del contenido del mensaje. Si queda mal asignado, se corrige después con el mecanismo de traspaso entre departamentos que ya existe en el sistema de tickets — no es responsabilidad del bot acertar esto. Ver decisión #14 para cómo se resuelve mientras no exista el sync trabajador↔departamento.
9. **`priority` siempre se confirma por botones (alta/media/baja) con timeout de 1 minuto.** Si no contesta, se manda "media" por default — nunca se deja sin valor, porque la columna es `NOT NULL` del otro lado.
10. **Deduplicación por `wamid`.** Meta reintenta entregas fallidas hasta 7 días con frecuencia decreciente — todo mensaje se verifica contra `raw_messages` antes de procesar.
11. **Responder 200 rápido, siempre.** El handler del webhook nunca hace trabajo pesado de forma síncrona (extracción LLM, llamadas al sistema de tickets) — se delega a background task.
12. **Config de un solo mecanismo.** La versión anterior (Twilio) tenía tres formas distintas de cargar `.env` que no se comunicaban entre sí (interpolación de Compose, `env_file` de pydantic inalcanzable dentro del contenedor, y un parche manual en `alembic/env.py`) — de ahí que hoy sea una sola fuente de verdad, sin excepciones.
13. **Sin interfaz de gestión de tickets ni dashboard ejecutivo en este repo.** Quedaron redundantes con las vistas Operación/Dirección del sistema de tickets y se eliminaron del roadmap original.
14. **Resolución temporal de `created_by` y `department_id`.** Mientras no exista el sync `users.bot_enabled` + `whatsapp_phone` del lado del sistema de tickets, `Worker` guarda dos referencias externas pobladas a mano por un admin: `external_user_id` (el `users.id` del sistema de tickets correspondiente al trabajador, usado como `created_by` al crear el ticket) y `external_department_id` (el `departments.id` correspondiente, usado para asociar el ticket al departamento de quien reenvía). Son columnas UUID nullable **sin FK real** — referencian filas que viven en la base del otro sistema. Este es un stopgap explícito, no la solución final; ver Pendientes.

## Estructura del repositorio (objetivo)

```
whatsapp-bot/
├── api/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── routes/
│   │   └── webhook.py          # dispatcher Cloud API — GET (verify) + POST /webhook
│   ├── models/
│   │   ├── orm.py               # Worker, RawMessage — únicamente
│   │   └── schemas.py           # Pydantic: extracción, replies interactivos
│   ├── services/
│   │   ├── meta/                 # cliente Graph API: texto, listas, botones
│   │   ├── llm/                  # extracción — interfaz única, proveedor intercambiable
│   │   ├── buffer/               # debounce genérico sobre Redis (TTL por llave)
│   │   ├── conversation/         # orquesta el flujo post-buffer (cliente, prioridad, creación)
│   │   ├── ticket_system/        # cliente HTTP hacia la API del sistema de tickets
│   │   └── alerts/               # notificador de eventos de alerta de Meta (stub de log por ahora)
│   └── core/
│       └── security.py          # firma X-Hub-Signature-256 + header de token interno saliente
├── db/
│   └── migrations/
├── docker-compose.yml            # solo `api` — NO define db/redis, se une a `cgho_net` externa
├── docker-compose.test.yml       # postgres/redis desechables, solo para correr pruebas
├── .env.example
├── docs/
│   ├── whatsapp-webhook-reference.md
│   └── bot-diseno-flujo-cliente.md
└── CLAUDE.md
```

## Flujo end-to-end (v1)

1. Webhook recibe mensaje → valida firma de Meta → valida whitelist (`workers`) → guarda `raw_message` (idempotente por `wamid`) → responde 200.
2. Buffer en Redis (TTL 30-60s por trabajador) agrupa mensajes consecutivos del mismo remitente.
3. Al cerrar la ventana: una llamada a `deepseek-v4-flash` genera el título (con fallback determinista si falla).
4. Se pregunta "¿Cliente?" (texto libre) → búsqueda vía API del sistema de tickets → 0/1/N resultados → lista interactiva si hay ambigüedad → timeout → `client_id = null` si no contesta.
5. Se pregunta prioridad (botones alta/media/baja) → timeout 1 min → default "media".
6. Llamada a la API de creación de tickets del sistema de tickets: `title`, `description`, `client_id`, `priority`, `created_by` (`Worker.external_user_id`, nunca "Sistema"). Si `Worker.external_department_id` está poblado, llamada de seguimiento para asociar el departamento.
7. Confirmación al trabajador con el número de ticket.

## Pendientes que bloquean piezas específicas

| Pendiente | Dónde se resuelve | Qué bloquea |
|---|---|---|
| Endpoint de búsqueda de clientes | Sistema de tickets | Paso 4 del flujo |
| Endpoint de creación de tickets para servicio externo autenticado | Sistema de tickets | Paso 6 del flujo |
| Campo `bot_enabled` + `whatsapp_phone` en `users`, pantalla de administración y mecanismo de sync (async vía `arq` + botón de resync manual) | Sistema de tickets | Sincronización de `workers` — mientras tanto, altas/bajas manuales |
| `Worker.external_user_id` es un stopgap manual (cargado a mano por un admin) | Este repo, temporal | `created_by` al crear tickets — reemplazar por resolución dinámica por teléfono en cuanto exista el sync de arriba |
| `Worker.external_department_id` es un stopgap manual (cargado a mano por un admin) | Este repo, temporal | Asociación de departamento al crear tickets — mismo reemplazo que `external_user_id` |
| Contrato real de cómo el sistema de tickets asocia un departamento a un ticket recién creado (¿mismo POST de creación, o llamada aparte a `ticket_departments`?) no está definido del otro lado | Sistema de tickets | La llamada `ticket_system.add_department(...)` de este repo es un diseño provisional, no verificado contra un endpoint real |
| Red Docker compartida `cgho_net` debe declararse `external: true` en `cgho-ops/docker-compose.yml` | Sistema de tickets (infra) | `docker compose up` de este repo no puede unirse a la red hasta que exista del otro lado |
| Verificación de Meta Business Manager (requiere sitio web activo) | Dominio `cghocontadores.mx` | Alta del número de WhatsApp Cloud API |
| CHECK constraint de `priority` (vocabulario ya confirmado: alta/media/baja, default media) | Sistema de tickets | No bloquea — el bot puede empezar a mandar valores válidos desde ya |
| Proveedor de canal para eventos de alerta de Meta (Telegram vs. correo, no decidido) | Este repo | `services/alerts/` hoy solo loguea; no hay integración real de notificación |

## Fases

```
Fase 0 — Restructuración: eliminar Ticket/Client/Department locales, migrar webhook y
          cliente de salida de Twilio a Cloud API, arreglar manejo de config/.env,
          separar docker-compose sin duplicar db/redis
Fase 1 — Webhook Cloud API + whitelist + raw_messages (equivalente a la Fase 1 anterior,
          reescrita para el nuevo proveedor)
Fase 2 — Buffer en Redis + extracción LLM (título, fallback determinista)
Fase 3 — Flujo conversacional: confirmación de cliente + prioridad + creación de ticket
          vía API del sistema de tickets
Fase 4 — Manejo de eventos de alerta de Meta (account_update, phone_number_quality_update,
          security) a canal separado; eventos informativos solo a log
```

**v1.5 (no antes):** resumen de entidades antepuesto a la descripción (monto, fecha, RFC mencionados), posible sugerencia de `recur_template_id` cuando esa función exista del lado del sistema de tickets — siempre como sugerencia a confirmar por el trabajador, nunca asignación automática sin confirmación.

## Fuera de alcance (v1 del bot)

Interfaz de gestión de tickets, dashboard ejecutivo, chat interno (no aplica, nunca se planteó aquí), asignación automática de prioridad o cliente sin confirmación del trabajador.

## Notas históricas

La primera versión de este repo (abril 2026, un solo commit) usaba Twilio WhatsApp Sandbox y modelaba tickets/clientes/departamentos localmente. Se abandonó ese enfoque por dos razones: (1) el cambio a Cloud API directa evita el cargo adicional de Twilio; (2) modelar tickets en este repo duplicaba por completo el sistema de tickets ya construido, con el riesgo de que ambos modelos divergieran con el tiempo. El manejo de variables de entorno de esa versión tenía tres mecanismos redundantes que no se comunicaban entre sí — de ahí la decisión #12 de arriba.
