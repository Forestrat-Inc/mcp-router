"""initial: create mcp_router schema and tables

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-16

Greenfield schema for the MCP router service:

  mcp_server          — application_id → MCP endpoint mapping
  mcp_server_history  — audit trail (INSERT/UPDATE/DELETE snapshot pairs)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "mcp_router"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.create_table(
        "mcp_server",
        # application_id is our natural key (one MCP server per application);
        # comes from aims-incident-service's core.applications.id but we do
        # NOT declare a hard FK to that schema — cross-schema FKs make the
        # multi-service split brittle. Callers validate the id exists at
        # write time; we trust it thereafter.
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("name", sa.Text, nullable=False),
        # 'http' | 'sse' | 'ws' | 'stdio' — only 'http' is fully wired in v1.
        # The other transports are placeholders so we can reject with a clear
        # message rather than fail-open.
        sa.Column("transport", sa.Text, nullable=False),
        sa.Column("endpoint_url", sa.Text, nullable=False),
        # 'none' | 'bearer' | 'oauth' | 'mtls' | 'header'
        sa.Column("auth_type", sa.Text, nullable=False),
        # A KV URI (`kv://forestrat-kv/secret-name`) or an inline placeholder
        # for `auth_type='none'`. NEVER a plaintext secret — router refuses at
        # write time if this looks like one (rudimentary shape check).
        sa.Column("auth_ref", sa.Text, nullable=True),
        # Server-declared capabilities from aims_discover_capabilities.
        # Populated at registration and refreshed on cache miss.
        sa.Column(
            "capabilities_snapshot",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Free-form owner/support metadata; not indexed.
        sa.Column(
            "metadata",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # 'active' | 'disabled' | 'deprecated'
        # A 'disabled' entry stays visible to ADMIN reads but is returned as
        # 404 to non-admin lookup callers (see api/apps.py).
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("owner_email", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_by", sa.String(255), nullable=True),
        schema=SCHEMA,
    )

    # For admin listings filtered by status='active' — cheap partial index.
    op.execute(
        f"CREATE INDEX idx_mcp_server_active ON {SCHEMA}.mcp_server(application_id) "
        f"WHERE status = 'active'"
    )

    # ── Audit shadow table ─────────────────────────────────────────────
    op.create_table(
        "mcp_server_history",
        sa.Column(
            "id", sa.BigInteger, primary_key=True, autoincrement=True
        ),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        # 'insert' | 'update' | 'delete'
        sa.Column("op", sa.Text, nullable=False),
        sa.Column("before", postgresql.JSONB, nullable=True),
        sa.Column("after", postgresql.JSONB, nullable=True),
        sa.Column("changed_by", sa.String(255), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema=SCHEMA,
    )
    op.execute(
        f"CREATE INDEX idx_mcp_server_history_app_time "
        f"ON {SCHEMA}.mcp_server_history(application_id, changed_at DESC)"
    )


def downgrade() -> None:
    op.drop_table("mcp_server_history", schema=SCHEMA)
    op.drop_table("mcp_server", schema=SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
