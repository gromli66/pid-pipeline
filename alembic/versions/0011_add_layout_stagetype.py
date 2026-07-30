"""Add 'layout' to stagetype enum.

Стадия авто-раскладки графа (Э5 плана docs/planning/AUTO_LAYOUT_INTEGRATION.md).
Состояние задачи определяется именно стадией, а не наличием артефакта:
отсутствие холста неразличимо между «считает», «упала» и «не стартовала».

Revision ID: 0011_add_layout_stagetype
Revises: 0010_add_junction_points
"""

from alembic import op

revision = "0011_add_layout_stagetype"
down_revision = "0010_add_junction_points"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("COMMIT")
    op.execute("ALTER TYPE stagetype ADD VALUE IF NOT EXISTS 'layout'")


def downgrade():
    # PostgreSQL does not support removing enum values.
    pass
