"""join token revocation, device config and certificate history

Revision ID: 25c58a9f802b
Revises: f4b009717dbc
Create Date: 2026-08-03 23:47:01.807521

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "25c58a9f802b"
down_revision: str | Sequence[str] | None = "f4b009717dbc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_certificate_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("serial_number", sa.Text(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.device_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_device_certificate_history_tenant_id_device_id",
        "device_certificate_history",
        ["tenant_id", "device_id"],
        unique=False,
    )
    op.create_table(
        "device_config_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("config_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pushed_by", sa.Text(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("apply_success", sa.Boolean(), nullable=True),
        sa.Column("apply_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.device_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_device_config_history_device_id_version",
        "device_config_history",
        ["device_id", "config_version"],
        unique=True,
    )
    op.create_index(
        "ix_device_config_history_tenant_id_device_id",
        "device_config_history",
        ["tenant_id", "device_id"],
        unique=False,
    )
    op.add_column("join_tokens", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("join_tokens", sa.Column("revoked_by", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("join_tokens", "revoked_by")
    op.drop_column("join_tokens", "revoked_at")
    op.drop_index(
        "ix_device_config_history_tenant_id_device_id", table_name="device_config_history"
    )
    op.drop_index("ix_device_config_history_device_id_version", table_name="device_config_history")
    op.drop_table("device_config_history")
    op.drop_index(
        "ix_device_certificate_history_tenant_id_device_id", table_name="device_certificate_history"
    )
    op.drop_table("device_certificate_history")
