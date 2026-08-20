import uuid
from datetime import datetime
from sqlalchemy import (
    String, Boolean, Text,
    DateTime, ForeignKey, text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


class Worker(Base):
    """
    Whitelist de trabajadores del despacho. Se mantiene sincronizada desde
    users.bot_enabled + whatsapp_phone del sistema de tickets — mecanismo que
    todavía no existe del otro lado (ver CLAUDE.md, Pendientes), así que hoy
    se edita a mano.

    external_user_id / external_department_id son referencias externas al
    sistema de tickets (UUID de sus tablas `users` / `departments`), sin FK
    real porque viven en otra base de datos. Son un stopgap manual para poder
    mandar `created_by` y el departamento al crear un ticket mientras no
    exista el sync — ver CLAUDE.md, decisión #14.
    """
    __tablename__ = "workers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    external_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    external_department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    raw_messages: Mapped[list["RawMessage"]] = relationship(back_populates="worker")


class RawMessage(Base):
    """
    Mensaje crudo tal como llegó del webhook de Cloud API, antes de cualquier
    procesamiento (auditoría y reintento en caso de fallo del buffer/LLM).

    external_ticket_id es una referencia externa (UUID del ticket creado en
    el sistema de tickets), sin FK real — igual que en Worker, vive en otra
    base de datos.
    """
    __tablename__ = "raw_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workers.id"), nullable=False)
    wamid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    external_ticket_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    worker: Mapped["Worker"] = relationship(back_populates="raw_messages")
