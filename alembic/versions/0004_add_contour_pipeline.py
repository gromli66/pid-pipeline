"""Add contour extraction pipeline enums.

NOTE: ALTER TYPE ... ADD VALUE cannot run inside a transaction in PostgreSQL.
We issue COMMIT before ALTER TYPE statements. This makes the migration
non-transactional -- downgrade is a no-op because PostgreSQL does not
support removing enum values.

Revision ID: 0004_add_contour_pipeline
Revises: 0003
"""

from alembic import op
from sqlalchemy import text

revision = "0004_add_contour_pipeline"
down_revision = "0003_add_ocr_stagetype"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(text("COMMIT"))
    # DiagramStatus
    op.execute(text("ALTER TYPE diagramstatus ADD VALUE IF NOT EXISTS 'extracting_contours'"))
    op.execute(text("ALTER TYPE diagramstatus ADD VALUE IF NOT EXISTS 'contours_extracted'"))
    op.execute(text("ALTER TYPE diagramstatus ADD VALUE IF NOT EXISTS 'contours_validated'"))
    # ArtifactType
    op.execute(text("ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS 'contours_auto'"))
    op.execute(text("ALTER TYPE artifacttype ADD VALUE IF NOT EXISTS 'contours_validated'"))
    # StageType
    op.execute(text("ALTER TYPE stagetype ADD VALUE IF NOT EXISTS 'contour_extraction'"))


def downgrade():
    # PostgreSQL does not support removing enum values.
    # Values are harmless if unused.
    pass
