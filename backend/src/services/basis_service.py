"""测算依据（basis）：抓取 / 固化 / 失效判定。

一份计划只有在「测算依据」与当前一致时才算有效。依据包含：

  policy   当前生效策略版本与三档单价、评分下限、冷区前置条件、迁移成本
  heat     热度算法参数（窗口 90 天 / 半衰期 14 天 / 引用加成）
  access   热度窗口内的访问日志汇总（行数、总访问、日期上限、内容摘要）
  profiles 表档案摘要（体积 / 分区 / 报表引用 / 当前温区）
  as_of    测算日期（热度窗口的右端点，决定 90 天窗口落在哪里）

抓取时把完整快照与 sha256 指纹写进计划；之后任何一项变化（季度调价升版、
查询日志补录、表体积/分区变化、窗口前移）都会让指纹不同，草稿即标记失效。

已提交计划的依据在提交时固化：不再复检、不再改写，历史工单口径不受后续数据影响。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.settings import settings
from src.constants.enums import PlanStatus
from src.models.access_stat import AccessStat
from src.models.migration_plan import MigrationPlan
from src.models.table_profile import TableProfile
from src.services import basis_core, policy_service
from src.utils.numeric import money


def _policy_snapshot(group) -> dict:
    return {
        "version": int(group.version),
        "prices": {tier: money(group.price(tier)) for tier in ("hot", "warm", "cold")},
        "score_min": {"hot": float(group.hot_score_min), "warm": float(group.warm_score_min)},
        "quiet_days": int(group.quiet_days),
        "block_recent_days": int(group.block_recent_days),
        "min_partition": int(group.min_partition),
        "migrate_price": money(group.migrate_price),
        "migrate_fixed": money(group.migrate_fixed),
    }


def _heat_snapshot() -> dict:
    return {
        "window_days": int(settings.heat_window_days),
        "half_life_days": float(settings.heat_half_life_days),
        "report_bonus_per_ref": int(settings.report_bonus_per_ref),
        "report_bonus_cap": int(settings.report_bonus_cap),
    }


def _profiles_snapshot(db: Session) -> dict:
    """表档案摘要：参与测算的六个事实逐表取指纹（顺序固定，与查询计划无关）。"""
    rows = db.execute(select(TableProfile).order_by(TableProfile.full_name.asc())).scalars().all()
    lines = [
        "|".join(
            [
                row.full_name,
                row.tier,
                f"{float(row.size_gb):.2f}",
                str(int(row.partition_count)),
                str(int(row.report_refs or 0)),
            ]
        )
        for row in rows
    ]
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {"count": len(rows), "digest": digest}


def _access_snapshot(db: Session, as_of: date) -> dict:
    """热度窗口内的访问日志摘要：行数 / 总访问 / 日期上限 + 全量内容摘要。

    补录一条旧日期的访问、修改某天的 access_count，都会改变 digest；
    窗口前移（as_of 变化）会让一些日子移出窗口，同样会改变 digest。
    """
    window_start = as_of - timedelta(days=settings.heat_window_days - 1)
    rows = db.execute(
        select(AccessStat)
        .where(AccessStat.stat_date >= window_start, AccessStat.stat_date <= as_of)
        .order_by(AccessStat.table_id.asc(), AccessStat.stat_date.asc())
    ).scalars().all()
    lines = [
        f"{row.table_id}|{row.stat_date.isoformat()}|{int(row.access_count)}"
        f"|{int(row.query_count)}|{int(row.scan_bytes)}"
        for row in rows
    ]
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {
        "rows": len(rows),
        "total_access": sum(int(row.access_count) for row in rows),
        "max_date": max((row.stat_date.isoformat() for row in rows), default=None),
        "digest": digest,
    }


def fingerprint_of(basis: dict) -> str:
    """依据指纹：优先用快照里已算好的值，缺失时按 payload 现算（容错旧快照 / 手工构造）。"""
    return basis.get("fingerprint") or basis_core.fingerprint(basis)


def current_basis(db: Session, group=None, as_of: date | None = None) -> dict:
    """当前测算依据（读取时的真值）。group 可由调用方传入避免重复加载策略。"""
    group = group or policy_service.load_current(db)
    as_of = as_of or date.today()
    basis = {
        "as_of": as_of.isoformat(),
        "policy": _policy_snapshot(group),
        "heat": _heat_snapshot(),
        "access": _access_snapshot(db, as_of),
        "profiles": _profiles_snapshot(db),
    }
    basis["fingerprint"] = basis_core.fingerprint(basis)
    return basis


def capture(
    db: Session,
    plan: MigrationPlan,
    group=None,
    as_of: date | None = None,
    reasons: str = "",
) -> dict:
    """把当前依据固化到计划头：快照 + 指纹 + 测算日期 + 抓取时间。"""
    basis = current_basis(db, group, as_of)
    plan.basis_as_of = basis["as_of"]
    plan.basis_fingerprint = basis["fingerprint"]
    plan.basis_snapshot = basis
    plan.basis_captured_at = datetime.now(timezone.utc)
    plan.basis_reasons = reasons
    db.flush()
    return basis


def _basis_stale_reasons(plan: MigrationPlan, basis: dict) -> list[str]:
    fingerprint = fingerprint_of(basis)
    if not plan.basis_fingerprint:
        return ["计划尚未记录测算依据，请先风险复检"]
    if plan.basis_fingerprint == fingerprint:
        return []
    return basis_core.diff(plan.basis_snapshot or {}, basis) or ["测算依据已变化"]


def freshness(plan: MigrationPlan, basis: dict) -> dict:
    """计划相对当前依据的新鲜度。

    draft 且指纹不同 -> stale（不可提交、不可导出）；已提交 / 已审批 / 已撤销 -> frozen
    （口径已固化，后续数据变化不改写历史）。frozen 计划若与当前依据不同，只提示 drift，
    不视为失效。
    """
    fingerprint = fingerprint_of(basis)
    current = {
        "fingerprint": fingerprint,
        "as_of": basis.get("as_of"),
        "policy_version": int((basis.get("policy") or {}).get("version") or 0),
    }
    if plan.status != PlanStatus.DRAFT:
        drift = bool(plan.basis_fingerprint) and plan.basis_fingerprint != fingerprint
        return {
            "state": "frozen",
            "is_stale": False,
            "reasons": (
                basis_core.diff(plan.basis_snapshot or {}, basis) if drift else []
            ),
            "drift": drift,
            "current": current,
        }
    reasons = _basis_stale_reasons(plan, basis)
    return {
        "state": "stale" if reasons else "fresh",
        "is_stale": bool(reasons),
        "reasons": reasons,
        "drift": bool(reasons),
        "current": current,
    }
