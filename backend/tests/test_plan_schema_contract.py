"""PlanOut / PlanSummaryOut 出参契约：serialize_plan 的返回必须能被响应模型原样校验。

防止「服务层加了 basis 字段、schema 没跟上」时 FastAPI 在运行时 500。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from src.constants.enums import PlanItemStatus, PlanStatus
from src.models.migration_plan import MigrationPlan
from src.models.migration_plan_item import MigrationPlanItem
from src.schemas.plan import PlanOut, PlanSummaryOut
from src.services import basis_core, basis_service, plan_service
from tests.test_basis_core import _basis


class _Db:
    def flush(self) -> None:
        pass

    def get(self, model, key):
        return None


def _plan(status: str, basis: dict, previous: dict | None = None) -> MigrationPlan:
    plan = MigrationPlan(
        id=uuid.uuid4(),
        code="MP-2026Q4-001",
        title="2026-Q4 冷存降本计划",
        status=status,
        policy_version=3,
        threshold=20,
        created_by="analyst@coldtier.io",
        submitted_by="",
        note="季度巡检基线",
    )
    plan.total_size_gb = 100.0
    plan.save_month = 100.0
    plan.save_year = 1200.0
    plan.migration_cost = 300.0
    plan.net_save_year = 900.0
    plan.avg_payback = 3.0
    plan.risk_block_count = 0
    plan.risk_warn_count = 0
    plan.rechecked_at = datetime.now(timezone.utc)
    plan.basis_fingerprint = basis_core.fingerprint(basis)
    plan.basis_snapshot = basis
    plan.basis_as_of = basis["as_of"]
    plan.basis_captured_at = datetime.now(timezone.utc)
    plan.previous_snapshot = previous
    return plan


def _item(plan: MigrationPlan) -> MigrationPlanItem:
    item = MigrationPlanItem(
        id=uuid.uuid4(),
        plan_id=plan.id,
        table_id=uuid.uuid4(),
        table_name="dwd.trade_order_detail",
        from_tier="hot",
        suggested_tier="cold",
        risk="ok",
        reasons="近 7 天零访问、无报表引用、分区数满足灰度要求",
        status=PlanItemStatus.PENDING,
    )
    item.size_gb = 100.0
    item.hot_score = 10.0
    item.save_month = 100.0
    item.migration_cost = 300.0
    item.payback_months = 3.0
    item.net_save_year = 900.0
    item.prev_size_gb = 100.0
    item.prev_hot_score = 12.0
    item.prev_save_month = 90.0
    item.prev_migration_cost = 300.0
    item.prev_payback_months = 3.3
    item.prev_net_save_year = 780.0
    item.prev_risk = "warn"
    item.prev_reasons = "被 2 张报表引用，需报表 owner 确认"
    item.prev_basis_as_of = "2026-11-01"
    item.prev_policy_version = 3
    return item


def test_plan_out_accepts_fresh_draft_payload(monkeypatch) -> None:
    basis = _basis()
    plan = _plan(PlanStatus.DRAFT, basis)
    item = _item(plan)
    monkeypatch.setattr(plan_service, "active_items", lambda _db, _id: [item])
    monkeypatch.setattr(basis_service, "current_basis", lambda _db, group=None, as_of=None: basis)

    payload = plan_service.serialize_plan(_Db(), plan, basis=basis)
    model = PlanOut.model_validate(payload)
    assert model.basis.state == "fresh"
    assert model.items[0].previous is not None
    assert model.items[0].previous.save_month == 90.0
    assert model.allow_submit is True
    assert model.allow_export is True


def test_plan_out_accepts_stale_and_frozen_payloads(monkeypatch) -> None:
    old = _basis()
    new = _basis(policy=old["policy"] | {"version": 4})
    item = _item(_plan(PlanStatus.DRAFT, old))
    monkeypatch.setattr(plan_service, "active_items", lambda _db, _id: [item])

    draft = plan_service.serialize_plan(_Db(), _plan(PlanStatus.DRAFT, old), basis=new)
    stale = PlanOut.model_validate(draft)
    assert stale.basis.is_stale is True
    assert stale.allow_submit is False

    frozen = plan_service.serialize_plan(_Db(), _plan(PlanStatus.SUBMITTED, old), basis=new)
    submitted = PlanOut.model_validate(frozen)
    assert submitted.basis.state == "frozen"
    assert submitted.basis.drift is True
    assert submitted.allow_export is True

    unknown = PlanOut.model_validate(
        plan_service.serialize_plan(_Db(), _plan(PlanStatus.DRAFT, old), with_items=False, basis=None)
    )
    assert unknown.basis.state == "unknown"


def test_plan_out_without_items_still_validates(monkeypatch) -> None:
    basis = _basis()
    monkeypatch.setattr(plan_service, "active_items", lambda _db, _id: [])
    payload = plan_service.serialize_plan(_Db(), _plan(PlanStatus.DRAFT, basis), with_items=False, basis=basis)
    model = PlanOut.model_validate(payload)
    assert model.items == []
    assert model.basis.state == "fresh"


def test_summary_out_accepts_service_payload(monkeypatch) -> None:
    basis = _basis()
    plan = _plan(PlanStatus.DRAFT, basis)
    item = _item(plan)
    monkeypatch.setattr(plan_service, "active_items", lambda _db, _id: [item])
    monkeypatch.setattr(plan_service, "_plan_or_404", lambda _db, _id: plan)
    monkeypatch.setattr(basis_service, "current_basis", lambda _db, group=None, as_of=None: basis)

    summary = PlanSummaryOut.model_validate(plan_service.summary_of(_Db(), str(plan.id)))
    assert summary.basis_state == "fresh"
    assert summary.save_month == 100.0
    assert summary.basis_fingerprint == basis_core.fingerprint(basis)
