"""ORM models for the mcp_router schema."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.models.base import Base


class MCPServer(Base):
    __tablename__ = "mcp_server"
    __table_args__ = {"schema": settings.DB_SCHEMA}

    application_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    auth_type: Mapped[str] = mapped_column(Text, nullable=False)
    auth_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    capabilities_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    server_metadata: Mapped[dict] = mapped_column(
        # Column literally named "metadata" in Postgres; renamed on the Python
        # side to avoid clashing with SQLAlchemy Declarative's own `metadata`
        # attribute on the class. Use `mcp.server_metadata` in code.
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    owner_email: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class MCPServerHistory(Base):
    __tablename__ = "mcp_server_history"
    __table_args__ = {"schema": settings.DB_SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    application_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    op: Mapped[str] = mapped_column(Text, nullable=False)  # insert | update | delete
    before: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    after: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    changed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
