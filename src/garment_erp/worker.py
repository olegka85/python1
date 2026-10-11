import argparse
import asyncio
import json
import logging

from garment_erp.infrastructure.database import Database
from garment_erp.infrastructure.events import DatabaseProjection, process_one
from garment_erp.settings import Settings


async def run(once: bool, drain: bool):
    settings = Settings()
    db = Database(settings.database_url)
    publisher = DatabaseProjection(db)
    count = 0
    try:
        while True:
            processed = await process_one(
                db, publisher, settings.worker_lease_seconds, settings.worker_max_attempts
            )
            count += int(processed)
            if once or (drain and not processed):
                print(json.dumps({"claimed_events": count}))
                return
            if not processed:
                await asyncio.sleep(1)
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(
        description="Deliver outbox events to the idempotent output-report consumer"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--drain", action="store_true", help="Process currently due events then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(args.once, args.drain))


if __name__ == "__main__":
    main()
