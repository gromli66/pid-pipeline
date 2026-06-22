# Д1 — единственная правка кода: адрес API в UI

**Зачем:** сейчас клиент всегда стучится на `localhost`, поэтому с другого компьютера сервер не находится. Делаем адрес настраиваемым через переменную окружения `PID_API_URL` — в том же стиле, что уже используется в проекте для `PID_GL_BACKEND`.

**Где:** `ui/windows/main_window.py`, строка 37.
**Важно:** применять в **рабочей копии**, НЕ в бэкапе.

## Было
```python
        self.api_client = APIClient("http://localhost:8000")
```

## Стало
```python
        import os
        api_url = os.environ.get("PID_API_URL", "http://localhost:8000")
        self.api_client = APIClient(api_url)
```
> `import os` можно вынести в начало файла к остальным импортам — тогда в этой строке оставить только две нижние.

## Проверка
- На своём ПК без переменной — поведение прежнее (`localhost`).
- С адресом сервера:
  - Linux/Astra: `PID_API_URL=http://<IP-сервера>:8000 python -m ui.main`
  - Windows: `set PID_API_URL=http://<IP-сервера>:8000` затем `python -m ui.main`

## Примечание
Файл `ui/editors/test_editors.py` тоже содержит `localhost`, но это тестовый модуль — для миграции не нужен, не трогаем.
