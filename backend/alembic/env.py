import asyncio

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.core.config import get_settings


def _run(connection: Connection) -> None:
    # Bodies of the row_security=off definer functions can only be planned by their final owner
    # (rag_definer, BYPASSRLS); as rag_owner, validating them at CREATE fails (ADR 0009).
    # pg_dump restores with the same setting; the tests exercise every body.
    connection.exec_driver_sql("SET check_function_bodies = off")
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def _main() -> None:
    engine = create_async_engine(get_settings().migration_database_url.get_secret_value())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


asyncio.run(_main())
