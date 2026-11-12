"""basis snapshot：计划头记录测算依据，逐表保留上次结果。

Revision ID: 0002_plan_basis
Revises: 0001_initial
Create Date: 2026-11-12

  migration_plans
    + basis_as_of / basis_fingerprint / basis_snapshot / basis_reasons / basis_captured_at
    + previous_snapshot / previous_fingerprint / previous_at
  migration_plan_items
    + prev_*（size / hot_score / save_month / migration_cost / payback / net_save_year /
      risk / reasons / basis_as_of / policy_version）

全部 ADD COLUMN IF NOT EXISTS：对已存在（bootstrap create_all 建的）库同样幂等。
"""
from __future__ import annotations

from alembic import op

from src.config.settings import settings

revision = "0002_plan_basis"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

PLAN_COLUMNS = [
    ("basis_as_of", "VARCHAR(10)"),
    ("basis_fingerprint", "VARCHAR(64)"),
    ("basis_snapshot", "JSONB"),
    ("basis_reasons", "TEXT NOT NULL DEFAULT ''"),
    ("basis_captured_at", "TIMESTAMPTZ"),
    ("previous_snapshot", "JSONB"),
    ("previous_fingerprint", "VARCHAR(64)"),
    ("previous_at", "TIMESTAMPTZ"),
]

ITEM_COLUMNS = [
    ("prev_size_gb", "NUMERIC(12, 2)"),
    ("prev_hot_score", "NUMERIC(6, 2)"),
    ("prev_save_month", "NUMERIC(12, 2)"),
    ("prev_migration_cost", "NUMERIC(12, 2)"),
    ("prev_payback_months", "NUMERIC(8, 2)"),
    ("prev_net_save_year", "NUMERIC(12, 2)"),
    ("prev_risk", "VARCHAR(10)"),
    ("prev_reasons", "TEXT"),
    ("prev_basis_as_of", "VARCHAR(10)"),
    ("prev_policy_version", "INTEGER"),
]


def upgrade() -> None:
    schema = settings.db_schema
    for name, ddl in PLAN_COLUMNS:
        op.execute(
            f'ALTER TABLE "{schema}"."migration_plans" ADD COLUMN IF NOT EXISTS "{name}" {ddl}'
        )
    for name, ddl in ITEM_COLUMNS:
        op.execute(
            f'ALTER TABLE "{schema}"."migration_plan_items" ADD COLUMN IF NOT EXISTS "{name}" {ddl}'
        )
    op.execute(
        f'CREATE INDEX IF NOT EXISTS "ix_migration_plans_basis_fingerprint" '
        f'ON "{schema}"."migration_plans" ("basis_fingerprint")'
    )


def downgrade() -> None:
    schema = settings.db_schema
    op.execute(f'DROP INDEX IF EXISTS "{schema}"."ix_migration_plans_basis_fingerprint"')
    for name, _ in reversed(ITEM_COLUMNS):
        op.execute(f'ALTER TABLE "{schema}"."migration_plan_items" DROP COLUMN IF EXISTS "{name}"')
    for name, _ in reversed(PLAN_COLUMNS):
        op.execute(f'ALTER TABLE "{schema}"."migration_plans" DROP COLUMN IF EXISTS "{name}"')
