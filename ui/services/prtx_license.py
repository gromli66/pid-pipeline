"""Диагностика лицензии САПФИР: видит ли клиент ключ и готов ли сервер.

Зачем отдельно от `prtx_converter`. Раньше про неверно прописанный путь
оператор узнавал в конце пайплайна — на экспорте, пройдя все этапы. Здесь
те же проверки вынесены в самостоятельную функцию без Qt, чтобы их можно
было вызвать в любой момент (кнопка «Лицензия САПФИР») и покрыть тестами.

Проверяется то, что можно проверить ДЁШЕВО и на клиенте: откуда взялся путь,
существует ли каталог, лежит ли в нём файл ключа, читается ли он и похож ли
на ключ. Валидность подписи не проверяется — это умеет только сам движок, и
стоит это 300 с очереди на сервере при плохом ключе (замер 2026-08-22).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: см. prtx_converter.LICENSE_KEY_FILE — дублировать имя не хотим
from ui.services.prtx_converter import LICENSE_KEY_FILE, license_home

#: эталон: ключ license4j — короткая ASCII-строка (замер: 29 байт)
_KEY_MIN = 8
_KEY_MAX = 4096


@dataclass
class Check:
    """Один пункт отчёта."""

    title: str
    ok: bool
    detail: str = ""
    #: что делать оператору, если не ок
    hint: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def first_problem(self) -> Check | None:
        return next((c for c in self.checks if not c.ok), None)

    def add(self, title, ok, detail="", hint=""):
        self.checks.append(Check(title, ok, detail, hint))
        return ok


def diagnose_local() -> Report:
    """Проверить ключ на этой машине. Сеть не трогает."""
    report = Report()
    raw = os.environ.get("PRTX_LICENSE_HOME")

    # 1. Откуда берётся путь
    if raw:
        report.add(
            "Путь из client.cfg", True,
            f"PRTX_LICENSE_HOME = {raw}",
        )
    else:
        report.add(
            "Путь по умолчанию", True,
            f"профиль пользователя: {Path.home()}",
            "Если САПФИР активирован под другой учётной записью — "
            "укажите её профиль в PRTX_LICENSE_HOME (client.cfg).",
        )

    # 2. Кавычки: client.cfg берёт значение как есть, кавычки уедут в путь
    if raw and (raw[0] in "\"'" or raw[-1] in "\"'"):
        report.add(
            "Кавычки в пути", False,
            f"значение начинается или кончается кавычкой: {raw}",
            "Уберите кавычки — путь пишется как есть: "
            "PRTX_LICENSE_HOME=C:\\Users\\ИмяУчётки",
        )
        return report

    home = license_home()

    # 3. Указали файл вместо каталога — частая ошибка
    if raw and (home.name == LICENSE_KEY_FILE or home.is_file()):
        report.add(
            "Путь ведёт к файлу, а не к папке", False,
            f"{home}",
            f"В PRTX_LICENSE_HOME нужна ПАПКА, в которой лежит {LICENSE_KEY_FILE}. "
            f"Уберите имя файла из конца пути: {home.parent}",
        )
        return report

    # 4. Каталог существует
    if not home.is_dir():
        report.add(
            "Папка с ключом", False,
            f"не найдена: {home}",
            "Проверьте, что путь набран верно и папка доступна с этой машины "
            "(сетевые диски могут быть не подключены).",
        )
        return report
    report.add("Папка с ключом", True, str(home))

    # 5. Файл ключа на месте
    key = home / LICENSE_KEY_FILE
    if not key.is_file():
        hint = f"Положите файл {LICENSE_KEY_FILE} в {home}."
        # если ключ есть в профиле, а ищем не там — подскажем прямо
        default_key = Path.home() / LICENSE_KEY_FILE
        if raw and default_key.is_file():
            hint = (
                f"Ключ есть в профиле пользователя ({default_key}). "
                "Уберите строку PRTX_LICENSE_HOME из client.cfg — "
                "тогда он возьмётся оттуда."
            )
        report.add("Файл ключа", False, f"не найден: {key}", hint)
        return report

    # 6. Читается
    try:
        data = key.read_bytes()
    except OSError as exc:
        report.add(
            "Чтение ключа", False, f"{key}: {exc}",
            "Нет прав на чтение файла или он занят другой программой.",
        )
        return report

    # 7. Похож на ключ
    size = len(data)
    if size == 0:
        report.add(
            "Содержимое ключа", False, "файл пустой (0 байт)",
            "Файл повреждён или скопирован не полностью — возьмите его заново "
            "с машины, где активирован САПФИР.",
        )
        return report
    if not (_KEY_MIN <= size <= _KEY_MAX) or not _looks_like_key(data):
        report.add(
            "Содержимое ключа", False,
            f"не похоже на ключ лицензии ({size} байт)",
            f"Похоже, это другой файл. Нужен именно {LICENSE_KEY_FILE} "
            "из профиля пользователя на машине с активированным САПФИРом.",
        )
        return report

    report.add("Файл ключа", True, f"{key} ({size} байт)")
    return report


def _looks_like_key(data: bytes) -> bool:
    """Ключ license4j — печатаемая ASCII-строка в одну строку (замер: 29 байт)."""
    text = data.strip()
    return bool(text) and all(32 <= b < 127 for b in text)


def diagnose(api_client=None) -> Report:
    """Полная проверка: ключ на клиенте + готовность сервера.

    `api_client` необязателен: без него отчёт ограничится локальной частью.
    """
    report = diagnose_local()

    # Ключа нет — сервер не опрашиваем: чинить надо у себя, а ожидание
    # сетевого таймаута только задержит ответ оператору.
    if api_client is None or not report.ok:
        return report

    try:
        state = api_client.prtx_health()
    except Exception as exc:  # noqa: BLE001 — сеть; текст уходит оператору
        # Сервер старее клиента: эндпоинта проверки на нём ещё нет. Это не
        # отказ лицензии — сборка схемы работать может, просто заранее не
        # спросить. Не пугаем оператора красным.
        if getattr(exc, "status_code", 0) == 404:
            report.add(
                "Конвертер на сервере", True,
                "проверка недоступна — сервер старее программы",
                "Обновите сервер, чтобы проверять готовность конвертера заранее.",
            )
            return report
        report.add(
            "Конвертер на сервере", False, str(exc),
            "Сервер недоступен или конвертер не запущен. "
            "Проверьте адрес сервера (PID_API_URL) и сообщите администратору.",
        )
        return report

    if state.get("ok"):
        busy = " (сейчас считает другую схему)" if state.get("busy") else ""
        report.add("Конвертер на сервере", True, f"готов{busy}")
    else:
        report.add(
            "Конвертер на сервере", False, str(state),
            "Сервис конвертера отвечает, но не готов — сообщите администратору.",
        )
    return report
