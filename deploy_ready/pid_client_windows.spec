# -*- mode: python ; coding: utf-8 -*-
# PyInstaller-спека Windows-клиента P&ID. Собирается НАТИВНО на Windows
# скриптом deploy_ready/build_client.ps1. Результат: onedir-папка PID-Client
# с PID-Client.exe (Python на машине пользователя не нужен).
from PyInstaller.utils.hooks import collect_submodules, collect_all
import os, sys

# Корень репозитория относительно расположения спеки (deploy_ready/ -> корень).
# SPECPATH задаётся PyInstaller и равен папке этого .spec.
ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

datas = []
binaries = []
hiddenimports = []

hiddenimports += collect_submodules('ui')
hiddenimports += collect_submodules('app')
hiddenimports += collect_submodules('modules')

# Статические ресурсы (скины, GIF-подсказки) — это НЕ .py, collect_submodules их
# не берёт. Кладём весь ui/resources в сборку с сохранением пути ui/resources/...
# (код ищет их в sys._MEIPASS/ui/resources). Пересканируется при каждой сборке —
# новые файлы/папки в ui/resources подхватываются автоматически.
for _root, _dirs, _files in os.walk(os.path.join(ROOT, 'ui', 'resources')):
    _rel = os.path.relpath(_root, ROOT)
    for _fn in _files:
        datas.append((os.path.join(_root, _fn), _rel))

# vendored-биндинг libavoid (Этап B: оконный роутинг труб в drag, а также
# серверный этап маршрутизации, если клиент считает раскладку локально).
# Путь ВНУТРИ бандла обязан совпадать с тем, что резолвит
# modules/graph/core/layout/_avoid_binding.py: он берёт parents[4]/vendor/
# adaptagrams, а у замороженного модуля это <MEIPASS>/vendor/adaptagrams —
# поэтому кладём с сохранением относительного пути от корня репо.
# Без этой упаковки редактор молча деградирует на самописную лестницу
# (один INFO в лог, глазами не видно) — проверено: tests/test_client_spec.py.
# .pyd идёт в binaries, а не в datas: так PyInstaller анализирует его
# зависимые DLL (MSVC runtime) и кладёт их рядом.
_AVOID = os.path.join(ROOT, 'vendor', 'adaptagrams',
                      'win' if sys.platform == 'win32' else 'linux')
_AVOID_REL = os.path.relpath(_AVOID, ROOT)
for _fn in sorted(os.listdir(_AVOID)) if os.path.isdir(_AVOID) else []:
    _src = os.path.join(_AVOID, _fn)
    if not os.path.isfile(_src):
        continue                      # __pycache__ и прочие каталоги — мимо
    if _fn.lower().endswith(('.pyd', '.dll', '.so')):
        binaries.append((_src, _AVOID_REL))
    else:
        datas.append((_src, _AVOID_REL))

# PySide6 целиком, включая QtWebEngine (вкладка CVAT).
_pyside = collect_all('PySide6')
datas += _pyside[0]; binaries += _pyside[1]; hiddenimports += _pyside[2]

a = Analysis(
    [os.path.join(ROOT, 'client_main.py')],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PID-Client',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,        # GUI-приложение, без чёрного окна консоли
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PID-Client',
)
