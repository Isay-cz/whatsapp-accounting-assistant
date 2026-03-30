# WhatsApp Accounting Assistant

> **Sprint de desarrollo activo** — Este repositorio documenta la construcción iterativa de un sistema de gestión de tickets para un despacho contable. Cada fase está documentada con las decisiones técnicas tomadas y su justificación.

Sistema que recibe mensajes de WhatsApp reenviados por trabajadores del despacho, extrae información estructurada con un LLM, genera tickets en PostgreSQL y expone métricas en tiempo real a través de un dashboard ejecutivo y una interfaz de gestión para trabajadores.

---

## Contexto

El despacho recibe solicitudes de clientes por WhatsApp de forma desestructurada. Los trabajadores reenvían esos mensajes al bot, que los clasifica automáticamente y crea un ticket trazable. El objetivo del MVP es estrecho y deliberado: **recibir → extraer → registrar → confirmar**.

Este proyecto es el entregable central de un sprint de 10 días y forma parte de mi portafolio para búsqueda de beca en Data Science / ML.

---

## Stack

| Capa | Tecnología | Justificación |
|---|---|---|
| Mensajería | Twilio WhatsApp Sandbox → Twilio prod | Sin aprobación Meta requerida; el volumen del despacho no justifica Cloud API directa |
| Backend | FastAPI + asyncpg | Async nativo para I/O concurrente con Twilio + DB + LLM |
| Pipeline NLP | Ollama (dev) / Gemini Flash (prod) | Costo cero en desarrollo; misma interfaz abstraída por variable de entorno |
| Base de datos | PostgreSQL 16 + Alembic | On-premise, datos sensibles del despacho no salen a nube |
| Gestión de tickets | FastAPI + HTMX | Interactividad sin frontend separado; se monta sobre el mismo servidor FastAPI |
| Dashboard ejecutivo | Plotly Dash | Integración directa con PostgreSQL, sin capa intermedia |
| Deploy | Docker Compose | Mismo `docker-compose.yml` en desarrollo y producción |

---

## Arquitectura del sistema

```
WhatsApp (cliente)
      │
      ▼
Trabajador reenvía mensaje
      │
      ▼
Twilio ──► POST /api/v1/whatsapp
               │
               ├─ Validación firma HMAC-SHA1
               ├─ Whitelist (tabla workers)
               ├─ Guardado raw_message
               │
               ▼
         Pipeline NLP
         (Ollama / Gemini Flash)
               │
               ▼
         Ticket en PostgreSQL
               │
               ├─► Confirmación TwiML al trabajador
               │
               ├─► Interfaz de gestión (trabajadores)
               │     Marcar estado · Editar entidades · Ver mensaje original
               │
               └─► Dashboard ejecutivo (directivos)
                     Métricas · Volumen · Proyecciones
```

---

## Interfaces de usuario

El sistema tiene dos interfaces con propósitos y audiencias distintas:

**Interfaz de gestión de tickets** — para trabajadores del despacho. Permite revisar los tickets generados, ver el mensaje original del cliente, corregir campos que el LLM extrajo incorrectamente, y actualizar el estado del ticket (`abierto → en_proceso → completado`). Construida con FastAPI + HTMX sobre el mismo servidor del backend, sin servicio adicional.

**Dashboard ejecutivo** — para directivos. Vista agregada de métricas: volumen por departamento, tiempo de resolución, tipos de solicitud más frecuentes, carga de trabajo proyectada. Read-only, construido con Plotly Dash.

---

## Modelo de datos

Cinco tablas core. Diseñadas para soportar el MVP con espacio para crecer sin migraciones destructivas.

```
workers ──────────┐
                  ├──► raw_messages ──► tickets ◄── clients
departments ──────┘                        │
                                           └──► departments
```

**Decisiones de diseño documentadas:**
- `raw_messages` persiste el payload completo de Twilio antes de cualquier procesamiento (auditoría y reintento en caso de fallo NLP)
- `tickets.ticket_number` es un `BIGSERIAL` legible además del UUID primario — los trabajadores citan números, no UUIDs
- `tickets.extracted_entities` en JSONB absorbe entidades nuevas sin alterar el schema
- `departments` como tabla dinámica en lugar de ENUM — los directivos agregan departamentos desde el dashboard sin tocar código
- `llm_provider` + `llm_confidence` por ticket para observabilidad desde el día uno

---

## Fases de desarrollo

### ✅ Fase 0 — Diseño y modelo de datos
- [x] Definición del stack técnico
- [x] Modelo de datos completo (5 tablas, constraints, índices)
- [x] Schema Pydantic de extracción de entidades
- [x] Estructura del repositorio

### 🔄 Fase 1 — Fundación (en progreso)
- [ ] Modelos SQLAlchemy (ORM)
- [ ] Primera migración Alembic
- [ ] Webhook `/api/v1/whatsapp` con validación de firma Twilio
- [ ] Validación de whitelist
- [ ] Guardado de `raw_messages`
- [ ] Docker Compose (`api` + `db`)
- [ ] Smoke test con ngrok

