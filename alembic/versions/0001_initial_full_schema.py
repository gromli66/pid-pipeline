"""Full initial schema (squashed from 7 migrations).

Creates all tables, enums, constraints, and indexes for the PID pipeline.

Revision ID: 0001_full_schema
Revises: -
Create Date: 2026-03-25
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = '0001_full_schema'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Enum types ──────────────────────────────────────────────────────
    # Python models use (str, Enum) with lowercase values.
    # PostgreSQL enum is case-sensitive, so values here MUST match model .value.

    diagramstatus = sa.Enum(
        # Upload
        'uploaded',
        # Phase 1: Detection
        'detecting', 'detected', 'validating_bbox', 'validated_bbox',
        # Phase 2: Segmentation + skeleton
        'segmenting', 'skeletonizing', 'skeletonized',
        # Phase 3: Mask validation
        'validating_masks', 'validated_masks',
        # Phase 4: Final skeletonization
        'skeletonizing_final', 'skeletonized_final',
        # Phase 5: Junction/Bridge
        'detecting_junctions', 'detected_junctions',
        # Phase 6: Junction validation
        'validating_junctions', 'validated_junctions',
        # Phase 7: Graph
        'building_graph', 'built', 'validating_graph', 'validated_graph',
        # Phase 8: OCR
        'ocr_processing', 'ocr_completed', 'ocr_bound',
        # Phase 9: FXML
        'generating_fxml', 'completed',
        # Error
        'error',
        name='diagramstatus',
    )

    artifacttype = sa.Enum(
        # Original
        'original_image',
        # Detection
        'yolo_predicted', 'yolo_validated',
        'coco_predicted', 'coco_validated',
        # Segmentation
        'node_mask', 'pipe_mask', 'pipe_mask_validated',
        # Skeleton
        'skeleton', 'skeleton_mask', 'skeleton_final',
        # Junction
        'junction_mask', 'bridge_mask',
        'junction_mask_validated', 'bridge_mask_validated',
        # Graph
        'graph_json', 'graph_validated',
        # OCR
        'ocr_cleaned', 'ocr_result', 'ocr_binding', 'ocr_validation',
        # Output
        'fxml',
        # Debug overlays
        'detection_overlay', 'segmentation_overlay', 'graph_overlay',
        name='artifacttype',
    )

    stagetype = sa.Enum(
        'upload', 'detection', 'cvat_validation', 'segmentation',
        'skeletonization', 'junction_classification', 'mask_validation',
        'final_skeletonization', 'graph_building', 'graph_validation',
        'fxml_generation',
        name='stagetype',
    )

    stagestatus = sa.Enum(
        'pending', 'running', 'completed', 'failed', 'skipped',
        name='stagestatus',
    )

    # ── Tables ──────────────────────────────────────────────────────────

    op.create_table(
        'projects',
        sa.Column('code', sa.String(50), primary_key=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('cvat_project_id', sa.Integer, nullable=True),
        sa.Column('cvat_project_name', sa.String(200), nullable=True),
        sa.Column('config_path', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )

    op.create_table(
        'diagrams',
        sa.Column('uid', UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('project_code', sa.String(50),
                  sa.ForeignKey('projects.code'), nullable=False, index=True),
        sa.Column('number', sa.Integer, nullable=False),
        sa.Column('original_filename', sa.String(255), nullable=False),
        sa.Column('status', diagramstatus, nullable=False,
                  server_default='uploaded'),
        sa.Column('error_message', sa.Text, nullable=True),
        sa.Column('error_stage', sa.String(50), nullable=True),
        sa.Column('cvat_task_id', sa.Integer, nullable=True),
        sa.Column('cvat_job_id', sa.Integer, nullable=True),
        sa.Column('image_width', sa.Integer, nullable=True),
        sa.Column('image_height', sa.Integer, nullable=True),
        sa.Column('detection_count', sa.Integer, nullable=True),
        sa.Column('detection_model', sa.String(64), nullable=True),
        sa.Column('validated_detection_count', sa.Integer, nullable=True),
        sa.Column('segmentation_pixels', sa.Integer, nullable=True),
        sa.Column('junction_count', sa.Integer, nullable=True),
        sa.Column('bridge_count', sa.Integer, nullable=True),
        sa.Column('node_count', sa.Integer, nullable=True),
        sa.Column('edge_count', sa.Integer, nullable=True),
        sa.Column('is_deleted', sa.Boolean, nullable=False,
                  server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        # Composite unique: project_code + number
        sa.UniqueConstraint('project_code', 'number',
                            name='uq_diagram_project_number'),
    )
    op.create_index('ix_diagrams_status', 'diagrams', ['status'])

    op.create_table(
        'artifacts',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('diagram_uid', UUID(as_uuid=True),
                  sa.ForeignKey('diagrams.uid', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('artifact_type', artifacttype, nullable=False, index=True),
        sa.Column('file_path', sa.String(500), nullable=False),
        sa.Column('file_size', sa.BigInteger, nullable=True),
        sa.Column('mime_type', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        # One artifact of each type per diagram
        sa.UniqueConstraint('diagram_uid', 'artifact_type',
                            name='uq_artifact_diagram_type'),
    )

    op.create_table(
        'processing_stages',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('diagram_uid', UUID(as_uuid=True),
                  sa.ForeignKey('diagrams.uid', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('stage_type', stagetype, nullable=False, index=True),
        sa.Column('status', stagestatus, nullable=False,
                  server_default='pending'),
        sa.Column('attempt', sa.Integer, server_default='1'),
        sa.Column('celery_task_id', sa.String(255), nullable=True),
        sa.Column('started_at', sa.DateTime, nullable=True),
        sa.Column('completed_at', sa.DateTime, nullable=True),
        sa.Column('duration_seconds', sa.Float, nullable=True),
        sa.Column('error_message', sa.Text, nullable=True),
        sa.Column('error_traceback', sa.Text, nullable=True),
        sa.Column('metrics_json', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime, nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('processing_stages')
    op.drop_table('artifacts')
    op.drop_table('diagrams')
    op.drop_table('projects')

    op.execute('DROP TYPE IF EXISTS stagestatus')
    op.execute('DROP TYPE IF EXISTS stagetype')
    op.execute('DROP TYPE IF EXISTS artifacttype')
    op.execute('DROP TYPE IF EXISTS diagramstatus')
