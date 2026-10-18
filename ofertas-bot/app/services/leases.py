"""Database leases shared by HTTP calls, schedulers and independent processes."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import MonitoringLease

SessionFactory = Callable[[], Session]
Clock = Callable[[], datetime]


def utc_now(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("O relógio de monitoramento deve informar um fuso horário")
    return value.astimezone(UTC)


def conflict_insert(session: Session, model):
    """Use the atomic conflict handling supported by both configured databases."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql_insert(model)
    if dialect == "sqlite":
        return sqlite_insert(model)
    raise ValueError("Leases exigem PostgreSQL ou SQLite")


def acquire_lease(
    session_factory: SessionFactory,
    name: str,
    ttl_seconds: float,
    clock: Clock = lambda: datetime.now(UTC),
) -> str | None:
    """Claim a new/expired row atomically, without overwriting a live owner."""
    if not name or len(name) > 100 or ttl_seconds <= 0:
        raise ValueError("Nome e prazo de lease inválidos")
    now = utc_now(clock)
    owner = uuid4().hex
    expires_at = now + timedelta(seconds=float(ttl_seconds))
    with session_factory() as session:
        statement = (
            conflict_insert(session, MonitoringLease)
            .values(name=name, owner=owner, expires_at=expires_at, updated_at=now)
            .on_conflict_do_nothing(index_elements=[MonitoringLease.name])
        )
        inserted = session.execute(statement).rowcount
        if not inserted:
            claimed = session.execute(
                update(MonitoringLease)
                .where(MonitoringLease.name == name, MonitoringLease.expires_at <= now)
                .values(owner=owner, expires_at=expires_at, updated_at=now)
            ).rowcount
            if not claimed:
                session.rollback()
                return None
        session.commit()
    return owner


def renew_lease(
    session: Session,
    name: str,
    owner: str,
    ttl_seconds: float,
    clock: Clock = lambda: datetime.now(UTC),
) -> bool:
    """Fence a transaction: only the still-current, unexpired owner may commit."""
    if ttl_seconds <= 0:
        raise ValueError("Prazo de lease inválido")
    now = utc_now(clock)
    result = session.execute(
        update(MonitoringLease)
        .where(
            MonitoringLease.name == name,
            MonitoringLease.owner == owner,
            MonitoringLease.expires_at > now,
        )
        .values(expires_at=now + timedelta(seconds=float(ttl_seconds)), updated_at=now)
    )
    return bool(result.rowcount)


def release_lease(session_factory: SessionFactory, name: str, owner: str) -> bool:
    """An old worker must never release a lease acquired by its successor."""
    with session_factory() as session:
        result = session.execute(
            delete(MonitoringLease).where(
                MonitoringLease.name == name, MonitoringLease.owner == owner
            )
        )
        session.commit()
        return bool(result.rowcount)


def lease_expires_at(session_factory: SessionFactory, name: str) -> datetime | None:
    with session_factory() as session:
        return session.scalar(
            select(MonitoringLease.expires_at).where(MonitoringLease.name == name)
        )
