"""建表引导：幂等 create_all（容器 entrypoint 第一步）。

demo 场景用 SQLAlchemy metadata 建表，避免首次启动卡在迁移版本对齐；
正式环境可改用 `alembic upgrade head`（见 backend/migrations/）。

create_all 只建新表、不给已有库补列 —— 这里额外执行一次幂等的加列 DDL，
让已有 volume 上的旧库也能拿到测算依据字段（与 0002_plan_basis 一致）。
"""
from __future__ import annotations

from sqlalchemy import text

from src.config.database import engine
from src.models import Base

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


def _ensure_columns() -> None:
    from src.config.settings import settings

    schema = settings.db_schema
    with engine.begin() as conn:
        for name, ddl in PLAN_COLUMNS:
            conn.execute(
                text(
                    f'ALTER TABLE "{schema}"."migration_plans" '
                    f'ADD COLUMN IF NOT EXISTS "{name}" {ddl}'
                )
            )
        for name, ddl in ITEM_COLUMNS:
            conn.execute(
                text(
                    f'ALTER TABLE "{schema}"."migration_plan_items" '
                    f'ADD COLUMN IF NOT EXISTS "{name}" {ddl}'
                )
            )
        conn.execute(
            text(
                f'CREATE INDEX IF NOT EXISTS "ix_migration_plans_basis_fingerprint" '
                f'ON "{schema}"."migration_plans" ("basis_fingerprint")'
            )
        )


def main() -> None:
    from src.config.settings import settings

    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{settings.db_schema}"'))
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    print(f"[bootstrap] schema={settings.db_schema} tables={len(Base.metadata.tables)}", flush=True)


if __name__ == "__main__":
    main()
