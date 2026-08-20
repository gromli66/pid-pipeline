"""Add prtx artifact type

Revision ID: 0013_add_prtx
Revises: 0012_add_residual_defects

Adds to PostgreSQL enum 'artifacttype':
- PRTX — расчётная схема САПФИР (слой CMS), собранная коробкой prt_convertor
  из GRAPH_VALIDATED. Хранится рядом с FXML: fxml/diagram.prtx.
"""
from alembic import op

revision = "0013_add_prtx"
down_revision = "0012_add_residual_defects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # COMMIT required: ALTER TYPE ADD VALUE cannot run inside a transaction
    op.execute("COMMIT")
    op.execute("ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS 'prtx'")


def downgrade() -> None:
    # PostgreSQL не поддерживает удаление значений из enum
    pass
