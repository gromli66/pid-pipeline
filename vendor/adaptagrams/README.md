# vendor/adaptagrams — libavoid-биндинг для этапа роутинга (Э7-b)

Python-биндинг (SWIG) библиотек **Adaptagrams** — используется только
`Avoid::*` (libavoid): ортогональный роутер с препятствиями, пинами и
нуджингом. Потребитель — `modules/graph/core/layout/_avoid_binding.py`
(загрузчик) и `modules/graph/core/layout/avoid_router.py` (адаптер).

## Источник и версия

* Репозиторий: <https://github.com/mjwybrow/adaptagrams>
* Коммит: `840ebcff20dbba36ad03a2160edf7cbaf9859984`
  (master, merge PR #84, 2025-10-29) — фиксирован в `build/build_linux.sh`.
* Интерфейс SWIG: `cola/adaptagrams.i` из того же коммита, без правок.

## Лицензия

Библиотеки Adaptagrams — **LGPL 2.1** (файл `LICENSE` здесь — копия
`cola/LICENSE` апстрима). Требование LGPL о динамической линковке
соблюдено самой формой поставки: `_adaptagrams.pyd` / `_adaptagrams.so` —
динамически загружаемый модуль CPython, наш код линкуется с ним только
в рантайме через import. Исходники — по ссылке выше; наши микропатчи —
`build/micropatches.diff` (см. ниже).

## Раскладка каталога

```
win/    _adaptagrams.pyd + adaptagrams.py  — собрано MSVC 2022, CPython 3.11 win64
        (проверено живым тестом _scratch/layout_align/libavoid_rnd/test_pyd.py)
linux/  _adaptagrams.so + adaptagrams.py   — НЕ в git: собирается слоем
        Dockerfile.worker из build/build_linux.sh
build/  скрипты сборки + micropatches.diff
```

`adaptagrams.py` — SWIG-прокси, генерируется вместе с бинарём и обязан быть
из ТОЙ ЖЕ генерации SWIG, что и бинарь (прокси зовёт функции по именам).
Поэтому он лежит рядом с каждым бинарём в своей платформенной папке.

## Микропатчи исходников (`build/micropatches.diff`)

Три правки, необходимые для сборки MSVC (для g++ безвредны и корректны;
разведка Э7-b, `_scratch/layout_align/libavoid_rnd/`):

1. `cola/libavoid/assertions.h` — ветка `_MSC_VER` не должна перекрывать
   `USE_ASSERT_EXCEPTIONS` (иначе assert валит процесс вместо исключения,
   которое SWIG-обёртка переводит в Python-исключение).
2. `cola/libdialect/constraints.cpp` — `swap` → `std::swap` (MSVC не находит
   перегрузку через ADL для enum-типов).
3. `cola/libdialect/trees.cpp` — унарный `+` на лямбдах: приведение к
   указателю на функцию, иначе MSVC не сводит типы веток тернарника.

## Сборка

* **Windows (dev)**: `build/build_libavoid_msvc.bat` + `build_alllibs_msvc.bat`
  (статические .lib всех пяти библиотек), затем `build/build_pyd_msvc.bat`
  (SWIG-обёртка + линковка .pyd). Пути к MSVC/Python в скриптах — под
  dev-машину, поправить под свою. SWIG-генерация: `swig -DNDEBUG -c++ -python
  adaptagrams.i` в `cola/`.
* **Linux (бой, CPU-only)**: `build/build_linux.sh <outdir>` — клонирует
  фиксированный коммит, накладывает micropatches.diff, компилирует пять
  библиотек напрямую g++ (без autotools), генерирует обёртку SWIG и линкует
  `_adaptagrams.so`. Вызывается из `Dockerfile.worker` отдельным слоем.

Флаги обеих сборок: `-DUSE_ASSERT_EXCEPTIONS` (assert → исключение, а не
abort воркера) и `-DSWIG_PYTHON_SILENT_MEMLEAK` (не спамить stderr на
незалоченных прокси-объектах).
