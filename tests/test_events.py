import asyncio

import pytest
from sqlalchemy import select

from garment_erp.domain.errors import BusinessError
from garment_erp.infrastructure.events import (
    DatabaseProjection,
    claim_event,
    process_one,
    settle_event,
)
from garment_erp.infrastructure.models import DailyOutput, OutboxEvent, new_id, now


async def add_event(db, kind="production.completed", units=10):
    event = {
        "id": new_id(),
        "tenant_id": "factory-a",
        "kind": kind,
        "aggregate_id": new_id(),
        "schema_version": 1,
        "payload": {"units": units},
        "created_at": now(),
    }
    async with db.sessions() as session, session.begin():
        session.add(OutboxEvent(**event))
    return event


async def test_inbox_deduplicates_output_and_rejects_changed_payload(harness):
    event = await add_event(harness.db)
    consumer = DatabaseProjection(harness.db)
    await consumer.publish(event)
    await consumer.publish(event)
    with pytest.raises(BusinessError, match="different content"):
        await consumer.publish({**event, "payload": {"units": 20}})
    async with harness.db.sessions() as session:
        rows = (await session.scalars(select(DailyOutput))).all()
        assert len(rows) == 1 and rows[0].units == 10


async def test_expired_lease_can_be_reclaimed_and_old_worker_cannot_ack(harness):
    event = await add_event(harness.db)
    timestamp = now() + 1
    first = await claim_event(harness.db, 10, 5, timestamp)
    assert first["id"] == event["id"] and first["attempts"] == 1
    assert await claim_event(harness.db, 10, 5, timestamp + 5) is None
    second = await claim_event(harness.db, 10, 5, timestamp + 11)
    assert second["attempts"] == 2 and second["claim"] != first["claim"]
    assert await settle_event(harness.db, first, True, 5, timestamp + 12) is False
    assert await settle_event(harness.db, second, True, 5, timestamp + 12) is True


async def test_delivery_failure_retries_with_backoff_then_dead_letters(harness):
    event = await add_event(harness.db)

    class BrokenPublisher:
        async def publish(self, event):
            raise RuntimeError("provider unavailable")

    assert await process_one(harness.db, BrokenPublisher(), max_attempts=2)
    async with harness.db.sessions() as session:
        failed = await session.get(OutboxEvent, event["id"])
        assert failed.state == "PENDING" and failed.attempts == 1
        due = failed.available_at
    second = await claim_event(harness.db, 10, 2, due)
    assert second["attempts"] == 2
    await settle_event(harness.db, second, False, 2, due)
    async with harness.db.sessions() as session:
        assert (await session.get(OutboxEvent, event["id"])).state == "DEAD"


async def test_crashes_exhausting_attempts_do_not_strand_processing_events(harness):
    event = await add_event(harness.db)
    timestamp = now() + 1
    await claim_event(harness.db, 10, 1, timestamp)
    assert await claim_event(harness.db, 10, 1, timestamp + 11) is None
    async with harness.db.sessions() as session:
        assert (await session.get(OutboxEvent, event["id"])).state == "DEAD"


async def test_concurrent_workers_claim_an_event_once(harness):
    event = await add_event(harness.db)
    claims = await asyncio.gather(*[claim_event(harness.db, 30, 5) for _ in range(3)])
    assert sum(x is not None for x in claims) == 1
    assert next(x for x in claims if x)["id"] == event["id"]


async def test_consumer_failure_rolls_back_inbox_so_fixed_event_can_retry(harness):
    event = await add_event(harness.db, units=-1)
    consumer = DatabaseProjection(harness.db)
    with pytest.raises(BusinessError):
        await consumer.publish(event)
    await consumer.publish({**event, "payload": {"units": 3}})
    async with harness.db.sessions() as session:
        assert (await session.scalars(select(DailyOutput))).one().units == 3
