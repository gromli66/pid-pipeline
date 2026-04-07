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
    # SQLAlchemy sends enum member NAMES (uppercase) by default,
    # so PostgreSQL enum values must match the Python enum member names.

    diagramstatus = sa.Enum(
        # Upload
        'UPLOADED',
        # Phase 1: Detection
        'DETECTING', 'DETECTED', 'VALIDATING_BBOX', 'VALIDATED_BBOX',
        # Phase 2: Segmentation + skeleton
        'SEGMENTING', 'SKELETONIZING', 'SKELETONIZED',
        # Phase 3: Mask validation
        'VALIDATING_MASKS', 'VALIDATED_MASKS',
        # Phase 4: Final skeletonization
        'SKELETONIZING_FINAL', 'SKELETONIZED_FINAL',
        # Phase 5: Junction/Bridge
        'DETECTING_JUNCTIONS', 'DETECTED_JUNCTIONS',
        # Phase 6: Junction validation
        'VALIDATING_JUNCTIONS', 'VALIDATED_JUNCTIONS',
        # Phase 7: Graph
        'BUILDING_GRAPH', 'BUILT', 'VALIDATING_GRAPH', 'VALIDATED_GRAPH',
        # Phase 8: OCR
        'OCR_PROCESSING', 'OCR_COMPLETED', 'OCR_BOUND',
        # Phase 9: FXML
        'GENERATING_FXML', 'COMPLETED',
        # Error
        'ERROR',
        name='diagramstatus',
    )

    artifacttype = sa.Enum(
        # Original
        'ORIGINAL_IMAGE',
        # Detection
        'YOLO_PREDICTED', 'YOLO_VALIDATED',
        'COCO_PREDICTED', 'COCO_VALIDATED',
        # Segmentation
        'NODE_MASK', 'PIPE_MASK', 'PIPE_MASK_VALIDATED',
        # Skeleton
        'SKELETON', 'SKELETON_MASK', 'SKELETON_FINAL',
        # Junction
        'JUNCTION_MASK', 'BRIDGE_MASK',
        'JUNCTION_MASK_VALIDATED', 'BRIDGE_MASK_VALIDATED',
        # Graph
        'GRAPH_JSON', 'GRAPH_VALIDATED',
        # OCR
        'OCR_CLEANED', 'OCR_RESULT', 'OCR_BINDING', 'OCR_VALIDATION',
        # Output
        'FXML',
        # Debug overlays
        'DETECTION_OVERLAY', 'SEGMENTATION_OVERLAY', 'GRAPH_OVERLAY',
        name='artifacttype',
    )

    stagetype = sa.Enum(
        'UPLOAD', 'DETECTION', 'CVAT_VALIDATION', 'SEGMENTATION',
        'SKELETONIZATION', 'JUNCTION_CLASSIFICATION', 'MASK_VALIDATION',
        'FINAL_SKELETONIZATION', 'GRAPH_BUILDING', 'GRAPH_VALIDATION',
        'FXML_GENERATION',
        name='stagetype',
    )

    stagestatus = sa.Enum(
        'PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'SKIPPED',
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
                  server_default='UPLOADED'),
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
                  server_default='PENDING'),
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
