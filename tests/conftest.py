"""
Test configuration — neutralize async engine before any app imports.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# test_ocr_phase0.py — не pytest-модуль, а скрипт-проверка: печатает отчёт и
# зовёт sys.exit() на УРОВНЕ МОДУЛЯ. Pytest ловит это как INTERNALERROR и
# ОБРЫВАЕТ сбор целиком, поэтому всё, что идёт после него по алфавиту, в
# обычный прогон не попадало вовсе — test_phase7_cyrillic, test_refactoring,
# test_refactoring_v2, test_stage7_graph_flow, test_text_import, tests/ui,
# tests/test_worker. Запускать руками: python tests/test_ocr_phase0.py
# (в pytest.ini такая попытка уже была, но там collect_ignore не действует —
# это переменная conftest, а не опция ini, отсюда и PytestConfigWarning).
collect_ignore = ["test_ocr_phase0.py"]

# Add project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Set test env vars BEFORE any app import
os.environ["DATABASE_URL"] = "sqlite:///test.db"
os.environ["STORAGE_PATH"] = "/tmp/test_storage"
os.environ["PROJECTS_CONFIG_DIR"] = "/tmp/test_configs"
os.environ["CELERY_BROKER_URL"] = "redis://localhost:6380/0"

# Monkeypatch create_async_engine to return a mock
# This prevents the crash when app.db.session is imported
import sqlalchemy.ext.asyncio as _asyncio_mod
_original_create_async_engine = _asyncio_mod.create_async_engine
_asyncio_mod.create_async_engine = lambda *a, **kw: MagicMock()

# Also patch async_sessionmaker
import sqlalchemy.ext.asyncio as _asyncio_mod2
if hasattr(_asyncio_mod2, 'async_sessionmaker'):
    _original_async_sessionmaker = _asyncio_mod2.async_sessionmaker
    _asyncio_mod2.async_sessionmaker = lambda *a, **kw: MagicMock()

# Now safe to import app modules
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_test_engine = create_engine("sqlite:///:memory:", echo=False)
_test_session_factory = sessionmaker(bind=_test_engine)

# Patch session module after it's imported
import app.db.session as _session_mod
_session_mod.engine = _test_engine
_session_mod.SessionLocal = _test_session_factory