### ⬜ Fase 2 — Pipeline NLP
- [ ] Extractor de entidades con Ollama (desarrollo)
- [ ] Extractor con Gemini Flash (producción)
- [ ] Validación con Pydantic + manejo de JSON malformado
- [ ] Creación de tickets en PostgreSQL
- [ ] Deduplicación de clientes por RFC / teléfono

### ⬜ Fase 3 — Respuestas e interfaz de gestión
- [ ] Confirmación TwiML al trabajador (número de ticket, tipo, entidades extraídas)
- [ ] Manejo de errores con mensaje amigable al trabajador
- [ ] Interfaz de gestión: lista de tickets con filtros por estado y departamento
- [ ] Interfaz de gestión: vista de ticket individual con mensaje original del cliente
- [ ] Interfaz de gestión: edición inline de campos extraídos por el LLM
- [ ] Interfaz de gestión: cambio de estado con historial de transiciones

### ⬜ Fase 4 — Dashboard ejecutivo
- [ ] Volumen de tickets por departamento
- [ ] Tiempo promedio de resolución
- [ ] Tipos de solicitud más frecuentes
- [ ] Clientes con mayor actividad
- [ ] Proyección de carga (regresión lineal)

### ⬜ Fase 5 — Containerización y docs
- [ ] Docker Compose completo (api + db + dashboard)
- [ ] Variables de entorno documentadas
- [ ] README de despliegue en servidor del despacho
- [ ] Notebooks de análisis histórico

---

## Estructura del repositorio

```
whatsapp-accounting-assistant/
├── api/
│   ├── main.py
│   ├── config.py                  # Settings centralizados con Pydantic
│   ├── database.py                # Engine async + sesión
│   ├── routes/
│   │   ├── webhook.py             # POST /api/v1/whatsapp
│   │   └── tickets.py             # GET/PATCH /api/v1/tickets (interfaz de gestión)
│   ├── models/
│   │   ├── orm.py                 # SQLAlchemy models
│   │   └── schemas.py             # Pydantic request/response
│   ├── services/
│   │   ├── whatsapp/              # Cliente Twilio + TwiML
│   │   └── nlp/                   # Pipeline Ollama / Gemini
│   └── templates/                 # Jinja2 + HTMX (interfaz de gestión)
│       ├── base.html
│       ├── tickets/
│       │   ├── list.html          # Lista de tickets con filtros
│       │   └── detail.html        # Vista individual + edición inline
│       └── partials/              # Fragmentos HTMX para actualizaciones parciales
│           ├── ticket_row.html
│           └── ticket_status.html
├── dashboard/                     # Plotly Dash (directivos)
├── notebooks/                     # Análisis históricos Jupyter
├── db/
│   └── migrations/                # Alembic
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Setup local

### Prerrequisitos
- Docker y Docker Compose
- Python 3.12+
- [Ollama](https://ollama.com) con `llama3.1:8b` o `qwen2.5:7b`
- Cuenta Twilio con WhatsApp Sandbox configurado
- [ngrok](https://ngrok.com) para exponer el webhook localmente

### 1. Clonar e instalar

```bash
git clone https://github.com/Isay-cz/whatsapp-accounting-assistant
cd whatsapp-accounting-assistant
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Variables de entorno

```bash
cp .env.example .env
# Editar .env con tus credenciales de Twilio
```

### 3. Levantar servicios

```bash
docker compose up -d
```

### 4. Correr migraciones

```bash
alembic upgrade head
```

### 5. Exponer webhook

```bash
ngrok http 8000
# Copiar la URL https://xxxx.ngrok-free.app/api/v1/whatsapp
# Pegarla en Twilio Console → Sandbox → "When a message comes in"
```

### 6. Agregar un worker de prueba

```bash
docker compose exec db psql -U devuser -d accounting_bot -c \
  "INSERT INTO workers (phone_number, name, role) VALUES ('+521XXXXXXXXXX', 'Tu nombre', 'dev');"
```

---

## Variables de entorno

| Variable | Descripción | Ejemplo |
|---|---|---|
| `DATABASE_URL` | Conexión a PostgreSQL | `postgresql://user:pass@db:5432/accounting_bot` |
| `TWILIO_ACCOUNT_SID` | SID de la cuenta Twilio | `ACxxxxxxxx` |
| `TWILIO_AUTH_TOKEN` | Token de autenticación Twilio | `xxxxxxxx` |
| `TWILIO_WHATSAPP_NUMBER` | Número del sandbox | `whatsapp:+14155238886` |
| `LLM_PROVIDER` | Proveedor LLM activo | `ollama` \| `gemini` |
| `OLLAMA_BASE_URL` | URL de Ollama local | `http://localhost:11434` |
| `OLLAMA_MODEL` | Modelo a usar en desarrollo | `llama3.1:8b` |
| `GEMINI_API_KEY` | API key de Gemini (producción) | `AIzaxxxx` |
| `DEBUG` | Modo debug de FastAPI + SQLAlchemy | `true` \| `false` |

---

*Documentación actualizada iterativamente durante el sprint de desarrollo.*
