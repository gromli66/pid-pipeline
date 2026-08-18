# -*- coding: utf-8 -*-
"""Заглушки `sys.modules` не должны переживать свой тест (пункт 0.3x дороги).

Мина. `sys.modules.setdefault("cv2", MagicMock())` на уровне модуля исполняется
на СБОРКЕ pytest — то есть до первого теста и на всю сессию. Дальше настоящий
`cv2` (он объявлен в `requirements/ui.txt` и стоит и локально, и в CI) уже не
достанется никому: `imread`/`imwrite`/`connectedComponentsWithStats` молча
возвращают `MagicMock`. Замерено на этой правке: один такой файл красит 11
тестов `tests/ui/test_square_size_ops.py`, которые в одиночку зелёные
(MEASUREMENTS §37), а стенд 0.11 пришлось переписывать с cv2 на Pillow.
Запрет и канон-фикстура — `docs/TESTING.md §6`.

Два слоя, потому что мина двухсторонняя:
  * структурный — новый модуль-уровневый мок не проедет в набор;
  * рантайм — объявленная зависимость в живой сессии не подменена.
Рантайм-проверка ловит и то, чего структурная не видит (чужой conftest,
плагин), но значима только в полном прогоне: в одиночку файл-нарушитель
не импортируется, и она зелёная.
"""
import ast
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

TESTS_DIR = Path(__file__).resolve().parent

# Единственная законная запись в sys.modules на уровне модуля: приватный
# псевдоним модуля-под-тестом, загруженного по файловому пути
# (`spec_from_file_location("stats_api_under_test", ...)` →
# `sys.modules[_spec.name] = mod`). Такой ключ ничего не затеняет — имени
# `*_under_test` нет ни в одном пакете. Разрешаем по ФОРМЕ ключа, а не по
# имени файла: иначе новая строка в уже разрешённом файле проедет молча.
ALLOWED_KEY = "_spec.name"

_WRITERS = ("setdefault", "update", "pop", "__setitem__")


def _is_sys_modules(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "modules"
            and isinstance(node.value, ast.Name) and node.value.id == "sys")


class _ModuleLevelWrites(ast.NodeVisitor):
    """Записи в `sys.modules` вне функций.

    Вложенность в `if`/`for`/`try` уровнем не считается — этот код исполняется
    на импорте так же, как и голая строка (так спрятаны 2 из 5 очагов).
    """

    def __init__(self) -> None:
        self.in_function = 0
        self.hits: list[tuple[int, str]] = []

    def visit_FunctionDef(self, node):  # noqa: N802 (имя задано ast)
        self.in_function += 1
        self.generic_visit(node)
        self.in_function -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):  # noqa: N802
        self.in_function += 1
        self.generic_visit(node)
        self.in_function -= 1

    def visit_Call(self, node):  # noqa: N802
        if (not self.in_function and isinstance(node.func, ast.Attribute)
                and node.func.attr in _WRITERS and _is_sys_modules(node.func.value)):
            self.hits.append((node.lineno, ast.unparse(node)))
        self.generic_visit(node)

    def visit_Assign(self, node):  # noqa: N802
        for target in node.targets:
            if (not self.in_function and isinstance(target, ast.Subscript)
                    and _is_sys_modules(target.value)):
                if ast.unparse(target.slice) != ALLOWED_KEY:
                    self.hits.append((node.lineno, ast.unparse(node)))
        self.generic_visit(node)


def _test_modules() -> list[Path]:
    return sorted(TESTS_DIR.rglob("test_*.py"))


def test_no_module_level_sys_modules_stubs():
    """Ни один тест-модуль не правит sys.modules на уровне модуля."""
    offenders = []
    for path in _test_modules():
        visitor = _ModuleLevelWrites()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), str(path)))
        for lineno, src in visitor.hits:
            offenders.append(f"{path.relative_to(TESTS_DIR).as_posix()}:{lineno}  {src}")

    assert not offenders, (
        "заглушка sys.modules на уровне модуля живёт всю сессию pytest "
        "(канон — фикстура с monkeypatch.setitem, docs/TESTING.md §6):\n  "
        + "\n  ".join(offenders)
    )


# Объявлены в requirements/ui.txt (opencv-python, scikit-image) → стоят и на
# машине разработки, и на раннере. Значит мок вместо них — всегда чужая
# заглушка, а не отсутствующая зависимость.
@pytest.mark.parametrize("name", ["cv2", "skimage"])
def test_declared_dependency_is_not_shadowed(name):
    """В живой сессии объявленная зависимость — настоящая, а не заглушка."""
    module = sys.modules.get(name)
    if module is None:
        pytest.skip(f"{name} в этой сессии не импортирован — подменять нечего")

    # types.ModuleType-подделка не Mock, но и файла у неё нет.
    assert not isinstance(module, Mock) and getattr(module, "__file__", None), (
        f"sys.modules[{name!r}] = {module!r} — настоящий модуль подменён "
        "заглушкой на уровне сессии; ищи запись sys.modules на уровне модуля в tests/"
    )
