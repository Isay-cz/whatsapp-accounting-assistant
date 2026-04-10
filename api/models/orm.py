import uuid
from datetime import datetime, date
from sqlalchemy import (
    String, Boolean, Text, Numeric, Date,
    DateTime, SmallInteger, Float, BigInteger,
    ForeignKey, CheckConstraint, UniqueConstraint,
    text
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    raw_messages: Mapped[list["RawMessage"]] = relationship(back_populates="worker")
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="worker")


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number: Mapped[str | None] = mapped_column(String(20), unique=True)
    name: Mapped[str | None] = mapped_column(String(120))
    rfc: Mapped[str | None] = mapped_column(String(13))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), onupdate=datetime.utcnow)

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="client")


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="department")


class RawMessage(Base):
    __tablename__ = "raw_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workers.id"), nullable=False)
    twilio_sid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    forwarded_body: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    twilio_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    worker: Mapped["Worker"] = relationship(back_populates="raw_messages")
    ticket: Mapped["Ticket | None"] = relationship(back_populates="raw_message")


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint("status IN ('abierto','en_proceso','completado')", name="chk_status"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="chk_currency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_number: Mapped[int] = mapped_column(BigInteger, autoincrement=True, unique=True)
    raw_message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("raw_messages.id"), nullable=False)
    worker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workers.id"), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("clients.id"))
    department_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"))

    request_type: Mapped[str | None] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default="abierto", nullable=False)

    amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="MXN")
    reference_date: Mapped[date | None] = mapped_column(Date)
    rfc_mentioned: Mapped[str | None] = mapped_column(String(13))
    extracted_entities: Mapped[dict | None] = mapped_column(JSONB)
    llm_provider: Mapped[str | None] = mapped_column(String(20))
    llm_confidence: Mapped[float | None] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), onupdate=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    raw_message: Mapped["RawMessage"] = relationship(back_populates="ticket")
    worker: Mapped["Worker"] = relationship(back_populates="tickets")
    client: Mapped["Client | None"] = relationship(back_populates="tickets")
    department: Mapped["Department | None"] = relationship(back_populates="tickets")