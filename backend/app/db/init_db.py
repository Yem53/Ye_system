from sqlalchemy import text

from app.db import base  # noqa: F401
from app.db.session import engine
from app.models import (  # noqa: F401
    announcement,
    announcement_return,
    execution_log,
    manual_plan,
    position,
    trade_analysis,
    trade_plan,
)


def init_db() -> None:
    base.Base.metadata.create_all(bind=engine)
    ensure_execution_log_schema()


def ensure_execution_log_schema() -> None:
    """Ensure optional columns/constraints exist (for older databases without migrations)."""
    stmts = [
        # 确保 trade_plan_id 可以为 NULL（手动计划没有 trade_plan）
        text(
            """
            DO $$
            BEGIN
                -- 检查列是否存在且是 NOT NULL，如果是则修改为可空
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'execution_logs_codex'
                    AND column_name = 'trade_plan_id'
                    AND is_nullable = 'NO'
                ) THEN
                    ALTER TABLE execution_logs_codex
                    ALTER COLUMN trade_plan_id DROP NOT NULL;
                END IF;
            END$$;
            """
        ),
        text(
            """
            ALTER TABLE execution_logs_codex
            ADD COLUMN IF NOT EXISTS manual_plan_id UUID NULL
            """
        ),
        text(
            """
            ALTER TABLE execution_logs_codex
            ADD COLUMN IF NOT EXISTS position_id UUID NULL
            """
        ),
        text(
            """
            ALTER TABLE execution_logs_codex
            ADD COLUMN IF NOT EXISTS order_id VARCHAR(100) NULL
            """
        ),
        text(
            """
            ALTER TABLE execution_logs_codex
            ADD COLUMN IF NOT EXISTS symbol VARCHAR(50) NULL
            """
        ),
        text(
            """
            ALTER TABLE execution_logs_codex
            ADD COLUMN IF NOT EXISTS side VARCHAR(4) NULL
            """
        ),
        text(
            """
            ALTER TABLE execution_logs_codex
            ADD COLUMN IF NOT EXISTS price NUMERIC(36, 18) NULL
            """
        ),
        text(
            """
            ALTER TABLE execution_logs_codex
            ADD COLUMN IF NOT EXISTS quantity NUMERIC(36, 18) NULL
            """
        ),
        text(
            """
            ALTER TABLE execution_logs_codex
            ADD COLUMN IF NOT EXISTS status VARCHAR(50) NULL
            """
        ),
        text(
            """
            ALTER TABLE execution_logs_codex
            ADD COLUMN IF NOT EXISTS payload JSONB NULL
            """
        ),
        text(
            """
            ALTER TABLE manual_plans_codex
            ADD COLUMN IF NOT EXISTS max_slippage_pct NUMERIC(5, 4) DEFAULT 0.5 NOT NULL
            """
        ),
        text(
            """
            ALTER TABLE trade_plans_codex
            ADD COLUMN IF NOT EXISTS max_slippage_pct NUMERIC(5, 4) DEFAULT 0.5 NOT NULL
            """
        ),
        text(
            """
            ALTER TABLE positions_codex
            ADD COLUMN IF NOT EXISTS max_slippage_pct NUMERIC(5, 4) DEFAULT 0.5 NOT NULL
            """
        ),
        text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.table_constraints
                    WHERE constraint_name = 'execution_logs_codex_manual_plan_id_fkey'
                ) THEN
                    ALTER TABLE execution_logs_codex
                    ADD CONSTRAINT execution_logs_codex_manual_plan_id_fkey
                    FOREIGN KEY (manual_plan_id)
                    REFERENCES manual_plans_codex(id)
                    ON DELETE CASCADE;
                END IF;
            END$$;
            """
        ),
        text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM information_schema.table_constraints
                    WHERE constraint_name = 'execution_logs_codex_position_id_fkey'
                ) THEN
                    ALTER TABLE execution_logs_codex
                    ADD CONSTRAINT execution_logs_codex_position_id_fkey
                    FOREIGN KEY (position_id)
                    REFERENCES positions_codex(id)
                    ON DELETE CASCADE;
                END IF;
            END$$;
            """
        ),
    ]
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(stmt)
