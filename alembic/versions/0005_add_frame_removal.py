"""Add frame/stamp removal pipeline enums.

NOTE: ALTER TYPE ... ADD VALUE cannot run inside a transaction in PostgreSQL.
We issue COMMIT before ALTER TYPE statements. This makes the migration
non-transactional -- downgrade is a no-op because PostgreSQL does not
support removing enum values.

Revision ID: 0005_add_frame_removal
Revises: 0004_add_contour_pipeline
"""

from alembic import op
from sqlalchemy import text

revision = "0005_add_frame_removal"
down_revision = "0004_add_contour_pipeline"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(text("COMMIT"))
    # DiagramStatus
    op.execute(text("ALTER TYPE diagramstatus ADD VALUE IF NOT EXISTS 'cleaning_frame'"))
    op.execute(text("ALTER TYPE diagramstatus ADD VALUE IF NOT EXISTS 'frame_cleaned'"))
    # ArtifactType
    op.execute(text("ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS 'original_cleaned'"))
    # StageType
    op.execute(text("ALTER TYPE stagetype ADD VALUE IF NOT EXISTS 'frame_removal'"))


def downgrade():
    # PostgreSQL does not support removing enum values.
    # Values are harmless if unused.
    pass
