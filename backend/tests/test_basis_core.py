"""测算依据（basis）纯函数层单测：指纹稳定、差异归因、失效判定。

不连库：指纹 / 差异 / 新鲜度只依赖 dict 与带属性的计划对象。
验收口径：同一依据指纹恒定；任一参与字段变化即失效；已提交计划永远 frozen。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services import basis_core, basis_service


def _basis(**overrides) -> dict:
    base = {
        "as_of": "2026-11-12",
        "policy": {
            "version": 3,
            "prices": {"hot": 1.05, "warm": 0.85, "cold": 0.55},
            "score_min": {"hot": 60.0, "warm": 20.0},
            "quiet_days": 30,
            "block_recent_days": 7,
            "min_partition": 3,
            "migrate_price": 0.75,
            "migrate_fixed": 270.0,
        },
        "heat": {"window_days": 90, "half_life_days": 14.0, "report_bonus_per_ref": 8, "report_bonus_cap": 20},
        "access": {"rows": 3600, "total_access": 123456, "max_date": "2026-11-12", "digest": "a" * 64},
        "profiles": {"count": 42, "digest": "b" * 64},
    }
    base.update(overrides)
    return base


def test_fingerprint_is_deterministic_and_order_insensitive() -> None:
    first = _basis()
    second = dict(reversed(list(_basis().items())))
    assert basis_core.fingerprint(first) == basis_core.fingerprint(second)
    assert basis_core.fingerprint(first) == basis_core.fingerprint(first)
    assert len(basis_core.fingerprint(first)) == 64


def test_fingerprint_ignores_metadata_fields() -> None:
    basis = _basis()
    with_meta = {**basis, "fingerprint": "x" * 64, "captured_at": "2026-11-12T00:00:00Z"}
    assert basis_core.fingerprint(basis) == basis_core.fingerprint(with_meta)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("policy", _basis()["policy"] | {"version": 4}),
        ("policy", _basis()["policy"] | {"prices": {"hot": 1.02, "warm": 0.85, "cold": 0.51}}),
        ("heat", _basis()["heat"] | {"half_life_days": 21.0}),
        ("access", _basis()["access"] | {"digest": "c" * 64}),
        ("profiles", _basis()["profiles"] | {"digest": "d" * 64}),
        ("as_of", "2026-11-13"),
    ],
)
def test_any_basis_field_change_changes_fingerprint(key: str, value) -> None:
    assert basis_core.fingerprint(_basis(**{key: value})) != basis_core.fingerprint(_basis())


def test_diff_names_policy_repricing_and_backfilled_access() -> None:
    old = _basis(policy=_basis()["policy"] | {"version": 3, "prices": {"hot": 1.05, "warm": 0.85, "cold": 0.55}})
    new = _basis(
        policy=_basis()["policy"] | {"version": 4, "prices": {"hot": 1.02, "warm": 0.85, "cold": 0.51}},
        access=_basis()["access"] | {"rows": 3601, "total_access": 123500},
    )
    reasons = basis_core.diff(old, new)
    assert "策略版本 v3 → v4" in reasons
    assert "访问日志已变化（含回溯补录）" in reasons


def test_diff_identical_basis_is_empty() -> None:
    assert basis_core.diff(_basis(), _basis()) == []


def test_diff_without_snapshot_marks_missing_basis() -> None:
    assert basis_core.diff({}, _basis()) == ["计划尚未记录测算依据，请先风险复检"]


def test_diff_access_only_backfill_without_version_change() -> None:
    new = _basis(access=_basis()["access"] | {"digest": "e" * 64})
    reasons = basis_core.diff(_basis(), new)
    assert reasons == ["访问日志已变化（含回溯补录）"]


def _plan(status: str, fingerprint: str | None, snapshot: dict | None = None):
    return SimpleNamespace(
        status=status,
        basis_fingerprint=fingerprint,
        basis_snapshot=snapshot or {},
    )


def test_freshness_fresh_when_fingerprint_matches() -> None:
    basis = _basis()
    plan = _plan("draft", basis_core.fingerprint(basis), basis)
    fresh = basis_service.freshness(plan, basis)
    assert fresh["state"] == "fresh"
    assert fresh["is_stale"] is False
    assert fresh["reasons"] == []


def test_freshness_draft_stale_after_policy_upgrade() -> None:
    old = _basis()
    new = _basis(policy=_basis()["policy"] | {"version": 4})
    plan = _plan("draft", basis_core.fingerprint(old), old)
    fresh = basis_service.freshness(plan, new)
    assert fresh["state"] == "stale"
    assert fresh["is_stale"] is True
    assert "策略版本 v3 → v4" in fresh["reasons"]


def test_freshness_draft_without_basis_is_stale() -> None:
    fresh = basis_service.freshness(_plan("draft", None), _basis())
    assert fresh["is_stale"] is True
    assert fresh["reasons"] == ["计划尚未记录测算依据，请先风险复检"]


@pytest.mark.parametrize("status", ["submitted", "approved", "cancelled"])
def test_freshness_frozen_after_submit_never_stale(status: str) -> None:
    old = _basis()
    new = _basis(policy=_basis()["policy"] | {"version": 4})
    plan = _plan(status, basis_core.fingerprint(old), old)
    fresh = basis_service.freshness(plan, new)
    assert fresh["state"] == "frozen"
    assert fresh["is_stale"] is False
    assert fresh["drift"] is True
    assert "策略版本 v3 → v4" in fresh["reasons"]
