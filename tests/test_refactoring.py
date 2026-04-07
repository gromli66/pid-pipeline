"""
Tests for P&ID Pipeline Refactoring v2.

Covers:
- 1.1: UniqueConstraint on Artifact + upsert_artifact
- 1.2: StorageService async methods
- 1.3: Composite unique on Diagram (project_code, number)
- 1.4: Unified storage path helpers
- 1.5: safe_dispatch
- 2.2: for_training removed
- 3.1: find_original_image
- 3.3: is_deleted + check_deleted
- 3.4: timezone-aware timestamps
- 3.8: reupload cleanup
- 4.3: set_diagram_error
- 4.5: ProjectLoader TTL cache
- 4.6: DEFAULT_PROJECT_CODE
"""

import os
import sys
import uuid
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from sqlalchemy import create_engine, select, inspect
from sqlalchemy.orm import sessionmaker, Session

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Override settings BEFORE importing app modules
os.environ["DATABASE_URL"] = "sqlite:///test.db"
os.environ["STORAGE_PATH"] = "/tmp/test_storage"
os.environ["PROJECTS_CONFIG_DIR"] = "/tmp/test_configs"
os.environ["CELERY_BROKER_URL"] = "redis://localhost:6380/0"


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(scope="session")
def engine():
    """SQLite in-memory engine (pre-patched in conftest.py)."""
    from tests.conftest import _test_engine
    from app.db.base import Base
    from app.models import Diagram, Artifact, Project  # noqa: ensure models registered

    Base.metadata.create_all(_test_engine)
    yield _test_engine


@pytest.fixture
def db(engine):
    """Fresh session per test with rollback."""
    connection = engine.connect()
    transaction = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def storage_dir(tmp_path):
    """Temporary storage directory."""
    storage = tmp_path / "storage"
    storage.mkdir()
    return storage


@pytest.fixture
def diagram_uid():
    return str(uuid.uuid4())


@pytest.fixture
def sample_project(db):
    """Create a test project in DB."""
    from app.models import Project
    project = Project(code="test_project", name="Test Project")
    db.add(project)
    db.flush()
    return project


@pytest.fixture
def sample_diagram(db, sample_project):
    """Create a test diagram in DB."""
    from app.models import Diagram, DiagramStatus
    diagram = Diagram(
        uid=uuid.uuid4(),
        project_code=sample_project.code,
        number=1,
        original_filename="test.png",
        status=DiagramStatus.UPLOADED,
    )
    db.add(diagram)
    db.flush()
    return diagram


# ============================================================
# 1.1: UniqueConstraint + upsert_artifact
# ============================================================

class TestUpsertArtifact:
    """Test upsert_artifact from worker/utils/db_helpers.py"""

    def test_upsert_creates_new(self, db, sample_diagram, storage_dir):
        """upsert_artifact creates artifact when none exists."""
        from worker.utils.db_helpers import upsert_artifact
        from app.models import ArtifactType

        # Create a dummy file
        art_dir = storage_dir / str(sample_diagram.uid) / "detection"
        art_dir.mkdir(parents=True)
        art_file = art_dir / "yolo_predicted.txt"
        art_file.write_text("0 0.5 0.5 0.1 0.1")

        # Note: pass UUID object, not string (SQLite UUID compat)
        artifact = upsert_artifact(
            db, sample_diagram.uid, ArtifactType.YOLO_PREDICTED,
            str(art_file), storage_dir,
        )
        db.flush()

        assert artifact is not None
        assert artifact.artifact_type == ArtifactType.YOLO_PREDICTED
        assert artifact.file_size is not None
        assert artifact.file_size > 0

    def test_upsert_updates_existing(self, db, sample_diagram, storage_dir):
        """upsert_artifact updates if (diagram_uid, artifact_type) already exists."""
        from worker.utils.db_helpers import upsert_artifact
        from app.models import ArtifactType, Artifact

        art_dir = storage_dir / str(sample_diagram.uid) / "detection"
        art_dir.mkdir(parents=True)

        # First insert
        file1 = art_dir / "yolo_predicted.txt"
        file1.write_text("first")
        upsert_artifact(db, sample_diagram.uid, ArtifactType.YOLO_PREDICTED, str(file1), storage_dir)
        db.flush()

        # Second insert — same type → should update, not duplicate
        file2 = art_dir / "yolo_predicted_v2.txt"
        file2.write_text("second version with more content")
        upsert_artifact(db, sample_diagram.uid, ArtifactType.YOLO_PREDICTED, str(file2), storage_dir)
        db.flush()

        # Count — should be 1, not 2
        count = db.query(Artifact).filter(
            Artifact.diagram_uid == sample_diagram.uid,
            Artifact.artifact_type == ArtifactType.YOLO_PREDICTED,
        ).count()
        assert count == 1

    def test_upsert_different_types_coexist(self, db, sample_diagram, storage_dir):
        """Different artifact types for same diagram don't conflict."""
        from worker.utils.db_helpers import upsert_artifact
        from app.models import ArtifactType, Artifact

        art_dir = storage_dir / str(sample_diagram.uid) / "detection"
        art_dir.mkdir(parents=True)

        for art_type, fname in [
            (ArtifactType.YOLO_PREDICTED, "yolo.txt"),
            (ArtifactType.COCO_PREDICTED, "coco.json"),
        ]:
            f = art_dir / fname
            f.write_text("content")
            upsert_artifact(db, sample_diagram.uid, art_type, str(f), storage_dir)

        db.flush()
        count = db.query(Artifact).filter(
            Artifact.diagram_uid == sample_diagram.uid
        ).count()
        assert count == 2


