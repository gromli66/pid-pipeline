"""Сервис мониторинга Flower (пункт 3.x1 дороги).

Живая часть гейта — `tools/flower_gate.py --check`: четыре ноги на поднятом
стеке (механизм восстановления чужих unacked, парность окон, окно в контейнере,
видимость очередей и задач). Нужен брокер и docker, поэтому в CI её нет.
Здесь заперто то, что проверяется без брокера.

Главный инвариант — **Flower поднимается с приложением проекта**
(`-A worker.celery_app`). На голом URL его канал kombu берёт дефолтное окно
невидимости 3600 с, а `restore_visible` режет по окну СКАНИРУЮЩЕГО канала и
работает с общим ключом `unacked_index`: Flower возвращал бы в очередь
сообщения, которые воркер держит под окном 7200, то есть отменял бы фикс
пункта 1.2. Числа здесь абсолютные: 7200 записано отдельно от кода, чтобы
тест краснел при расхождении, а не подстраивался под него.

Второй инвариант — порт наружу не публикуется: у Flower по умолчанию нет
аутентификации, а в UI есть управление (отозвать задачу, снять воркера).
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from tools.flower_gate import DEFAULT_FLOWER, EXPECTED_WINDOW  # noqa: E402
from worker.celery_app import celery_app  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "docker-compose.yml"
DOCKERFILE = REPO / "Dockerfile.worker"
SERVICE = "flower"
CONTAINER = "pid_flower"
PORT = "5555"
LOOPBACK = "127.0.0.1"
VISIBILITY_TIMEOUT = 7200
PROBE_QUEUE = "default"


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def flower(compose):
    services = compose["services"]
    assert SERVICE in services, "сервис flower исчез из docker-compose.yml"
    return services[SERVICE]


@pytest.fixture(scope="module")
def dockerfile():
    return DOCKERFILE.read_text(encoding="utf-8")


def test_service_is_declared(flower):
    assert flower["container_name"] == CONTAINER


def test_flower_is_built_from_the_project_image(flower, dockerfile):
    """Свой образ, а не чужой: Flower обязан импортировать worker.celery_app."""
    build = flower["build"]
    assert build["dockerfile"] == DOCKERFILE.name
    assert build["target"] == "flower"
    assert "FROM worker AS flower" in dockerfile
    assert "image" not in flower, "чужой образ проектного приложения не содержит"


def test_flower_starts_with_the_project_app(dockerfile):
    """⛔ Тот самый инвариант: без -A канал возьмёт дефолтное окно 3600."""
    stage = dockerfile.split("FROM worker AS flower", 1)[1]
    command = " ".join(stage.split())
    assert '"celery", "-A", "worker.celery_app", "flower"' in command


def test_worker_image_does_not_include_flower_stage(compose):
    """Сборка воркера обязана останавливаться на своей стадии."""
    assert compose["services"]["worker"]["build"]["target"] == "worker"


def test_port_is_bound_to_loopback_only(flower):
    """Порт наружу не публикуется: у Flower нет аутентификации."""
    published = [str(p) for p in flower["ports"]]
    assert published == [f"{LOOPBACK}:{PORT}:{PORT}"]


def test_broker_is_the_same_as_workers(flower, compose):
    env = dict(item.split("=", 1) for item in flower["environment"])
    worker_env = dict(item.split("=", 1) for item in compose["services"]["worker"]["environment"])
    assert env["CELERY_BROKER_URL"] == worker_env["CELERY_BROKER_URL"]


def test_waits_for_healthy_redis(flower):
    assert flower["depends_on"]["redis"]["condition"] == "service_healthy"


def test_gate_expects_the_configured_window():
    """Сторож самого гейта: его число не разъехалось с конфигом приложения."""
    assert EXPECTED_WINDOW == VISIBILITY_TIMEOUT
    assert celery_app.conf.broker_transport_options["visibility_timeout"] == VISIBILITY_TIMEOUT


def test_gate_targets_the_published_port_and_a_served_queue():
    """Сторож самого гейта: он стучится туда, куда сервис опубликован."""
    assert DEFAULT_FLOWER == f"http://{LOOPBACK}:{PORT}"
    worker_cmd = (REPO / "Dockerfile.worker").read_text(encoding="utf-8")
    assert f'"{PROBE_QUEUE},' in worker_cmd, "очередь зонда никто не слушает"
