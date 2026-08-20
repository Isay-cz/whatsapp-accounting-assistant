# Diseño del flujo de negocio — Bot de WhatsApp (despacho)

Complementa a `whatsapp-webhook-reference.md` (ese cubre qué manda Meta; este cubre qué hace el bot con eso). Son decisiones de diseño, todavía no implementadas.

---

## 1. Modelo Human-in-the-Loop y Degradación Elegante

**El flujo:** los empleados actúan como primer filtro, reenviando los mensajes de los clientes al bot. Reduce la fricción operativa para personal en movimiento.

**Diseño pragmático:** el bot hace el trabajo pesado básico — parseo de keywords, guardar el texto crudo, asociar al cliente correcto. Las correcciones complejas o la captura de datos detallados se delegan a la plataforma web, para no terminar construyendo una interfaz de formularios dentro de un chat de texto (frustrante y propenso a errores).

---

## 2. Buffer de mensajes (ventana de reenvío)

**El problema:** los usuarios no mandan todo en un solo bloque — llegan en ráfagas de mensajes fragmentados, lo que crearía tickets inconexos si cada mensaje se procesara por separado.

**La solución (patrón debounce):** un temporizador (30-60 segundos) asociado al número del empleado. Cada mensaje nuevo reinicia el contador y se concatena en un buffer temporal. Cuando el tiempo expira sin mensajes nuevos, el bloque completo se manda a procesar como un único ticket con todo el contexto junto.

**Decisión pendiente — dónde vive el buffer:**
- **En memoria (dict + asyncio, dentro del mismo proceso de FastAPI):** cero infraestructura extra, funciona bien para un solo worker. Se pierde el buffer si el proceso se reinicia a medio conteo.
- **Redis:** persistente, preparado para más de un worker/réplica en el futuro. Es un contenedor adicional en el docker-compose del bot — evaluar si se justifica al volumen actual.

---

## 3. Asignación de cliente (mensajes interactivos)

**La ventana de 24 horas:** como el empleado inicia la interacción al reenviar el mensaje, se abre una sesión de servicio al cliente de 24h — dentro de esa ventana no se necesitan plantillas de pago para que el bot responda (mensajes de formato libre).

**Búsqueda y desambiguación:** al terminar de recibirse el/los mensaje(s) del ticket (después de que expira el buffer), el bot busca si hay un nombre mencionado y lo compara contra la base de clientes:
- **Una sola coincidencia** → se asigna directo.
- **Varias coincidencias** (mismo nombre, distinto apellido, por ejemplo) → el bot responde con una lista para que el empleado elija.

**Mecanismo — Listas y Botones:**
- Hasta 10 opciones → **Mensaje de Lista**.
- Hasta 3 opciones → **Botones de Respuesta Rápida**.

Esto garantiza que el webhook reciba un **ID exacto** (dato estructurado) al hacer clic, en vez de texto libre — elimina errores de escritura y facilita la asignación directa en base de datos.

**Estructura esperada del webhook al responder** (según documentación de Meta, aún no probada — verificar contra un payload real cuando se implemente):
```json
{
  "type": "interactive",
  "interactive": {
    "type": "list_reply",
    "list_reply": { "id": "cliente_482", "title": "Juan Pérez López" }
  }
}
```
El `id` lo define el bot al construir la lista/botones — ahí se mete el ID del cliente en la base, para que la respuesta sea inequívoca sin importar qué tan parecidos sean los nombres.
