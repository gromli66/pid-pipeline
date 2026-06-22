"""Add 'direction_classification' to stagetype enum.

Revision ID: 0006_add_direction_stagetype
Revises: 0005_add_frame_removal
"""

from alembic import op

revision = "0006_add_direction_stagetype"
down_revision = "0005_add_frame_removal"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("COMMIT")
    op.execute(
        "ALTER TYPE stagetype ADD VALUE IF NOT EXISTS 'direction_classification'"
    )


def downgrade():
    # PostgreSQL does not support removing enum values.
    pass
