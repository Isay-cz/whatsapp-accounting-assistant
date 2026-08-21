import uuid
from datetime import datetime
from sqlalchemy import (
    String, Boolean, Text, Integer,
    DateTime, ForeignKey, text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


class Worker(Base):
    """
    Whitelist de trabajadores del despacho. Se mantiene sincronizada por poll
    contra GET /internal/workers del sistema de tickets, que la arma con
    users.bot_enabled + whatsapp_phone (ver CLAUDE.md, decisión #15). El
    `is_active` local se calcula como `bot_enabled AND is_active` del payload.

    external_user_id es una referencia externa al sistema de tickets (el UUID
    de su tabla `users`), sin FK real porque vive en otra base de datos. Es lo
    que se manda como `created_by` al crear un ticket, para que el actor de
    todos los eventos sea una persona real y nunca "el bot".

    phone_number se guarda normalizado a solo dígitos: el sistema de tickets
    manda E.164 con `+` y Meta manda el número sin `+`. Ver services/
    ticket_system/sync.py.
    """
    __tablename__ = "workers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    external_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    raw_messages: Mapped[list["RawMessage"]] = relationship(back_populates="worker")


class RawMessage(Base):
    """
    Mensaje crudo tal como llegó del webhook de Cloud API, antes de cualquier
    procesamiento (auditoría y reintento en caso de fallo del buffer/LLM).

    ticket_creation_id apunta al intento de creación que generó este bloque
    de mensajes (N mensajes -> 1 intento). Reemplazó a external_ticket_id,
    que era una referencia suelta al otro sistema y que nadie llegó a
    escribir: ahora el ticket se alcanza por el join, y de paso queda
    registrado el intento aunque haya fallado (CLAUDE.md, decisión #20).
    """
    __tablename__ = "raw_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workers.id"), nullable=False)
    wamid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ticket_creation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ticket_creations.id")
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    worker: Mapped["Worker"] = relationship(back_populates="raw_messages")
    ticket_creation: Mapped["TicketCreation | None"] = relationship(
        back_populates="raw_messages"
    )


class TicketCreation(Base):
    """
    Bitácora de los tickets que este bot mandó crear — **no** un espejo del
    ticket (CLAUDE.md, decisión #20). Guarda lo que se envió en el momento
    de crear, más la identidad que devolvió el sistema de tickets. Si allá
    alguien renombra o reasigna el ticket, esta tabla no se entera, y eso es
    correcto: es un registro de auditoría, no una caché.

    Se escribe también cuando la creación falla (`status='failed'` con el
    error), que es justo el caso que de otro modo no deja rastro en ningún
    lado.

    No guarda la descripción: se reconstruye desde los `raw_messages`
    vinculados más `title` y `entities`, que es lo único de la descripción
    que no sale del texto crudo.

    external_ticket_id y client_id son referencias externas sin FK real —
    viven en la base del sistema de tickets.
    """
    __tablename__ = "ticket_creations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workers.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    entities: Mapped[dict | None] = mapped_column(JSONB)
    priority: Mapped[str] = mapped_column(String(10), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    external_ticket_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ticket_number: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    worker: Mapped["Worker"] = relationship()
    raw_messages: Mapped[list["RawMessage"]] = relationship(back_populates="ticket_creation")
