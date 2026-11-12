"""MigrationPlanItem：计划项（表 + 测算结果 + 风险闸门结论）。MigrationPlan 的子聚合。"""
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.config.settings import settings
from src.models.base import Base, TimestampMixin, new_uuid


class MigrationPlanItem(Base, TimestampMixin):
    __tablename__ = "migration_plan_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "table_id", name="uq_migration_plan_items_plan_table"),
        {"schema": settings.db_schema},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{settings.db_schema}.migration_plans.id", ondelete="CASCADE"), index=True
    )
    table_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{settings.db_schema}.table_profiles.id", ondelete="CASCADE"), index=True
    )
    table_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    from_tier: Mapped[str] = mapped_column(String(12), nullable=False, default="hot")
    suggested_tier: Mapped[str] = mapped_column(String(12), nullable=False, default="cold")
    size_gb: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    hot_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=0)
    save_month: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    migration_cost: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    payback_months: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    net_save_year: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    risk: Mapped[str] = mapped_column(String(10), nullable=False, default="ok")
    reasons: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 上次测算结果（复检 / 追加 / 移除前保留，供页面对比；首次测算时为 None）
    prev_size_gb: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    prev_hot_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    prev_save_month: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    prev_migration_cost: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    prev_payback_months: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    prev_net_save_year: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    prev_risk: Mapped[str | None] = mapped_column(String(10), nullable=True)
    prev_reasons: Mapped[str | None] = mapped_column(Text, nullable=True)
    prev_basis_as_of: Mapped[str | None] = mapped_column(String(10), nullable=True)
    prev_policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="pending", index=True)
