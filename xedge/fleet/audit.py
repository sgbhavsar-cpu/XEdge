"""Manager-side admin action audit log (Sprint P2, XEDGE-505). See
`xedge.fleet.db_models.FleetAuditLogEntry`'s docstring for why this is a
tenant-scoped Postgres table rather than a literal port of
`xedge.observability.audit_log.AuditLog`'s hash-chained JSONL file.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from xedge.fleet.db_models import FleetAuditLogEntry


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    created_at: datetime
    actor: str
    event: str
    details: dict[str, Any]


class FleetAuditLog:
    def __init__(self, engine: AsyncEngine) -> None:
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def record(
        self, tenant_id: str, actor: str, event: str, details: dict[str, Any] | None = None
    ) -> None:
        async with self._sessionmaker() as session, session.begin():
            session.add(
                FleetAuditLogEntry(
                    tenant_id=uuid.UUID(tenant_id),
                    created_at=datetime.now(UTC),
                    actor=actor,
                    event=event,
                    details=details or {},
                )
            )

    async def tail(
        self,
        tenant_id: str,
        *,
        since_seq: int | None = None,
        actor: str | None = None,
        event_prefix: str | None = None,
        limit: int = 200,
    ) -> list[AuditEntry]:
        """Most recent `limit` entries for this tenant, newest first,
        optionally filtered to `id > since_seq` / an exact `actor` / an
        `event` prefix -- matches the query-param vocabulary of the
        device-local Web UI's `GET /api/v1/audit`."""
        tenant_uuid = uuid.UUID(tenant_id)
        query = select(FleetAuditLogEntry).where(FleetAuditLogEntry.tenant_id == tenant_uuid)
        if since_seq is not None:
            query = query.where(FleetAuditLogEntry.id > since_seq)
        if actor is not None:
            query = query.where(FleetAuditLogEntry.actor == actor)
        if event_prefix is not None:
            query = query.where(FleetAuditLogEntry.event.startswith(event_prefix))
        query = query.order_by(FleetAuditLogEntry.id.desc()).limit(limit)
        async with self._sessionmaker() as session:
            result = await session.execute(query)
            return [
                AuditEntry(
                    id=r.id,
                    created_at=r.created_at,
                    actor=r.actor,
                    event=r.event,
                    details=r.details,
                )
                for r in result.scalars().all()
            ]
