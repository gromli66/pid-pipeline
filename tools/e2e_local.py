# -*- coding: utf-8 -*-
"""e2e_local.py — прогон конвейера end-to-end на локальном стеке (пункт 0.12 дороги).

Зачем. Мастер-план записал: «прогнать конвейер локально сегодня **нечем**» —
отсюда и «состояние napravlenie неизвестно», и отсутствие любого ML-бенча.
Аудит плана пометил это утверждение ❔ (чтением кода не проверяется). Замер
2026-08-18: не хватало не компонентов, а способа провести ОДИН uid через все
этапы. Стек поднимается и полон, `/health` зелёный, обе очереди воркеров живы;
но конвейер ведёт клиент, а клиент — окно PySide6, и каждый ручной этап ждёт
оператора.

Стенд ведёт диаграмму по тем же REST-эндпоинтам, что дёргает клиент
(`ui/services/api_client.py`), подтверждая ручные этапы авто-приёмкой, которая
в API уже есть (`STATUS_MACHINE.md §6`): маски и перекрёстки копируются как
validated, контуры принимаются `auto-accept`, граф сохраняется как есть.
Правок оператора при этом ноль — прогон измеряет машину, а не человека.

Цепочка шагов = таблица `STEPS` (статус → что дёрнуть → чего ждать). Она же
делает стенд возобновляемым: `--resume UID` смотрит текущий статус и продолжает
с него, что важно на этапах в десятки минут.

⚠ Расхождения с доками, найденные прогоном (адреса — в коде, не в доках):
- детекция стартует с `frame_cleaned`, а не с `uploaded` (`app/api/detection.py:40`);
  между ними — фаза очистки рамки (`/api/frame/{uid}/skip`);
- `complete-simple` больше НЕ копирует `graph.json` в validated молча
  (`app/api/validation.py:728`) — граф обязан быть сохранён явно;
- SAM2-контуры веером после перекрёстков БОЛЬШЕ НЕ запускаются
  (`app/api/validation.py:666-669`): их считает вкладка по требованию, поэтому
  стенд сам дёргает `/api/contours/{uid}/extract`;
- OCR и контуры `DiagramStatus` не двигают вообще (`worker/tasks/ocr.py:45`),
  их готовность видна только по артефактам — стенд и ждёт артефакты.

Ноль правок боевого кода: `tools/` в `app/`/`worker/`/`ui/` не импортируется.

Запуск (из корня репо):
    python -X utf8 tools/e2e_local.py --check          # готов ли стек, exit 1 если нет
    python -X utf8 tools/e2e_local.py --run            # полный прогон, картинка по умолчанию
    python -X utf8 tools/e2e_local.py --run --image PATH --report out.json
    python -X utf8 tools/e2e_local.py --resume UID     # продолжить прогон с текущего статуса

`--run` создаёт НОВУЮ диаграмму в локальной БД и каталог в `storage/diagrams/`.
Это данные разработчика на этой машине; в git не попадает ничего.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import httpx

REPO = Path(__file__).resolve().parents[1]

API_DEFAULT = os.getenv("E2E_API", "http://localhost:8000")
BROKER_DEFAULT = os.getenv("E2E_BROKER", "redis://localhost:6380/0")
PROJECT_DEFAULT = os.getenv("E2E_PROJECT", "thermohydraulics")

# Картинка по умолчанию — оригинал самой мелкой схемы корпуса 0.8 (её граф
# лежит в git: tests/fixtures/graph/d74eb9f1.json). Сам файл не в git —
# данные заказчика; на чистом клоне путь надо задать через --image.
IMAGE_DEFAULT = (REPO / "storage" / "diagrams"
                 / "d74eb9f1-668d-4596-891f-d4e5a16d04d0" / "original" / "image.png")

# Очереди, без которых прогон встанет: gpu/default — основной воркер,
# sam2 — контуры, ocr — отдельный воркер (docker-compose.yml: worker, worker_ocr).
REQUIRED_QUEUES = ("default", "gpu", "sam2", "ocr")

# След каждого этапа. Список — не пожелание, а замер: ровно эти артефакты
# конвейер оставляет на прогоне без правок оператора (MEASUREMENTS §34).
REQUIRED_ARTIFACTS = (
    "original_image",       # upload
    "yolo_predicted",       # детекция
    "coco_validated",       # приёмка bbox из CVAT
    "pipe_mask",            # сегментация
    "node_mask",            # сегментация (узлы из coco_validated)
    "skeleton",             # скелет #1
    "pipe_mask_validated",  # приёмка масок
    "skeleton_final",       # скелет #2
    "junction_mask",        # перекрёстки
    "graph_json",           # граф
    "graph_validated",      # приёмка графа
    "contours_auto",        # SAM2
    "contours_validated",   # приёмка контуров
    "ocr_result",           # OCR
    "ocr_binding",          # привязка текста
    "fxml",                 # выгрузка
)


class E2EError(RuntimeError):
    """Прогон остановлен: ошибка этапа, таймаут или неожиданный статус."""


# ---------------------------------------------------------------------------
# Цепочка шагов (чистая часть — тестируется без сети)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """Один шаг конвейера.

    id      — короткое имя для отчёта;
    frm     — статус, на котором шаг применим (ключ таблицы);
    kind    — как исполнять (см. Runner.run_step);
    paths   — пути API по порядку, `{uid}` подставляется;
    until   — статусы, на которых шаг считается закрытым;
    timeout — сколько секунд ждать until.
    """

    id: str
    frm: str
    kind: str
    paths: tuple[str, ...]
    until: frozenset[str]
    timeout: int


# Порядок = порядок конвейера. Таймауты — с запасом над боевыми
# (`WORKER_TASKS.md`: детекция 5400 с на CPU-сервере); локально они не
# достигаются, но стенд не должен врать «упало» на медленной машине.
STEPS: tuple[Step, ...] = (
    Step("frame", "uploaded", "post",
         ("/api/frame/{uid}/skip",), frozenset({"frame_cleaned"}), 120),
    Step("detection", "frame_cleaned", "post",
         ("/api/detection/{uid}/detect",), frozenset({"detected"}), 5400),
    Step("cvat_open", "detected", "post",
         ("/api/cvat/{uid}/create-task", "/api/cvat/{uid}/open-validation"),
         frozenset({"validating_bbox"}), 600),
    Step("cvat_fetch", "validating_bbox", "post",
         ("/api/cvat/{uid}/fetch-annotations",),
         frozenset({"validated_bbox"}), 600),
    # Одним шагом: направление → сегментация → скелет #1 (celery chain).
    Step("segmentation", "validated_bbox", "post",
         ("/api/segmentation/{uid}/segment",), frozenset({"skeletonized"}), 5400),
    Step("masks_start", "skeletonized", "post",
         ("/api/validation/{uid}/masks/start",),
         frozenset({"validating_masks"}), 120),
    # Авто-приёмка масок тянет за собой скелет #2 и перекрёстки.
    Step("masks_done", "validating_masks", "post",
         ("/api/validation/{uid}/masks/complete",),
         frozenset({"detected_junctions"}), 5400),
    Step("junctions_start", "detected_junctions", "post",
         ("/api/validation/{uid}/junctions/start",),
         frozenset({"validating_junctions"}), 120),
    # Приёмка перекрёстков веером пускает граф + SAM2 + OCR.
    Step("junctions_done", "validating_junctions", "post",
         ("/api/validation/{uid}/junctions/complete",), frozenset({"built"}), 3600),
    Step("graph_start", "built", "post",
         ("/api/validation/{uid}/graph/start",),
         frozenset({"validating_graph"}), 120),
    Step("graph_done", "validating_graph", "graph_confirm",
         ("/api/validation/{uid}/graph/save",
          "/api/validation/{uid}/graph/complete-simple"),
         frozenset({"validated_graph"}), 600),
    # SAM2 запускается ЗДЕСЬ, а не веером после перекрёстков: авто-запуск
    # снят (`app/api/validation.py:666`), контуры считает вкладка по требованию.
    Step("contours", "validated_graph", "contours",
         ("/api/contours/{uid}/extract", "/api/contours/{uid}/complete"),
         frozenset({"contours_validated"}), 3600),
    Step("ocr_bind", "contours_validated", "ocr_bind",
         ("/api/ocr/{uid}/binding/save", "/api/ocr/{uid}/binding/apply"),
         frozenset({"ocr_bound"}), 5400),
    Step("fxml", "ocr_bound", "post",
         ("/api/validation/{uid}/graph/complete",), frozenset({"completed"}), 1800),
)

# Промежуточные статусы: работа уже идёт у воркера, дёргать ничего не нужно —
# только ждать. Нужны для `--resume`: таймаут стенда оставляет диаграмму
# ровно на таком статусе, и без этих строк возобновление упиралось бы в «тупик».
WAIT_STEPS: tuple[Step, ...] = (
    Step("wait_detection", "detecting", "wait", (), frozenset({"detected"}), 5400),
    Step("wait_segmentation", "segmenting", "wait", (),
         frozenset({"skeletonized"}), 5400),
    Step("wait_skeleton", "skeletonizing", "wait", (),
         frozenset({"skeletonized"}), 5400),
    Step("wait_skeleton_final", "validated_masks", "wait", (),
         frozenset({"detected_junctions"}), 5400),
    Step("wait_skeleton_final", "skeletonizing_final", "wait", (),
         frozenset({"detected_junctions"}), 5400),
    Step("wait_junctions", "skeletonized_final", "wait", (),
         frozenset({"detected_junctions"}), 5400),
    Step("wait_junctions", "detecting_junctions", "wait", (),
         frozenset({"detected_junctions"}), 5400),
    Step("wait_graph", "building_graph", "wait", (), frozenset({"built"}), 3600),
    Step("wait_fxml", "generating_fxml", "wait", (), frozenset({"completed"}), 1800),
)

_BY_STATUS = {s.frm: s for s in STEPS + WAIT_STEPS}
FINAL_STATUS = "completed"


def next_step(status: str) -> Step | None:
    """Что делать на этом статусе. None — делать нечего (финал или тупик)."""
    return _BY_STATUS.get(status)


def chain_from(status: str) -> list[Step]:
    """Шаги от статуса до финала. Пустой список — статуса нет в цепочке."""
    seen: list[Step] = []
    cur = status
    while cur != FINAL_STATUS:
        step = next_step(cur)
        if step is None:
            break
        if step in seen:                       # защита от петли в таблице
            raise E2EError(f"петля в STEPS на шаге {step.id}")
        seen.append(step)
        cur = sorted(step.until)[0]
    return seen


# ---------------------------------------------------------------------------
# Прогон
# ---------------------------------------------------------------------------

@dataclass
class RunReport:
    uid: str = ""
    image: str = ""
    project: str = ""
    steps: list[dict] = field(default_factory=list)
    artifacts: dict[str, bool] = field(default_factory=dict)
    total_seconds: float = 0.0
    final_status: str = ""

    def as_dict(self) -> dict:
        return {
            "uid": self.uid,
            "image": self.image,
            "project": self.project,
            "steps": self.steps,
            "artifacts": self.artifacts,
            "total_seconds": round(self.total_seconds, 1),
            "final_status": self.final_status,
        }


class Runner:
    """Ведёт одну диаграмму по цепочке. Всё общение — через REST, как клиент."""

    def __init__(self, client: httpx.Client, *, poll: float = 2.0,
                 log: Callable[[str], None] = print) -> None:
        self.client = client
        self.poll = poll
        self.log = log

    # --- элементарные операции -------------------------------------------

    def _post(self, path: str, **kw) -> dict:
        resp = self.client.post(path, timeout=300.0, **kw)
        if resp.status_code >= 400:
            raise E2EError(f"POST {path} → {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}

    def _get(self, path: str) -> dict:
        resp = self.client.get(path, timeout=120.0)
        if resp.status_code >= 400:
            raise E2EError(f"GET {path} → {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def status(self, uid: str) -> dict:
        return self._get(f"/api/diagrams/{uid}/status")

    def upload(self, image: Path, project: str, name: str) -> str:
        files = {"file": (name, image.read_bytes(), "image/png")}
        data = {"project_code": project}
        result = self._post("/api/diagrams/upload", files=files, data=data)
        return str(result["uid"])

    def has_artifact(self, uid: str, kind: str) -> bool:
        resp = self.client.get(f"/api/diagrams/{uid}/download/{kind}", timeout=120.0)
        ok = resp.status_code == 200
        resp.close()
        return ok

    # --- ожидание ---------------------------------------------------------

    def wait_status(self, uid: str, until: Iterable[str], timeout: int,
                    label: str) -> tuple[str, float]:
        """Ждать статус из `until`. `error` — остановка с текстом из БД."""
        wanted = set(until)
        started = time.monotonic()
        last = ""
        while True:
            info = self.status(uid)
            cur = str(info.get("status", ""))
            if cur != last:
                self.log(f"    {label}: {cur} ({time.monotonic() - started:.0f} c)")
                last = cur
            if cur in wanted:
                return cur, time.monotonic() - started
            if cur == "error":
                raise E2EError(
                    f"{label}: этап упал на '{info.get('error_stage')}' — "
                    f"{info.get('error_message')}")
            if time.monotonic() - started > timeout:
                raise E2EError(f"{label}: таймаут {timeout} с, статус '{cur}'")
            time.sleep(self.poll)

    def wait_flag(self, path: str, key: str, timeout: int,
                  label: str) -> float:
        """Ждать флаг готовности параллельной ветки (OCR, контуры)."""
        started = time.monotonic()
        while True:
            if bool(self._get(path).get(key)):
                return time.monotonic() - started
            if time.monotonic() - started > timeout:
                raise E2EError(f"{label}: таймаут {timeout} с, {key} так и не готов")
            time.sleep(self.poll)

    # --- шаги -------------------------------------------------------------

    def run_step(self, uid: str, step: Step) -> None:
        """Исполнить действия шага (без ожидания статуса)."""
        paths = [p.format(uid=uid) for p in step.paths]
        if step.kind == "wait":
            return                              # работа уже у воркера
        if step.kind == "post":
            for path in paths:
                self._post(path)
        elif step.kind == "graph_confirm":
            # Приёмка графа без правок: то, что построил воркер, и есть
            # validated. Молчаливой копии в API больше нет (validation.py:728).
            graph = self.client.get(f"/api/diagrams/{uid}/download/graph_json",
                                    timeout=300.0)
            if graph.status_code != 200:
                raise E2EError(f"graph.json не отдан: {graph.status_code}")
            files = {"file": ("graph_validated.json", graph.content,
                              "application/json")}
            self._post(paths[0], files=files)
            self._post(paths[1])
        elif step.kind == "contours":
            self._post(paths[0], json={"ann_ids": None})   # весь лист
            self.wait_flag(f"/api/contours/{uid}/status", "has_auto",
                           step.timeout, "контуры (SAM2)")
            self._post(paths[1])            # auto-accept внутри complete
        elif step.kind == "ocr_bind":
            self.wait_flag(f"/api/ocr/{uid}/status", "has_ocr_result",
                           step.timeout, "OCR")
            empty = json.dumps({"version": "2", "bindings": [], "edited_blocks": []},
                               ensure_ascii=False).encode()
            files = {"file": ("ocr_binding.json", empty, "application/json")}
            self._post(paths[0], files=files)
            self._post(paths[1])
        else:
            raise E2EError(f"неизвестный вид шага: {step.kind}")

    # --- прогон целиком ---------------------------------------------------

    def drive(self, uid: str, report: RunReport) -> RunReport:
        """Вести диаграмму от текущего статуса до `completed`."""
        started_all = time.monotonic()
        while True:
            info = self.status(uid)
            cur = str(info.get("status", ""))
            if cur == FINAL_STATUS:
                break
            step = next_step(cur)
            if step is None:
                raise E2EError(f"статус '{cur}' не описан в STEPS — тупик")
            self.log(f"  → {step.id} (из {cur})")
            t0 = time.monotonic()
            self.run_step(uid, step)
            end, _ = self.wait_status(uid, step.until, step.timeout, step.id)
            report.steps.append({"id": step.id, "from": cur, "to": end,
                                 "seconds": round(time.monotonic() - t0, 1)})
        report.final_status = FINAL_STATUS
        report.total_seconds = time.monotonic() - started_all
        report.artifacts = {a: self.has_artifact(uid, a) for a in REQUIRED_ARTIFACTS}
        report.uid = uid
        return report


# ---------------------------------------------------------------------------
# Проверка стека
# ---------------------------------------------------------------------------

# Мёртвый брокер kombu отдаёт своей OperationalError (наследник ConnectionError
# из kombu.exceptions). Импорт ленивый: celery нужен только этой проверке.
try:
    from kombu.exceptions import OperationalError as _KombuOperationalError
    kombu_errors: tuple[type[BaseException], ...] = (_KombuOperationalError, OSError)
except ImportError:                              # celery не установлен
    kombu_errors = (OSError,)


def celery_queues(broker: str) -> set[str]:
    """Очереди, которые СЕЙЧАС слушают воркеры (celery inspect через брокер)."""
    from celery import Celery

    app = Celery(broker=broker)
    info = app.control.inspect(timeout=10.0).active_queues() or {}
    return {q["name"] for queues in info.values() for q in queues}


def check_stack(client: httpx.Client, *, project: str, broker: str,
                queues_probe: Callable[[str], set[str]] = celery_queues,
                image: Path | None = None) -> tuple[int, list[str]]:
    """Готов ли стек принять прогон. (код возврата, строки отчёта)."""
    lines: list[str] = []
    bad = 0

    def get(path: str) -> httpx.Response | None:
        """None — до API не достучались (стек не поднят): это тоже ответ."""
        try:
            return client.get(path, timeout=30.0)
        except httpx.RequestError as exc:
            lines.append(f"ПЛОХО  {path}: {type(exc).__name__} — {exc}")
            return None

    health = get("/health")
    if health is None:
        bad += 1
    elif health.status_code != 200:
        lines.append(f"ПЛОХО  /health → {health.status_code}")
        bad += 1
    else:
        checks = health.json().get("checks", {})
        sick = sorted(k for k, v in checks.items() if v != "healthy")
        lines.append(f"health: {', '.join(f'{k}={v}' for k, v in sorted(checks.items()))}")
        if sick:
            lines.append(f"ПЛОХО  нездоровы: {', '.join(sick)}")
            bad += 1

    # Спрашиваем ИМЕННО про свой проект: списочные `/api/projects/` и
    # `/summary` отдают пустоту на всех вызовах после первого (замер 0.12:
    # `project_loader.py:579` пропускает подпапку, уже попавшую в `_cache`)
    # — на такой ответ проверку опирать нельзя.
    proj = get(f"/api/projects/{project}")
    if proj is None:
        bad += 1
    elif proj.status_code != 200:
        lines.append(f"ПЛОХО  проекта '{project}' нет: "
                     f"/api/projects/{project} → {proj.status_code}")
        bad += 1
    else:
        lines.append(f"проект: {proj.json().get('code')}")

    try:
        queues = queues_probe(broker)
    except kombu_errors as exc:                 # брокер мёртв — тоже ответ
        lines.append(f"ПЛОХО  брокер {broker}: {type(exc).__name__} — {exc}")
        queues = set()
    lines.append(f"очереди воркеров: {', '.join(sorted(queues)) or '—'}")
    missing = [q for q in REQUIRED_QUEUES if q not in queues]
    if missing:
        lines.append(f"ПЛОХО  никто не слушает: {', '.join(missing)}")
        bad += 1

    if image is not None:
        if image.is_file():
            lines.append(f"картинка: {image} ({image.stat().st_size} Б)")
        else:
            lines.append(f"ПЛОХО  картинки нет: {image}")
            bad += 1

    lines.append(f"\nпровалов: {bad}")
    return (1 if bad else 0), lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_report(report: RunReport) -> None:
    print(f"\nuid: {report.uid}   ({report.image})")
    print(f"{'шаг':<16} {'из':<20} {'в':<20} {'сек':>8}")
    for row in report.steps:
        print(f"{row['id']:<16} {row['from']:<20} {row['to']:<20} {row['seconds']:>8.1f}")
    print(f"{'ИТОГО':<16} {'':<20} {report.final_status:<20} "
          f"{report.total_seconds:>8.1f}")
    lost = sorted(k for k, v in report.artifacts.items() if not v)
    got = sum(1 for v in report.artifacts.values() if v)
    print(f"\nартефакты: {got}/{len(report.artifacts)}"
          + (f"; НЕТ: {', '.join(lost)}" if lost else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Прогон конвейера end-to-end на локальном стеке (0.12)")
    ap.add_argument("--check", action="store_true",
                    help="проверить готовность стека, exit 1 если не готов")
    ap.add_argument("--run", action="store_true",
                    help="загрузить картинку и провести её через все этапы")
    ap.add_argument("--resume", metavar="UID",
                    help="продолжить прогон существующей диаграммы")
    ap.add_argument("--image", type=Path, default=IMAGE_DEFAULT)
    ap.add_argument("--project", default=PROJECT_DEFAULT)
    ap.add_argument("--api", default=API_DEFAULT)
    ap.add_argument("--broker", default=BROKER_DEFAULT)
    ap.add_argument("--poll", type=float, default=2.0, help="период опроса, с")
    ap.add_argument("--report", type=Path, help="куда записать отчёт json")
    args = ap.parse_args(argv)

    if not (args.check or args.run or args.resume):
        ap.error("нужен один из режимов: --check, --run, --resume UID")

    with httpx.Client(base_url=args.api) as client:
        if args.check:
            code, lines = check_stack(client, project=args.project,
                                      broker=args.broker, image=args.image)
            print("\n".join(lines))
            if not (args.run or args.resume):
                return code
            if code:
                return code

        runner = Runner(client, poll=args.poll)
        report = RunReport(image=str(args.image), project=args.project)
        try:
            if args.resume:
                uid = args.resume
                print(f"продолжаю {uid} со статуса "
                      f"'{runner.status(uid).get('status')}'")
            else:
                if not args.image.is_file():
                    print(f"картинки нет: {args.image}", file=sys.stderr)
                    return 1
                name = f"e2e_{int(time.time())}_{args.image.name}"
                uid = runner.upload(args.image, args.project, name)
                print(f"загружено: {uid} ({name})")
            report.uid = uid
            runner.drive(uid, report)
        except E2EError as exc:
            print(f"\nПРОВАЛ: {exc}", file=sys.stderr)
            if report.uid:
                print(f"продолжить: python -X utf8 tools/e2e_local.py "
                      f"--resume {report.uid}", file=sys.stderr)
            return 1

        _print_report(report)
        if args.report:
            args.report.write_text(
                json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"отчёт: {args.report}")
        return 0 if all(report.artifacts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
