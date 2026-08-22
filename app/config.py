"""
Application Configuration - загрузка настроек из .env
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional
from functools import lru_cache


class Settings(BaseSettings):
    """Настройки приложения."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",  # Игнорировать лишние переменные в .env
    )
    
    # Database
    DATABASE_URL: str = Field(
        default="postgresql://pid_user:changeme@localhost:5433/pid_pipeline"
    )
    
    # Redis / Celery
    CELERY_BROKER_URL: str = Field(default="redis://localhost:6380/0")
    CELERY_RESULT_BACKEND: str = Field(default="redis://localhost:6380/0")
    CELERY_TASK_TIME_LIMIT: int = Field(default=3600)
    
    # CVAT
    CVAT_URL: str = Field(default="http://localhost:8080")
    CVAT_BROWSER_URL: str = Field(
        default="http://localhost:8080",
        description="URL для открытия CVAT в браузере пользователя"
    )
    CVAT_TOKEN: Optional[str] = Field(default=None)
    
    # Storage
    STORAGE_PATH: str = Field(default="./storage/diagrams")

    # === PDF rendering (upload) ===
    # Модели обучались на 300 DPI сканах (~4900x3500 px). PDF рендерим в 300 DPI.
    PDF_RENDER_DPI: int = Field(default=300)
    # Защитный лимит: если длинная сторона при 300 DPI > этого, масштабируем вниз
    # (большие форматы A0/A1 иначе дают гигантские растры и превышают UI-лимит).
    PDF_MAX_SIDE: int = Field(default=16000)
    
    # Projects
    PROJECTS_CONFIG_DIR: str = Field(default="./configs/projects")
    
    # === Конвертер расчётной схемы САПФИР (.prtx) ===
    # Сервис из docker/prtx: держит движок САПФИР, ключ лицензии приезжает с
    # клиентом на время одного прогона и на сервере не хранится.
    PRTX_SERVICE_URL: str = Field(default="http://prtx:8081")
    # Полный прогон на 400-узловой схеме — десятки секунд; запас на очередь,
    # т.к. сервис считает схемы по одной (движок пишет в общий SETTINGS).
    PRTX_TIMEOUT_SEC: int = Field(default=900)

    # API
    API_HOST: str = Field(default="0.0.0.0")
    API_PORT: int = Field(default=8000)
    DEBUG: bool = Field(default=False)
    
    # Logging (DEBUG по умолчанию, переключается на INFO через .env)
    LOG_LEVEL: str = Field(default="DEBUG")


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
