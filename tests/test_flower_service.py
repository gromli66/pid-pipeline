"""Сервис мониторинга Flower в docker-compose (пункт 3.x1 дороги).

Живая часть гейта — `tools/flower_gate.py --check`: он кладёт задачу в очередь
и требует увидеть её в Flower. Нужен поднятый стек, поэтому в CI его нет.
Здесь заперто то, что проверяется без брокера: сервис объявлен и его порт
не выставлен наружу.

Про порт отдельно: у Flower по умолчанию нет аутентификации, а в UI есть
управление — отозвать задачу, снять воркера. Публикация `5555:5555` (без адреса)
открыла бы это всем, кто дотянется до сервера. Числа в тесте абсолютные:
проверяется буквальный префикс `127.0.0.1:`, а не «то же, что в файле».
"""
import pytest

yaml = pytest.importorskip("yaml")

from tools.flower_gate import DEFAULT_FLOWER, PROBE_QUEUE  # noqa: E402

from pathlib import Path  # noqa: E402

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
SERVICE = "flower"
CONTAINER = "pid_flower"
PORT = "5555"
LOOPBACK = "127.0.0.1"


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def flower(compose):
    services = compose["services"]
    assert SERVICE in services, "сервис flower исчез из docker-compose.yml"
    return services[SERVICE]


def test_service_is_declared(flower):
    assert flower["container_name"] == CONTAINER
    assert flower["image"].startswith("mher/flower")


def test_port_is_bound_to_loopback_only(flower):
    """Порт наружу не публикуется: у Flower нет аутентификации."""
    published = [str(p) for p in flower["ports"]]
    assert published == [f"{LOOPBACK}:{PORT}:{PORT}"]


def test_broker_points_at_stack_redis(flower):
    """Flower смотрит в тот же брокер, что и воркеры, а не в чужой."""
    command = " ".join(flower["command"].split())
    assert "--broker=redis://redis:6379/0" in command
    assert "flower" in command


def test_waits_for_healthy_redis(flower):
    assert flower["depends_on"]["redis"]["condition"] == "service_healthy"


def test_probe_targets_the_published_port_and_a_served_queue():
    """Сторож самого зонда: он стучится туда, куда сервис опубликован."""
    assert DEFAULT_FLOWER == f"http://{LOOPBACK}:{PORT}"
    worker_cmd = (COMPOSE.parent / "Dockerfile.worker").read_text(encoding="utf-8")
    assert f'"{PROBE_QUEUE},' in worker_cmd, "очередь зонда никто не слушает"
