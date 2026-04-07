"""
Smoke tests: verify all worker tasks and utilities import without errors.

These catch broken imports after refactoring (e.g. renamed functions,
removed modules, circular imports). They do NOT test business logic.
"""

import pytest


# ============================================================
# Worker utils
# ============================================================

class TestDbHelpersImport:

    def test_set_diagram_error(self):
        from worker.utils.db_helpers import set_diagram_error
        assert callable(set_diagram_error)

    def test_check_deleted(self):
        from worker.utils.db_helpers import check_deleted
        assert callable(check_deleted)

    def test_upsert_artifact(self):
        from worker.utils.db_helpers import upsert_artifact
        assert callable(upsert_artifact)

    def test_safe_dispatch(self):
        from worker.utils.db_helpers import safe_dispatch
        assert callable(safe_dispatch)


# ============================================================
# Worker tasks
# ============================================================

class TestTaskImports:
    """Each task module imports without error and exposes its celery task."""

    def test_detection(self):
        from worker.tasks.detection import task_detect_yolo
        assert callable(task_detect_yolo)

    def test_segmentation(self):
        from worker.tasks.segmentation import task_segment_pipes
        assert callable(task_segment_pipes)

    def test_skeleton(self):
        from worker.tasks.skeleton import task_skeletonize
        from worker.tasks.skeleton import task_skeletonize_simple
        assert callable(task_skeletonize)
        assert callable(task_skeletonize_simple)

    def test_junction(self):
        from worker.tasks.junction import task_detect_junctions
        assert callable(task_detect_junctions)

    def test_graph(self):
        from worker.tasks.graph import task_build_graph
        from worker.tasks.graph import task_generate_fxml
        assert callable(task_build_graph)
        assert callable(task_generate_fxml)

    def test_ocr(self):
        from worker.tasks.ocr import task_run_ocr
        assert callable(task_run_ocr)


# ============================================================
# Models
# ============================================================

class TestModelImports:

    def test_diagram_status_enum(self):
        from app.models.diagram import DiagramStatus
        assert hasattr(DiagramStatus, "UPLOADED")
        assert hasattr(DiagramStatus, "ERROR")
        assert DiagramStatus.UPLOADED.value == "uploaded"

    def test_artifact_type_enum(self):
        from app.models.artifact import ArtifactType
        assert hasattr(ArtifactType, "ORIGINAL_IMAGE")
        assert hasattr(ArtifactType, "FXML")

    def test_all_diagram_status_values_lowercase(self):
        """BUG-8 regression: all DiagramStatus values must be lowercase."""
        from app.models.diagram import DiagramStatus
        for member in DiagramStatus:
            assert member.value == member.value.lower(), \
                f"DiagramStatus.{member.name} has non-lowercase value: {member.value!r}"

    def test_all_artifact_type_values_lowercase(self):
        """BUG-9 regression: all ArtifactType values must be lowercase."""
        from app.models.artifact import ArtifactType
        for member in ArtifactType:
            assert member.value == member.value.lower(), \
                f"ArtifactType.{member.name} has non-lowercase value: {member.value!r}"

    def test_diagram_model(self):
        from app.models.diagram import Diagram
        assert hasattr(Diagram, "uid")
        assert hasattr(Diagram, "status")
        assert hasattr(Diagram, "is_deleted")

    def test_artifact_model(self):
        from app.models.artifact import Artifact
        assert hasattr(Artifact, "diagram_uid")
        assert hasattr(Artifact, "artifact_type")
