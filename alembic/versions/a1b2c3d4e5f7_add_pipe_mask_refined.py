"""Add pipe_mask_refined artifact type

Revision ID: a1b2c3d4e5f7
Revises: 0001_full_schema
Create Date: 2026-03-25

Adds to PostgreSQL enum 'artifacttype':
- PIPE_MASK_REFINED
"""
from alembic import op

revision = 'a1b2c3d4e5f7'
down_revision = '0001_full_schema'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # COMMIT required: ALTER TYPE ADD VALUE cannot run inside a transaction
    op.execute("COMMIT")
    op.execute("ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS 'PIPE_MASK_REFINED'")


def downgrade() -> None:
    # PostgreSQL не поддерживает удаление значений из enum
    pass
