# -*- mode: python ; coding: utf-8 -*-
# PyInstaller-спека Linux/Astra-клиента P&ID.
# Собирается ВНУТРИ Docker-образа manylinux_2_28 (glibc 2.28) — см. Dockerfile.manylinux.
# Результат: onedir-папка PID_Pipeline (самодостаточная, Python на клиенте не нужен).
from PyInstaller.utils.hooks import collect_submodules, collect_all
import os, sys

# Корень репозитория относительно расположения спеки
# (deploy_ready/astra_client/ -> корень). SPECPATH задаётся PyInstaller.
ROOT = os.path.abspath(os.path.join(SPECPATH, '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

datas = []
binaries = []
hiddenimports = []

# Динамические импорты приложения — собираем подмодули явно.
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

# vendored-биндинг libavoid (Э7-b): ортогональный роутер труб в редакторе.
# Путь ВНУТРИ бандла обязан совпадать с тем, что резолвит
# modules/graph/core/layout/_avoid_binding.py: он берёт parents[4]/vendor/
# adaptagrams, а у замороженного модуля это <MEIPASS>/vendor/adaptagrams —
# поэтому кладём с сохранением относительного пути от корня репо.
# .so идёт в binaries: так PyInstaller видит его зависимые библиотеки.
# Бинарь собирается слоем Dockerfile.manylinux и кладётся сюда inner-скриптом;
# без него редактор молча уходит на самописную лестницу (один INFO в лог).
_AVOID = os.path.join(ROOT, 'vendor', 'adaptagrams', 'linux')
_AVOID_REL = os.path.relpath(_AVOID, ROOT)
for _fn in sorted(os.listdir(_AVOID)) if os.path.isdir(_AVOID) else []:
    _src = os.path.join(_AVOID, _fn)
    if not os.path.isfile(_src):
        continue                      # __pycache__ и прочие каталоги — мимо
    if _fn.lower().endswith(('.so', '.pyd', '.dll')):
        binaries.append((_src, _AVOID_REL))
    else:
        datas.append((_src, _AVOID_REL))

# PySide6 целиком, включая QtWebEngine из PySide6_Addons (нужен для вкладки CVAT).
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
    name='PID_Pipeline',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX ломает часть Qt .so на Linux и не входит в образ
    console=True,         # видно лог в терминале при запуске ./PID_Pipeline
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
    upx=False,
    upx_exclude=[],
    name='PID_Pipeline',
)
