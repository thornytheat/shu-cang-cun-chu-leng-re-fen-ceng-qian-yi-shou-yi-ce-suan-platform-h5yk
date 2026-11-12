"""迁移计划：建计划 → 风险复检 → 移除阻断 → 提交 → 导出。

计划汇总口径与 TrialResultOut 一致：
  save_month = Σ 逐表月省；save_year = save_month × 12
  migration_cost = Σ (size × 迁移单价 + 每表固定工单成本)
  net_save_year = save_year - migration_cost
  avg_payback = migration_cost / save_month（加权，1.8 月即此口径）

测算依据（basis，见 basis_service）：
  - 建计划 / 追加 / 复检时记录当时生效的策略、热度口径、访问日志摘要、表档案与 as_of；
  - 草稿计划的依据与当前不一致 -> stale：页面标失效，提交与导出返回 409；
  - 复检按最新依据整体更新逐表收益与风险，旧结果写入 previous_* 供页面对比；
  - 提交时固化依据（frozen），之后数据变化不再改写已提交计划的历史工单口径。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants import error_codes as codes
from src.constants.enums import PLAN_STATUS_LABELS, PlanItemStatus, PlanStatus, StorageTier
from src.constants.limits import MAX_PLAN_ITEMS
from src.constants.log_templates import (
    ACTION_PLAN_CREATE,
    ACTION_PLAN_EXPORT,
    ACTION_PLAN_ITEM_ADD,
    ACTION_PLAN_ITEM_REMOVE,
    ACTION_PLAN_RECHECK,
    ACTION_PLAN_SUBMIT,
)
from src.models.migration_plan import MigrationPlan
from src.models.migration_plan_item import MigrationPlanItem
from src.models.table_profile import TableProfile
from src.models.user import User
from src.services import audit_service, basis_service, cost_service, heat_service, savings_core
from src.utils.errors import AppError, not_found
from src.utils.ids import parse_uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_code(db: Session, moment: datetime) -> str:
    quarter = (moment.month - 1) // 3 + 1
    prefix = f"MP-{moment.year}Q{quarter}-"
    existing = db.execute(
        select(MigrationPlan.code).where(MigrationPlan.code.like(f"{prefix}%"))
    ).scalars().all()
    seq = 1
    for code in existing:
        try:
            seq = max(seq, int(code.rsplit("-", 1)[1]) + 1)
        except (IndexError, ValueError):
            continue
    return f"{prefix}{seq:03d}"


def active_items(db: Session, plan_id) -> list[MigrationPlanItem]:
    return list(
        db.execute(
            select(MigrationPlanItem)
            .where(
                MigrationPlanItem.plan_id == plan_id,
                MigrationPlanItem.status != PlanItemStatus.REMOVED,
            )
            .order_by(MigrationPlanItem.save_month.desc())
        ).scalars().all()
    )


def recount(db: Session, plan: MigrationPlan) -> dict:
    """把计划项汇总写回计划头（口径见 savings_core.aggregate，与试算共用）。"""
    items = active_items(db, plan.id)
    summary = savings_core.aggregate(items)
    plan.total_size_gb = summary["size_total_gb"]
    plan.save_month = summary["save_month"]
    plan.save_year = summary["save_year"]
    plan.migration_cost = summary["migration_cost"]
    plan.net_save_year = summary["net_save_year"]
    plan.avg_payback = summary["avg_payback"]
    plan.risk_block_count = summary["risk_block_count"]
    plan.risk_warn_count = summary["risk_warn_count"]
    db.flush()
    return {
        "item_count": summary["count"],
        "total_size_gb": plan.total_size_gb,
        "save_month": plan.save_month,
        "save_year": plan.save_year,
        "migration_cost": plan.migration_cost,
        "net_save_year": plan.net_save_year,
        "avg_payback": float(plan.avg_payback) if plan.avg_payback is not None else None,
        "risk_block_count": plan.risk_block_count,
        "risk_warn_count": plan.risk_warn_count,
    }


def serialize_item(item: MigrationPlanItem) -> dict:
    payload = {
        "id": str(item.id),
        "table_id": str(item.table_id),
        "table_name": item.table_name,
        "from_tier": item.from_tier,
        "suggested_tier": item.suggested_tier,
        "size_gb": float(item.size_gb),
        "hot_score": float(item.hot_score),
        "save_month": float(item.save_month),
        "migration_cost": float(item.migration_cost),
        "payback_months": float(item.payback_months) if item.payback_months is not None else None,
        "net_save_year": float(item.net_save_year),
        "risk": item.risk,
        "reasons": item.reasons,
        "status": item.status,
        "previous": None,
    }
    if item.prev_basis_as_of is not None or item.prev_save_month is not None:
        payload["previous"] = {
            "size_gb": float(item.prev_size_gb) if item.prev_size_gb is not None else None,
            "hot_score": float(item.prev_hot_score) if item.prev_hot_score is not None else None,
            "save_month": float(item.prev_save_month) if item.prev_save_month is not None else None,
            "migration_cost": (
                float(item.prev_migration_cost) if item.prev_migration_cost is not None else None
            ),
            "payback_months": (
                float(item.prev_payback_months) if item.prev_payback_months is not None else None
            ),
            "net_save_year": (
                float(item.prev_net_save_year) if item.prev_net_save_year is not None else None
            ),
            "risk": item.prev_risk,
            "reasons": item.prev_reasons,
            "basis_as_of": item.prev_basis_as_of,
            "policy_version": int(item.prev_policy_version) if item.prev_policy_version is not None else None,
        }
    return payload


def _basis_payload(plan: MigrationPlan, basis: dict | None) -> dict:
    """计划出参里的测算依据块：状态 + 失效原因 + 冻结口径 + 上次结果。"""
    if basis is None:
        return {
            "state": "unknown",
            "is_stale": False,
            "reasons": [],
            "drift": False,
            "as_of": plan.basis_as_of,
            "fingerprint": plan.basis_fingerprint,
            "policy_version": int(plan.policy_version),
            "captured_at": plan.basis_captured_at.isoformat() if plan.basis_captured_at else None,
            "current_policy_version": None,
            "previous": plan.previous_snapshot,
        }
    fresh = basis_service.freshness(plan, basis)
    snapshot = plan.basis_snapshot or {}
    return {
        "state": fresh["state"],
        "is_stale": fresh["is_stale"],
        "reasons": fresh["reasons"],
        "drift": fresh["drift"],
        "as_of": plan.basis_as_of,
        "fingerprint": plan.basis_fingerprint,
        "policy_version": (
            int(snapshot["policy"]["version"]) if snapshot.get("policy") else int(plan.policy_version)
        ),
        "captured_at": plan.basis_captured_at.isoformat() if plan.basis_captured_at else None,
        "current_policy_version": int((basis.get("policy") or {}).get("version") or 0),
        "current_as_of": basis.get("as_of"),
        # 计划头保留的整份上次结果（汇总 + 逐表），复检后用于页面对比
        "previous": plan.previous_snapshot,
    }


def serialize_plan(
    db: Session,
    plan: MigrationPlan,
    with_items: bool = True,
    basis: dict | None = None,
) -> dict:
    items = active_items(db, plan.id)
    if basis is None and with_items:
        basis = basis_service.current_basis(db)
    stale = bool(basis and basis_service.freshness(plan, basis)["is_stale"])
    payload = {
        "id": str(plan.id),
        "code": plan.code,
        "title": plan.title,
        "status": plan.status,
        "status_label": PLAN_STATUS_LABELS.get(plan.status, plan.status),
        "policy_version": int(plan.policy_version),
        "threshold": float(plan.threshold),
        "total_size_gb": float(plan.total_size_gb),
        "save_month": float(plan.save_month),
        "save_year": float(plan.save_year),
        "migration_cost": float(plan.migration_cost),
        "net_save_year": float(plan.net_save_year),
        "avg_payback": float(plan.avg_payback) if plan.avg_payback is not None else None,
        "risk_block_count": int(plan.risk_block_count),
        "risk_warn_count": int(plan.risk_warn_count),
        "created_by": plan.created_by,
        "submitted_by": plan.submitted_by,
        "submitted_at": plan.submitted_at.isoformat() if plan.submitted_at else None,
        "rechecked_at": plan.rechecked_at.isoformat() if plan.rechecked_at else None,
        "note": plan.note,
        "item_count": len(items),
        "items": [serialize_item(item) for item in items] if with_items else [],
        "basis": _basis_payload(plan, basis),
        # 页面禁用按钮用：草稿 + 依据有效 + 无阻断项 才能提交；导出同样要求依据有效
        "allow_submit": bool(
            plan.status == PlanStatus.DRAFT and not stale and items and plan.risk_block_count == 0
        ),
        "allow_export": bool(items) and not stale,
    }
    return payload


def list_plans(db: Session) -> dict:
    rows = db.execute(select(MigrationPlan).order_by(MigrationPlan.created_at.desc())).scalars().all()
    basis = basis_service.current_basis(db)
    items = [serialize_plan(db, plan, with_items=False, basis=basis) for plan in rows]
    return {"items": items, "meta": {"total": len(items), "returned": len(items), "filters": {}}}


def get_plan(db: Session, plan_id: str) -> dict:
    return serialize_plan(db, load_plan(db, plan_id))


def _plan_or_404(db: Session, plan_id) -> MigrationPlan:
    """非法 UUID 与不存在 UUID 统一 404（避免驱动层 500）。"""
    key = parse_uuid(plan_id)
    plan = db.get(MigrationPlan, key) if key is not None else None
    if plan is None:
        raise not_found("MigrationPlan", plan_id)
    return plan


def build_item(db: Session, plan: MigrationPlan, profile: TableProfile, hot, group) -> MigrationPlanItem:
    econ = cost_service.economics(float(profile.size_gb), profile.tier, group)
    verdict = cost_service.assess_risk(profile, hot, group)
    return MigrationPlanItem(
        plan_id=plan.id,
        table_id=profile.id,
        table_name=profile.full_name,
        from_tier=profile.tier,
        suggested_tier=hot.suggested_tier,
        size_gb=float(profile.size_gb),
        hot_score=hot.hot_score,
        save_month=econ.save_month,
        migration_cost=econ.migration_cost,
        payback_months=econ.payback_months,
        net_save_year=econ.net_save_year,
        risk=verdict.risk,
        reasons=verdict.reasons,
        status=PlanItemStatus.PENDING,
    )


def _remember_item(item: MigrationPlanItem, plan: MigrationPlan) -> None:
    """把当前逐表结果挪到 previous_*（复检前调用），供页面对比上次口径。"""
    item.prev_size_gb = item.size_gb
    item.prev_hot_score = item.hot_score
    item.prev_save_month = item.save_month
    item.prev_migration_cost = item.migration_cost
    item.prev_payback_months = item.payback_months
    item.prev_net_save_year = item.net_save_year
    item.prev_risk = item.risk
    item.prev_reasons = item.reasons
    item.prev_basis_as_of = plan.basis_as_of
    item.prev_policy_version = int(plan.policy_version)


def _plan_snapshot(db: Session, plan: MigrationPlan) -> dict:
    """上次结果快照：汇总 + 逐表，和 basis 指纹一起留存，供页面「对比上次」。"""
    items = active_items(db, plan.id)
    return {
        "fingerprint": plan.basis_fingerprint,
        "policy_version": int(plan.policy_version),
        "basis_as_of": plan.basis_as_of,
        "captured_at": plan.basis_captured_at.isoformat() if plan.basis_captured_at else None,
        "rechecked_at": plan.rechecked_at.isoformat() if plan.rechecked_at else None,
        "summary": {
            "item_count": len(items),
            "total_size_gb": float(plan.total_size_gb),
            "save_month": float(plan.save_month),
            "save_year": float(plan.save_year),
            "migration_cost": float(plan.migration_cost),
            "net_save_year": float(plan.net_save_year),
            "avg_payback": float(plan.avg_payback) if plan.avg_payback is not None else None,
            "risk_block_count": int(plan.risk_block_count),
            "risk_warn_count": int(plan.risk_warn_count),
        },
        "items": [
            {
                "table_id": str(item.table_id),
                "table_name": item.table_name,
                "size_gb": float(item.size_gb),
                "hot_score": float(item.hot_score),
                "save_month": float(item.save_month),
                "migration_cost": float(item.migration_cost),
                "payback_months": (
                    float(item.payback_months) if item.payback_months is not None else None
                ),
                "net_save_year": float(item.net_save_year),
                "risk": item.risk,
                "reasons": item.reasons,
                "policy_version": int(plan.policy_version),
                "basis_as_of": plan.basis_as_of,
            }
            for item in items
        ],
    }


def _require_fresh(db: Session, plan: MigrationPlan, basis: dict, action: str) -> None:
    """草稿计划的依据必须与当前一致，否则拒绝提交 / 导出（409）。"""
    fresh = basis_service.freshness(plan, basis)
    if fresh["is_stale"]:
        raise AppError(
            codes.PLAN_BASIS_STALE,
            "测算依据已失效，请先风险复检后再" + action,
            "；".join(fresh["reasons"]),
            extra={"reasons": fresh["reasons"]},
        )


def create_plan(
    db: Session,
    group,
    payload,
    actor: User | None = None,
    trace_id: str = "",
    code: str | None = None,
    as_of: date | None = None,
) -> dict:
    table_ids = [str(tid) for tid in payload.table_ids]
    try:
        savings_core.check_plan_size(table_ids)
    except ValueError:
        raise AppError(
            codes.VALIDATION_ERROR, f"单次最多 {MAX_PLAN_ITEMS} 张表", f"count={len(table_ids)}"
        ) from None
    now = _now()
    # 热度窗口与依据快照必须用同一个 as_of，否则跨零点建计划会出现「算的是昨天、记的是今天」
    as_of = as_of or date.today()
    plan = MigrationPlan(
        code=code or _next_code(db, now),
        title=payload.title,
        status=PlanStatus.DRAFT,
        policy_version=group.version,
        threshold=payload.threshold,
        created_by=actor.email if actor else "system",
        note=payload.note,
    )
    db.add(plan)
    db.flush()

    scores = heat_service.compute_scores(db, group, as_of)
    added = 0
    for table_id in table_ids:
        profile_key = parse_uuid(table_id)
        profile = db.get(TableProfile, profile_key) if profile_key is not None else None
        if profile is None:
            raise not_found("TableProfile", table_id)
        hot = scores.get(str(profile.id))
        if hot is None:
            continue
        if profile.tier == StorageTier.COLD:
            continue
        db.add(build_item(db, plan, profile, hot, group))
        added += 1
    db.flush()
    summary = recount(db, plan)
    basis = basis_service.capture(db, plan, group, as_of)
    if actor is not None:
        audit_service.record(
            db,
            action=ACTION_PLAN_CREATE,
            actor=actor,
            entity_type="MigrationPlan",
            entity_id=plan.code,
            detail={
                "items": added,
                "threshold": float(payload.threshold),
                "basis_fingerprint": basis_service.fingerprint_of(basis),
                "basis_as_of": basis["as_of"],
                **summary,
            },
            trace_id=trace_id,
        )
    return serialize_plan(db, plan, basis=basis)


def add_items(
    db: Session,
    group,
    plan_id: str,
    table_ids: list[str],
    actor: User,
    trace_id: str = "",
    as_of: date | None = None,
) -> dict:
    plan = _plan_or_404(db, plan_id)
    if plan.status != PlanStatus.DRAFT:
        raise AppError(codes.PLAN_NOT_DRAFT, "只有草稿状态的计划可以添加表", f"status={plan.status}")
    # 依据已失效时不允许追加：新表会用新口径，旧表仍是旧口径，混算没有意义
    as_of = as_of or date.today()
    basis = basis_service.current_basis(db, group, as_of)
    _require_fresh(db, plan, basis, "追加表")

    existing = {str(item.table_id) for item in active_items(db, plan.id)}
    scores = heat_service.compute_scores(db, group, as_of)
    added = 0
    for table_id in table_ids:
        table_key = str(table_id)
        if table_key in existing:
            continue
        profile_key = parse_uuid(table_id)
        profile = db.get(TableProfile, profile_key) if profile_key is not None else None
        if profile is None:
            raise not_found("TableProfile", table_id)
        hot = scores.get(table_key)
        if hot is None or profile.tier == StorageTier.COLD:
            continue
        db.add(build_item(db, plan, profile, hot, group))
        existing.add(table_key)
        added += 1
    db.flush()
    summary = recount(db, plan)
    if added and not plan.basis_fingerprint:
        basis = basis_service.capture(db, plan, group, as_of)
    audit_service.record(
        db,
        action=ACTION_PLAN_ITEM_ADD,
        actor=actor,
        entity_type="MigrationPlan",
        entity_id=plan.code,
        detail={"added": added, "basis_fingerprint": plan.basis_fingerprint, **summary},
        trace_id=trace_id,
    )
    return serialize_plan(db, plan, basis=basis)


def remove_item(db: Session, plan_id: str, item_id: str, actor: User, trace_id: str = "") -> dict:
    plan = _plan_or_404(db, plan_id)
    if plan.status != PlanStatus.DRAFT:
        raise AppError(codes.PLAN_NOT_DRAFT, "只有草稿状态的计划可以移除表", f"status={plan.status}")
    item_key = parse_uuid(item_id)
    item = db.get(MigrationPlanItem, item_key) if item_key is not None else None
    if item is None or str(item.plan_id) != str(plan.id):
        raise not_found("MigrationPlanItem", item_id)
    item.status = PlanItemStatus.REMOVED
    db.flush()
    summary = recount(db, plan)
    basis = basis_service.current_basis(db)
    audit_service.record(
        db,
        action=ACTION_PLAN_ITEM_REMOVE,
        actor=actor,
        entity_type="MigrationPlan",
        entity_id=plan.code,
        detail={"removed": item.table_name, "risk": item.risk, **summary},
        trace_id=trace_id,
    )
    return serialize_plan(db, plan, basis=basis)


def recheck(
    db: Session,
    group,
    plan_id: str,
    actor: User,
    trace_id: str = "",
    as_of: date | None = None,
) -> dict:
    """风险复检：按最新依据整体重算每张表的风险与收益，刷新汇总并记录新依据。

    上次结果整份挪到 previous（计划头 previous_snapshot + 逐表 previous_*），
    页面据此对比「复检前 vs 复检后」，而不是让旧省额混进新汇总。
    """
    plan = _plan_or_404(db, plan_id)
    if plan.status != PlanStatus.DRAFT:
        raise AppError(codes.PLAN_NOT_DRAFT, "只有草稿状态的计划可以复检", f"status={plan.status}")
    as_of = as_of or date.today()
    if plan.basis_fingerprint:
        plan.previous_snapshot = _plan_snapshot(db, plan)
        plan.previous_fingerprint = plan.basis_fingerprint
        plan.previous_at = plan.basis_captured_at
    scores = heat_service.compute_scores(db, group, as_of)
    for item in active_items(db, plan.id):
        profile = db.get(TableProfile, item.table_id)
        hot = scores.get(str(item.table_id))
        if profile is None or hot is None:
            continue
        _remember_item(item, plan)
        econ = cost_service.economics(float(profile.size_gb), profile.tier, group)
        verdict = cost_service.assess_risk(profile, hot, group)
        item.size_gb = float(profile.size_gb)
        item.hot_score = hot.hot_score
        item.suggested_tier = hot.suggested_tier
        item.from_tier = profile.tier
        item.save_month = econ.save_month
        item.migration_cost = econ.migration_cost
        item.payback_months = econ.payback_months
        item.net_save_year = econ.net_save_year
        item.risk = verdict.risk
        item.reasons = verdict.reasons
    plan.policy_version = int(group.version)
    plan.rechecked_at = _now()
    db.flush()
    summary = recount(db, plan)
    basis = basis_service.capture(db, plan, group, as_of)
    audit_service.record(
        db,
        action=ACTION_PLAN_RECHECK,
        actor=actor,
        entity_type="MigrationPlan",
        entity_id=plan.code,
        detail={
            "basis_fingerprint": basis_service.fingerprint_of(basis),
            "basis_as_of": basis["as_of"],
            "policy_version": int(group.version),
            "previous_fingerprint": plan.previous_fingerprint,
            **summary,
        },
        trace_id=trace_id,
    )
    return serialize_plan(db, plan, basis=basis)


def submit(db: Session, plan_id: str, actor: User, trace_id: str = "", as_of: date | None = None) -> dict:
    plan = _plan_or_404(db, plan_id)
    if plan.status != PlanStatus.DRAFT:
        raise AppError(codes.PLAN_NOT_DRAFT, "该计划已提交，不能重复提交", f"status={plan.status}")
    items = active_items(db, plan.id)
    if not items:
        raise AppError(codes.PLAN_EMPTY, "计划中没有可提交的表", f"plan={plan.code}")
    basis = basis_service.current_basis(db, as_of=as_of)
    _require_fresh(db, plan, basis, "提交")
    blockers = savings_core.blockers(items)
    if blockers:
        raise AppError(
            codes.PLAN_HAS_BLOCKERS,
            f"仍有 {len(blockers)} 张表被风险闸门阻断，请先移除",
            ",".join(blockers),
            extra={"blockers": blockers},
        )
    now = _now()
    for item in items:
        item.status = PlanItemStatus.SUBMITTED
    plan.status = PlanStatus.SUBMITTED
    plan.submitted_at = now
    plan.submitted_by = actor.email
    # 冻结当时口径：补充一次快照（此时依据已与当前一致），供历史工单回查；
    # 提交后不再复检，后续数据变化不改写本计划。
    plan.basis_snapshot = basis
    plan.basis_fingerprint = basis_service.fingerprint_of(basis)
    plan.basis_as_of = basis["as_of"]
    plan.basis_captured_at = _now()
    db.flush()
    summary = recount(db, plan)
    audit_service.record(
        db,
        action=ACTION_PLAN_SUBMIT,
        actor=actor,
        entity_type="MigrationPlan",
        entity_id=plan.code,
        detail={
            "basis_fingerprint": plan.basis_fingerprint,
            "basis_as_of": plan.basis_as_of,
            **summary,
        },
        trace_id=trace_id,
    )
    # 提交后的响应仍用提交时的依据，保证「历史口径」不回读当前数据
    return serialize_plan(db, plan, basis=plan.basis_snapshot)


def record_export(db: Session, plan: MigrationPlan, actor: User, row_count: int, trace_id: str = "") -> None:
    audit_service.record(
        db,
        action=ACTION_PLAN_EXPORT,
        actor=actor,
        entity_type="MigrationPlan",
        entity_id=plan.code,
        detail={"rows": row_count},
        trace_id=trace_id,
    )


def load_plan(db: Session, plan_id: str) -> MigrationPlan:
    return _plan_or_404(db, plan_id)


def summary_of(db: Session, plan_id: str) -> dict:
    plan = load_plan(db, plan_id)
    counts = recount(db, plan)
    basis = basis_service.current_basis(db)
    fresh = basis_service.freshness(plan, basis)
    return {
        "plan_id": str(plan.id),
        "status": plan.status,
        "rechecked_at": plan.rechecked_at.isoformat() if plan.rechecked_at else None,
        "basis_state": fresh["state"],
        "basis_is_stale": fresh["is_stale"],
        "basis_reasons": fresh["reasons"],
        "basis_as_of": plan.basis_as_of,
        "basis_fingerprint": plan.basis_fingerprint,
        "current_basis_fingerprint": basis_service.fingerprint_of(basis),
        **counts,
    }


def plan_item_rows(db: Session, plan: MigrationPlan) -> list[MigrationPlanItem]:
    return active_items(db, plan.id)


def stats(db: Session) -> dict:
    total = db.query(func.count(MigrationPlan.id)).scalar() or 0
    return {"plan_total": int(total)}