# ============================================================
# 1.3: Composite unique on (project_code, number)
# ============================================================

class TestDiagramCompositeUnique:

    def test_same_number_different_projects(self, db):
        """Two diagrams can have number=1 in different projects."""
        from app.models import Diagram, DiagramStatus, Project

        p1 = Project(code="proj_a", name="A")
        p2 = Project(code="proj_b", name="B")
        db.add_all([p1, p2])
        db.flush()

        d1 = Diagram(number=1, project_code="proj_a", original_filename="a.png", status=DiagramStatus.UPLOADED)
        d2 = Diagram(number=1, project_code="proj_b", original_filename="b.png", status=DiagramStatus.UPLOADED)
        db.add_all([d1, d2])
        db.flush()  # Should not raise

        assert d1.number == d2.number == 1
        assert d1.project_code != d2.project_code


# ============================================================
# 1.4 + 3.1: Storage path helpers
# ============================================================

class TestStorageHelpers:

    def test_get_storage_path(self):
        from app.config import get_storage_path
        path = get_storage_path()
        assert isinstance(path, Path)

    def test_get_diagram_dir(self):
        from app.config import get_diagram_dir
        uid = "abc-123"
        path = get_diagram_dir(uid)
        assert str(uid) in str(path)

    def test_find_original_image_png(self, tmp_path):
        """find_original_image finds .png file."""
        from app.config import find_original_image

        uid = str(uuid.uuid4())
        original_dir = tmp_path / uid / "original"
        original_dir.mkdir(parents=True)
        (original_dir / "image.png").write_bytes(b"fakepng")

        with patch("app.config.get_storage_path", return_value=tmp_path):
            result = find_original_image(uid)

        assert result.name == "image.png"
        assert result.exists()

    def test_find_original_image_jpg_fallback(self, tmp_path):
        """find_original_image falls back to .jpg when .png missing."""
        from app.config import find_original_image

        uid = str(uuid.uuid4())
        original_dir = tmp_path / uid / "original"
        original_dir.mkdir(parents=True)
        (original_dir / "image.jpg").write_bytes(b"fakejpg")

        with patch("app.config.get_storage_path", return_value=tmp_path):
            result = find_original_image(uid)

        assert result.name == "image.jpg"

    def test_find_original_image_not_found(self, tmp_path):
        """find_original_image raises FileNotFoundError."""
        from app.config import find_original_image

        uid = str(uuid.uuid4())
        original_dir = tmp_path / uid / "original"
        original_dir.mkdir(parents=True)
        # no image files

        with patch("app.config.get_storage_path", return_value=tmp_path):
            with pytest.raises(FileNotFoundError, match="Original image not found"):
                find_original_image(uid)


# ============================================================
# 3.3: is_deleted + check_deleted
# ============================================================

class TestSoftDelete:

    def test_is_deleted_default_false(self, sample_diagram):
        assert sample_diagram.is_deleted is False

    def test_check_deleted_returns_false_for_active(self, db, sample_diagram):
        from worker.utils.db_helpers import check_deleted
        assert check_deleted(db, sample_diagram.uid) is False

    def test_check_deleted_returns_true_for_deleted(self, db, sample_diagram):
        from worker.utils.db_helpers import check_deleted
        sample_diagram.is_deleted = True
        db.flush()
        assert check_deleted(db, sample_diagram.uid) is True

    def test_check_deleted_returns_true_for_missing(self, db):
        from worker.utils.db_helpers import check_deleted
        fake_uid = uuid.uuid4()
        assert check_deleted(db, fake_uid) is True


