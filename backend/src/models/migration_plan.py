"""MigrationPlan：迁移计划（一次季度降冷动作的汇总与审批载体）。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.config.settings import settings
from src.models.base import Base, TimestampMixin, new_uuid


class MigrationPlan(Base, TimestampMixin):
    __tablename__ = "migration_plans"
    __table_args__ = {"schema": settings.db_schema}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    threshold: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=20)
    # 测算依据（basis）：上次测算时的策略 / 口径 / 访问汇总 / 表档案 / as_of。
    # basis_fingerprint 与当前依据不一致 -> 草稿失效（不可提交、不可导出）；
    # 提交时把当时依据固化，后续数据变化不再改写已提交计划。
    basis_as_of: Mapped[str | None] = mapped_column(String(10), nullable=True)
    basis_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    basis_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    basis_reasons: Mapped[str] = mapped_column(Text, nullable=False, default="")
    basis_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 复检前保留的「上次结果」，供页面对比（basis_previous 存汇总 + 逐表快照）
    previous_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    previous_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_size_gb: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    save_month: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    save_year: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    migration_cost: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    net_save_year: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    avg_payback: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    risk_block_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_warn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    submitted_by: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rechecked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
