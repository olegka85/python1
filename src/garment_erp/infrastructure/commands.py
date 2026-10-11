import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from garment_erp.domain.errors import BusinessError
from garment_erp.infrastructure.database import Database
from garment_erp.infrastructure.models import IdempotencyRecord
from garment_erp.infrastructure.repository import SQLFactoryRepository


def digest(body: dict) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def insert_for(session, model):
    dialect = session.bind.dialect.name
    if dialect == "postgresql":
        return pg_insert(model)
    if dialect == "sqlite":
        return sqlite_insert(model)
    raise RuntimeError(f"Unsupported SQL dialect: {dialect}")


@dataclass(frozen=True)
class CommandResult:
    payload: dict
    replayed: bool


class CommandExecutor:
    def __init__(self, db: Database):
        self.db = db

    async def execute(
        self,
        tenant: str,
        actor: str,
        scope: str,
        key: str,
        body: dict,
        handler: Callable[[SQLFactoryRepository], Awaitable[dict]],
    ) -> CommandResult:
        identity = {"tenant_id": tenant, "principal_id": actor, "scope": scope, "key": key}
        predicate = [getattr(IdempotencyRecord, k) == v for k, v in identity.items()]
        body_hash = digest(body)
        try:
            async with self.db.sessions() as session, session.begin():
                result = await session.execute(
                    insert_for(session, IdempotencyRecord)
                    .values(**identity, body_hash=body_hash, response={})
                    .on_conflict_do_nothing(index_elements=list(identity))
                )
                if result.rowcount == 0:
                    record = await session.scalar(select(IdempotencyRecord).where(*predicate))
                    if record is None or record.body_hash != body_hash:
                        raise BusinessError(
                            "idempotency_conflict", "This key was used with another request body"
                        )
                    return CommandResult(record.response, True)
                repo = SQLFactoryRepository(session, tenant, actor)
                payload = await handler(repo)
                await session.execute(
                    update(IdempotencyRecord).where(*predicate).values(response=payload)
                )
                return CommandResult(payload, False)
        except IntegrityError as exc:
            # Never expose SQL, parameters or connection details in HTTP errors.
            raise BusinessError(
                "constraint_conflict", "Duplicate reference/SKU/receipt or invalid relationship"
            ) from exc
