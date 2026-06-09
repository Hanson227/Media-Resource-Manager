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

_root = Path(SPECPATH).parent.resolve()  # desktop/

a = Analysis(
    ['main.py'],
    pathex=[str(_root)],
    binaries=[],
    datas=[
        # Web 前端（SPA 文件）
        ('../web/index.html', 'web'),
        ('../web/app.js', 'web'),
        ('../web/style.css', 'web'),
        ('../web/sw.js', 'web'),
        ('../web/manifest.json', 'web'),
        ('../web/icon-192.png', 'web'),
        ('../web/icon-512.png', 'web'),
        # 人脸检测模型
        ('models/', 'models'),
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
    icon='../web/icon-512.png',
)
