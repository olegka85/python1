import logging
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import and_, or_, select, update

from garment_erp.domain.errors import BusinessError
from garment_erp.infrastructure.commands import digest, insert_for
from garment_erp.infrastructure.database import Database
from garment_erp.infrastructure.models import DailyOutput, InboxRecord, OutboxEvent, new_id, now

logger = logging.getLogger(__name__)


class Publisher(Protocol):
    async def publish(self, event: dict) -> None: ...


class DatabaseProjection:
    """Local event consumer. Inbox receipt and report mutation share one transaction."""

    def __init__(self, db: Database):
        self.db = db

    async def publish(self, event: dict) -> None:
        if event["schema_version"] != 1:
            raise BusinessError("unsupported_event", "Unsupported event schema version")
        body_hash = digest(event)
        async with self.db.sessions() as session, session.begin():
            identity = {"tenant_id": event["tenant_id"], "event_id": event["id"]}
            inserted = await session.execute(
                insert_for(session, InboxRecord)
                .values(**identity, body_hash=body_hash)
                .on_conflict_do_nothing(index_elements=list(identity))
            )
            if inserted.rowcount == 0:
                record = await session.get(InboxRecord, identity)
                if record.body_hash != body_hash:
                    raise BusinessError("event_conflict", "Event ID reused with different content")
                return
            if event["kind"] == "production.completed":
                units = event["payload"].get("units")
                if type(units) is not int or units <= 0:
                    raise BusinessError("invalid_event", "Output units must be positive integers")
                day = datetime.fromtimestamp(event["created_at"], UTC).date().isoformat()
                await session.execute(
                    insert_for(session, DailyOutput)
                    .values(tenant_id=event["tenant_id"], day=day, units=units)
                    .on_conflict_do_update(
                        index_elements=["tenant_id", "day"],
                        set_={"units": DailyOutput.units + units},
                    )
                )


async def claim_event(
    db: Database, lease_seconds: int, max_attempts: int, timestamp: int | None = None
) -> dict | None:
    timestamp = now() if timestamp is None else timestamp
    due = or_(
        and_(OutboxEvent.state == "PENDING", OutboxEvent.available_at <= timestamp),
        and_(OutboxEvent.state == "PROCESSING", OutboxEvent.lease_until <= timestamp),
    )
    async with db.sessions() as session, session.begin():
        await session.execute(
            update(OutboxEvent)
            .where(due, OutboxEvent.attempts >= max_attempts)
            .values(state="DEAD", claimed_by=None)
        )
        event_id = await session.scalar(
            select(OutboxEvent.id)
            .where(due, OutboxEvent.attempts < max_attempts)
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if event_id is None:
            return None
        claim = new_id()
        result = await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == event_id, due)
            .values(
                state="PROCESSING",
                claimed_by=claim,
                lease_until=timestamp + lease_seconds,
                attempts=OutboxEvent.attempts + 1,
            )
            .returning(
                OutboxEvent.id,
                OutboxEvent.tenant_id,
                OutboxEvent.kind,
                OutboxEvent.aggregate_id,
                OutboxEvent.payload,
                OutboxEvent.schema_version,
                OutboxEvent.created_at,
                OutboxEvent.attempts,
            )
        )
        row = result.mappings().first()
        if row is None:
            return None
        return {**dict(row), "claim": claim}


async def settle_event(
    db: Database, event: dict, succeeded: bool, max_attempts: int, timestamp: int | None = None
) -> bool:
    timestamp = now() if timestamp is None else timestamp
    state = (
        "PUBLISHED" if succeeded else ("DEAD" if event["attempts"] >= max_attempts else "PENDING")
    )
    async with db.sessions() as session, session.begin():
        result = await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event["id"],
                OutboxEvent.state == "PROCESSING",
                OutboxEvent.claimed_by == event["claim"],
            )
            .values(
                state=state,
                claimed_by=None,
                lease_until=0,
                available_at=timestamp + min(2 ** event["attempts"], 300),
            )
        )
        return result.rowcount == 1


async def process_one(
    db: Database, publisher: Publisher, lease_seconds: int = 30, max_attempts: int = 5
) -> bool:
    event = await claim_event(db, lease_seconds, max_attempts)
    if event is None:
        return False
    envelope = {k: v for k, v in event.items() if k not in {"claim", "attempts"}}
    try:
        await publisher.publish(envelope)
    except Exception as exc:
        logger.warning(
            "event_delivery_failed",
            extra={"event_id": event["id"], "error_type": type(exc).__name__},
        )
        await settle_event(db, event, False, max_attempts)
    else:
        await settle_event(db, event, True, max_attempts)
    return True
