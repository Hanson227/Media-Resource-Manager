# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec —— 便携版（Web/API，无桌面 GUI）。

**一套代码，体积靠编译开关**：本 spec 与 build.spec 用的是同一份源码、同一条
运行路径，差别只在于"包里放了什么重资产"。功能可用性由 `app.utils.capabilities`
在运行时真实探测（Pillow / cv2 / pillow-heif / rawpy / 人脸模型 / PySide6），
不再有"轻量档不注册查重路由"这种代码级分叉。

完全功能同步（= 桌面档去掉 Qt）：
    $env:MEDIA_BUILD_WITH_FACE="1"; pyinstaller build_web.spec
会比默认多出 desktop/models/（SFace 37MB），人脸线索随之可用。

体积开关：
    $env:MEDIA_BUILD_NO_CV2="1"     # 去掉 OpenCV（-70MB）：视频缩略图/视频帧查重/人脸不可用
    $env:MEDIA_BUILD_WITH_FACE="1"  # 加上人脸模型（+38MB）：人脸线索可用
两者可组合。默认：带 OpenCV、不带人脸模型。

用法：
    pip install -r requirements-web.txt pyinstaller
    pyinstaller build_web.spec
输出：
    dist/影视资源管理器-便携版/影视资源管理器-便携版.exe
"""

import os
import sys
from pathlib import Path

# SPECPATH = 本 spec 所在目录（desktop/）。**不要**再用 .parent 上溯：
# 那样 _root 会变成仓库根，`_root / "main_web.py"` 就找不到了（实测构建报
# "script '...\Media\main_web.py' not found"）。datas 也一律用绝对路径，
# 避免依赖"从哪个目录执行 pyinstaller"。
_root = Path(SPECPATH).resolve()          # desktop/
_web = _root.parent / "web"               # 仓库根的 web/
_models = _root / "models"                # 人脸模型（可选打包）

_NO_CV2 = os.environ.get("MEDIA_BUILD_NO_CV2") == "1"
_WITH_FACE = os.environ.get("MEDIA_BUILD_WITH_FACE") == "1"
if _WITH_FACE and not _models.is_dir():
    raise SystemExit(f"MEDIA_BUILD_WITH_FACE=1 但找不到模型目录: {_models}")

_NAME = '影视资源管理器-便携版'
if _NO_CV2:
    _NAME += '-无CV'
_BUILD_NAME = _NAME

_EXCLUDES = [
    # 桌面 GUI：便携版完全不加载，排除后包体大幅缩小
    'PySide6', 'shiboken6',
    'pystray', 'win10toast',
    # 没用到的重型库（cv2 由开关控制，见下）
    'scipy', 'matplotlib', 'tensorflow', 'torch', 'sklearn',
    'imagehash',
    # 其它无关标准库/第三方
    'tkinter', 'pytest', 'playwright',
]
if _NO_CV2:
    _EXCLUDES.append('cv2')

a = Analysis(
    [str(_root / "main_web.py")],
    pathex=[str(_root)],
    binaries=[],
    datas=[
        # Web 前端 SPA（含 web/vendor 下的离线 Vue/vue-router）
        (str(_web), "web"),
    ] + ([(str(_models), "models")] if _WITH_FACE else []),
    hiddenimports=[
        # uvicorn 的动态导入（不在代码里显式 import，必须声明）
        'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
        'sqlalchemy.sql.default_comparator',
        'PIL._webp',
        'numpy.core._methods', 'numpy.lib.format',
    ] + ([] if _NO_CV2 else ['cv2']),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz, a.scripts,
    [],
    exclude_binaries=True,
    name=_BUILD_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,          # 保留控制台：能看到访问地址与日志，关窗口即退出
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(_web / 'icon-512.png'),
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=_BUILD_NAME,
)
