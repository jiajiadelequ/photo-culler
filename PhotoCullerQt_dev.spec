# -*- mode: python ; coding: utf-8 -*-
# Dev spec — onedir 模式，保留 COLLECT，增量构建

a = Analysis(
    ['photo_culler_qt.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'PySide6.QtMultimedia',
        'PySide6.QtMultimediaWidgets',
        'PIL',
        'PIL.Image',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PySide6.QtBluetooth', 'PySide6.QtDBus', 'PySide6.QtDesigner',
        'PySide6.QtHelp', 'PySide6.QtLocation', 'PySide6.QtNetwork', 'PySide6.QtNfc',
        'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets', 'PySide6.QtPdf',
        'PySide6.QtPdfWidgets', 'PySide6.QtPositioning', 'PySide6.QtPrintSupport',
        'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuick3D',
        'PySide6.QtQuickControls2', 'PySide6.QtQuickWidgets',
        'PySide6.QtRemoteObjects', 'PySide6.QtScxml', 'PySide6.QtSensors',
        'PySide6.QtSerialPort', 'PySide6.QtSerialBus', 'PySide6.QtSql',
        'PySide6.QtStateMachine', 'PySide6.QtSvg', 'PySide6.QtSvgWidgets',
        'PySide6.QtTest', 'PySide6.QtTextToSpeech', 'PySide6.QtUiTools',
        'PySide6.QtWebChannel', 'PySide6.QtWebEngine', 'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineQuick', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebSockets', 'PySide6.QtXml', 'PySide6.QtXmlPatterns',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='照片筛选_Qt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='照片筛选_Qt',
)
