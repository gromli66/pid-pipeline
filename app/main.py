"""
P&ID Pipeline API - FastAPI Application.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import setup_logging, get_logger
from app.db import async_engine, get_async_db
from app.api import projects, diagrams, detection, cvat, segmentation, skeleton, junction, graph, validation, ocr, rollback, contours, frame, stats

# Настройка логирования (DEBUG по умолчанию, переключается через LOG_LEVEL в .env)
setup_logging(level=settings.LOG_LEVEL)

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle events."""
    logger.info("Starting P&ID Pipeline API v1.0.0")
    yield
    logger.info("Shutting down P&ID Pipeline API")
    await async_engine.dispose()


app = FastAPI(
    title="P&ID Pipeline API",
    description="API для обработки P&ID диаграмм",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # UI десктопный, credentials не нужны
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Глобальный обработчик исключений."""
    logger.error(f"Unhandled exception: {request.url.path} - {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Health
@app.get("/health", tags=["Health"])
async def health_check(db: AsyncSession = Depends(get_async_db)):
    """Проверка здоровья API и зависимостей."""
    checks = {"api": "healthy"}

    # Проверка PostgreSQL
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "healthy"
    except Exception as e:
        checks["database"] = f"unhealthy: {e}"

    # Проверка Redis (через Celery ping)
    try:
        from worker.celery_app import celery_app
        result = celery_app.control.ping(timeout=2)
        checks["redis"] = "healthy" if result else "no workers"
    except Exception:
        checks["redis"] = "unhealthy"

    status = "healthy" if all(v == "healthy" for v in checks.values()) else "degraded"
    return {"status": status, "checks": checks}


# Routers
app.include_router(projects.router, prefix="/api/projects", tags=["Projects"])
app.include_router(diagrams.router, prefix="/api/diagrams", tags=["Diagrams"])
app.include_router(frame.router, prefix="/api/frame", tags=["Frame"])
app.include_router(detection.router, prefix="/api/detection", tags=["Detection"])
app.include_router(cvat.router, prefix="/api/cvat", tags=["CVAT"])
app.include_router(segmentation.router, prefix="/api/segmentation", tags=["Segmentation"])
app.include_router(skeleton.router, prefix="/api/skeleton", tags=["Skeleton"])
app.include_router(junction.router, prefix="/api/junction", tags=["Junction"])
app.include_router(graph.router, prefix="/api/graph", tags=["Graph"])
app.include_router(validation.router, prefix="/api/validation", tags=["Validation"])
app.include_router(ocr.router, prefix="/api/ocr", tags=["OCR"])
app.include_router(contours.router, prefix="/api/contours", tags=["Contours"])
app.include_router(rollback.router, prefix="/api/diagrams", tags=["Rollback"])
app.include_router(stats.router, prefix="/api/stats", tags=["Stats"])


@app.get("/", tags=["Root"])
async def root():
    """Корневой endpoint."""
    return {"name": "P&ID Pipeline API", "version": "1.0.0", "docs": "/docs"}
