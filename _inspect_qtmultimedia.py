import importlib
import sys

targets = [
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
]

for name in targets:
    try:
        module = importlib.import_module(name)
        print(f"{name}: OK {getattr(module, '__file__', '<builtin>')}")
    except Exception as exc:
        print(f"{name}: FAIL {type(exc).__name__}: {exc}")

try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaMetaData, QVideoSink

    print("QMediaPlayer:", QMediaPlayer)
    print("QAudioOutput:", QAudioOutput)
    print("QMediaMetaData:", QMediaMetaData)
    print("QVideoSink:", QVideoSink)
except Exception as exc:
    print(f"symbol import failed: {type(exc).__name__}: {exc}")

