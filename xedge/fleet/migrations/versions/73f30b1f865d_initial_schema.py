"""initial schema: tenants, devices, join_tokens

Revision ID: 73f30b1f865d
Revises:
Create Date: 2026-07-31 19:14:07.686833

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "73f30b1f865d"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "devices",
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_version", sa.Text(), nullable=True),
        sa.Column("heartbeat_interval_seconds", sa.Float(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("driver_count", sa.Integer(), nullable=True),
        sa.Column("uptime_seconds", sa.Float(), nullable=True),
        sa.Column("last_config_apply_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("pending_config_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("pending_config_version", sa.Integer(), nullable=False),
        sa.Column("cert_serial_number", sa.Text(), nullable=True),
        sa.Column("cert_not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("serial_number", sa.Text(), nullable=True),
        sa.Column("make", sa.Text(), nullable=True),
        sa.Column("protocol", sa.Text(), nullable=True),
        sa.Column("hardware_firmware_version", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("device_id"),
    )
    op.create_index("ix_devices_tenant_id", "devices", ["tenant_id"], unique=False)
    op.create_table(
        "join_tokens",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_join_tokens_tenant_id", "join_tokens", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_join_tokens_tenant_id", table_name="join_tokens")
    op.drop_table("join_tokens")
    op.drop_index("ix_devices_tenant_id", table_name="devices")
    op.drop_table("devices")
    op.drop_table("tenants")
