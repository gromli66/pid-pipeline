"""
Application Services.
"""

# NOTE: StorageService НЕ реэкспортируется из __init__.py чтобы не тянуть
# fastapi в worker-контейнеры. Импортируйте напрямую:
#   from app.services.storage import StorageService