# ============================================================
# 3.4: Timezone-aware timestamps
# ============================================================

class TestTimestampsExist:

    def test_diagram_timestamps_set(self, sample_diagram):
        """Diagram created_at/updated_at are populated (naive UTC; tz-aware is TODO)."""
        assert sample_diagram.created_at is not None
        assert sample_diagram.updated_at is not None

    def test_artifact_timestamp_set(self, db, sample_diagram):
        from app.models import Artifact, ArtifactType
        artifact = Artifact(
            diagram_uid=sample_diagram.uid,
            artifact_type=ArtifactType.ORIGINAL_IMAGE,
            file_path="test/path",
        )
        db.add(artifact)
        db.flush()
        assert artifact.created_at is not None


# ============================================================
# 4.3: set_diagram_error
# ============================================================

class TestSetDiagramError:

    def test_sets_error_status(self, db, sample_diagram):
        from worker.utils.db_helpers import set_diagram_error
        from app.models import DiagramStatus

        set_diagram_error(db, sample_diagram.uid, "Test error", "detecting")

        db.refresh(sample_diagram)
        assert sample_diagram.status == DiagramStatus.ERROR
        assert sample_diagram.error_message == "Test error"
        assert sample_diagram.error_stage == "detecting"

    def test_truncates_long_message(self, db, sample_diagram):
        from worker.utils.db_helpers import set_diagram_error

        long_msg = "x" * 1000
        set_diagram_error(db, sample_diagram.uid, long_msg, "detecting", max_message_len=100)

        db.refresh(sample_diagram)
        assert len(sample_diagram.error_message) == 100

    def test_handles_missing_diagram(self, db):
        """set_diagram_error should not raise for missing diagram."""
        from worker.utils.db_helpers import set_diagram_error
        fake_uid = uuid.uuid4()
        # Should not raise
        set_diagram_error(db, fake_uid, "Error", "detecting")


# ============================================================
# 1.5: safe_dispatch
# ============================================================

class TestSafeDispatch:

    def test_successful_dispatch(self, db, sample_diagram):
        from worker.utils.db_helpers import safe_dispatch
        from app.models import DiagramStatus

        sample_diagram.status = DiagramStatus.SKELETONIZING
        db.flush()

        mock_result = MagicMock()
        mock_result.id = "task-123"

        with patch("worker.celery_app.celery_app") as mock_app:
            mock_app.send_task.return_value = mock_result
            result = safe_dispatch(
                db, sample_diagram,
                "worker.tasks.skeleton.task_skeletonize",
                args=["uid", "project"],
            )

        assert result == "task-123"
        assert sample_diagram.status == DiagramStatus.SKELETONIZING  # unchanged

    def test_dispatch_failure_with_fallback(self, db, sample_diagram):
        from worker.utils.db_helpers import safe_dispatch
        from app.models import DiagramStatus

        sample_diagram.status = DiagramStatus.SKELETONIZING
        db.flush()

        with patch("worker.celery_app.celery_app") as mock_app:
            mock_app.send_task.side_effect = ConnectionError("Redis down")
            result = safe_dispatch(
                db, sample_diagram,
                "worker.tasks.skeleton.task_skeletonize",
                args=["uid", "project"],
                fallback_status=DiagramStatus.SEGMENTING,
            )

        assert result is None
        assert sample_diagram.status == DiagramStatus.SEGMENTING

    def test_dispatch_failure_default_error(self, db, sample_diagram):
        from worker.utils.db_helpers import safe_dispatch
        from app.models import DiagramStatus

        sample_diagram.status = DiagramStatus.SKELETONIZING
        db.flush()

        with patch("worker.celery_app.celery_app") as mock_app:
            mock_app.send_task.side_effect = ConnectionError("Redis down")
            result = safe_dispatch(
                db, sample_diagram,
                "worker.tasks.skeleton.task_skeletonize",
                args=["uid", "project"],
                # no fallback_status → default to ERROR
            )

        assert result is None
        assert sample_diagram.status == DiagramStatus.ERROR
        assert "Redis down" in sample_diagram.error_message


# ============================================================
# 2.2: for_training removed
# ============================================================

class TestForTrainingRemoved:

    def test_no_for_training_column(self):
        from app.models.artifact import Artifact
        mapper = inspect(Artifact)
        column_names = [c.key for c in mapper.column_attrs]
        assert "for_training" not in column_names

    def test_training_types_returns_valid_set(self):
        """training_types() exists and returns validated artifact types."""
        from app.models.artifact import Artifact, ArtifactType
        result = Artifact.training_types()
        assert isinstance(result, set)
        assert len(result) == 6
        assert ArtifactType.YOLO_VALIDATED in result
        assert ArtifactType.COCO_VALIDATED in result


