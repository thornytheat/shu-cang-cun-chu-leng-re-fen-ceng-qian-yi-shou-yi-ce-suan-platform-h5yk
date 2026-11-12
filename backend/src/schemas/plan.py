"""迁移计划入出参。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PlanItemPreviousOut(BaseModel):
    size_gb: float | None = None
    hot_score: float | None = None
    save_month: float | None = None
    migration_cost: float | None = None
    payback_months: float | None = None
    net_save_year: float | None = None
    risk: str | None = None
    reasons: str | None = None
    basis_as_of: str | None = None
    policy_version: int | None = None


class PlanItemOut(BaseModel):
    id: str
    table_id: str
    table_name: str
    from_tier: str
    suggested_tier: str
    size_gb: float
    hot_score: float
    save_month: float
    migration_cost: float
    payback_months: float | None
    net_save_year: float
    risk: str
    reasons: str
    status: str
    previous: PlanItemPreviousOut | None = None


class PlanBasisOut(BaseModel):
    """测算依据状态：fresh / stale（草稿失效）/ frozen（已提交冻结）/ unknown。"""

    state: str
    is_stale: bool
    reasons: list[str] = Field(default_factory=list)
    drift: bool = False
    as_of: str | None = None
    fingerprint: str | None = None
    policy_version: int | None = None
    captured_at: str | None = None
    current_policy_version: int | None = None
    current_as_of: str | None = None
    previous: dict[str, Any] | None = None


class PlanOut(BaseModel):
    id: str
    code: str
    title: str
    status: str
    status_label: str
    policy_version: int
    threshold: float
    total_size_gb: float
    save_month: float
    save_year: float
    migration_cost: float
    net_save_year: float
    avg_payback: float | None
    risk_block_count: int
    risk_warn_count: int
    created_by: str
    submitted_by: str
    submitted_at: str | None
    rechecked_at: str | None
    note: str
    item_count: int
    items: list[PlanItemOut] = Field(default_factory=list)
    basis: PlanBasisOut
    allow_submit: bool = False
    allow_export: bool = False


class PlanCreateIn(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    threshold: float = Field(default=20, ge=0, le=100)
    table_ids: list[str] = Field(default_factory=list)
    note: str = ""


class PlanItemsIn(BaseModel):
    table_ids: list[str] = Field(min_length=1)


class PlanSummaryOut(BaseModel):
    plan_id: str
    status: str
    item_count: int
    total_size_gb: float
    save_month: float
    save_year: float
    migration_cost: float
    net_save_year: float
    avg_payback: float | None
    risk_block_count: int
    risk_warn_count: int
    rechecked_at: str | None
    basis_state: str = "unknown"
    basis_is_stale: bool = False
    basis_reasons: list[str] = Field(default_factory=list)
    basis_as_of: str | None = None
    basis_fingerprint: str | None = None
    current_basis_fingerprint: str | None = None
