# Referencia de Webhooks — Bot de WhatsApp (despacho)

Payloads reales capturados durante las pruebas del entorno desechable (túnel de Cloudflare + FastAPI + Postgres). Sirve como referencia de estructura al escribir el parser real.

## Estructura común (envelope)

Todo evento —sin importar cuál— llega al **mismo** `POST /webhook`, con esta forma exterior:

```json
{
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "<waba_id>",
      "changes": [
        {
          "field": "<nombre_del_evento>",
          "value": { ... }
        }
      ]
    }
  ]
}
```

**El dispatcher se decide por `entry[0].changes[0].field`.** Un solo handler, una rama por campo — no hace falta (ni existe) un endpoint distinto por tipo de evento.

> ⚠️ En pruebas reales, `entry` a veces trae más de un elemento y `changes` también — no asumas longitud 1 en producción, itera ambas listas.

---

## Eventos críticos — procesar activamente

### `messages`
La razón de ser del bot. Aquí llegan los mensajes reales de clientes.

```json
{
  "field": "messages",
  "value": {
    "messaging_product": "whatsapp",
    "metadata": {
      "display_phone_number": "16505551111",
      "phone_number_id": "123456123"
    },
    "contacts": [
      {
        "profile": { "name": "test user name" },
        "wa_id": "16315551181",
        "user_id": "US.13491208655302741918"
      }
    ],
    "messages": [
      {
        "id": "ABGGFlA5Fpa",
        "from": "16315551181",
        "from_user_id": "US.13491208655302741918",
        "timestamp": "1504902988",
        "type": "text",
        "text": { "body": "this is a text message" }
      }
    ]
  }
}
```

**Campos clave:**
- `messages[0].id` → el `wamid`, úsalo como llave de idempotencia para deduplicar (ver sección de convenciones).
- `messages[0].from` → número del remitente.
- `messages[0].type` → `"text"`, `"image"`, `"document"`, etc. — el parser debe ramificar aquí también, no asumir siempre texto.
- `contacts[0].profile.name` → nombre que el cliente tiene configurado en WhatsApp (no siempre confiable/verificado).
- `contacts[0].user_id` y `messages[0].from_user_id` → **no aparecen en la documentación oficial de Meta**, solo en este payload de ejemplo. No asumas que siempre van a estar — usa `.get()` en vez de acceso directo (`dict["key"]`).

**Nota de negocio pendiente:** cuando el mensaje es un *forward* de un trabajador (no un cliente escribiendo directo), este payload no trae quién era el remitente original — hay que resolverlo pidiéndole al trabajador que identifique al cliente por nombre (buscar coincidencias; confirmar si hay una sola, preguntar cuál si hay varias).

---

## Eventos de alerta — notificar por canal separado (no WhatsApp)

Si el problema es el número/WABA mismo, notificar por WhatsApp no sirve — usa Telegram o correo.

### `account_update`
Cambios de estado de la cuenta: desconexión, violación de política, revocación.

```json
{
  "field": "account_update",
  "value": {
    "event": "VERIFIED_ACCOUNT",
    "phone_number": "16505551111"
  }
}
```

`event` puede traer distintos valores (`VERIFIED_ACCOUNT`, baja, violación, etc.) — el handler de alertas debe leer `event` y decidir la urgencia del mensaje, no tratarlos todos igual.

### `phone_number_quality_update`
Cambios en el tier de límite de mensajes — señal temprana de problemas de calidad/spam.

```json
{
  "field": "phone_number_quality_update",
  "value": {
    "event": "ONBOARDING",
    "old_limit": "TIER_NOT_SET",
    "current_limit": "TIER_250",
    "display_phone_number": "16505551111",
    "max_daily_conversations_per_business": "TIER_250"
  }
}
```

Alertar sobre todo cuando `current_limit` sea **menor** que `old_limit` — eso es lo que indica un problema, no cualquier cambio (el de este ejemplo es solo el onboarding inicial, no una degradación).

### `security`
Cambios de PIN del número.

```json
{
  "field": "security",
  "value": {
    "event": "PIN_CHANGED",
    "requester": "1000",
    "display_phone_number": "16505551111"
  }
}
```

Alertar siempre — un cambio de PIN no esperado es la señal más directa de acceso no autorizado.

---

## Eventos informativos — solo loguear (no interrumpir)

### `account_alerts`
```json
{
  "field": "account_alerts",
  "value": {
    "entity_id": 123456,
    "alert_type": "OBA_APPROVED",
    "entity_type": "WABA",
    "alert_status": "NONE",
    "alert_severity": "INFORMATIONAL",
    "alert_description": "Sample alert description, informational in nature with no status"
  }
}
```
Trae su propio `alert_severity` — si en el futuro aparece algo distinto de `INFORMATIONAL`, vale la pena revisar si merece pasar a la lista de alertas activas.

### `message_template_quality_update`
```json
{
  "field": "message_template_quality_update",
  "value": {
    "new_quality_score": "YELLOW",
    "message_template_id": 12345678,
    "message_template_name": "my_message_template",
    "previous_quality_score": "GREEN",
    "message_template_language": "pt-BR"
  }
}
```

### `message_template_status_update`
```json
{
  "field": "message_template_status_update",
  "value": {
    "event": "APPROVED",
    "reason": null,
    "message_template_id": 12345678,
    "message_template_name": "my_message_template",
    "message_template_category": "MARKETING",
    "message_template_language": "pt-BR"
  }
}
```

### `phone_number_name_update`
Resultado de la revisión del nombre visible (el que sí te va a importar cuando mandes "CGHO Contadores" a revisión).
```json
{
  "field": "phone_number_name_update",
  "value": {
    "decision": "APPROVED",
    "rejection_reason": null,
    "display_phone_number": "16505551111",
    "requested_verified_name": "WhatsApp"
  }
}
```
Cuando `decision` sea `"REJECTED"`, `rejection_reason` trae el motivo — vale la pena loguearlo con detalle, es la única pista de por qué falló la aprobación del nombre.

---

## Explícitamente no suscrito

### `calls`
Es para la API de llamadas de voz de WhatsApp (evento `connect`, `terminate`, etc.), no mensajería de texto. No aplica al bot — desuscrito a propósito.

---

## Convenciones y gotchas para el parser real

1. **Parsing defensivo siempre.** Usa `.get()` en cada nivel, nunca acceso directo por índice/llave — algunos payloads reales traen campos que no están en la documentación oficial (ver `user_id`/`from_user_id` arriba), y otros pueden faltar campos "opcionales".
2. **Idempotencia por `wamid`.** Antes de crear un ticket, verifica si ya existe uno con ese `messages[0].id` — Meta reintenta entregas fallidas hasta por 7 días con frecuencia decreciente, y eso puede generar el mismo payload más de una vez.
3. **Responde 200 rápido, siempre.** El handler de `POST /webhook` no debe hacer el trabajo pesado (llamar a la API del sistema de tickets, extraer entidades con el LLM, etc.) de forma síncrona — usa background tasks. Una respuesta lenta o un error dispara reintentos innecesarios.
4. **Un solo dispatcher, no un endpoint por evento.** Todos los `field` llegan al mismo `/webhook` — la ramificación va adentro del handler, según el valor de `field`.
5. **Los eventos de prueba vs. reales se distinguen por el contenido, no por la estructura.** Los que generaste con el botón "Probar" del dashboard usan datos de ejemplo reconocibles (`16505551111`, `test user name`, `my_message_template`) — la estructura JSON es idéntica a la de producción, así que sirven como fixtures válidos para tests automatizados del parser.
