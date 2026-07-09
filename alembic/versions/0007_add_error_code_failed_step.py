"""Add error_code + failed_step to processing_stages.

Revision ID: 0007_add_error_code_failed_step
Revises: 0006_add_direction_stagetype

Волна 0 (observability): доменный код ошибки и упавший под-шаг на строке стадии.
Обе колонки nullable — заполняются только при ошибке; на успехе остаются NULL.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_add_error_code_failed_step"
down_revision = "0006_add_direction_stagetype"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "processing_stages",
        sa.Column("error_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "processing_stages",
        sa.Column("failed_step", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("processing_stages", "failed_step")
    op.drop_column("processing_stages", "error_code")
