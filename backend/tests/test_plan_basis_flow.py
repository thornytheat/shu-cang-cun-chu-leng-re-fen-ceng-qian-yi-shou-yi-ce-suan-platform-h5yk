"""计划 × 测算依据（basis）业务流单测：失效拦截、复检刷新与提交冻结。

不连数据库：用内存替身替代 Session，只覆盖决策路径 ——
  1. 草稿依据失效 -> 提交 / 导出返回 PLAN_BASIS_STALE（409）；
  2. 复检按最新依据整体更新逐表收益与风险，旧结果整份保留到 previous；
  3. 提交时固化依据快照，之后依据变化不改写已提交计划的历史工单（frozen + drift 提示）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.constants import error_codes as codes
from src.constants.enums import PlanItemStatus, PlanStatus
from src.models.migration_plan import MigrationPlan
from src.models.migration_plan_item import MigrationPlanItem
from src.services import basis_core, basis_service, export_service, plan_service
from src.utils.errors import AppError
from tests.test_basis_core import _basis


class FakeDb:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        pass

    def get(self, model, key):
        return None


def _actor(email: str = "analyst@coldtier.io") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), email=email, role="analyst")


def _plan(status: str = PlanStatus.DRAFT, basis: dict | None = None) -> MigrationPlan:
    plan = MigrationPlan(
        id=uuid.uuid4(),
        code="MP-2026Q4-777",
        title="依据失效场景",
        status=status,
        policy_version=3,
        threshold=20,
        created_by="analyst@coldtier.io",
    )
    plan.total_size_gb = 0
    plan.save_month = 0
    plan.save_year = 0
    plan.migration_cost = 0
    plan.net_save_year = 0
    plan.risk_block_count = 0
    plan.risk_warn_count = 0
    if basis is not None:
        plan.basis_fingerprint = basis_core.fingerprint(basis)
        plan.basis_snapshot = basis
        plan.basis_as_of = basis["as_of"]
        plan.basis_captured_at = datetime.now(timezone.utc)
        plan.previous_snapshot = None
    return plan


def _item(name: str, save: float = 100.0, risk: str = "ok") -> MigrationPlanItem:
    item = MigrationPlanItem(
        id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        table_id=uuid.uuid4(),
        table_name=name,
        from_tier="hot",
        suggested_tier="cold",
        risk=risk,
        status=PlanItemStatus.PENDING,
    )
    item.size_gb = 100.0
    item.hot_score = 10.0
    item.save_month = save
    item.migration_cost = 300.0
    item.net_save_year = save * 12 - 300.0
    item.payback_months = 3.0
    item.reasons = "近 7 天零访问、无报表引用、分区数满足灰度要求"
    return item


@pytest.fixture()
def db() -> FakeDb:
    return FakeDb()


@pytest.fixture()
def submitted_plan(monkeypatch) -> MigrationPlan:
    plan = _plan(PlanStatus.DRAFT, _basis())
    plan.status = PlanStatus.SUBMITTED
    return plan


# ------------------------------------------------------------- 提交拦截 ----
def test_submit_rejects_stale_draft(monkeypatch, db) -> None:
    old = _basis()
    plan = _plan(PlanStatus.DRAFT, old)
    items = [_item("dwd.trade_order_detail")]
    new = _basis(policy=old["policy"] | {"version": 4})
    monkeypatch.setattr(plan_service, "active_items", lambda _db, _id: items)
    monkeypatch.setattr(basis_service, "current_basis", lambda _db, group=None, as_of=None: new)
    monkeypatch.setattr(plan_service, "_plan_or_404", lambda _db, _id: plan)

    with pytest.raises(AppError) as exc:
        plan_service.submit(db, str(plan.id), _actor())
    assert exc.value.code == codes.PLAN_BASIS_STALE
    assert exc.value.http_status == 409
    assert "策略版本 v3 → v4" in "；".join(exc.value.extra["reasons"])
    # 被拒绝时计划仍在草稿，不会被误标提交
    assert plan.status == PlanStatus.DRAFT
    assert items[0].status == PlanItemStatus.PENDING


def test_submit_freezes_basis_snapshot(monkeypatch, db) -> None:
    basis = _basis()
    plan = _plan(PlanStatus.DRAFT, basis)
    items = [_item("dwd.trade_order_detail")]
    monkeypatch.setattr(plan_service, "active_items", lambda _db, _id: items)
    monkeypatch.setattr(basis_service, "current_basis", lambda _db, group=None, as_of=None: basis)
    monkeypatch.setattr(plan_service, "_plan_or_404", lambda _db, _id: plan)

    result = plan_service.submit(db, str(plan.id), _actor())

    assert plan.status == PlanStatus.SUBMITTED
    assert plan.basis_fingerprint == basis_core.fingerprint(basis)
    assert plan.basis_as_of == basis["as_of"]
    assert result["basis"]["state"] == "frozen"
    assert result["basis"]["is_stale"] is False
    assert result["allow_export"] is True
    assert result["allow_submit"] is False


# --------------------------------------------------------------- 导出拦截 ----
def test_export_rejects_stale_draft(monkeypatch, db) -> None:
    old = _basis()
    plan = _plan(PlanStatus.DRAFT, old)
    new = _basis(access=old["access"] | {"rows": 3601})
    monkeypatch.setattr(plan_service, "active_items", lambda _db, _id: [_item("dwd.trade_order_detail")])
    monkeypatch.setattr(basis_service, "current_basis", lambda _db, group=None, as_of=None: new)
    monkeypatch.setattr(export_service, "basis_service", basis_service)

    with pytest.raises(AppError) as exc:
        export_service.build_ticket_csv(db, plan)
    assert exc.value.code == codes.PLAN_BASIS_STALE
    assert "访问日志已变化" in "；".join(exc.value.extra["reasons"])


def test_export_submitted_plan_uses_frozen_basis(monkeypatch, db) -> None:
    """已提交计划：即使当前访问日志 / 策略已变化，也按提交时口径导出历史工单。"""
    frozen = _basis()
    plan = _plan(PlanStatus.SUBMITTED, frozen)
    items = [_item("dwd.trade_order_detail")]
    drifted = _basis(policy=frozen["policy"] | {"version": 5})
    monkeypatch.setattr(plan_service, "active_items", lambda _db, _id: items)
    monkeypatch.setattr(basis_service, "current_basis", lambda _db, group=None, as_of=None: drifted)

    content, rows = export_service.build_ticket_csv(db, plan)
    assert rows == 1
    assert "测算日期" in content and "策略版本" in content
    assert frozen["as_of"] in content
    assert "v3" in content
    assert "[object Object]" not in content


def test_export_still_requires_items(monkeypatch, db) -> None:
    plan = _plan(PlanStatus.DRAFT, _basis())
    monkeypatch.setattr(plan_service, "active_items", lambda _db, _id: [])
    monkeypatch.setattr(basis_service, "current_basis", lambda _db, group=None, as_of=None: _basis())
    with pytest.raises(AppError) as exc:
        export_service.build_ticket_csv(db, plan)
    assert exc.value.code == codes.EXPORT_EMPTY


# --------------------------------------------------------------- 复检刷新 ----
def test_recheck_updates_all_items_and_keeps_previous(monkeypatch, db) -> None:
    old = _basis()
    plan = _plan(PlanStatus.DRAFT, old)
    plan.total_size_gb = 100.0
    plan.save_month = 100.0
    plan.save_year = 1200.0
    plan.migration_cost = 300.0
    plan.net_save_year = 900.0
    plan.avg_payback = 3.0
    plan.rechecked_at = datetime.now(timezone.utc)
    item = _item("dwd.trade_order_detail", save=100.0)
    items = [item]
    new = _basis(policy=old["policy"] | {"version": 4, "prices": {"hot": 1.02, "warm": 0.85, "cold": 0.51}})

    profile = SimpleNamespace(
        size_gb=100.0, tier="hot", partition_count=36, report_refs=0,
        report_names=[], full_name="dwd.trade_order_detail",
    )
    hot = SimpleNamespace(hot_score=12.5, suggested_tier="cold", recent_7_access=0)
    monkeypatch.setattr(plan_service, "_plan_or_404", lambda _db, _id: plan)
    monkeypatch.setattr(plan_service, "active_items", lambda _db, _id: items)
    monkeypatch.setattr(plan_service.heat_service, "compute_scores", lambda _db, group, as_of=None: {str(item.table_id): hot})
    monkeypatch.setattr(plan_service.cost_service, "economics", lambda size, tier, group: SimpleNamespace(
        save_month=50.0, migration_cost=300.0, payback_months=6.0, net_save_year=300.0
    ))
    monkeypatch.setattr(plan_service.cost_service, "assess_risk", lambda profile, hot, group: SimpleNamespace(
        risk="warn", reasons="被 2 张报表引用，需报表 owner 确认"
    ))
    monkeypatch.setattr(plan_service.basis_service, "current_basis", lambda _db, group=None, as_of=None: {
        **new, "fingerprint": basis_core.fingerprint(new)
    })
    monkeypatch.setattr(db, "get", lambda model, key: profile if key == item.table_id else None)

    result = plan_service.recheck(db, SimpleNamespace(version=4), str(plan.id), _actor("admin@coldtier.io"))

    # 逐表结果整体更新
    assert item.save_month == 50.0
    assert item.risk == "warn"
    assert item.net_save_year == 300.0
    # 上次结果保留（逐表 + 计划头整份快照）
    assert item.prev_save_month == 100.0
    assert item.prev_risk == "ok"
    assert item.prev_basis_as_of == old["as_of"]
    assert plan.previous_snapshot is not None
    assert plan.previous_snapshot["summary"]["save_month"] == 100.0
    assert plan.previous_fingerprint == basis_core.fingerprint(old)
    # 计划头切到新依据
    assert plan.basis_fingerprint == basis_core.fingerprint(new)
    assert result["basis"]["state"] == "fresh"
    assert result["basis"]["previous"]["summary"]["net_save_year"] == 900.0
    assert result["items"][0]["previous"]["save_month"] == 100.0


def test_recheck_rejects_submitted_plan(monkeypatch, db) -> None:
    plan = _plan(PlanStatus.SUBMITTED, _basis())
    monkeypatch.setattr(plan_service, "_plan_or_404", lambda _db, _id: plan)
    with pytest.raises(AppError) as exc:
        plan_service.recheck(db, SimpleNamespace(version=4), str(plan.id), _actor("x"))
    assert exc.value.code == codes.PLAN_NOT_DRAFT


# --------------------------------------------------------- 列表 / 详情状态 ----
def test_serialize_plan_exposes_stale_and_blocks_actions(monkeypatch, db) -> None:
    old = _basis()
    plan = _plan(PlanStatus.DRAFT, old)
    items = [_item("dwd.trade_order_detail")]
    new = _basis(policy=old["policy"] | {"version": 4})
    monkeypatch.setattr(plan_service, "active_items", lambda _db, _id: items)
    monkeypatch.setattr(basis_service, "current_basis", lambda _db, group=None, as_of=None: new)

    payload = plan_service.serialize_plan(db, plan, basis=new)
    assert payload["basis"]["state"] == "stale"
    assert payload["basis"]["is_stale"] is True
    assert payload["basis"]["current_policy_version"] == 4
    assert payload["allow_submit"] is False
    assert payload["allow_export"] is False
    assert "策略版本 v3 → v4" in payload["basis"]["reasons"]


def test_stale_draft_recheck_restores_fresh_and_submittable(monkeypatch, db) -> None:
    """季度调价使草稿失效 -> 复检按新口径整体刷新 -> 依据恢复有效、可提交。

    这也是页面主流程：失效横幅 -> 点「风险复检」 -> 新结果 + 上次结果对比。
    """
    old = _basis()
    plan = _plan(PlanStatus.DRAFT, old)
    plan.save_month = 100.0
    plan.save_year = 1200.0
    plan.migration_cost = 300.0
    plan.net_save_year = 900.0
    item = _item("dwd.trade_order_detail", save=100.0)
    items = [item]
    new = _basis(policy=old["policy"] | {"version": 4, "prices": {"hot": 1.02, "warm": 0.85, "cold": 0.51}})

    profile = SimpleNamespace(
        size_gb=100.0, tier="hot", partition_count=36, report_refs=0,
        report_names=[], full_name="dwd.trade_order_detail",
    )
    hot = SimpleNamespace(hot_score=9.0, suggested_tier="cold", recent_7_access=0)
    monkeypatch.setattr(plan_service, "_plan_or_404", lambda _db, _id: plan)
    monkeypatch.setattr(plan_service, "active_items", lambda _db, _id: items)
    monkeypatch.setattr(plan_service.heat_service, "compute_scores", lambda _db, group, as_of=None: {str(item.table_id): hot})
    monkeypatch.setattr(plan_service.cost_service, "economics", lambda size, tier, group: SimpleNamespace(
        save_month=47.0, migration_cost=300.0, payback_months=6.4, net_save_year=264.0
    ))
    monkeypatch.setattr(plan_service.cost_service, "assess_risk", lambda profile, hot, group: SimpleNamespace(
        risk="ok", reasons="近 7 天零访问、无报表引用、分区数满足灰度要求"
    ))
    monkeypatch.setattr(db, "get", lambda model, key: profile if key == item.table_id else None)
    # 真实 capture 会从 DB 取快照；这里让 current_basis 返回「新的当前依据」（含指纹）
    current = {**new, "fingerprint": basis_core.fingerprint(new)}
    monkeypatch.setattr(basis_service, "current_basis", lambda _db, group=None, as_of=None: current)
    monkeypatch.setattr(
        basis_service,
        "capture",
        lambda _db, target, group=None, as_of=None, reasons="": (
            setattr(target, "basis_fingerprint", current["fingerprint"])
            or setattr(target, "basis_snapshot", current)
            or setattr(target, "basis_as_of", current["as_of"])
            or current
        ),
    )

    result = plan_service.recheck(db, SimpleNamespace(version=4), str(plan.id), _actor())

    assert result["basis"]["state"] == "fresh"
    assert result["allow_submit"] is True
    assert result["allow_export"] is True
    assert result["items"][0]["save_month"] == 47.0
    assert result["items"][0]["previous"]["save_month"] == 100.0
    assert result["basis"]["previous"]["summary"]["net_save_year"] == 900.0
    assert item.prev_risk == "ok"