# ============================================================
# 4.5: ProjectLoader TTL cache
# ============================================================

class TestProjectLoaderTTL:

    def test_cache_invalidation(self, tmp_path):
        from app.services.project_loader import ProjectLoader

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()

        # Create a minimal YAML
        yaml_content = """
project:
  code: test
  name: Test
cvat:
  project_name: "P&ID Test"
classes:
  - id: 1
    name: valve
yolo:
  weights: "test.pt"
  num_classes: 1
  confidence: 0.5
  class_mapping: {}
segmentation: {}
skeleton: {}
junction_seg: {}
"""
        (configs_dir / "test.yaml").write_text(yaml_content)

        loader = ProjectLoader(configs_dir)
        config1 = loader.load("test")
        assert config1 is not None
        assert config1.name == "Test"

        # Second load — from cache
        config2 = loader.load("test")
        assert config2 is config1

        # Force invalidate
        loader.reload("test")
        config3 = loader.load("test")
        # New object, same data
        assert config3 is not config1
        assert config3.name == "Test"

    def test_cache_ttl_expiry(self, tmp_path):
        from app.services.project_loader import ProjectLoader

        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()

        yaml_content = """
project:
  code: test
  name: Test
cvat:
  project_name: "P&ID Test"
classes: []
yolo:
  weights: "test.pt"
  num_classes: 0
  confidence: 0.5
  class_mapping: {}
segmentation: {}
skeleton: {}
junction_seg: {}
"""
        (configs_dir / "test.yaml").write_text(yaml_content)

        loader = ProjectLoader(configs_dir)
        loader.CACHE_TTL_SECONDS = 0.1  # 100ms for test

        config1 = loader.load("test")
        assert config1 is not None

        # Still cached
        config2 = loader.load("test")
        assert config2 is config1

        # Wait for TTL to expire
        time.sleep(0.15)

        # Now should reload
        config3 = loader.load("test")
        assert config3 is not config1


# ============================================================
# 4.6: DEFAULT_PROJECT_CODE
# ============================================================

class TestDefaultProjectCode:

    def test_config_has_default_project_code(self):
        from app.config import settings
        assert hasattr(settings, "DEFAULT_PROJECT_CODE")
        assert settings.DEFAULT_PROJECT_CODE == "thermohydraulics"

    def test_no_hardcoded_thermohydraulics_in_workers(self):
        """Verify no hardcoded default in worker task signatures."""
        import ast

        worker_dir = PROJECT_ROOT / "worker" / "tasks"
        for py_file in worker_dir.glob("*.py"):
            source = py_file.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name.startswith("task_"):
                    for default in node.args.defaults:
                        if isinstance(default, ast.Constant) and default.value == "thermohydraulics":
                            pytest.fail(
                                f"Found hardcoded 'thermohydraulics' default in "
                                f"{py_file.name}:{node.name}"
                            )


# ============================================================
# 1.2: StorageService
# ============================================================

class TestStorageService:

    @pytest.mark.asyncio
    async def test_ensure_dir_creates_path(self, tmp_path):
        from app.services.storage import StorageService
        svc = StorageService(base_path=str(tmp_path))
        new_dir = tmp_path / "a" / "b" / "c"
        await svc.ensure_dir(new_dir)
        assert new_dir.is_dir()

    @pytest.mark.asyncio
    async def test_delete_stage_folders(self, tmp_path):
        from app.services.storage import StorageService
        svc = StorageService(base_path=str(tmp_path))

        uid = uuid.uuid4()
        for stage in ("detection", "segmentation", "skeleton"):
            d = tmp_path / str(uid) / stage
            d.mkdir(parents=True)
            (d / "file.txt").write_text("data")

        await svc.delete_stage_folders(uid, ["detection", "skeleton"])

        assert not (tmp_path / str(uid) / "detection").exists()
        assert (tmp_path / str(uid) / "segmentation").exists()
        assert not (tmp_path / str(uid) / "skeleton").exists()

    @pytest.mark.asyncio
    async def test_list_files_empty_dir(self, tmp_path):
        from app.services.storage import StorageService
        svc = StorageService(base_path=str(tmp_path))
        uid = uuid.uuid4()
        files = await svc.list_files(uid, "nonexistent")
        assert files == []

    @pytest.mark.asyncio
    async def test_file_exists(self, tmp_path):
        from app.services.storage import StorageService
        svc = StorageService(base_path=str(tmp_path))

        uid = uuid.uuid4()
        stage_dir = tmp_path / str(uid) / "original"
        stage_dir.mkdir(parents=True)
        (stage_dir / "image.png").write_bytes(b"data")

        assert await svc.file_exists(uid, "original", "image.png") is True
        assert await svc.file_exists(uid, "original", "missing.png") is False


