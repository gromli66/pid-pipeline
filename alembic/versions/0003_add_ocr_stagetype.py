"""Add 'ocr' to stagetype enum.

Revision ID: 0003_add_ocr_stagetype
Revises: a1b2c3d4e5f7
"""

from alembic import op

revision = '0003_add_ocr_stagetype'
down_revision = 'a1b2c3d4e5f7'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("COMMIT")
    op.execute("ALTER TYPE stagetype ADD VALUE IF NOT EXISTS 'ocr'")


def downgrade():
    # PostgreSQL does not support removing enum values.
    pass
