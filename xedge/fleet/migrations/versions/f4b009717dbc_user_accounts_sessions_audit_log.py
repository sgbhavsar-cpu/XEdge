"""user accounts, sessions, audit log

Revision ID: f4b009717dbc
Revises: 73f30b1f865d
Create Date: 2026-08-01 10:37:53.126161

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4b009717dbc"
down_revision: str | Sequence[str] | None = "73f30b1f865d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fleet_audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_fleet_audit_log_tenant_id_created_at",
        "fleet_audit_log",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "fleet_users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fleet_users_tenant_id", "fleet_users", ["tenant_id"], unique=False)
    op.create_index(
        "ix_fleet_users_tenant_id_username",
        "fleet_users",
        ["tenant_id", "username"],
        unique=True,
    )
    op.create_table(
        "fleet_sessions",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["fleet_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_fleet_sessions_expires_at", "fleet_sessions", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_fleet_sessions_expires_at", table_name="fleet_sessions")
    op.drop_table("fleet_sessions")
    op.drop_index("ix_fleet_users_tenant_id_username", table_name="fleet_users")
    op.drop_index("ix_fleet_users_tenant_id", table_name="fleet_users")
    op.drop_table("fleet_users")
    op.drop_index("ix_fleet_audit_log_tenant_id_created_at", table_name="fleet_audit_log")
    op.drop_table("fleet_audit_log")
