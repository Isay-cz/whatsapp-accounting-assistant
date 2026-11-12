# PLAN — Auditoría de noviembre 2026: endurecimiento del bot y comprobantes por WhatsApp

Sale de la auditoría de los dos repos del 2026-11-10. Este documento trae las
órdenes de **este repo**. Las del sistema de tickets, el contrato completo de
`/internal` y el diseño de la bandeja de comprobantes están en el repo hermano:
`cgho-ops/docs/PLAN-AUDITORIA-2026-11.md`. **Si los dos documentos difieren en
el contrato, manda aquel.**

- [0. Cómo usar este plan](#0-cómo-usar-este-plan)
- [1. Endurecimiento (órdenes B1–B13)](#1-endurecimiento)
- [2. Comprobantes por WhatsApp (órdenes M1–M5)](#2-comprobantes-por-whatsapp)

---

## 0. Cómo usar este plan

Está escrito para ejecutarse **una orden por sesión**, también con un modelo
pequeño.

**Antes de empezar una orden**

1. Leer `CLAUDE.md` completo: las decisiones numeradas no se reabren.
2. Leer la orden completa, incluida su sección «No hacer».
3. **Buscar el código por el fragmento citado, no por el número de línea.** Los
   números son de la auditoría (commit `dd2c518`) y se mueven.
4. Si el código no coincide con lo que la orden describe, **parar y preguntar**.

**Cómo correr las pruebas** (README, sección Pruebas):

```bash
docker compose -f docker-compose.test.yml up -d
pip install -r api/requirements-dev.txt
export DATABASE_URL=postgresql://bot_test:bot_test@localhost:55432/bot_test
export REDIS_URL=redis://localhost:56379/1
alembic upgrade head        # desde donde esté alembic.ini; comprobarlo antes
cd api && pytest
```

**Al terminar una orden**

1. Pegar el resultado real de `pytest` en el resumen.
2. Marcar la orden en la tabla de abajo, en el mismo commit.
3. Una decisión nueva va al final de la lista numerada de `CLAUDE.md` (la
   siguiente libre al escribir este plan es la #22).
4. Commits pequeños, en español, con el estilo del repo (`fix: …`, `feat: …`,
   `ci: …`).

**Reglas que valen para todas las órdenes**

- No modelar tickets, clientes ni departamentos aquí (decisión #2).
- No acceder a las tablas del sistema de tickets: todo por `/internal`
  (decisión #1).
- El bot corre con **un solo proceso de uvicorn**. Los vigilantes del buffer y
  el poll de la whitelist viven en memoria: no agregar `--workers`.
- Lo marcado **Verificación humana** necesita el teléfono o el servidor: se deja
  anotado como pendiente, no se da por hecho. Desde el 2026-11-12 hay un número
  disponible para pruebas reales.

**Dificultad:** 🟢 acotada y con pruebas automáticas · 🟡 toca despliegue,
esquema o varios archivos · 🔴 requiere sesión supervisada.

| Orden | Qué | Dif. | Depende de | Estado |
|---|---|---|---|---|
| B1 | El arranque falla sin secretos; firma con secreto vacío siempre inválida | 🟢 | — | Pendiente |
| B2 | Títulos de botones y listas dentro del límite de WhatsApp | 🟢 | — | Pendiente |
| B3 | CI con pruebas, `:latest` solo por tag, imagen sin `--reload` | 🟡 | — | Pendiente |
| B4 | Defaults de configuración: versión de Graph API, `DEBUG`, Redis con usuario | 🟢 | pareja: O4 de cgho-ops | Pendiente |
| B5 | Llave de idempotencia y candado al crear el ticket | 🟡 | pareja: O3 de cgho-ops | Pendiente |
| B6 | Validar las respuestas interactivas contra el paso | 🟢 | — | Pendiente |
| B7 | Deduplicación atómica y mensajes que no se pierden en un reinicio | 🟡 | — | Pendiente |
| B8 | Ventana de reintento tras un fallo; una sesión no pisa a otra | 🟡 | B5 | Pendiente |
| B9 | `/health` que revisa Postgres y Redis; healthcheck en el compose | 🟢 | — | Pendiente |
| B10 | Canal de alertas de Meta | 🟢 | visto bueno del dueño | Pendiente |
| B11 | Documentación al día | 🟢 | — | Pendiente |
| B12 | Dependencias: Dependabot y foto del contrato | 🟡 | O8 de cgho-ops | Pendiente |
| B13 | Imagen sin root | 🟡 | B3 | Pendiente |
| M1 | Recibir medios: webhook, descarga de Meta y bitácora | 🟡 | B1, B7 | Pendiente |
| M2 | Subir el archivo y crear el borrador en el sistema de tickets | 🟡 | M1, F1 de cgho-ops | Pendiente |
| M3 | Conversación: cliente y tipo | 🟡 | M2 | Pendiente |
| M4 | Extracción con Claude Haiku | 🟡 | M2 | Pendiente |
| M5 | Prueba de humo con medios y con el teléfono real | 🟡 | M3, M4 | Pendiente |

Orden sugerido: B1, B2, B4, B6, B9, B11 (rápidas) → B3 → B5 → B7 → B8 → B10 →
M1…M5 → B12, B13.

---

## 1. Endurecimiento

### B1 — El arranque falla sin secretos 🟢

**Por qué.** `api/config.py` deja `meta_app_secret` en `""` por default.
`verify_meta_signature` calcula el HMAC con esa llave vacía, que cualquiera
puede reproducir: la firma del webhook se puede falsificar. Nada avisa.

**Cambios.**

1. `api/config.py`: un `model_validator(mode="after")` que lance `ValueError`
   con un mensaje claro si alguno de estos está vacío o es un marcador
   (`changeme`, `placeholder`, `xxx`): `meta_app_secret`, `meta_verify_token`,
   `meta_access_token`, `meta_phone_number_id`, `internal_api_token`,
   `ticket_system_base_url`. `deepseek_api_key` puede ir vacío (la extracción
   degrada al título de respaldo, decisión #7): solo un aviso en el log.
2. `api/core/security.py`, `verify_meta_signature`: devolver `False` si
   `app_secret` está vacío, antes de calcular nada.
3. `api/routes/webhook.py`, `verify_webhook`: comparar el token con
   `hmac.compare_digest` y rechazar si el token recibido está vacío.
4. `.env.example` y `docker-compose.prod.yml`: quitar el comentario que sugiere
   valores provisionales para `META_*`; decir que el arranque falla sin ellos.

**Pruebas.** En `tests/test_security.py`: secreto vacío → `False` aunque la
firma «coincida». Archivo nuevo `tests/test_config.py`: construir `Settings()`
(no `get_settings()`, que está en caché) con `META_APP_SECRET` vacío →
`ValueError`; con todo puesto → pasa. `conftest.py` ya define los valores de
prueba; revisar que ninguno caiga en la lista de marcadores.

**Hecho cuando.** La suite pasa y `docker compose up` con un `.env` sin
`META_APP_SECRET` muere con el mensaje nuevo.

### B2 — Títulos dentro del límite de WhatsApp 🟢

**Por qué.** `orchestrator.py` manda el nombre del cliente tal cual como título
(`{"id": r.id, "title": r.name}`). Según la documentación de Meta, el título de
un botón admite 20 caracteres y el de una fila de lista, 24. Con un nombre más
largo Meta rechaza el mensaje, la tarea muere y el trabajador no recibe nada.
`scripts/meta_sink.py` no valida longitudes, por eso no se ha visto.

**Cambios.**

1. `api/services/meta/client.py`: constantes `BUTTON_TITLE_MAX = 20`,
   `LIST_ROW_TITLE_MAX = 24`, `LIST_ROW_DESCRIPTION_MAX = 72`. `send_list`
   acepta `description` opcional por fila. `send_buttons` y `send_list` lanzan
   `ValueError` si un título pasa del límite: que falle en las pruebas y no en
   Meta.
2. `api/services/conversation/orchestrator.py`, `_handle_client_reply`: las
   coincidencias de cliente van **siempre** por lista. El título es el nombre
   recortado a 24 (con «…» si se recortó) y la descripción, el nombre completo
   recortado a 72. La prioridad sigue por botones.
3. Actualizar las pruebas que esperaban botones con pocas coincidencias
   (`test_full_happy_path_single_client_match` y vecinas en
   `tests/test_conversation_flow.py` y `tests/test_ticket_lifecycle.py`).

**Pruebas nuevas.** Cliente de 60 caracteres → título de 24 que termina en «…» y
descripción con el nombre completo. `send_list` con título de 25 → `ValueError`.

**Verificación humana.** Con el teléfono: un cliente de nombre largo aparece en
la lista y se puede elegir. Confirmar de paso los límites reales de Meta.

### B3 — CI con pruebas y despliegue por tag 🟡

**Por qué.** `.github/workflows/build-and-push.yml` publica `:latest` en cada
push a `main` sin correr una sola prueba; `docker-compose.prod.yml` vigila
`:latest` con WUD y el entrypoint migra solo. Es el problema que el sistema de
tickets ya cerró (su decisión #42). Además `api/Dockerfile` termina en
`--reload`, y el compose de producción usa ese `CMD`.

**Cambios.**

1. Workflow: un job `tests` con servicios `postgres:16-alpine` y
   `redis:7-alpine`, que instala `api/requirements-dev.txt`, migra y corre
   `pytest`. `build-and-push` lleva `needs: [tests]`.
2. Etiquetas como en `cgho-ops/.github/workflows/ci.yml`: push a `main` →
   `:main` y `:<sha>`; tag `v*` sin guion → versión y `:latest`. Disparar también
   con `tags: ["v*"]` y en `pull_request` (solo pruebas).
3. `api/Dockerfile`: `CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]`
   y `pip install --no-cache-dir`. El `--reload` se queda donde ya está, en el
   `command` de `docker-compose.yml` de desarrollo.
4. `README.md`: sección «Desplegar» (empujar un tag `v*`; WUD lo aplica de
   madrugada) y «Volver atrás» (fijar el tag y `wud.watch=false`).
5. Quitar el `TODO` viejo de `docker-compose.prod.yml` sobre el nombre de la
   imagen.

**No hacer.** No agregar `--workers`.

**Verificación humana.** Después de fusionar, `:latest` ya no se mueve sola: hay
que empujar el primer tag (`v0.1.0`) para desplegar.

### B4 — Defaults de configuración 🟢

1. `docker-compose.yml` y `docker-compose.prod.yml` inyectan
   `META_API_VERSION: ${META_API_VERSION:-v21.0}`, que le gana al `v26.0` de
   `config.py`. Dejar `v26.0` en los dos, y quitar el default `"v21.0"` del
   constructor de `MetaClient`.
2. `.env.example`: `DEBUG=false`.
3. `api/database.py`: `echo=settings.debug` imprime cada sentencia SQL con sus
   parámetros, es decir, el texto de los mensajes. Separarlo en un ajuste propio
   (`sql_echo`, default `False`).
4. `.env.example`: documentar la forma nueva de `REDIS_URL` cuando el sistema de
   tickets active usuarios en Redis (su orden O4):
   `redis://bot:<contraseña>@redis:6379/1`. El usuario `bot` solo puede tocar
   llaves `bot:*`; todas las de este repo ya llevan ese prefijo
   (`services/buffer/keys.py`). `scripts/smoke_test.py` lee Redis: revisar que
   use esas credenciales.

### B5 — Llave de idempotencia y candado 🟡

**Por qué.** Se pueden crear dos tickets del mismo bloque: con doble toque del
botón, cuando el vencimiento de prioridad coincide con el toque, o cuando el
`POST` tarda más de 10 s y el servidor sí lo creó (se registra «falló» y el
reintento crea otro).

**Cambios.**

1. `on_buffer_close`: agregar `"attempt_id": str(uuid.uuid4())` al estado de la
   sesión. Una sesión escrita por una versión anterior no lo trae: `_finish` lo
   genera y lo guarda antes de llamar.
2. `TicketSystemClient.create_ticket`: parámetro `idempotency_key`, que se manda
   como cabecera `Idempotency-Key`. Aceptar `200` y `201` como éxito (`200`
   significa «ya existía, aquí está»).
3. `_finish`: tomar un candado en Redis al entrar
   (`SET bot:locks:finish:{phone} 1 NX EX 30`) y soltarlo en `finally`. Si no se
   obtiene, salir sin hacer nada: otra ejecución ya está creando ese ticket.

**Pruebas nuevas.** Dos `_finish` simultáneos → una sola llamada a
`create_ticket`. Reintento tras un fallo → misma `Idempotency-Key`. Respuesta
`200` → se confirma al trabajador con ese número de ticket.

**Compatibilidad.** Un servidor que todavía no entiende la cabecera la ignora:
esta orden se puede desplegar antes o después de su pareja.

### B6 — Validar las respuestas interactivas 🟢

**Por qué.** `handle_interactive_reply` usa el `reply_id` sin revisarlo. Si el
trabajador toca un botón de prioridad viejo mientras se espera el cliente, queda
`client_id = "alta"` y cada reintento da 422.

**Cambios.**

1. Al mandar la lista de clientes, guardar en la sesión los ids ofrecidos
   (`client_options`). Para no reiniciar el vencimiento de la sesión, agregar
   `session.update_session(...)`, que reescribe con `KEEPTTL`.
2. En `STEP_AWAITING_CLIENT`: aceptar solo `NO_CLIENT_OPTION_ID` o un id de
   `client_options`. En `STEP_AWAITING_PRIORITY`: solo un valor de `Priority`.
3. Lo que no corresponda se ignora, con una línea en el log.

**Pruebas nuevas.** Botón de prioridad durante la espera de cliente → la sesión
no cambia. Id de cliente que no se ofreció → se ignora.

### B7 — Deduplicación atómica y mensajes que no se pierden 🟡

**Por qué.** `_handle_messages` hace `SELECT` y luego `INSERT`: dos entregas
simultáneas del mismo `wamid` terminan en 500. Y el mensaje se marca como visto
(commit) **antes** de procesarse: si el proceso cae entre el commit y la tarea
en segundo plano, el reintento de Meta se descarta y el mensaje nunca entra al
buffer.

**Cambios.**

1. Insertar con `INSERT … ON CONFLICT (wamid) DO NOTHING RETURNING id`. Sin fila
   devuelta, es un duplicado: se salta.
2. Migración: `raw_messages.processed_at TIMESTAMPTZ NULL`. Se llena al terminar
   `_process_message`.
3. Al arrancar (`lifespan`, después de recuperar los vigilantes): reprocesar las
   filas con `processed_at IS NULL` de la última hora, en orden de llegada,
   desde su `payload` guardado.
4. Como un mensaje puede entrar dos veces al buffer (caída después de
   bufferizar y antes de marcar), quitar `wamid` repetidos al cerrar el bloque.

**Pruebas nuevas.** Mismo `wamid` dos veces a la vez → 200 las dos y una sola
fila. Fila sin `processed_at` al arrancar → se reprocesa y queda marcada. Bloque
con un `wamid` repetido → el texto aparece una vez en la descripción.

### B8 — Ventana de reintento y sesiones que no se pisan 🟡

**Por qué.** Tras un fallo de creación la sesión se renueva por 90 s sin
vigilante: si el trabajador no toca en ese tiempo, el bloque se pierde en
silencio, contra la intención de la decisión #21. Y `on_buffer_close`
sobrescribe cualquier sesión en curso.

**Cambios.**

1. Paso nuevo `STEP_AWAITING_RETRY`: la sesión dura
   `creation_retry_window_seconds` (default 900). Un toque de prioridad
   reintenta. Al vencer **no se reintenta solo** (decisión #21): se avisa al
   trabajador que no se pudo crear y que avise a un administrador, y se cierra
   la sesión. El intento ya quedó en `ticket_creations` como `failed`.
2. `on_buffer_close` con una sesión activa: posponer el cierre 15 s (volver a
   poner el marcador del buffer) hasta ocho veces. Si la sesión sigue ahí,
   registrar el bloque anterior como `failed` con el error «sesión reemplazada»
   y continuar con el nuevo.

**Pruebas nuevas.** Fallo y luego toque a los 5 min → se crea. Fallo y
vencimiento → mensaje al trabajador y sesión cerrada, sin segunda llamada al
sistema de tickets. Buffer que cierra con sesión activa → no la sobrescribe.

### B9 — `/health` de verdad 🟢

`main.py` responde `{"status": "ok"}` sin tocar nada, y es lo que vigila el
latido del sistema de tickets. Hacer que ejecute `SELECT 1` y un `PING` a Redis,
y responda 503 si alguno falla, sin detalle en la respuesta (es pública detrás
del proxy). Agregar al compose de producción un healthcheck con Python, porque
la imagen `slim` no trae `curl`:

```yaml
healthcheck:
  test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
  interval: 15s
  timeout: 5s
  retries: 5
  start_period: 30s
```

### B10 — Canal de alertas de Meta 🟢

**Qué es.** Además de mensajes, Meta manda al mismo webhook avisos sobre la
cuenta: que restringió o suspendió la cuenta (`account_update`), que bajó el
límite de mensajes del número (`phone_number_quality_update`) o que cambió algo
de seguridad (`security`). `routes/webhook.py` ya los separa y los pasa a
`services/alerts/`, pero ese módulo solo escribe en el log del contenedor. Si
Meta restringe el número, el bot deja de funcionar y nadie se entera hasta que
alguien nota que no contesta.

**Propuesta (pendiente del visto bueno del dueño).** Usar healthchecks.io, que
el sistema de tickets ya usa para sus latidos (su decisión #43): un check
propio, y el bot le manda un `POST` a `<URL>/fail` con el aviso en el cuerpo.
healthchecks.io notifica por los canales que ya estén configurados ahí. No
agrega proveedor, cuenta ni dependencia.

**Cambios.** `HealthchecksAlertNotifier` detrás del protocolo `AlertNotifier`
que ya existe; se usa si `ALERTS_HEALTHCHECK_URL` está definida y si no, sigue
el de log. Nunca lanza: un fallo al avisar se registra y el webhook responde
200 igual (decisión #11). Actualizar la fila de pendientes de `CLAUDE.md`.

### B11 — Documentación al día 🟢

- `CLAUDE.md` describe un servicio `cloudflared` en `docker-compose.yml` y un
  `cloudflared/config.yml` versionado. No existen: marcarlo como pendiente (el
  túnel nombrado espera al dominio) en vez de describirlo como hecho.
- `docs/bot-diseno-flujo-cliente.md` dice que una sola coincidencia de cliente
  se asigna directo. Contradice la decisión #5: corregirlo, o marcar el
  documento como histórico.
- `README.md`: el paso de migrar a mano sobra (lo hace el entrypoint); el
  diagrama dice `POST /tickets` y es `POST /internal/tickets`; el ejemplo de
  alta manual de un trabajador ya no aplica (decisión #15).

### B12 — Dependabot y foto del contrato 🟡

1. `.github/dependabot.yml`: `pip` en `/api`, `docker` en `/api`,
   `github-actions` en `/`, semanal y agrupando las menores.
   `api/requirements.txt` fija versiones de 2024: la primera tanda va a ser
   grande; subirlas de a una familia por PR.
2. Copiar `docs/contracts/internal-openapi.json` del sistema de tickets (su
   orden O8) a `api/tests/contract/`. Una prueba comprueba que cada campo que
   usan `ClientSearchResult`, `CreatedTicket` y `WorkerSync` existe en ese
   esquema, y que lo que manda `create_ticket` coincide con lo que el esquema
   acepta.

### B13 — Imagen sin root 🟡

`api/Dockerfile`: crear un usuario sin privilegios y terminar con `USER`.
Comprobar que `entrypoint.sh` (que corre `alembic upgrade head`) no necesita
escribir en disco. Agregar `api/tests/` al `.dockerignore`: en desarrollo llegan
por el bind mount.

---

## 2. Comprobantes por WhatsApp

Diseño completo, decisiones y contrato en `cgho-ops/docs/PLAN-AUDITORIA-2026-11.md`
§2. Aquí, lo que le toca al bot. Entra con su decisión escrita en `CLAUDE.md` (la
siguiente libre) al implementarse.

### 2.1 Resumen del flujo

1. Llega un mensaje `image` o `document` de un trabajador de la whitelist.
2. El bot pide a Meta los datos del archivo, lo baja en memoria y lo sube al
   sistema de tickets. No guarda archivos.
3. Si la extracción está encendida, Claude Haiku lee el comprobante y devuelve
   un JSON con monto, fecha, concepto y referencia.
4. El bot resume lo que detectó y pregunta el cliente (igual que en tickets) y
   el tipo: «Cobro a cliente», «Pago o gasto», «No sé».
5. Crea el borrador en el sistema de tickets y confirma con su número.

Finanzas lo revisa y lo aprueba desde la web. El bot nunca registra dinero.

### 2.2 Decisiones que le tocan al bot

| | Decisión |
|---|---|
| I4 | **El cliente se pregunta**, como en tickets (decisión #5). El nombre que el modelo lea en el comprobante viaja como dato informativo, nunca asigna |
| I6 | **Proveedor: Claude Haiku 5.5** (`claude-haiku-5-5`), detrás de una interfaz, con `VISION_PROVIDER=none` para apagar la extracción y seguir capturando |
| I7 | **Sin etapa de OCR.** Un XML de CFDI no pasa por ningún modelo: lo lee la pantalla de Finanzas |
| I9 | **El bot no guarda archivos.** El archivo va en memoria, con tope, de Meta al sistema de tickets |
| — | **La extracción nunca bloquea** (espíritu de la decisión #7): si falla, tarda o el modelo rehúsa, el borrador se crea sin datos sugeridos |
| — | **Si nadie contesta, el borrador se crea igual**, sin cliente y con tipo `desconocido`. Es un borrador: lo revisa una persona |

### 2.3 Contrato que consume

Copia del de `cgho-ops`; si difieren, manda aquel.

- `PUT /internal/finance/intake-files?filename=<nombre>`: cuerpo crudo →
  `{ "sha256", "size_bytes", "content_type" }`. `413` si pasa del tope, `422` si
  el tipo no se acepta.
- `POST /internal/finance/intakes`: `source` (`"whatsapp"`), `external_ref` (el
  `wamid`), `submitted_by` (`Worker.external_user_id`), `client_id`,
  `kind_hint` (`cobro` / `pago` / `desconocido`), `file` (`sha256`, `filename`),
  `caption`, `extraction` →
  `{ "id", "intake_number", "possible_duplicate" }`. `201` si lo creó, `200` si
  ya existía con ese `wamid`.

Forma de `extraction`:

```json
{
  "schema_version": 1,
  "document_type": "transferencia | deposito | ticket_compra | factura | otro | ilegible",
  "amount": "12500.00",
  "date": "2026-11-05",
  "concept": "string | null",
  "reference": "string | null",
  "counterparty": { "name": null, "rfc": null, "bank": null, "account_last4": null },
  "direction_hint": "entrada | salida | null",
  "confidence": { "amount": "alta | media | baja", "date": "alta | media | baja" },
  "warnings": [],
  "extractor": { "provider": "anthropic", "model": "claude-haiku-5-5", "method": "vision" }
}
```

El monto es **texto** con dos decimales. Nunca `float`.

### 2.4 Órdenes

#### M1 — Recibir medios 🟡

**Cambios.**

1. `routes/webhook.py`: hoy `_process_message` registra «Tipo de mensaje sin
   manejar» para todo lo que no es texto o interactivo. Agregar la rama
   `image` / `document`. Parseo defensivo siempre (`.get()`): de
   `message["image"]` o `message["document"]` salen `id`, `mime_type`, `sha256`,
   `caption` y, en documentos, `filename`. `_message_body` devuelve el `caption`
   para estos tipos.
2. `services/meta/client.py`:
   - `get_media_info(media_id)` → `GET {base}/{version}/{media_id}` con el token;
     devuelve `url`, `mime_type`, `sha256`, `file_size`.
   - `download_media(url, max_bytes)` → baja en streaming y corta si pasa del
     tope. **Antes de mandar el token, comprobar que la URL es `https` y que su
     host está en `meta_media_allowed_hosts`** (ajuste nuevo; confirmar el host
     real con un mensaje de prueba). El token viaja en esa petición: no se le
     manda a cualquier dirección.
   - Comparar el SHA-256 de lo bajado con el que dijo Meta.
3. Rechazar antes de bajar si `file_size` pasa de `media_max_bytes` (default
   10 MB, igual que el tope del sistema de tickets) o si `mime_type` no es JPG,
   PNG, PDF o XML. Contestar al trabajador qué se acepta. Un sticker (`webp`)
   cae aquí.
4. Tabla nueva `media_intakes` y su migración: bitácora de salida, con el mismo
   criterio que `ticket_creations` (decisión #20).

   | Columna | Notas |
   |---|---|
   | `id` UUID PK | |
   | `worker_id` FK → workers | |
   | `raw_message_id` FK → raw_messages, `UNIQUE` | Un mensaje, un intento |
   | `media_id` | El id de Meta. Meta conserva el archivo 30 días: permite reintentar |
   | `mime_type`, `sha256`, `size_bytes` | |
   | `status` | `received` / `uploaded` / `submitted` / `failed` / `rejected` |
   | `extraction` JSONB NULL | Lo que se mandó |
   | `client_id`, `kind_hint` | Lo que contestó el trabajador |
   | `external_intake_id`, `intake_number` | Lo que devolvió el sistema de tickets |
   | `error` | |
   | `created_at` | |

5. Límite por trabajador: un contador en Redis (`bot:media:rate:{phone}:{hora}`)
   con `media_max_per_hour` (default 20). Pasado el límite, se contesta que
   espere. Acota el costo del modelo y el disco del otro lado.

**Pruebas.** Payloads nuevos en `tests/fixtures/webhook_payloads/` (imagen con y
sin `caption`, documento PDF, sticker). Host fuera de la lista → no se baja y no
se manda el token. Archivo más grande que el tope → se corta. SHA distinto →
`failed`. Reintento de Meta con el mismo `wamid` → un solo intento.

**No hacer.** No escribir el archivo a disco ni guardarlo en `bot_db`.

#### M2 — Subir y crear el borrador 🟡

`services/ticket_system/client.py`: `upload_intake_file(content, filename)` y
`create_intake(...)`, con las formas de §2.3. Aceptar `200` y `201`. Tras cada
paso, actualizar el `status` de `media_intakes`. Un fallo se registra con su
error y se le avisa al trabajador; no hay reintento automático (mismo criterio
que la decisión #21).

**Pruebas.** Con `pytest-httpx`: forma exacta de las dos peticiones (cabecera
del token, cuerpo crudo, JSON). `422` del servidor → `failed` y aviso.

#### M3 — Conversación 🟡

**Cambios.**

1. La sesión gana `kind`: `"ticket"` (la de hoy) o `"intake"`. Una sesión sin
   `kind` es de tickets.
2. Los medios tienen su propio debounce, separado del buffer de texto: varios
   archivos seguidos del mismo trabajador forman **una tanda** (tope de 10). El
   cliente y el tipo se preguntan una vez por tanda; cada archivo es su propio
   borrador.
3. Si al cerrar la tanda hay otra sesión activa, se espera (reintento cada 15 s
   hasta 5 min). Si sigue ocupada, los borradores se crean sin cliente y con
   tipo `desconocido`: no se pierde nada.
4. Mensajes al trabajador:
   - Al recibir: «Recibí 1 comprobante: Transferencia · $12,500.00 ·
     05/11/2026. ¿De qué cliente es? Escribe el nombre.» Sin extracción, solo
     «Recibí 1 comprobante».
   - Cliente: mismo mecanismo que tickets (búsqueda, lista con «Sin cliente»),
     con los títulos recortados de B2.
   - Tipo: tres botones, «Cobro a cliente», «Pago o gasto», «No sé» (los tres
     caben en 20 caracteres).
   - Al terminar: «Enviado a Finanzas para verificación (C-31).» Si el servidor
     marcó `possible_duplicate`, agregar que parece repetido.
5. Vencimientos: cliente → sin cliente; tipo → `desconocido`. En los dos casos
   el borrador se crea.

**Pruebas.** Flujo completo de un archivo; tanda de tres con una sola pregunta;
los dos vencimientos; texto que llega durante la espera de cliente se toma como
la respuesta; sesión de tickets activa al llegar un medio.

#### M4 — Extracción con Claude Haiku 🟡

**Antes de escribir código**, leer la documentación vigente del SDK de Anthropic
para Python (en Claude Code, la skill `claude-api`). El SDK cambia más rápido
que este plan.

**Cambios.**

1. `api/requirements.txt`: el SDK oficial `anthropic`, con la versión fijada a
   la que se instale. **No** llamar a la API con `httpx` a mano. Ojo: el SDK 1.x
   usa `httpx2` y no `httpx`, así que `pytest-httpx` no intercepta sus
   peticiones; por eso las pruebas usan un extractor falso (punto 5).
2. `services/llm/vision.py`, con el mismo patrón que `services/llm/`:
   - Protocolo `ReceiptExtractor` con
     `async def extract(content: bytes, media_type: str) -> ReceiptExtraction | None`.
     **Nunca lanza.**
   - `AnthropicReceiptExtractor`: cliente `AsyncAnthropic`, modelo
     `claude-haiku-5-5`, salida estructurada con `messages.parse(...)` y un
     modelo Pydantic como `output_format`. La imagen va como bloque `image` en
     base64 y el PDF como bloque `document`, antes del texto. Esfuerzo `low`.
   - Revisar `stop_reason` antes de leer el resultado: si es `refusal`, no hay
     extracción.
   - `NullReceiptExtractor` para `VISION_PROVIDER=none`: devuelve `None`.
   - Los XML no se mandan al modelo: `extraction` va en `null`.
3. `services/llm/receipt.py`: validación después del modelo, sin lanzar. Monto
   con forma `^\d{1,10}\.\d{2}$` y mayor que cero; fecha ISO, no futura. Lo que
   no cumpla va en `null` con un aviso en `warnings`. Textos recortados
   (`concept` 255, `reference` 100).
4. Ajustes en `config.py` y en los dos compose: `VISION_PROVIDER`
   (`none` | `anthropic`, default `none`), `ANTHROPIC_API_KEY`, `VISION_MODEL`
   (default `claude-haiku-5-5`), `VISION_TIMEOUT_SECONDS` (default 30). El
   validador de B1 exige la llave cuando el proveedor es `anthropic`.
5. La instrucción al modelo dice: copia lo que está escrito; si un dato no se
   lee con claridad, `null`; no deduzcas el cliente; el monto es el total de la
   operación, no un saldo ni una comisión.

**Pruebas.** Con un extractor falso inyectado, como hoy se inyecta el de
DeepSeek: resultado bueno, resultado con monto inválido (queda `null` con
aviso), excepción (el borrador se crea sin extracción), rechazo del modelo. Una
prueba `live` aparte, fuera de la corrida normal, con un comprobante de ejemplo
sin datos reales.

**No hacer.** No usar el resultado para elegir cliente ni tipo. No reintentar la
extracción en bucle: un intento.

#### M5 — Prueba de humo con medios 🟡

1. `scripts/meta_sink.py`: atender `GET /vXX/<media_id>` (devuelve una `url` que
   apunta al propio sink) y servir un archivo de ejemplo. Agregar el host del
   sink a `meta_media_allowed_hosts` solo en ese entorno.
2. `scripts/smoke_test.py`: un caso que manda un mensaje `image` firmado y
   comprueba la fila en `media_intakes` y el borrador del otro lado.
3. **Verificación humana con el teléfono real** (disponible desde el
   2026-11-12). Mandar y anotar el resultado de cada uno:
   - una foto de un ticket de compra;
   - una captura de una transferencia;
   - un PDF de un comprobante bancario;
   - un XML de CFDI;
   - un sticker (debe contestar qué se acepta);
   - un archivo de más de 10 MB;
   - tres fotos seguidas (una sola tanda, una sola pregunta);
   - un comprobante de un cliente de nombre largo (B2).

   Anotar además el host real de las URL de descarga de Meta, para fijar
   `meta_media_allowed_hosts`.
