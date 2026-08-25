"""Контракт `GET /api/projects/{code}/classes`: `name` английское, `display_name` рядом.

`display_name` добавлено, а не подменило `name`, — и это ровно то, что делает
правку обратно совместимой: клиент любой версии, читающий `name`, кладёт в узел
графа прежнее английское имя. Поменяй здесь `name` на русское — и `class_name`
узла перестанет находить скин, а FXML уедет заказчику с русским классом.

У эндпоинта нет `response_model`, схемы в `app/schemas/project.py` для него тоже
нет — форму ответа не держит ничто, кроме этого файла.

Реальный конфиг подсовывается своим загрузчиком: `tests/conftest.py` подменяет
`PROJECTS_CONFIG_DIR` на `/tmp/test_configs`.
"""
import asyncio
from pathlib import Path

import pytest

from app.api.projects import get_project_classes
from app.services.class_display import sort_key
from app.services.project_loader import ProjectLoader

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def response():
    loader = ProjectLoader(ROOT / "configs/projects")
    return asyncio.run(get_project_classes("thermohydraulics", loader))


def test_every_class_has_all_three_fields(response):
    assert response["num_classes"] == len(response["classes"])
    for c in response["classes"]:
        assert set(c) == {"id", "name", "display_name"}, c


def test_name_stays_english(response):
    """Обратная совместимость: старый клиент читает `name` и получает прежнее."""
    for c in response["classes"]:
        assert c["name"].isascii(), f"внутреннее имя стало нерусским ASCII: {c}"


def test_display_name_filled_for_every_class(response):
    empty = [c["name"] for c in response["classes"] if not c["display_name"]]
    assert not empty, f"без отображаемого имени: {empty}"


def test_order_stays_yaml_order(response):
    """Ответ отдаётся в порядке `classes:`; сортировка — забота клиента.

    Так и задумано: `id` в ответе 1-based канонический, и порядок обязан ему
    соответствовать, иначе любой потребитель, читающий список по индексу,
    поедет.
    """
    ids = [c["id"] for c in response["classes"]]
    assert ids == sorted(ids) == list(range(1, len(ids) + 1))


def test_client_can_sort_into_alphabet(response):
    """То, что сделает палитра: отсортировать по display_name."""
    shown = sorted((c["display_name"] for c in response["classes"]), key=sort_key)
    assert shown[0] == "Аннотация"
    assert shown[-1] == "Электронагреватель"


def test_unknown_project_is_404(response):
    from fastapi import HTTPException

    loader = ProjectLoader(ROOT / "configs/projects")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_project_classes("no_such_project", loader))
    assert exc.value.status_code == 404
