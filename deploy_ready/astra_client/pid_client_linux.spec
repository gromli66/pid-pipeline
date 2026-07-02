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
