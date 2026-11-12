"""测算依据抓取（basis_service.current_basis / capture）单测。

用内存替身替代 Session：只验证「取哪些事实、怎么归一化」。
覆盖：策略升版 / 访问补录 / 表档案变化 / 窗口前移 都会改变指纹；口径参数变更同样。
"""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

from src.models.migration_plan import MigrationPlan
from src.services import basis_core, basis_service
from tests.conftest import FakePolicyGroup


class _Result:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _StubDb:
    """按 SQL 里的表名返回对应行；默认返回空集。"""

    def __init__(self, access_rows=None, profile_rows=None) -> None:
        self.access_rows = access_rows or []
        self.profile_rows = profile_rows or []
        self.flushed = 0

    def execute(self, stmt):
        sql = str(stmt)
        if "access_stats" in sql:
            return _Result(self.access_rows)
        if "table_profiles" in sql:
            return _Result(self.profile_rows)
        return _Result([])

    def flush(self) -> None:
        self.flushed += 1


def _access(table_id: str, day: date, count: int) -> SimpleNamespace:
    return SimpleNamespace(
        table_id=table_id,
        stat_date=day,
        access_count=count,
        query_count=count // 2,
        scan_bytes=count * 1024,
    )


def _profile(name: str, tier: str = "hot", size: float = 100.0, partitions: int = 12, refs: int = 0):
    return SimpleNamespace(
        full_name=name,
        tier=tier,
        size_gb=size,
        partition_count=partitions,
        report_refs=refs,
    )


class _Group(FakePolicyGroup):
    def __init__(self, version: int = 3, hot_price: float = 1.05) -> None:
        self.version = version
        self._hot_price = hot_price

    def price(self, tier: str) -> float:
        return self._hot_price if tier == "hot" else {"warm": 0.85, "cold": 0.55}[tier]


AS_OF = date(2026, 11, 12)


def _basis(db: _StubDb, group: _Group | None = None) -> dict:
    return basis_service.current_basis(db, group or _Group(), AS_OF)


def test_current_basis_has_all_fingerprint_fields() -> None:
    db = _StubDb(
        access_rows=[_access("t1", AS_OF, 10)],
        profile_rows=[_profile("dwd.trade_order_detail")],
    )
    basis = _basis(db)
    assert set(basis_core.PAYLOAD_KEYS) <= set(basis)
    assert basis["as_of"] == "2026-11-12"
    assert basis["policy"]["version"] == 3
    assert basis["policy"]["prices"] == {"hot": 1.05, "warm": 0.85, "cold": 0.55}
    assert basis["heat"]["window_days"] == 90
    assert basis["access"]["rows"] == 1
    assert basis["access"]["total_access"] == 10
    assert basis["access"]["max_date"] == "2026-11-12"
    assert basis["profiles"]["count"] == 1
    assert basis["fingerprint"] == basis_core.fingerprint(basis)


def test_fingerprint_changes_after_quarterly_repricing() -> None:
    db = _StubDb(profile_rows=[_profile("dwd.trade_order_detail")])
    before = _basis(db, _Group(version=3))
    after = _basis(db, _Group(version=4, hot_price=1.02))
    assert before["fingerprint"] != after["fingerprint"]
    reasons = basis_core.diff(before, after)
    assert "策略版本 v3 → v4" in reasons


def test_fingerprint_changes_after_backfilled_access_row() -> None:
    old_rows = [_access("t1", AS_OF, 10)]
    db = _StubDb(access_rows=old_rows, profile_rows=[_profile("dwd.trade_order_detail")])
    before = _basis(db)
    # 补录一条 60 天前的访问（不影响近 7 天闸门，但会改变热度窗口的事实）
    db.access_rows = old_rows + [_access("t1", date(2026, 9, 13), 3)]
    after = _basis(db)
    assert before["fingerprint"] != after["fingerprint"]
    assert basis_core.diff(before, after) == ["访问日志已变化（含回溯补录）"]


def test_fingerprint_changes_after_profile_size_change() -> None:
    db = _StubDb(profile_rows=[_profile("dwd.trade_order_detail", size=100.0)])
    before = _basis(db)
    db.profile_rows = [_profile("dwd.trade_order_detail", size=120.0)]
    after = _basis(db)
    assert before["fingerprint"] != after["fingerprint"]
    assert basis_core.diff(before, after) == ["表档案（体积 / 分区 / 报表引用）已变化"]


def test_fingerprint_changes_when_window_moves() -> None:
    rows = [_access("t1", AS_OF, 10)]
    db = _StubDb(access_rows=rows, profile_rows=[_profile("dwd.trade_order_detail")])
    before = _basis(db)
    after = basis_service.current_basis(db, _Group(), date(2026, 11, 13))
    assert before["fingerprint"] != after["fingerprint"]
    assert "热度窗口已前移" in basis_core.diff(before, after)[-1]


def test_capture_writes_snapshot_and_metadata() -> None:
    db = _StubDb(profile_rows=[_profile("dwd.trade_order_detail")])
    plan = MigrationPlan(
        id=uuid.uuid4(), code="MP-2026Q4-888", title="t", status="draft",
        policy_version=3, threshold=20,
    )
    basis = basis_service.capture(db, plan, _Group(), AS_OF, reasons="首测")
    assert plan.basis_fingerprint == basis["fingerprint"]
    assert plan.basis_as_of == "2026-11-12"
    assert plan.basis_snapshot == basis
    assert plan.basis_captured_at is not None
    assert plan.basis_reasons == "首测"
    assert db.flushed >= 1
