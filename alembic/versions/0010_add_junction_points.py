"""Add junction_points / junction_points_validated artifact types

Revision ID: 0010_add_junction_points
Revises: 0009_add_graph_canvas
Create Date: 2026-07-28

Adds to PostgreSQL enum 'artifacttype':
- JUNCTION_POINTS — модельный points.json этапа junction (центры квадратов);
- JUNCTION_POINTS_VALIDATED — центры, правленые оператором во вкладке
  «Проверка узлов». Без них операция «изменить размер перекрёстков» теряет
  источник истины при переоткрытии вкладки, а экстрактор с дефолтным окном 15
  не разбирает квадраты, ужатые до <15.

ПОРЯДОК ДЕПЛОЯ: миграция накатывается РАНЬШЕ кода воркера — иначе insert
нового значения enum уронит этап junction для всех диаграмм.
Значение enum в PostgreSQL неудаляемо, downgrade — no-op (как у 0009).
"""
from alembic import op

revision = "0010_add_junction_points"
down_revision = "0009_add_graph_canvas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # COMMIT required: ALTER TYPE ADD VALUE cannot run inside a transaction
    op.execute("COMMIT")
    op.execute("ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS 'junction_points'")
    op.execute(
        "ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS 'junction_points_validated'"
    )


def downgrade() -> None:
    # PostgreSQL не поддерживает удаление значений из enum
    pass
