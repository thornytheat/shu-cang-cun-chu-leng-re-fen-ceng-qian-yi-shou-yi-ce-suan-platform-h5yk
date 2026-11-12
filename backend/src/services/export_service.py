"""工单 CSV 导出（业务链路最后一环）。

草稿计划导出前必须确认测算依据仍然有效（basis stale -> 409），
已提交计划导出的是提交时固化的历史口径（不随当前访问日志 / 策略变化）。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.constants import error_codes as codes
from src.constants.enums import RISK_LABELS, TIER_LABELS
from src.models.migration_plan import MigrationPlan
from src.models.user import User
from src.services import basis_service, plan_service
from src.utils.errors import AppError

HEADERS = [
    "工单号",
    "计划编号",
    "计划标题",
    "库表",
    "当前温区",
    "目标温区",
    "大小GB",
    "月省元",
    "迁移成本元",
    "回收期月",
    "净年省元",
    "风险",
    "风险说明",
    "测算日期",
    "策略版本",
    "负责人",
    "生成时间",
]


def _require_exportable(db: Session, plan: MigrationPlan) -> None:
    """草稿计划依据失效 -> 409（已提交计划冻结口径，允许导出历史工单）。"""
    fresh = basis_service.freshness(plan, basis_service.current_basis(db))
    if fresh["is_stale"]:
        raise AppError(
            codes.PLAN_BASIS_STALE,
            "测算依据已失效，请先风险复检后再导出",
            "；".join(fresh["reasons"]),
            extra={"reasons": fresh["reasons"]},
        )


def build_ticket_csv(db: Session, plan: MigrationPlan, actor: User | None = None, trace_id: str = "") -> tuple[str, int]:
    _require_exportable(db, plan)
    rows = plan_service.plan_item_rows(db, plan)
    if not rows:
        raise AppError(codes.EXPORT_EMPTY, "没有可导出的计划项", f"plan={plan.code}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    basis_as_of = plan.basis_as_of or ""
    frozen_version = (plan.basis_snapshot or {}).get("policy") or {}
    policy_version = f"v{int(frozen_version.get('version', plan.policy_version))}"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADERS)
    for index, item in enumerate(rows, start=1):
        writer.writerow(
            [
                f"WO-{plan.code.replace('MP-', '')}-{index:02d}",
                plan.code,
                plan.title,
                item.table_name,
                TIER_LABELS.get(item.from_tier, item.from_tier),
                TIER_LABELS.get(item.suggested_tier, item.suggested_tier),
                f"{float(item.size_gb):.2f}",
                f"{float(item.save_month):.2f}",
                f"{float(item.migration_cost):.2f}",
                f"{float(item.payback_months):.2f}" if item.payback_months is not None else "",
                f"{float(item.net_save_year):.2f}",
                RISK_LABELS.get(item.risk, item.risk),
                item.reasons,
                basis_as_of,
                policy_version,
                "",
                stamp,
            ]
        )
    if actor is not None:
        plan_service.record_export(db, plan, actor, len(rows), trace_id)
    return buffer.getvalue(), len(rows)
