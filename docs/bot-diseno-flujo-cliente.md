# Diseño del flujo de negocio — Bot de WhatsApp (despacho)

Complementa a `whatsapp-webhook-reference.md` (ese cubre qué manda Meta; este cubre qué hace el bot con eso).

> **Documento de diseño inicial.** El flujo ya está implementado y lo vigente son las decisiones numeradas de `CLAUDE.md`. Donde este texto y `CLAUDE.md` difieran, manda `CLAUDE.md`. Lo que cambió respecto del diseño original está marcado abajo.

---

## 1. Modelo Human-in-the-Loop y Degradación Elegante

**El flujo:** los empleados actúan como primer filtro, reenviando los mensajes de los clientes al bot. Reduce la fricción operativa para personal en movimiento.

**Diseño pragmático:** el bot hace el trabajo pesado básico — parseo de keywords, guardar el texto crudo, asociar al cliente correcto. Las correcciones complejas o la captura de datos detallados se delegan a la plataforma web, para no terminar construyendo una interfaz de formularios dentro de un chat de texto (frustrante y propenso a errores).

---

## 2. Buffer de mensajes (ventana de reenvío)

**El problema:** los usuarios no mandan todo en un solo bloque — llegan en ráfagas de mensajes fragmentados, lo que crearía tickets inconexos si cada mensaje se procesara por separado.

**La solución (patrón debounce):** un temporizador (30-60 segundos) asociado al número del empleado. Cada mensaje nuevo reinicia el contador y se concatena en un buffer temporal. Cuando el tiempo expira sin mensajes nuevos, el bloque completo se manda a procesar como un único ticket con todo el contexto junto.

**Dónde vive el buffer — decidido: Redis.** El contenido del bloque y el estado de la conversación viven en Redis (el del stack de cgho-ops, con el prefijo de llave `bot:`), así que sobreviven a un reinicio; lo que corre en memoria son solo los vigilantes que esperan el cierre, y se vuelven a levantar al arrancar. La alternativa descartada era guardar el buffer en memoria, que se perdía con cada reinicio.

---

## 3. Asignación de cliente (mensajes interactivos)

**La ventana de 24 horas:** como el empleado inicia la interacción al reenviar el mensaje, se abre una sesión de servicio al cliente de 24h — dentro de esa ventana no se necesitan plantillas de pago para que el bot responda (mensajes de formato libre).

**Búsqueda y desambiguación** *(cambió respecto del diseño original: decisión #5 de `CLAUDE.md`)*: al cerrar el buffer, el bot **le pregunta al empleado** a qué cliente corresponde. No busca un nombre dentro del mensaje ni lo extrae con el LLM. Lo que el empleado escribe se manda a la búsqueda de clientes del sistema de tickets:
- **Una o varias coincidencias** → el bot responde con las opciones más una fija, «Sin cliente», y el empleado elige. **Nunca se asigna directo**, ni con una sola coincidencia: escogerla sola sería adivinar.
- **Ninguna coincidencia** → sigue a la prioridad, y el ticket queda sin cliente.

**Mecanismo — Listas y Botones:**
- Hasta 10 opciones (9 coincidencias más «Sin cliente») → **Mensaje de Lista**.
- Hasta 3 opciones → **Botones de Respuesta Rápida**. La prioridad (alta, media, baja) usa botones.

Esto garantiza que el webhook reciba un **ID exacto** (dato estructurado) al hacer clic, en vez de texto libre — elimina errores de escritura y facilita la asignación directa en base de datos.

**Estructura del webhook al responder** (los payloads de prueba están en `api/tests/fixtures/webhook_payloads/`):
```json
{
  "type": "interactive",
  "interactive": {
    "type": "list_reply",
    "list_reply": { "id": "cliente_482", "title": "Juan Pérez López" }
  }
}
```
El `id` lo define el bot al construir la lista/botones — ahí va el UUID del cliente en el sistema de tickets (el ejemplo de arriba usa un id ilustrativo), para que la respuesta sea inequívoca sin importar qué tan parecidos sean los nombres.
