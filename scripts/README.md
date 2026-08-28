# scripts/ — prueba de humo del despliegue

Verifican el bot **ya desplegado**, desde afuera: los requests entran por
HTTPS público, pasan por el reverse proxy y llegan al contenedor real.

Es la capa que falta sobre `api/tests/`:

| | entra por | Meta | DeepSeek | Sistema de tickets |
|---|---|---|---|---|
| `pytest` (normal) | ASGI, en proceso | mock | mock | mock |
| `pytest -m live` | ASGI, en proceso | mock | **real** | **real** |
| `scripts/smoke_test.py` | **HTTPS público → proxy → contenedor** | sink local | **real** | **real** |

Lo que solo esta capa detecta: un `META_APP_SECRET` vacío, un alias de red
equivocado, una ruta de proxy faltante, un contenedor que arrancó sin la
variable que creías. Nada de eso es visible desde pytest, y todo rompe el bot
en producción.

## Archivos

- **`smoke_test.py`** — corre en la VM (solo stdlib de Python 3, más acceso al
  socket de Docker). Es el que se invoca.
- **`meta_sink.py`** — recibe lo que el bot manda hacia Graph API y lo guarda
  en memoria, en vez de mandarlo por WhatsApp. Corre como contenedor en
  `cgho_net`; lo levanta `smoke_test.py setup`.

## Uso

```bash
scp -i ~/.ssh/<llave> scripts/*.py <usuario>@<vm>:~/whatsapp-accounting-assistant/scripts/
ssh -i ~/.ssh/<llave> <usuario>@<vm>

cd ~/whatsapp-accounting-assistant/scripts
python3 smoke_test.py all              # setup + verificaciones + teardown
python3 smoke_test.py all --skip-flow  # solo conectividad: no gasta DeepSeek ni crea tickets
python3 smoke_test.py all --keep       # deja el sink arriba para inspeccionarlo
```

Los subcomandos por separado (`setup`, `run`, `teardown`) sirven para iterar:
`setup` una vez, `run` las veces que haga falta, `teardown` al final.

## Qué verifica

**Preflight** — los cinco contenedores arriba, el bot con `META_APP_SECRET`,
`META_VERIFY_TOKEN` y `META_GRAPH_BASE_URL` en su entorno, el proxy ruteando
hacia el bot, y un trabajador activo con `external_user_id` en la whitelist.

**1 · Conectividad** (sin efectos secundarios, con un número que no está en la
whitelist) — handshake `GET /webhook` con token correcto, equivocado, vacío y
sin `hub.challenge`; `POST` sin firma, con firma inválida y con el cuerpo
alterado después de firmar; y que un número no autorizado reciba 200 pero no
deje nada en `raw_messages`.

**2 · Flujo completo** — tres mensajes firmados por HTTPS → dedup por `wamid`
→ `raw_messages` → cierre del debounce → título y entidades de DeepSeek real →
búsqueda de clientes contra `/internal` → prioridad → **ticket real**. Después
verifica las dos bases: `ticket_creations` con los tres mensajes vinculados
(decisión #20), y del otro lado que el ticket exista con el `created_by`
correcto, la descripción con el texto crudo y el departamento derivado
server-side (decisión #8).

## Requisitos del entorno

1. **Ruta del proxy hacia el bot.** El bot no publica puertos (decisión #17).
   En producción eso lo resuelve Cloudflare Tunnel; en una VM de prueba sin
   dominio en Cloudflare, la vía es el Caddy que ya administra el certificado
   del stack de cgho-ops:

   ```caddy
   handle /bot/* {
       uri strip_prefix /bot
       reverse_proxy bot-api:8000
   }
   ```

   `bot-api` es el alias del bot en `cgho_net` — **no** `api`, que en esa red
   resuelve indistintamente a este contenedor o al del sistema de tickets,
   porque los dos composes llaman `api` a su servicio.

2. **`META_APP_SECRET` y `META_VERIFY_TOKEN` con valor.** No hacen falta
   credenciales de Meta reales, pero sí que no estén vacíos: con un App Secret
   vacío el HMAC es calculable por cualquiera y el webhook queda abierto.
   `setup` genera valores si los encuentra vacíos.

3. **`META_GRAPH_BASE_URL` apuntando al sink.** Lo hace `setup`. La variable
   existe solo para esto: su default es Graph API y en producción se deja sin
   definir.

## Qué deja atrás

A propósito, como evidencia:

- Filas en `raw_messages` y `ticket_creations` con `wamid` `wamid.smoke.<id>.*`
- **Un ticket real** en el sistema de tickets, con marcador de prueba,
  prioridad baja y sin cliente — para que no encabece la cola de nadie ni se
  cuelgue del historial de un cliente real
- ~US$0.001 de DeepSeek

`teardown` restaura el `.env` y quita el sink, pero no borra nada de eso.

## Supuestos

Está pensado para una VM de prueba: asume que nadie más le está mandando
mensajes al bot mientras corre, porque limpia las llaves de Redis del teléfono
que usa antes de empezar.
