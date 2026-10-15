# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — Media Resource Manager 桌面应用打包配置。

用法：
    pip install pyinstaller
    pyinstaller build.spec

输出：
    dist/影视资源管理器/影视资源管理器.exe
"""

import os
import sys
from pathlib import Path

# SPECPATH = 本 spec 所在目录（desktop/）；所有源都用绝对路径拼出来，
# 避免"_root 上溯到仓库根"以及"必须从 desktop/ 执行 pyinstaller"这两个坑。
_root = Path(SPECPATH).resolve()          # desktop/
_web = _root.parent / "web"               # 仓库根的 web/

a = Analysis(
    [str(_root / 'main.py')],
    pathex=[str(_root)],
    binaries=[],
    datas=[
        # Web 前端（SPA 文件，含 web/vendor 下的离线 Vue/vue-router）。
        # 整个目录一起打包：逐个列文件曾漏过 vendor/ 与后续新增资源。
        (str(_web), 'web'),
        # 人脸检测模型（完整档需要；便携版见 build_web.spec）
        (str(_root / 'models'), 'models'),
    ],
    hiddenimports=[
        'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets',
        'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
        'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
        'sqlalchemy.sql.default_comparator',
        'PIL._webp', 'cv2', 'numpy.core._methods', 'numpy.lib.format',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除不需要的 PySide6 模块（大幅减小包体）
        'PySide6.QtWebEngine', 'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineQuick', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebChannel',
        'PySide6.Qt3DAnimation', 'PySide6.Qt3DCore', 'PySide6.Qt3DExtras',
        'PySide6.Qt3DInput', 'PySide6.Qt3DLogic', 'PySide6.Qt3DRender',
        'PySide6.QtBluetooth', 'PySide6.QtCharts', 'PySide6.QtDataVisualization',
        'PySide6.QtDesigner', 'PySide6.QtHelp', 'PySide6.QtLocation',
        'PySide6.QtNfc', 'PySide6.QtNetwork', 'PySide6.QtOpenGL',
        'PySide6.QtOpenGLWidgets', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
        'PySide6.QtPositioning', 'PySide6.QtPrintSupport', 'PySide6.QtQuick',
        'PySide6.QtQuick3D', 'PySide6.QtQuickControls2', 'PySide6.QtQuickWidgets',
        'PySide6.QtSensors', 'PySide6.QtSerialPort', 'PySide6.QtSpatialAudio',
        'PySide6.QtSql', 'PySide6.QtSvg', 'PySide6.QtTest',
        'PySide6.QtTextToSpeech', 'PySide6.QtVirtualKeyboard', 'PySide6.QtXml',
        'tkinter', 'matplotlib', 'scipy', 'tensorflow', 'torch',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='影视资源管理器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_web / 'icon-512.png'),
)
