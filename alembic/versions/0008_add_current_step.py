"""Add current_step to processing_stages.

Revision ID: 0008_add_current_step
Revises: 0007_add_error_code_failed_step

Волна B (observability): текущий под-шаг бегущей стадии — подстадия в статусбаре
клиента (на CPU-стендах видно, что медленная стадия жива). Nullable: заполняется
воркером на step.start, чистится на complete/fail.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_add_current_step"
down_revision = "0007_add_error_code_failed_step"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "processing_stages",
        sa.Column("current_step", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("processing_stages", "current_step")
