"""Add graph_canvas artifact type

Revision ID: 0009_add_graph_canvas
Revises: 0008_add_current_step
Create Date: 2026-07-16

Adds to PostgreSQL enum 'artifacttype':
- GRAPH_CANVAS — граф в холсте 1920x1080 (WYSIWYG «Ручная правка»),
  производная от graph_validated; назад не пишется.
"""
from alembic import op

revision = "0009_add_graph_canvas"
down_revision = "0008_add_current_step"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # COMMIT required: ALTER TYPE ADD VALUE cannot run inside a transaction
    op.execute("COMMIT")
    op.execute("ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS 'graph_canvas'")


def downgrade() -> None:
    # PostgreSQL не поддерживает удаление значений из enum
    pass
