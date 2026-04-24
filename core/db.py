"""
Postgres layer for PRism — SQLAlchemy 2.0 async ORM.

Owns a single async engine / sessionmaker for the life of the server.

Schema:
  pr_reviews   — every generated review, keyed on (repo, pr_number, head_sha)
                 so we can diff "what changed since the last review" on the
                 next push instead of re-reviewing already-seen code.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.logger import get_logger

logger = get_logger("prism.db")


class Base(DeclarativeBase):
    pass


class PRReview(Base):
    __tablename__ = "pr_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String, nullable=False, index=True)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    head_sha: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def _to_async_dsn(dsn: str) -> str:
    # SQLAlchemy needs an explicit async driver prefix.
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgres://"):
        return "postgresql+asyncpg://" + dsn[len("postgres://") :]
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    return dsn


class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = _to_async_dsn(dsn)
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        if self._engine is not None:
            return
        logger.info("Opening SQLAlchemy async engine")
        self._engine = create_async_engine(
            self._dsn, pool_size=5, pool_pre_ping=True
        )
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Postgres schema ready")

    async def close(self) -> None:
        if self._engine is None:
            return
        await self._engine.dispose()
        self._engine = None
        self._sessionmaker = None
        logger.info("SQLAlchemy engine disposed")

    def session(self) -> AsyncSession:
        if self._sessionmaker is None:
            raise RuntimeError("Database not initialised")
        return self._sessionmaker()

    async def get_last_review(
        self, repo: str, pr_number: int
    ) -> dict[str, Any] | None:
        async with self.session() as session:
            stmt = (
                select(PRReview)
                .where(PRReview.repo == repo, PRReview.pr_number == pr_number)
                .order_by(PRReview.created_at.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return {
                "head_sha": row.head_sha,
                "body": row.body,
                "created_at": row.created_at,
            }

    async def save_review(
        self,
        repo: str,
        pr_number: int,
        head_sha: str,
        body: str,
    ) -> None:
        async with self.session() as session:
            session.add(
                PRReview(
                    repo=repo,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    body=body,
                )
            )
            await session.commit()


_db: Database | None = None


async def init_db() -> Database | None:
    """
    Idempotently initialise the module-level engine from DATABASE_URL.
    Returns None when DATABASE_URL is absent so the server can still boot
    for users who only want the stateless tools.
    """
    global _db
    if _db is not None:
        return _db
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        logger.warning("DATABASE_URL not set — stateful review memory disabled")
        return None
    db = Database(dsn)
    await db.connect()
    _db = db
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def get_db() -> Database | None:
    return _db