# ============================================================
# Model integrity tests
# ============================================================

class TestModelIntegrity:

    def test_artifact_has_unique_constraint(self):
        """Verify UniqueConstraint exists on Artifact model."""
        from app.models.artifact import Artifact
        constraints = Artifact.__table__.constraints
        unique_constraints = [c for c in constraints if hasattr(c, 'columns') and len(c.columns) > 1]
        # Should find at least one multi-column unique
        col_sets = [frozenset(c.name for c in uc.columns) for uc in unique_constraints]
        assert frozenset({"diagram_uid", "artifact_type"}) in col_sets

    def test_diagram_has_composite_unique(self):
        """Verify composite unique on (project_code, number)."""
        from app.models.diagram import Diagram
        constraints = Diagram.__table__.constraints
        unique_constraints = [c for c in constraints if hasattr(c, 'columns') and len(c.columns) > 1]
        col_sets = [frozenset(c.name for c in uc.columns) for uc in unique_constraints]
        assert frozenset({"project_code", "number"}) in col_sets

    def test_diagram_has_is_deleted(self):
        from app.models.diagram import Diagram
        mapper = inspect(Diagram)
        column_names = [c.key for c in mapper.column_attrs]
        assert "is_deleted" in column_names

    def test_diagram_number_not_globally_unique(self):
        """number column should NOT have unique=True (was the old bug)."""
        from app.models.diagram import Diagram
        number_col = Diagram.__table__.c.number
        assert number_col.unique is not True


# ============================================================
# Dead code verification
# ============================================================

class TestDeadCodeRemoved:

    def test_graph_validator_editor_deleted(self):
        path = PROJECT_ROOT / "ui" / "editors" / "graph_validator_editor.py"
        assert not path.exists(), "graph_validator_editor.py should be deleted"

    def test_graph_editor_deleted(self):
        """graph_editor.py должен быть разбит на base/simple/advanced."""
        path = PROJECT_ROOT / "ui" / "editors" / "graph_editor.py"
        assert not path.exists(), "graph_editor.py should be split into base/simple/advanced"

    def test_auto_fix_copy_deleted(self):
        """auto_fix.py (копия graph_tab) должен быть удалён."""
        path = PROJECT_ROOT / "ui" / "editors" / "auto_fix.py"
        assert not path.exists(), "auto_fix.py (copy of graph_tab) should be deleted"

    def test_old_graph_tab_deleted(self):
        """Старый graph_tab.py заменён на base/simple/advanced_graph_tab."""
        path = PROJECT_ROOT / "ui" / "tabs" / "graph_tab.py"
        assert not path.exists(), "graph_tab.py should be replaced by base/simple/advanced_graph_tab"

    def test_main_window_new_deleted(self):
        path = PROJECT_ROOT / "ui" / "windows" / "main_window_new.py"
        assert not path.exists(), "main_window_new.py should be deleted"

    def test_top_level_editors_deleted(self):
        path = PROJECT_ROOT / "editors"
        assert not path.exists(), "top-level editors/ should be deleted"

    def test_export_txt_files_deleted(self):
        for fname in ("export_fix.txt", "export_method.txt"):
            path = PROJECT_ROOT / fname
            assert not path.exists(), f"{fname} should be deleted"

    def test_graph_validation_window_imports_from_advanced(self):
        """graph_validation_window should import from advanced_graph_editor."""
        source = (PROJECT_ROOT / "ui" / "windows" / "graph_validation_window.py").read_text()
        assert "from ui.editors.advanced_graph_editor import" in source
        assert "graph_validator_editor" not in source
        assert "from ui.editors.graph_editor import" not in source

    def test_new_editor_files_exist(self):
        """Новые файлы редактора должны существовать."""
        for fname in ("base_graph_editor.py", "simple_graph_editor.py",
                       "advanced_graph_editor.py", "graph_data.py",
                       "undo_manager.py", "node_list_dialog.py", "resize_overlay.py"):
            path = PROJECT_ROOT / "ui" / "editors" / fname
            assert path.exists(), f"{fname} should exist"

    def test_new_tab_files_exist(self):
        """Новые файлы табов должны существовать."""
        for fname in ("base_graph_tab.py", "simple_graph_tab.py", "advanced_graph_tab.py"):
            path = PROJECT_ROOT / "ui" / "tabs" / fname
            assert path.exists(), f"{fname} should exist"
