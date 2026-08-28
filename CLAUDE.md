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
- **Exposición pública:** Cloudflare Tunnel (`cloudflared`) como único punto de entrada al webhook — `api` no publica ningún puerto al host. Configuración local (`cloudflared/config.yml`, versionada) en vez de reglas de ingreso administradas solo desde el dashboard de Cloudflare, para mantener las decisiones de ruteo dentro del repo. `cloudflared` vive en la red interna del stack del bot — no necesita `cgho_net`, solo reenvía hacia `api`.

## Convenciones

- **Idioma:** código, tablas, columnas, endpoints y comentarios en inglés. Mensajes al trabajador (WhatsApp) en español.
- **PKs:** UUID en toda tabla.
- **Timestamps:** `created_at` en toda tabla.
- **Migraciones:** todo cambio de esquema pasa por Alembic.
- **Config:** variables de entorno inyectadas directo por Docker Compose (`environment:` en `docker-compose.yml`) — **nunca** `env_file` dentro de `Settings` de pydantic. El `.env` vive solo en el host, para alimentar la interpolación `${VAR}` de `docker-compose.yml`; no se copia ni se monta dentro del contenedor (ver decisión #12).
- **Plan antes de código** en toda feature no trivial.
- **Parsing siempre defensivo** en el webhook (`.get()`, nunca acceso directo por llave/índice) — hay campos de payloads reales de Meta que no están en la documentación oficial.

## Decisiones de arquitectura ya tomadas (no reabrir sin discusión explícita)

1. **Nunca acceso directo a las tablas del sistema de tickets, ni viceversa.** Toda comunicación es vía API HTTP interna, autenticada con un token compartido (`INTERNAL_API_TOKEN`, variable de entorno en ambos stacks, nunca en código ni en docs). Aunque Postgres corre en el mismo proceso físico, cada sistema tiene su propio rol de base de datos sin permisos sobre las tablas del otro. El uso de `INTERNAL_API_TOKEN` es **solo saliente**: este bot lo manda como `Authorization: Bearer` al llamar a la API del sistema de tickets; no expone ningún endpoint que el sistema de tickets necesite invocar de vuelta (ver decisión #15 — es la razón por la que la sync de whitelist es *pull*, no *push*).
2. **`tickets`, `clients`, `departments` no se modelan en este repo.** Se eliminaron los modelos `Ticket`/`Client`/`Department` heredados de la primera versión (Twilio) — esos datos viven solo en el sistema de tickets.
3. **Postgres y Redis compartidos a nivel de proceso, no de datos.** Un solo servidor de cada uno corriendo en la VPS (ahorra recursos en el CX32 de 8GB), pero bases de datos/namespaces separados — nunca queries cruzados entre los dos sistemas.
4. **Cloud API de Meta, no Twilio.** Requiere cuenta de Meta Business Manager verificada, lo cual depende de tener un sitio web activo — engancha con la resolución del dominio `cghocontadores.mx`.
5. **El nombre del cliente nunca se extrae vía LLM.** Siempre se pregunta explícitamente al trabajador después de cerrada la ventana del buffer (no antes, no mezclado con la extracción del título) — la respuesta de texto libre se manda tal cual al endpoint de búsqueda de clientes del sistema de tickets, que regresa máximo 9 coincidencias. El bot arma la lista interactiva con esas coincidencias más una opción fija **"Sin cliente"** (10 elementos en total, el máximo de WhatsApp) — así el trabajador no depende del timeout cuando ya sabe que es trabajo interno o no hay cliente claro. Si aun así no contesta antes del timeout, el ticket se crea igual con `client_id = null`. Nunca se asigna un cliente adivinado — una asignación automática silenciosamente equivocada es peor que un ticket temporalmente sin cliente.
6. **El LLM se llama siempre, una vez por bloque de buffer.** No hay un filtro previo que decida si "vale la pena" llamarlo. El volumen y costo (estimado en menos de US$1/mes con `deepseek-v4-flash` al volumen del despacho) no justifican la complejidad de mantener un clasificador propio, y un filtro mal calibrado tiene más riesgo de error silencioso que llamar siempre al LLM.
7. **Fallback determinista si el LLM falla.** Si la llamada de extracción falla o tarda, el título cae a los primeros ~60 caracteres del mensaje crudo. La descripción (mensajes crudos concatenados, con el resumen de entidades antepuesto cuando lo hay — decisión #19) no depende del LLM para existir. La creación del ticket nunca queda bloqueada por una falla del LLM — la función de extracción nunca lanza excepción hacia arriba, degrada internamente.
8. **`department_id` del ticket = departamento del trabajador que reenvía — pero el bot nunca lo maneja.** Se deriva enteramente server-side en el sistema de tickets a partir de `created_by`. El bot no manda `department_id`, no lo guarda localmente, no hace ninguna llamada de seguimiento para asociarlo — es responsabilidad exclusiva del otro sistema. Si queda mal asignado, se corrige después con el mecanismo de traspaso entre departamentos que ya existe ahí.
9. **`priority` siempre se confirma por botones (alta/media/baja) con timeout de 1 minuto.** Si no contesta, se manda "media" por default — nunca se deja sin valor, porque la columna es `NOT NULL` del otro lado.
10. **Deduplicación por `wamid`.** Meta reintenta entregas fallidas hasta 7 días con frecuencia decreciente — todo mensaje se verifica contra `raw_messages` antes de procesar.
11. **Responder 200 rápido, siempre.** El handler del webhook nunca hace trabajo pesado de forma síncrona (extracción LLM, llamadas al sistema de tickets) — se delega a background task.
12. **Config de un solo mecanismo.** La versión anterior (Twilio) tenía tres formas distintas de cargar `.env` que no se comunicaban entre sí (interpolación de Compose, `env_file` de pydantic inalcanzable dentro del contenedor, y un parche manual en `alembic/env.py`) — de ahí que hoy sea una sola fuente de verdad, sin excepciones.
13. **Sin interfaz de gestión de tickets ni dashboard ejecutivo en este repo.** Quedaron redundantes con las vistas Operación/Dirección del sistema de tickets y se eliminaron del roadmap original.
14. **`created_by` sale de `Worker.external_user_id`.** `Worker` guarda `external_user_id` (el `users.id` del sistema de tickets correspondiente al trabajador), que se manda como `created_by` al crear el ticket. Es una columna UUID nullable **sin FK real** — referencia una fila que vive en la base del otro sistema. Ya **no** se captura a mano: la llena el poll de la whitelist (decisión #15) a partir de `user_id` del roster. Si un `Worker` llega sin `external_user_id`, el flujo aborta la creación y le pide al trabajador que avise a un administrador — nunca se inventa un actor.
15. **La sincronización de la whitelist es *pull*, no *push*.** El bot hace poll periódico (intervalo configurable, default ~5 min) contra `GET /internal/workers` en el sistema de tickets, y hace upsert local sobre `workers` con lo que regresa — nunca al revés. Es consecuencia directa de la decisión #1: el bot no expone ningún endpoint entrante más allá del webhook de Meta. La ventana de hasta un intervalo de retraso es aceptable dado el volumen (~22 personas, altas/bajas poco frecuentes). El poll es autocurativo por diseño — si se pierde un ciclo, el siguiente lo corrige, sin necesidad de reintentos ni botón de resync manual del otro lado. **Implementado** en `services/ticket_system/sync.py`, arrancado como task del `lifespan` en `main.py`; intervalo en `WORKER_SYNC_INTERVAL_SECONDS` (default 300s). Cada ciclo reconcilia la tabla completa: da de alta a los nuevos, recalcula `is_active` como `bot_enabled AND is_active`, y desactiva a los que ya no aparecen en el roster (a alguien le borraron el número del otro lado). Se desactivan en vez de borrarse porque `raw_messages` los referencia por FK. El loop nunca muere por un error: una caída de red se loguea y el siguiente ciclo reintenta.
16. **La confirmación al trabajador usa `ticket_number` (entero secuencial), no el `id` UUID.** El sistema de tickets lo regresa en la respuesta de `POST /internal/tickets` específicamente para esto — un UUID es incómodo de leer o repetir por WhatsApp.
17. **Exposición pública del webhook vía Cloudflare Tunnel, no puerto publicado directo en el host.** `cloudflared` corre como contenedor adicional en el mismo `docker-compose.yml`, con configuración local (`cloudflared/config.yml`) apuntando solo hacia `api` — no toca `cgho_net`. Requiere un hostname con DNS gestionado por Cloudflare para el túnel nombrado de producción; mientras se resuelve `cghocontadores.mx`, un *quick tunnel* sin dominio sirve para seguir probando, como ya se hizo antes. **Excepción de staging:** en una VM de prueba sin dominio en Cloudflare, la exposición es un `handle /bot/*` en el Caddy del stack de cgho-ops hacia `bot-api:8000` — aprovecha el certificado que Caddy ya administra ahí en vez de publicar un puerto sin TLS. Sigue sin haber `ports:` en el compose del bot. `bot-api` es el alias del bot en `cgho_net`, espejo de `tickets-api`: los dos composes nombran `api` a su servicio, así que el nombre pelado resuelve a cualquiera de los dos contenedores.
18. **Los teléfonos se guardan y se comparan normalizados a dígitos.** El sistema de tickets guarda E.164 con `+` y Meta manda el número sin él, así que `workers.phone_number` guarda solo dígitos y el sync normaliza al hacer el upsert. Además, la búsqueda de whitelist compara **los últimos 10 dígitos** y no la cadena completa: los celulares mexicanos llegan como `52XXXXXXXXXX` o `521XXXXXXXXXX` según el contexto, y comparar todo produciría falsos negativos con los que el bot ignoraría a un trabajador legítimo. Vale para un despacho 100% mexicano; **confirmar contra números reales** cuando el número de Cloud API esté verificado en producción. Ver `services/ticket_system/sync.py`.

19. **Las entidades que extrae el LLM se copian, no se infieren.** Además del título, la misma llamada devuelve `monto`, `fecha`, `rfc`, `periodo` y `tramite`, que se anteponen a la descripción como un bloque separado — el texto del cliente va íntegro y sin tocar debajo. El seguro es la **regla verbatim**: los cuatro primeros solo sobreviven si la cadena aparece literalmente en el bloque de mensajes (`services/llm/entities.py`); lo que no, se descarta con un log. La razón no es el costo (son ~100 tokens más sobre una llamada que ya ocurre siempre): la descripción se escribe **una sola vez** en un sistema que este bot no puede corregir después, así que un RFC alucinado se ve autoritativo y alguien lo copia a un trámite real. `tramite` es el único campo inferido y por eso su defensa es distinta: lista cerrada (`Tramite` en `models/schemas.py`), y lo que no caiga exacto se descarta. **No hay campo de cliente ni de prioridad** — eso lo sigue confirmando el trabajador (decisiones #5 y #9). La degradación es parcial a propósito: si el `title` viene bien pero las entidades vienen malformadas, se pierden las entidades y el título sobrevive.
20. **`ticket_creations` es una bitácora de salida, no un espejo del ticket.** Registra lo que este bot *mandó crear* (título, entidades, prioridad, cliente) más lo que devolvió el sistema de tickets (`ticket_number` y UUID), y **también los intentos fallidos** (`status='failed'` con el error) — que de otro modo no dejan rastro en ningún lado. Si allá renombran o reasignan el ticket, esta tabla no se entera: es auditoría del momento T, no caché, y por eso no contradice la decisión #2. No guarda la descripción: se reconstruye desde los `raw_messages` vinculados más `title` y `entities`, que es lo único de ella que no sale del texto crudo. El vínculo es una FK real local (`raw_messages.ticket_creation_id`, N mensajes → 1 intento), lo que obligó a que el buffer guarde `{wamid, body}` en vez de solo el texto — sin eso no hay forma de saber qué filas compusieron el bloque. `raw_messages.external_ticket_id` se retiró en la misma migración: nadie la escribía y el ticket ahora se alcanza por el join. El parseo del buffer es defensivo para la ventana de transición (bloques ya en Redis con el formato viejo). La bitácora nunca puede tumbar el flujo: si el ticket ya se creó, un error al registrarlo se loguea y se sigue — perder la bitácora es malo, crear el ticket dos veces es peor.
21. **La sesión no se borra hasta que el ticket existe.** Antes se limpiaba al entrar a la creación, así que un `POST /internal/tickets` fallido perdía el bloque de mensajes **y** dejaba al trabajador sin ninguna respuesta: no sabía si su ticket existía. Ahora la limpieza ocurre solo después de la confirmación; si falla, se registra en `ticket_creations`, se le avisa al trabajador y se le vuelven a mandar los botones de prioridad, de modo que un toque reintenta con el bloque intacto. No hay reintento automático: cada intento requiere una persona, así que un sistema de tickets caído no genera una tormenta de requests. La única excepción es el trabajador sin `external_user_id` (decisión #14), donde sí se cierra la sesión — reintentar no arreglaría nada hasta que cambie el roster del otro lado.

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
│   │   ├── orm.py               # Worker, RawMessage, TicketCreation — únicamente
│   │   └── schemas.py           # Pydantic: extracción, replies interactivos
│   ├── services/
│   │   ├── meta/                 # cliente Graph API: texto, listas, botones
│   │   ├── llm/                  # extracción — interfaz única, proveedor intercambiable.
│   │   │                         # entities.py: regla verbatim (decisión #19)
│   │   ├── buffer/               # debounce genérico sobre Redis (TTL por llave)
│   │   ├── conversation/         # orquesta el flujo post-buffer (cliente, prioridad, creación).
│   │   │                         # description.py: arma la descripción (entidades + crudo)
│   │   ├── ticket_system/        # client.py: cliente HTTP hacia /internal del sistema de
│   │   │                         # tickets. sync.py: poll de /internal/workers (decisión #15)
│   │   └── alerts/               # notificador de eventos de alerta de Meta (stub de log por ahora)
│   └── core/
│       └── security.py          # firma X-Hub-Signature-256 + header de token interno saliente
├── cloudflared/
│   └── config.yml                # ingress del túnel, versionado — credenciales fuera del repo
├── scripts/                      # prueba de humo del despliegue (solo stdlib, corre en la VM)
│   ├── smoke_test.py             # HTTPS público -> proxy -> contenedor real, flujo completo
│   └── meta_sink.py              # recibe la salida hacia Graph API en vez de mandarla a Meta
├── db/
│   └── migrations/
├── docker-compose.yml            # api + cloudflared — NO define db/redis, se une a `cgho_net` externa
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
3. Al cerrar la ventana: una llamada a `deepseek-v4-flash` genera el título **y las entidades** (con fallback determinista si falla — el fallback no trae entidades).
4. Se pregunta "¿Cliente?" (texto libre) → `GET /internal/clients/search` (máximo 9 resultados) → lista interactiva con las coincidencias + opción fija "Sin cliente" → timeout → `client_id = null` si no contesta. **Siempre se pregunta, incluso con una sola coincidencia**: escogerla automáticamente sería adivinar (decisión #5). Con cero coincidencias se pasa directo a prioridad — una lista de un solo elemento no aporta nada.
5. Se pregunta prioridad (botones alta/media/baja) → timeout 1 min → default "media".
6. `POST /internal/tickets` con `title`, `description` (entidades antepuestas al texto crudo, decisión #19), `client_id`, `priority`, `created_by` (`Worker.external_user_id`, nunca "Sistema"). El sistema de tickets deriva `department_id` por su cuenta — el bot no manda ni gestiona ese dato. Si el creador no tiene departamento (solo pasa con Dirección General), del otro lado cae en Gestión Operativa.
7. Se registra el intento en `ticket_creations` y se vinculan los `raw_messages` del bloque (decisión #20); recién entonces se limpia la sesión y se confirma al trabajador con `ticket_number` (ej. "Ticket #482 creado"), no el UUID. Si la creación falló, se registra igual, se le avisa y se le reofrecen los botones para reintentar (decisión #21).

Aparte del flujo por mensaje: un task del `lifespan` hace poll cada `WORKER_SYNC_INTERVAL_SECONDS` (default 300) contra `GET /internal/workers` del sistema de tickets para mantener `workers` sincronizado (decisión #15).

## Pendientes que bloquean piezas específicas

| Pendiente | Dónde se resuelve | Qué bloquea |
|---|---|---|
| ~~Endpoints `/internal` del sistema de tickets~~ | — | **Resuelto.** `GET /internal/clients/search`, `POST /internal/tickets` y `GET /internal/workers` ya existen y están probados de punta a punta |
| ~~Red Docker compartida `cgho_net`~~ | — | **Resuelto.** El diagnóstico original era incorrecto: no faltaba `external: true` del otro lado, faltaba `name: cgho_net` en el compose de cgho-ops, que es el dueño de la red y la crea. Aquí sigue siendo `external: true`. Consecuencia: cgho-ops se levanta primero |
| Formato real de los números de WhatsApp de los trabajadores | Producción | Confirmar la regla de match por últimos 10 dígitos (decisión #18) contra números reales cuando el número de Cloud API esté verificado |
| Verificación de Meta Business Manager (requiere sitio web activo) | Dominio `cghocontadores.mx` | Alta del número de WhatsApp Cloud API |
| NS de `cghocontadores.mx` (o un subdominio) apuntando a Cloudflare | Dominio `cghocontadores.mx` | Túnel nombrado estable para producción — mientras tanto, *quick tunnel* sin dominio |
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

**v1.5 (no antes):** posible sugerencia de `recur_template_id` cuando esa función exista del lado del sistema de tickets — siempre como sugerencia a confirmar por el trabajador, nunca asignación automática sin confirmación.

## Fuera de alcance (v1 del bot)

Interfaz de gestión de tickets, dashboard ejecutivo, chat interno (no aplica, nunca se planteó aquí), asignación automática de prioridad o cliente sin confirmación del trabajador.

## Notas históricas

La primera versión de este repo (abril 2026, un solo commit) usaba Twilio WhatsApp Sandbox y modelaba tickets/clientes/departamentos localmente. Se abandonó ese enfoque por dos razones: (1) el cambio a Cloud API directa evita el cargo adicional de Twilio; (2) modelar tickets en este repo duplicaba por completo el sistema de tickets ya construido, con el riesgo de que ambos modelos divergieran con el tiempo. El manejo de variables de entorno de esa versión tenía tres mecanismos redundantes que no se comunicaban entre sí — de ahí la decisión #12 de arriba.