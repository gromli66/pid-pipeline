"""Add residual_defects artifact type

Revision ID: 0012_add_residual_defects
Revises: 0011_add_layout_stagetype

Adds to PostgreSQL enum 'artifacttype':
- RESIDUAL_DEFECTS — остаточные очаги после авто-раскладки (Э12
  EDITOR_AFTER_LAYOUT_PLAN): невидимые трубы, боксы на чужих трубах,
  нелегальные наложения. Производен от GRAPH_CANVAS.
"""
from alembic import op

revision = "0012_add_residual_defects"
down_revision = "0011_add_layout_stagetype"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # COMMIT required: ALTER TYPE ADD VALUE cannot run inside a transaction
    op.execute("COMMIT")
    op.execute("ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS 'residual_defects'")


def downgrade() -> None:
    # PostgreSQL не поддерживает удаление значений из enum
    pass
