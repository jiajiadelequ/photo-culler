#!/usr/bin/env python3
"""照片筛选 — PySide6 / QGraphicsView 版本。

- 照片模式：浏览 JPG，自动匹配 RAW，鼠标滚轮缩放
- 视频模式：浏览视频，VLC 播放器，标记筛选
- 保留 / 删除 / 跳过 / 恢复 标记
- 快捷键、菜单、批量操作、会话持久化
"""

import ctypes
import json
import logging
import os
import queue
import sys
import threading
import time
from collections import OrderedDict
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import vlc
except Exception:  # 保留旧类定义兼容，实际播放已切到 Qt Multimedia
    class _VlcCompat:
        Instance = object
        MediaPlayer = object
        Media = object

    vlc = _VlcCompat()
from PIL import Image, ImageOps
if sys.platform == "win32":
    os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
    os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "d3d11va,d3d12va")
    os.environ.setdefault("QT_FFMPEG_DEBUG", "1")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=true")

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF, QObject, QThread, Signal, QProcess
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtGui import (
    QAction, QKeySequence, QPixmap, QImage,
    QWheelEvent, QMouseEvent, QFont,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QGraphicsView, QGraphicsScene,
    QGraphicsPixmapItem, QPushButton, QLabel, QMenuBar, QMenu,
    QToolBar, QStatusBar, QFileDialog, QMessageBox, QFrame, QSizePolicy,
    QSlider, QComboBox, QDialog, QFormLayout, QSpinBox, QDialogButtonBox,
)
from video_qt_backend import (
    VIDEO_PROXY_PROTOCOL_VERSION,
    VIDEO_QUALITY_AUTO,
    VIDEO_QUALITY_LABELS,
    VideoPlayerWidget as QtVideoPlayerWidget,
    VideoProbeInfo,
    discover_ffmpeg_tools,
    install_qt_logging_bridge,
    load_probe_info,
    remove_proxy_artifacts,
    run_video_probe_worker_job,
    run_video_proxy_worker_job,
    save_probe_info,
    should_use_proxy,
    video_proxy_cache_path,
    video_proxy_meta_path,
)

# ─── 常量 ─────────────────────────────────────────────

RAW_EXTENSIONS = {
    ".3fr", ".arw", ".cr2", ".cr3", ".dcr", ".dng", ".erf", ".kdc",
    ".mos", ".mrw", ".nef", ".nrw", ".orf", ".pef", ".raf", ".raw",
    ".rw2", ".sr2", ".srf", ".x3f",
}
JPG_EXTENSIONS = {".jpg", ".jpeg"}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".webm",
    ".mpg", ".mpeg", ".ts", ".mts", ".m2ts",
}

FO_DELETE = 3
FOF_ALLOWUNDO = 0x0040; FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400; FOF_SILENT = 0x0004
DEFAULT_PREVIEW_CACHE_SIZE = 192; DEFAULT_PREVIEW_LOOKAHEAD = 20
MAX_RECENT_SESSIONS = 12; MAX_PREVIEW_SOURCE_EDGE = 4096
ZOOM_STEP = 1.15; ZOOM_MIN = 0.01; ZOOM_MAX = 10.0
VIDEO_SWITCH_DEBOUNCE_MS = 200

# ─── 路径 ─────────────────────────────────────────────

def _settings_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "state"
    d = base / "PhotoCuller"
    d.mkdir(parents=True, exist_ok=True)
    return d

SETTINGS_FILE = _settings_dir() / "settings.json"
STATE_FILE = _settings_dir() / "photo_culler_state.json"
VIDEO_STATE_FILE = _settings_dir() / "photo_culler_video_state.json"
LOG_DIR = _settings_dir() / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "photo_culler.log"
RECYCLE_BIN_API = "SHFileOperationW"

LOGGER = logging.getLogger("photo_culler")
if not LOGGER.handlers:
    LOGGER.setLevel(logging.INFO)
    _handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(threadName)s:%(thread)d] %(message)s"
    ))
    LOGGER.addHandler(_handler)
    LOGGER.propagate = False

install_qt_logging_bridge(sys.modules.get("PySide6.QtCore"))

DELETE_PROTOCOL_VERSION = 1

def load_json_file(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default

def safe_write_json(path: Path, data: dict) -> bool:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        LOGGER.exception("Failed to safely write json path=%s", path)
        return False

def load_settings() -> dict[str, int]:
    defaults = {"preview_cache_size": DEFAULT_PREVIEW_CACHE_SIZE,
                "preview_lookahead": DEFAULT_PREVIEW_LOOKAHEAD}
    data = load_json_file(SETTINGS_FILE, {})
    for k in defaults:
        if k in data and isinstance(data[k], int):
            defaults[k] = max(1, min(data[k], 2000))
    return defaults

def save_settings(cache_size: int, lookahead: int, last_mode: str = "photo") -> None:
    safe_write_json(SETTINGS_FILE, {
        "preview_cache_size": cache_size,
        "preview_lookahead": lookahead,
        "last_mode": last_mode,
    })

# ─── Windows 回收站 ──────────────────────────────────

class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID), ("lpszProgressTitle", wintypes.LPCWSTR),
    ]

def move_to_recycle_bin(paths: list[Path]) -> None:
    existing = [str(p) for p in paths if p.exists()]
    if not existing:
        return
    joined = "\0".join(existing) + "\0\0"
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE; op.pFrom = joined
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0:
        raise OSError(f"回收站操作失败，错误代码：{result}")
    if op.fAnyOperationsAborted:
        raise OSError("删除操作已中止")

def move_path_to_recycle_bin(path: Path) -> None:
    move_to_recycle_bin([path])

def thread_diag() -> str:
    return f"name={threading.current_thread().name}, ident={threading.get_ident()}"

def delete_worker_event(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)

def run_delete_worker_job(job_path: str) -> int:
    started_at = time.perf_counter()
    thread_info = thread_diag()
    try:
        job = load_json_file(Path(job_path), {})
        paths = [Path(p) for p in job.get("paths", [])]
    except Exception as ex:
        delete_worker_event({
            "event": "fatal",
            "exception_type": type(ex).__name__,
            "error_message": str(ex),
            "job_path": job_path,
        })
        return 2

    LOGGER.info(
        "Delete worker process started: job=%s total=%s thread=%s api=%s",
        job_path,
        len(paths),
        thread_info,
        RECYCLE_BIN_API,
    )
    delete_worker_event({
        "event": "started",
        "protocol_version": DELETE_PROTOCOL_VERSION,
        "job_path": job_path,
        "thread": thread_info,
        "total": len(paths),
        "api": RECYCLE_BIN_API,
    })

    successful_paths: list[str] = []
    missing_paths: list[str] = []
    failed_items: list[dict] = []
    last_path = None

    for index, path in enumerate(paths, start=1):
        last_path = str(path)
        item_started = time.perf_counter()
        delete_worker_event({
            "event": "progress",
            "phase": "before_delete",
            "index": index,
            "total": len(paths),
            "path": str(path),
        })
        LOGGER.info("Delete worker item start: index=%s total=%s path=%s", index, len(paths), path)
        result_label = "success"
        try:
            if not path.exists():
                result_label = "missing"
                missing_paths.append(str(path))
            else:
                move_path_to_recycle_bin(path)
                if path.exists():
                    raise OSError("文件删除调用返回后文件仍然存在")
                successful_paths.append(str(path))
        except Exception as ex:
            if not path.exists():
                result_label = "missing_after_error"
                missing_paths.append(str(path))
                LOGGER.warning(
                    "Delete worker item raised but file disappeared: path=%s type=%s message=%s",
                    path,
                    type(ex).__name__,
                    ex,
                )
            else:
                result_label = "failed"
                failed_items.append({
                    "path": str(path),
                    "exception_type": type(ex).__name__,
                    "error_message": str(ex),
                })
                LOGGER.exception("Delete worker item failed path=%s", path)
        elapsed_ms = int((time.perf_counter() - item_started) * 1000)
        delete_worker_event({
            "event": "progress",
            "phase": "after_delete",
            "index": index,
            "total": len(paths),
            "path": str(path),
            "result": result_label,
            "elapsed_ms": elapsed_ms,
        })

    total_elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    result_payload = {
        "event": "finished",
        "successful_paths": successful_paths,
        "missing_paths": missing_paths,
        "failed_items": failed_items,
        "processed_count": len(successful_paths) + len(missing_paths) + len(failed_items),
        "total_count": len(paths),
        "cancelled": False,
        "elapsed_time_ms": total_elapsed_ms,
        "last_path": last_path,
    }
    LOGGER.info(
        "Delete worker process finished: total=%s success=%s failed=%s missing=%s elapsed_ms=%s last_path=%s",
        len(paths),
        len(successful_paths),
        len(failed_items),
        len(missing_paths),
        total_elapsed_ms,
        last_path,
    )
    delete_worker_event(result_payload)
    return 0


class VideoDeleteWorker(QObject):
    progress = Signal(int, int, str, str)
    finished = Signal(dict)

    def __init__(self, paths: list[Path]):
        super().__init__()
        self.paths = list(paths)
        self._cancel_requested = False

    def request_cancel(self):
        self._cancel_requested = True

    def run(self):
        started_at = time.perf_counter()
        successful_paths: list[str] = []
        missing_paths: list[str] = []
        failed_items: list[dict] = []
        last_path = None
        LOGGER.info(
            "Video delete worker started: total=%s thread=%s api=%s",
            len(self.paths),
            thread_diag(),
            RECYCLE_BIN_API,
        )
        for index, path in enumerate(self.paths, start=1):
            if self._cancel_requested:
                LOGGER.info("Video delete worker cancelled before path=%s", last_path)
                break
            last_path = str(path)
            item_start = time.perf_counter()
            LOGGER.info("Delete item start %s/%s path=%s", index, len(self.paths), path)
            result_label = "success"
            try:
                if not path.exists():
                    result_label = "missing"
                    missing_paths.append(str(path))
                else:
                    move_path_to_recycle_bin(path)
                    if path.exists():
                        raise OSError("文件删除调用返回后文件仍然存在")
                    successful_paths.append(str(path))
            except Exception as ex:
                if not path.exists():
                    result_label = "missing_after_error"
                    missing_paths.append(str(path))
                    LOGGER.warning(
                        "Delete item raised but path disappeared: path=%s type=%s message=%s",
                        path,
                        type(ex).__name__,
                        ex,
                    )
                else:
                    result_label = "failed"
                    failed_items.append({
                        "path": str(path),
                        "exception_type": type(ex).__name__,
                        "error_message": str(ex),
                    })
                    LOGGER.exception("Delete item failed path=%s", path)
            elapsed_ms = int((time.perf_counter() - item_start) * 1000)
            LOGGER.info(
                "Delete item done %s/%s path=%s result=%s elapsed_ms=%s",
                index,
                len(self.paths),
                path,
                result_label,
                elapsed_ms,
            )
            self.progress.emit(index, len(self.paths), str(path), result_label)
        total_elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        result = {
            "successful_paths": successful_paths,
            "missing_paths": missing_paths,
            "failed_items": failed_items,
            "processed_count": len(successful_paths) + len(missing_paths) + len(failed_items),
            "total_count": len(self.paths),
            "cancelled": self._cancel_requested,
            "elapsed_time_ms": total_elapsed_ms,
            "last_path": last_path,
        }
        LOGGER.info(
            "Video delete worker finished: processed=%s total=%s success=%s failed=%s missing=%s cancelled=%s elapsed_ms=%s last_path=%s",
            result["processed_count"],
            result["total_count"],
            len(successful_paths),
            len(failed_items),
            len(missing_paths),
            result["cancelled"],
            total_elapsed_ms,
            last_path,
        )
        self.finished.emit(result)

# ─── 数据模型 ─────────────────────────────────────────

@dataclass
class PhotoEntry:
    jpg_path: Path; relative_path: Path
    raw_paths: list[Path] = field(default_factory=list)
    status: str = "pending"

    @property
    def stem(self) -> str: return self.jpg_path.stem

    @property
    def display_name(self) -> str:
        raw_suffix = f" | RAW x{len(self.raw_paths)}" if self.raw_paths else ""
        return f"{self.status_label()} {self.relative_path}{raw_suffix}"

    def status_label(self) -> str:
        return {"pending": "[待处理]", "kept": "[保留]",
                "deleted": "[-]", "skipped": "[跳过]",
                "play_failed": "[播放失败]"}[self.status]

    def status_text(self) -> str:
        return {"pending": "待处理", "kept": "已保留",
                "deleted": "已标记删除", "skipped": "已跳过",
                "play_failed": "播放失败"}[self.status]

@dataclass
class VideoEntry:
    video_path: Path; relative_path: Path
    status: str = "pending"

    @property
    def display_name(self) -> str:
        return f"{self.status_label()} {self.relative_path}"

    def status_label(self) -> str:
        return {"pending": "[待处理]", "kept": "[保留]",
                "deleted": "[-]", "skipped": "[跳过]",
                "play_failed": "[播放失败]"}[self.status]

    def status_text(self) -> str:
        return {"pending": "待处理", "kept": "已保留",
                "deleted": "已标记删除", "skipped": "已跳过",
                "play_failed": "播放失败"}[self.status]

# ═══════════════════════════════════════════════════════
# 视频播放器（VLC 后端）
# ═══════════════════════════════════════════════════════

class VideoPlayerWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._instance: vlc.Instance | None = None
        self._player: vlc.MediaPlayer | None = None
        self._media: vlc.Media | None = None
        self._cleanup_threads: list[threading.Thread] = []
        self._current_path = ""
        self._is_playing = False; self._duration_ms = 0
        self._volume = 100; self._muted = False
        self._speed = 1.0; self._seeking = False
        self._autoplay = True; self._auto_advance = False
        self._pending_seek_ms = 0
        self._last_progress_second = -1
        self.on_state_changed = None

        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)

        self._video_frame = QWidget()
        self._video_frame.setStyleSheet("background-color: #000000;")
        self._video_frame.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        layout.addWidget(self._video_frame, 1)

        ctrl = QWidget(); cl = QHBoxLayout(ctrl); cl.setContentsMargins(4, 2, 4, 2)

        self._btn_play = QPushButton("▶"); self._btn_play.setFixedWidth(30)
        self._btn_play.clicked.connect(self._toggle_play); cl.addWidget(self._btn_play)

        self._lbl_time = QLabel("00:00 / 00:00"); cl.addWidget(self._lbl_time)

        self._slider = QSlider(Qt.Orientation.Horizontal); self._slider.setRange(0, 1000)
        self._slider.sliderPressed.connect(lambda: setattr(self, '_seeking', True))
        self._slider.sliderReleased.connect(self._on_seek_end); cl.addWidget(self._slider, 1)

        self._btn_mute = QPushButton("🔊"); self._btn_mute.setFixedWidth(30)
        self._btn_mute.clicked.connect(self._toggle_mute); cl.addWidget(self._btn_mute)

        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 100); self._vol_slider.setValue(100)
        self._vol_slider.setFixedWidth(80)
        self._vol_slider.valueChanged.connect(self._on_volume_change); cl.addWidget(self._vol_slider)

        self._cmb_speed = QComboBox()
        self._cmb_speed.addItems(["0.5x","1.0x","1.25x","1.5x","2.0x","3.0x","4.0x"])
        self._cmb_speed.setCurrentText("1.0x")
        self._cmb_speed.currentTextChanged.connect(self._on_speed_change); cl.addWidget(self._cmb_speed)

        layout.addWidget(ctrl)

        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._update_ui); self._update_timer.start(200)

    def _ensure_vlc(self):
        if self._instance is None:
            self._instance = vlc.Instance("--no-xlib --quiet")
            self._player = self._instance.media_player_new()

    def load(self, path: str):
        self._ensure_vlc()
        if self._player is None: return
        self.stop()
        self._current_path = path
        self._media = self._instance.media_new(path)
        self._player.set_media(self._media)
        if sys.platform == "win32":
            self._player.set_hwnd(int(self._video_frame.winId()))
        self._player.audio_set_volume(self._volume)
        self._player.audio_set_mute(self._muted)
        self._player.set_rate(self._speed)
        self._player.play()
        self._is_playing = True
        self._btn_play.setText("⏸")
        self._last_progress_second = -1
        if self._pending_seek_ms > 0 or not self._autoplay:
            QTimer.singleShot(150, self._apply_pending_start_state)
        self._emit_state_changed()

    def _apply_pending_start_state(self):
        if self._player is None:
            return
        if self._pending_seek_ms > 0:
            self._player.set_time(self._pending_seek_ms)
        if not self._autoplay:
            self._pause()
        self._pending_seek_ms = 0

    def _play(self):
        if self._player:
            self._player.play(); self._is_playing = True; self._btn_play.setText("⏸")
            self._emit_state_changed()

    def _pause(self):
        if self._player:
            self._player.pause(); self._is_playing = False; self._btn_play.setText("▶")
            self._emit_state_changed()

    def _toggle_play(self):
        self._pause() if self._is_playing else self._play()

    def _reset_ui_state(self):
        self._is_playing = False
        self._current_path = ""
        self._duration_ms = 0
        self._pending_seek_ms = 0
        self._last_progress_second = -1
        self._btn_play.setText("▶")
        self._lbl_time.setText("00:00 / 00:00")
        self._slider.setValue(0)
        self._emit_state_changed()

    def _release_vlc_handles(self, instance, player, media, reason: str):
        started_at = time.perf_counter()
        LOGGER.info("VLC cleanup thread started: reason=%s thread=%s", reason, thread_diag())
        try:
            if player is not None:
                LOGGER.info("VLC cleanup stop begin: reason=%s", reason)
                try:
                    player.stop()
                finally:
                    LOGGER.info("VLC cleanup stop end: reason=%s", reason)
                LOGGER.info("VLC cleanup clear media begin: reason=%s", reason)
                try:
                    player.set_media(None)
                except Exception:
                    LOGGER.exception("VLC cleanup set_media(None) failed: reason=%s", reason)
                finally:
                    LOGGER.info("VLC cleanup clear media end: reason=%s", reason)
            if media is not None:
                LOGGER.info("VLC cleanup media release begin: reason=%s", reason)
                media.release()
                LOGGER.info("VLC cleanup media release end: reason=%s", reason)
            if player is not None:
                LOGGER.info("VLC cleanup player release begin: reason=%s", reason)
                player.release()
                LOGGER.info("VLC cleanup player release end: reason=%s", reason)
            if instance is not None:
                LOGGER.info("VLC cleanup instance release begin: reason=%s", reason)
                instance.release()
                LOGGER.info("VLC cleanup instance release end: reason=%s", reason)
        except Exception:
            LOGGER.exception("VLC cleanup failed: reason=%s", reason)
        finally:
            LOGGER.info(
                "VLC cleanup thread finished: reason=%s thread=%s elapsed_ms=%s",
                reason,
                thread_diag(),
                int((time.perf_counter() - started_at) * 1000),
            )

    def _handoff_vlc_handles(self, reason: str):
        instance = self._instance
        player = self._player
        media = self._media
        self._instance = None
        self._player = None
        self._media = None
        self._reset_ui_state()
        if instance is None and player is None and media is None:
            return
        cleanup_thread = threading.Thread(
            target=self._release_vlc_handles,
            args=(instance, player, media, reason),
            daemon=True,
            name="vlc-cleanup",
        )
        self._cleanup_threads.append(cleanup_thread)
        self._cleanup_threads = [t for t in self._cleanup_threads if t.is_alive() or t is cleanup_thread]
        cleanup_thread.start()

    def detach_for_delete(self):
        LOGGER.info("Video widget detach_for_delete begin: thread=%s state=%s", thread_diag(), self.state_snapshot())
        self._handoff_vlc_handles("delete")
        LOGGER.info("Video widget detach_for_delete end: thread=%s", thread_diag())

    def stop(self):
        LOGGER.info("Video widget stop begin: thread=%s state=%s", thread_diag(), self.state_snapshot())
        if self._player:
            self._player.stop()
            try:
                self._player.set_media(None)
            except Exception:
                pass
            self._is_playing = False
            self._btn_play.setText("▶")
        if self._media:
            self._media.release()
            self._media = None
        self._current_path = ""; self._duration_ms = 0
        self._lbl_time.setText("00:00 / 00:00"); self._slider.setValue(0)
        self._pending_seek_ms = 0
        self._last_progress_second = -1
        self._emit_state_changed()

    def dispose(self):
        self._update_timer.stop(); self.stop()
        if self._player: self._player.release(); self._player = None
        if self._instance: self._instance.release(); self._instance = None

    def _update_ui(self):
        if self._player is None or not self._is_playing: return
        pos = self._player.get_time(); dur = self._player.get_length()
        if dur > 0:
            self._duration_ms = dur
            if not self._seeking:
                self._slider.setValue(int(pos / dur * 1000))
            self._lbl_time.setText(f"{self._fmt(pos)} / {self._fmt(dur)}")
            current_second = max(0, int(pos // 1000))
            if current_second != self._last_progress_second:
                self._last_progress_second = current_second
                self._emit_state_changed(high_frequency=True)

    def _on_seek_end(self):
        self._seeking = False
        if self._player and self._duration_ms > 0:
            self._player.set_time(int(self._slider.value() / 1000 * self._duration_ms))
            self._emit_state_changed(high_frequency=True)

    def _toggle_mute(self):
        self._muted = not self._muted
        if self._player: self._player.audio_set_mute(self._muted)
        self._btn_mute.setText("🔇" if self._muted else "🔊")
        self._emit_state_changed()

    def _on_volume_change(self, value: int):
        self._volume = value
        if self._player: self._player.audio_set_volume(value)
        self._emit_state_changed(high_frequency=True)

    def _on_speed_change(self, text: str):
        self._speed = float(text.replace("x", ""))
        if self._player: self._player.set_rate(self._speed)
        self._emit_state_changed()

    def get_preferences(self) -> dict:
        return {
            "playback_rate": self._speed,
            "volume": self._volume,
            "muted": self._muted,
            "autoplay": self._autoplay,
            "auto_advance": self._auto_advance,
        }

    def apply_preferences(self, prefs: dict | None):
        prefs = prefs or {}
        self._speed = float(prefs.get("playback_rate", 1.0) or 1.0)
        self._volume = max(0, min(int(prefs.get("volume", 100) or 100), 100))
        self._muted = bool(prefs.get("muted", False))
        self._autoplay = bool(prefs.get("autoplay", True))
        self._auto_advance = bool(prefs.get("auto_advance", False))
        self._cmb_speed.setCurrentText(f"{self._speed}x" if f"{self._speed}x" in [self._cmb_speed.itemText(i) for i in range(self._cmb_speed.count())] else "1.0x")
        self._vol_slider.setValue(self._volume)
        self._btn_mute.setText("🔇" if self._muted else "🔊")
        if self._player:
            self._player.audio_set_volume(self._volume)
            self._player.audio_set_mute(self._muted)
            self._player.set_rate(self._speed)

    def prepare_restore(self, position_ms: int = 0):
        self._pending_seek_ms = max(0, int(position_ms or 0))

    def current_position(self) -> int:
        if self._player is None:
            return 0
        try:
            return max(0, int(self._player.get_time()))
        except Exception:
            return 0

    def is_playing(self) -> bool:
        return self._is_playing

    def current_media_path(self) -> str:
        return self._current_path

    def state_snapshot(self) -> dict:
        return {
            "current_path": self._current_path,
            "is_playing": self._is_playing,
            "duration_ms": self._duration_ms,
            "position_ms": self.current_position(),
            "volume": self._volume,
            "muted": self._muted,
            "speed": self._speed,
            "has_media": self._media is not None,
            "has_player": self._player is not None,
        }

    def _emit_state_changed(self, high_frequency: bool = False):
        if callable(self.on_state_changed):
            self.on_state_changed(high_frequency)

    @staticmethod
    def _fmt(ms: int) -> str:
        s = ms // 1000; return f"{s // 60:02d}:{s % 60:02d}"

# ═══════════════════════════════════════════════════════
# 图片预览组件
# ═══════════════════════════════════════════════════════

class ImageGraphicsView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self); self.setScene(self._scene)
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._current_pixmap: QPixmap | None = None
        self._current_path = ""; self._zoom_level = 1.0; self._fit_zoom = 1.0
        self.setStyleSheet("background-color: #111111; border: none;")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setRenderHints(self.renderHints())
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def set_image(self, image: Image.Image, path: str = ""):
        self._current_path = path
        if image.mode not in ("RGB", "RGBA"): image = image.convert("RGBA")
        data = image.tobytes("raw", image.mode)
        qimage = QImage(data, image.width, image.height, image.width * len(image.mode),
                        QImage.Format.Format_RGBA8888 if image.mode == "RGBA"
                        else QImage.Format.Format_RGB888)
        self._current_pixmap = QPixmap.fromImage(qimage)
        self._update_scene(); self.fit_to_view()

    def _update_scene(self):
        if self._current_pixmap is None: return
        self._scene.clear()
        self._pixmap_item = QGraphicsPixmapItem(self._current_pixmap)
        self._scene.addItem(self._pixmap_item)
        self._scene.setSceneRect(QRectF(self._current_pixmap.rect()))

    def fit_to_view(self):
        if self._current_pixmap is None: return
        self.resetTransform()
        rect = self._current_pixmap.rect()
        if rect.width() <= 0 or rect.height() <= 0: return
        vw = self.viewport().width(); vh = self.viewport().height()
        self._fit_zoom = min(vw / rect.width(), vh / rect.height(), 1.0)
        self._zoom_level = self._fit_zoom
        self.setSceneRect(QRectF(rect))
        self.fitInView(QRectF(rect), Qt.AspectRatioMode.KeepAspectRatio)
        self._zoom_level = self.transform().m11()

    def clear_image(self):
        self._scene.clear(); self._pixmap_item = None
        self._current_pixmap = None; self._current_path = ""

    @property
    def zoom_level(self) -> float: return self._zoom_level

    def wheelEvent(self, event: QWheelEvent):
        angle = event.angleDelta().y()
        if angle == 0: return
        factor = ZOOM_STEP if angle > 0 else 1.0 / ZOOM_STEP
        new_zoom = self._zoom_level * factor
        if ZOOM_MIN <= new_zoom <= ZOOM_MAX:
            self._zoom_level = new_zoom; self.scale(factor, factor)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        self.fit_to_view(); event.accept()

# ═══════════════════════════════════════════════════════
# 主窗口
# ═══════════════════════════════════════════════════════

class PhotoCullerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("照片筛选"); self.resize(1280, 760); self.setMinimumSize(980, 620)

        # ── 业务状态 ──────────────────────────────────
        self.current_folder: Path | None = None
        self._photo_folder: Path | None = None
        self._video_folder: Path | None = None
        self._photo_entries: list = []; self._photo_index: int | None = None
        self._video_entries: list = []; self._video_index: int | None = None
        self._mode: str = "photo"
        self.entries: list = []; self.current_index: int | None = None
        self.preview_cache: OrderedDict[Path, Image.Image] = OrderedDict()
        self.preview_cache_lock = threading.Lock()

        self.preview_requests: queue.PriorityQueue = queue.PriorityQueue()
        self.preview_results: queue.Queue = queue.Queue()
        self.preview_request_id = 0; self.preview_task_id = 0
        self.preview_queued_paths: set[Path] = set()
        self.preview_queue_lock = threading.Lock()
        self.scan_requests: queue.Queue = queue.Queue()
        self.scan_results: queue.Queue = queue.Queue()
        self.scan_request_id = 0; self.is_scanning = False
        self.pending_restore_photo: str | None = None
        self.pending_restore_video: str | None = None
        self.pending_restore_index: int | None = None
        self.pending_restore_scroll: int | None = None
        self.pending_restore_video_position: int = 0
        self._video_persisted_statuses: dict[str, str] = {}
        self._photo_undo_stack: list[dict] = []
        self._video_undo_stack: list[dict] = []
        self._undo_stack: list[dict] = self._photo_undo_stack
        self._delete_in_progress = False
        self._delete_process: QProcess | None = None
        self._delete_output_buffer = ""
        self._delete_job_file: Path | None = None
        self._delete_result: dict | None = None
        self._close_after_delete = False
        self._delete_cancel_requested = False
        self._delete_restore_context: dict = {}
        self._gui_thread_ident = threading.get_ident()
        self._video_quality_mode = VIDEO_QUALITY_AUTO
        self._video_tools = discover_ffmpeg_tools()
        self._video_probe_process: QProcess | None = None
        self._video_probe_job_file: Path | None = None
        self._video_probe_output_buffer = ""
        self._video_probe_target: str | None = None
        self._video_probe_cache: dict[str, VideoProbeInfo] = {}
        self._video_proxy_process: QProcess | None = None
        self._video_proxy_job_file: Path | None = None
        self._video_proxy_output_buffer = ""
        self._video_proxy_target: str | None = None
        self._video_proxy_output_path: str | None = None
        self._video_proxy_progress_text = ""
        self._video_probe_switch_id = 0
        self._video_proxy_switch_id = 0
        self._video_switch_id = 0
        self._video_switch_timer: QTimer | None = None
        self._video_pending_target: VideoEntry | None = None
        self._video_load_state = "idle"
        self._video_load_timer: QTimer | None = None
        self._video_pending_source: Path | None = None
        self._video_stop_started_at = 0.0

        self.recent_sessions: list[dict] = []
        self.persisted_statuses: dict[str, str] = {}
        self._photo_state_store = self._load_mode_state("photo")
        self._video_state_store = self._load_mode_state("video")

        settings = load_settings()
        self.preview_cache_size = settings["preview_cache_size"]
        self.preview_lookahead = settings["preview_lookahead"]

        for _ in range(3):
            threading.Thread(target=self._preview_worker_loop, daemon=True).start()
        threading.Thread(target=self._scan_worker_loop, daemon=True).start()

        self._build_ui(); self._build_menus(); self._setup_shortcuts()

        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._process_preview_results)
        self._preview_timer.start(50)
        self._scan_timer = QTimer(self)
        self._scan_timer.timeout.connect(self._process_scan_results)
        self._scan_timer.start(50)
        self._state_save_timer = QTimer(self)
        self._state_save_timer.setSingleShot(True)
        self._state_save_timer.timeout.connect(self._save_state)

        QTimer.singleShot(100, self._restore_last_session)
        self._switch_mode("photo")

    def _empty_mode_state(self, mode: str) -> dict:
        data = {"last_directory": None, "directories": {}, "recent_sessions": []}
        if mode == "video":
            data["preferences"] = {
                "playback_rate": 1.0,
                "volume": 100,
                "muted": False,
                "autoplay": True,
                "auto_advance": False,
                "quality_mode": VIDEO_QUALITY_AUTO,
            }
        return data

    def _state_file_for_mode(self, mode: str) -> Path:
        return VIDEO_STATE_FILE if mode == "video" else STATE_FILE

    def _mode_state_store(self, mode: str) -> dict:
        return self._video_state_store if mode == "video" else self._photo_state_store

    def _load_mode_state(self, mode: str) -> dict:
        data = self._empty_mode_state(mode)
        loaded = load_json_file(self._state_file_for_mode(mode), {})
        if not isinstance(loaded, dict):
            return data
        directories = loaded.get("directories")
        if isinstance(directories, dict):
            data["directories"] = directories
        recent_sessions = loaded.get("recent_sessions")
        if isinstance(recent_sessions, list):
            data["recent_sessions"] = recent_sessions
        if isinstance(loaded.get("last_directory"), str):
            data["last_directory"] = loaded.get("last_directory")
        if mode == "video":
            prefs = loaded.get("preferences")
            if isinstance(prefs, dict):
                data["preferences"].update(prefs)
        # 兼容旧版单目录结构
        legacy_folder = loaded.get("folder")
        if isinstance(legacy_folder, str) and legacy_folder:
            legacy_dir = {
                "current_file": loaded.get("current_video" if mode == "video" else "current_photo"),
                "current_index": loaded.get("current_index"),
                "scroll_position": loaded.get("scroll_position", 0),
                "file_states": loaded.get("video_statuses" if mode == "video" else "photo_statuses", {}),
            }
            if mode == "video":
                legacy_dir["playback_position"] = loaded.get("playback_position", 0)
            data["directories"][legacy_folder] = legacy_dir
            if not data.get("last_directory"):
                data["last_directory"] = legacy_folder
        return data

    def _directory_key(self, folder: Path | None) -> str | None:
        if folder is None:
            return None
        try:
            return str(folder.resolve())
        except OSError:
            return str(folder)

    def _current_store_key(self, mode: str | None = None) -> str | None:
        mode = mode or self._mode
        folder = self.current_folder if mode == self._mode else (self._video_folder if mode == "video" else self._photo_folder)
        return self._directory_key(folder)

    def _schedule_state_save(self, delay_ms: int = 1200):
        if self.is_scanning or self._delete_in_progress:
            return
        self._state_save_timer.start(max(200, delay_ms))

    def _log_delete(self, level: int, message: str, *args):
        LOGGER.log(level, message, *args)

    def _set_delete_ui_busy(self, busy: bool):
        self._delete_in_progress = busy
        self._btn_open.setEnabled(not busy)
        self._btn_photo_mode.setEnabled(not busy)
        self._btn_video_mode.setEnabled(not busy)
        self._btn_keep.setEnabled(not busy and self.current_index is not None and len(self.entries) > 0)
        self._btn_delete.setEnabled(not busy and self.current_index is not None and len(self.entries) > 0)
        self._btn_skip.setEnabled(not busy and self.current_index is not None and len(self.entries) > 0)
        self._btn_restore.setEnabled(not busy and self.current_index is not None and len(self.entries) > 0 and self.entries[self.current_index].status == "deleted")
        self._btn_commit.setEnabled(not busy and any(e.status == "deleted" for e in self.entries))
        self._btn_batch_keep.setEnabled(False if busy else self._btn_batch_keep.isEnabled())
        self._btn_batch_delete.setEnabled(False if busy else self._btn_batch_delete.isEnabled())
        self._btn_batch_skip.setEnabled(False if busy else self._btn_batch_skip.isEnabled())
        self._btn_batch_restore.setEnabled(False if busy else self._btn_batch_restore.isEnabled())
        self._btn_cancel_delete.setEnabled(busy)
        if busy:
            self._lbl_hint.setText("正在后台删除已标记视频，请稍候…")
        else:
            self._update_controls()
            self._lbl_hint.setText(f"Delete 只做删除标记。预缓存 {self.preview_cache_size} 张，向前预读 {self.preview_lookahead} 张。")

    def _record_delete_restore_context(self):
        current_path = None
        if self.current_index is not None and self.entries:
            current_path = str(self.entries[self.current_index].relative_path)
        self._delete_restore_context = {
            "current_path": current_path,
            "current_index": self.current_index,
            "scroll_position": self._file_list.verticalScrollBar().value(),
        }

    def _restore_after_video_delete(self):
        if not self.entries:
            self.current_index = None
            self._refresh_list()
            self._set_status("列表已清空。")
            return
        ctx = self._delete_restore_context or {}
        scroll_value = max(0, int(ctx.get("scroll_position", 0) or 0))
        target_path = ctx.get("current_path")
        original_index = ctx.get("current_index")
        if target_path:
            for i, entry in enumerate(self.entries):
                if str(entry.relative_path) == target_path:
                    self._set_selection(i)
                    self._file_list.verticalScrollBar().setValue(scroll_value)
                    return
        if isinstance(original_index, int):
            anchor = max(0, min(original_index, len(self.entries) - 1))
        else:
            anchor = 0
        for i in range(anchor, len(self.entries)):
            if self.entries[i].status == "pending":
                self._set_selection(i)
                self._file_list.verticalScrollBar().setValue(scroll_value)
                return
        for i in range(anchor - 1, -1, -1):
            if self.entries[i].status == "pending":
                self._set_selection(i)
                self._file_list.verticalScrollBar().setValue(scroll_value)
                return
        self._set_selection(anchor)
        self._file_list.verticalScrollBar().setValue(scroll_value)

    def _cleanup_delete_process(self):
        if self._delete_process is not None:
            self._delete_process.deleteLater()
        self._delete_process = None
        self._delete_output_buffer = ""
        if self._delete_job_file is not None:
            try:
                if self._delete_job_file.exists():
                    self._delete_job_file.unlink()
            except OSError:
                LOGGER.exception("Failed to remove delete job file path=%s", self._delete_job_file)
        self._delete_job_file = None

    def _release_video_for_delete(self):
        snapshot_before = self._video_widget.state_snapshot()
        self._log_delete(logging.INFO, "Releasing VLC before delete: snapshot=%s", snapshot_before)
        started = time.perf_counter()
        self._video_widget.detach_for_delete()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._log_delete(logging.INFO, "VLC detach call finished: elapsed_ms=%s snapshot=%s", elapsed_ms, self._video_widget.state_snapshot())

    def _delete_process_program_and_args(self, job_file: Path) -> tuple[str, list[str]]:
        if getattr(sys, "frozen", False):
            return sys.executable, ["--delete-worker", str(job_file)]
        return sys.executable, [str(Path(__file__).resolve()), "--delete-worker", str(job_file)]

    def _write_delete_job_file(self, targets: list[Path]) -> Path:
        job_dir = _settings_dir() / "delete_jobs"
        job_dir.mkdir(parents=True, exist_ok=True)
        job_file = job_dir / f"delete_job_{int(time.time() * 1000)}.json"
        payload = {
            "protocol_version": DELETE_PROTOCOL_VERSION,
            "created_at": time.time(),
            "paths": [str(path) for path in targets],
        }
        if not safe_write_json(job_file, payload):
            raise OSError(f"无法写入删除任务文件：{job_file}")
        return job_file

    def _write_video_worker_job(self, worker_name: str, payload: dict) -> Path:
        job_dir = _settings_dir() / worker_name
        job_dir.mkdir(parents=True, exist_ok=True)
        job_file = job_dir / f"{worker_name}_{int(time.time() * 1000)}.json"
        if not safe_write_json(job_file, payload):
            raise OSError(f"无法写入任务文件：{job_file}")
        return job_file

    def _cleanup_worker_job_file(self, job_file: Path | None):
        if job_file is None:
            return
        try:
            if job_file.exists():
                job_file.unlink()
        except OSError:
            LOGGER.exception("Failed to remove worker job file path=%s", job_file)

    def _delete_worker_program_and_args(self, worker_flag: str, job_file: Path) -> tuple[str, list[str]]:
        if getattr(sys, "frozen", False):
            return sys.executable, [worker_flag, str(job_file)]
        return sys.executable, [str(Path(__file__).resolve()), worker_flag, str(job_file)]

    def _cached_probe_info(self, source_path: Path) -> VideoProbeInfo | None:
        key = str(source_path.resolve())
        if key in self._video_probe_cache:
            return self._video_probe_cache[key]
        meta_path = video_proxy_meta_path(_settings_dir(), source_path)
        info = load_probe_info(meta_path)
        if info is not None:
            self._video_probe_cache[key] = info
        return info

    def _set_probe_info(self, source_path: Path, info: VideoProbeInfo):
        key = str(source_path.resolve())
        self._video_probe_cache[key] = info
        save_probe_info(video_proxy_meta_path(_settings_dir(), source_path), info)

    def _proxy_path_for(self, source_path: Path) -> Path:
        return video_proxy_cache_path(_settings_dir(), source_path)

    def _should_use_proxy_for_current_mode(self, info: VideoProbeInfo | None) -> bool:
        return should_use_proxy(self._video_quality_mode, info)

    def _set_video_hint(self, message: str):
        self._video_proxy_progress_text = message
        self._lbl_hint.setText(message)

    def _clear_video_hint(self):
        self._video_proxy_progress_text = ""
        self._lbl_hint.setText(f"Delete 只做删除标记。预缓存 {self.preview_cache_size} 张，向前预读 {self.preview_lookahead} 张。")

    def _get_video_preferences(self) -> dict:
        prefs = self._video_widget.get_preferences()
        prefs["quality_mode"] = self._video_quality_mode
        return prefs

    def _apply_video_preferences(self):
        prefs = self._video_state_store.get("preferences", {})
        self._video_quality_mode = prefs.get("quality_mode", VIDEO_QUALITY_AUTO)
        self._video_widget.apply_preferences(prefs)
        if hasattr(self, "_cmb_video_quality"):
            self._cmb_video_quality.blockSignals(True)
            self._cmb_video_quality.setCurrentText(VIDEO_QUALITY_LABELS.get(self._video_quality_mode, "自动"))
            self._cmb_video_quality.blockSignals(False)

    def _on_video_quality_text_changed(self, text: str):
        reverse_map = {label: key for key, label in VIDEO_QUALITY_LABELS.items()}
        new_mode = reverse_map.get(text, VIDEO_QUALITY_AUTO)
        if new_mode == self._video_quality_mode:
            return
        self._video_quality_mode = new_mode
        self._save_video_state()
        if self._mode == "video" and self.current_index is not None and self.entries:
            self._show_current()

    def _video_playback_text(self, entry: VideoEntry, info: VideoProbeInfo | None, using_proxy: bool) -> str:
        lines = [f"路径：{entry.video_path}", f"状态：{entry.status_text()}"]
        if info is not None:
            lines.append(f"规格：{info.summary_text()}")
            if info.color_transfer:
                lines.append(f"色彩传递：{info.color_transfer}")
            hw_text = "未知"
            if info.hwaccel_active is True:
                hw_text = f"已启用（{info.hwaccel_requested}）"
            elif info.hwaccel_active is False:
                hw_text = f"未启用（已请求 {info.hwaccel_requested}）"
            lines.append(f"硬件解码：{hw_text}")
        lines.append(f"播放质量：{VIDEO_QUALITY_LABELS.get(self._video_quality_mode, '自动')}")
        if using_proxy:
            lines.append("当前播放：代理预览")
        elif self._video_quality_mode == "original":
            lines.append("当前播放：原片")
        if entry.status == "deleted":
            lines.append("提示：该视频已标记删除，只有点击“删除已标记视频”后才会移动到回收站。")
        return "\n".join(lines)

    def _load_video_source(self, entry, source_path, using_proxy=False):
        if self._video_load_state == "stopping":
            LOGGER.info("Video load queued — state=stopping, updating pending + restarting poll")
            self._video_pending_source = source_path
            self._video_pending_entry = entry
            self._start_load_poll()
            return
        self._video_load_state = "stopping"
        self._video_pending_source = source_path
        self._video_pending_entry = entry
        LOGGER.info("Video load stopping: from=%s to=%s",
                     self._video_widget.current_media_path() or "(none)", entry.video_path)
        self._video_widget.stop()
        self._video_stop_started_at = time.monotonic()
        self._start_load_poll()

    def _start_load_poll(self):
        if self._video_load_timer is None:
            self._video_load_timer = QTimer(self)
            self._video_load_timer.setInterval(80)
        try:
            self._video_load_timer.timeout.disconnect()
        except (TypeError, RuntimeError):
            pass
        current_switch = self._video_switch_id
        self._video_load_timer.timeout.connect(lambda sid=current_switch: self._poll_for_load_ready(sid))
        self._video_load_timer.start()

    def _poll_for_load_ready(self, switch_id):
        if self._video_load_state != "stopping":
            self._video_load_timer.stop()
            return
        if switch_id != self._video_switch_id:
            LOGGER.info("Stale poll ignored: old_switch=%s current=%s", switch_id, self._video_switch_id)
            self._video_load_timer.stop()
            return  # DO NOT set idle – only valid switch_id may change state
        status = self._video_widget.current_media_status()
        elapsed = time.monotonic() - self._video_stop_started_at
        LOGGER.info("Poll: status=%s elapsed=%.1fs state=%s", status, elapsed, self._video_load_state)
        if status in (QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.LoadedMedia):
            self._video_load_timer.stop()
            self._do_load_pending()
        elif elapsed > 3.0:
            LOGGER.warning("Stop timeout after %.1fs, forcing load", elapsed)
            self._video_load_timer.stop()
            self._do_load_pending()

    def _do_load_pending(self):
        entry = self._video_pending_entry
        source_path = self._video_pending_source
        if entry is None or source_path is None:
            self._video_load_state = "idle"
            return
        self._video_pending_source = None
        self._video_pending_entry = None
        self._video_load_state = "loading"
        restore_position = self.pending_restore_video_position
        self.pending_restore_video_position = 0
        LOGGER.info("Video loading: original=%s source=%s", entry.video_path, source_path)
        self._video_widget.prepare_restore(restore_position)
        self._video_widget.load(str(source_path), str(entry.video_path))
        self._video_load_state = "playing"
        info = self._cached_probe_info(entry.video_path)
        self._lbl_info.setText(self._video_playback_text(entry, info, False))

    def _cancel_video_probe(self):
        if self._video_probe_process is not None:
            self._video_probe_process.kill()
            self._video_probe_process.deleteLater()
        self._video_probe_process = None
        self._video_probe_output_buffer = ""
        self._video_probe_target = None
        self._cleanup_worker_job_file(self._video_probe_job_file)
        self._video_probe_job_file = None

    def _cancel_video_proxy(self):
        if self._video_proxy_process is not None:
            self._video_proxy_process.kill()
            self._video_proxy_process.deleteLater()
        self._video_proxy_process = None
        self._video_proxy_output_buffer = ""
        self._video_proxy_target = None
        self._video_proxy_output_path = None
        self._cleanup_worker_job_file(self._video_proxy_job_file)
        self._video_proxy_job_file = None
        self._clear_video_hint()

    def _start_video_probe(self, source_path: Path):
        if not self._video_tools.get("ffprobe"):
            LOGGER.warning("ffprobe not found; probe skipped for %s", source_path)
            return
        self._cancel_video_probe()
        self._video_probe_target = str(source_path.resolve())
        self._video_probe_switch_id = self._video_switch_id
        payload = {
            "source_path": str(source_path),
            "ffprobe_exe": self._video_tools["ffprobe"],
            "settings_dir": str(_settings_dir()),
            "protocol_version": VIDEO_PROXY_PROTOCOL_VERSION,
        }
        self._video_probe_job_file = self._write_video_worker_job("video_probe_jobs", payload)
        program, args = self._delete_worker_program_and_args("--video-probe-worker", self._video_probe_job_file)
        process = QProcess(self)
        process.setProgram(program)
        process.setArguments(args)
        process.readyReadStandardOutput.connect(self._on_video_probe_stdout)
        process.readyReadStandardError.connect(self._on_video_probe_stderr)
        process.finished.connect(self._on_video_probe_finished)
        process.errorOccurred.connect(self._on_video_probe_error)
        self._video_probe_process = process
        process.start()

    def _start_video_proxy(self, source_path: Path, info: VideoProbeInfo):
        if not self._video_tools.get("ffmpeg"):
            LOGGER.warning("ffmpeg not found; proxy skipped for %s", source_path)
            return
        self._cancel_video_proxy()
        output_path = self._proxy_path_for(source_path)
        self._video_proxy_target = str(source_path.resolve())
        self._video_proxy_switch_id = self._video_switch_id
        self._video_proxy_output_path = str(output_path)
        payload = {
            "source_path": str(source_path),
            "output_path": str(output_path),
            "ffmpeg_exe": self._video_tools["ffmpeg"],
            "duration_ms": info.duration_ms,
            "probe_info": asdict(info),
            "protocol_version": VIDEO_PROXY_PROTOCOL_VERSION,
        }
        self._video_proxy_job_file = self._write_video_worker_job("video_proxy_jobs", payload)
        program, args = self._delete_worker_program_and_args("--video-proxy-worker", self._video_proxy_job_file)
        process = QProcess(self)
        process.setProgram(program)
        process.setArguments(args)
        process.readyReadStandardOutput.connect(self._on_video_proxy_stdout)
        process.readyReadStandardError.connect(self._on_video_proxy_stderr)
        process.finished.connect(self._on_video_proxy_finished)
        process.errorOccurred.connect(self._on_video_proxy_error)
        self._video_proxy_process = process
        self._set_video_hint("正在生成流畅预览：0%")
        process.start()

    def _on_video_probe_stdout(self):
        if self._video_probe_process is None:
            return
        chunk = bytes(self._video_probe_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._video_probe_output_buffer += chunk
        while "\n" in self._video_probe_output_buffer:
            line, self._video_probe_output_buffer = self._video_probe_output_buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                LOGGER.warning("Invalid probe worker output line=%s", line)
                continue
            if payload.get("event") == "finished":
                info = VideoProbeInfo(**payload["info"])
                source_path = Path(info.path)
                self._set_probe_info(source_path, info)
                LOGGER.info(
                    "Video probe finished path=%s info=%s auto_proxy=%s smooth_proxy=%s",
                    source_path,
                    payload["info"],
                    info.is_heavy_for_auto(),
                    info.is_heavy_for_proxy_mode(),
                )
                if self._mode == "video" and self.current_index is not None and self.entries:
                    current = self.entries[self.current_index]
                    if hasattr(current, "video_path") and current.video_path.resolve() == source_path.resolve():
                        self._lbl_info.setText(self._video_playback_text(current, info, self._video_widget.current_source_path() != str(current.video_path)))
                        if self._should_use_proxy_for_current_mode(info):
                            proxy_path = self._proxy_path_for(source_path)
                            if proxy_path.exists():
                                LOGGER.info("Probe found proxy, switch_id=%s current=%s", self._video_probe_switch_id, self._video_switch_id)
                                if self._video_probe_switch_id == self._video_switch_id:
                                    self._load_video_source(current, proxy_path, True)
                                else:
                                    LOGGER.info("Stale probe result ignored: old_switch=%s current=%s", self._video_probe_switch_id, self._video_switch_id)
                            else:
                                self._start_video_proxy(source_path, info)
            elif payload.get("event") == "fatal":
                LOGGER.error("Video probe worker failed payload=%s", payload)

    def _on_video_probe_stderr(self):
        if self._video_probe_process is None:
            return
        text = bytes(self._video_probe_process.readAllStandardError()).decode("utf-8", errors="replace")
        if text.strip():
            LOGGER.error("Video probe stderr=%s", text.strip())

    def _on_video_probe_finished(self, *_args):
        self._cleanup_worker_job_file(self._video_probe_job_file)
        self._video_probe_job_file = None
        if self._video_probe_process is not None:
            self._video_probe_process.deleteLater()
        self._video_probe_process = None
        self._video_probe_output_buffer = ""

    def _on_video_probe_error(self, process_error):
        LOGGER.error("Video probe errorOccurred=%s", process_error)

    def _on_video_proxy_stdout(self):
        if self._video_proxy_process is None:
            return
        chunk = bytes(self._video_proxy_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._video_proxy_output_buffer += chunk
        while "\n" in self._video_proxy_output_buffer:
            line, self._video_proxy_output_buffer = self._video_proxy_output_buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                LOGGER.warning("Invalid proxy worker output line=%s", line)
                continue
            event_type = payload.get("event")
            if event_type == "progress":
                self._set_video_hint(f"正在生成流畅预览：{int(payload.get('percent', 0) or 0)}%")
            elif event_type == "finished":
                LOGGER.info("Video proxy generated payload=%s", payload)
                self._set_video_hint("流畅预览已生成")
                if self._mode == "video" and self.current_index is not None and self.entries:
                    current = self.entries[self.current_index]
                    current_path = str(current.video_path.resolve()) if hasattr(current, "video_path") else None
                    if current_path == payload.get("source_path") and self._video_quality_mode != "original":
                        LOGGER.info("Proxy finished, switch_id=%s current=%s", self._video_proxy_switch_id, self._video_switch_id)
                        if self._video_proxy_switch_id == self._video_switch_id:
                            current_position = self._video_widget.current_position()
                            self._video_widget.prepare_restore(current_position)
                            self._load_video_source(current, Path(payload["output_path"]), True)
                        else:
                            LOGGER.info("Stale proxy result ignored: old_switch=%s current=%s", self._video_proxy_switch_id, self._video_switch_id)
            elif event_type == "fatal":
                LOGGER.error("Video proxy worker failed payload=%s", payload)
                self._set_video_hint("流畅预览生成失败，已回退原片播放。")

    def _on_video_proxy_stderr(self):
        if self._video_proxy_process is None:
            return
        text = bytes(self._video_proxy_process.readAllStandardError()).decode("utf-8", errors="replace")
        if text.strip():
            LOGGER.error("Video proxy stderr=%s", text.strip())

    def _on_video_proxy_finished(self, *_args):
        self._cleanup_worker_job_file(self._video_proxy_job_file)
        self._video_proxy_job_file = None
        if self._video_proxy_process is not None:
            self._video_proxy_process.deleteLater()
        self._video_proxy_process = None
        self._video_proxy_output_buffer = ""

    def _on_video_proxy_error(self, process_error):
        LOGGER.error("Video proxy errorOccurred=%s", process_error)

    def _show_video_entry(self, entry: VideoEntry):
        self._video_switch_id += 1
        switch_id = self._video_switch_id
        self._video_pending_target = entry
        if self._video_switch_timer is None:
            self._video_switch_timer = QTimer(self)
            self._video_switch_timer.setSingleShot(True)
        try:
            self._video_switch_timer.timeout.disconnect()
        except (TypeError, RuntimeError):
            pass
        self._video_switch_timer.timeout.connect(lambda sid=switch_id: self._video_do_switch(sid))
        self._video_switch_timer.start(VIDEO_SWITCH_DEBOUNCE_MS)
        source_path = entry.video_path
        info = self._cached_probe_info(source_path)
        proxy_path = self._proxy_path_for(source_path)
        use_proxy = (self._video_quality_mode != "original" and proxy_path.exists()
                     and self._should_use_proxy_for_current_mode(info))
        self._lbl_info.setText(self._video_playback_text(entry, info, use_proxy))

    def _video_do_switch(self, switch_id: int):
        if switch_id != self._video_switch_id:
            return
        entry = self._video_pending_target
        if entry is None or not entry.video_path.exists():
            return
        self._video_pending_target = None
        source_path = entry.video_path
        info = self._cached_probe_info(source_path)
        proxy_path = self._proxy_path_for(source_path)
        use_proxy = (self._video_quality_mode != "original" and proxy_path.exists()
                     and self._should_use_proxy_for_current_mode(info))
        self._load_video_source(entry, proxy_path if use_proxy else source_path, use_proxy)
        if info is None:
            self._start_video_probe(source_path)
        elif self._should_use_proxy_for_current_mode(info) and not use_proxy and not proxy_path.exists():
            self._start_video_proxy(source_path, info)
        elif not self._should_use_proxy_for_current_mode(info):
            self._cancel_video_proxy()

    def _collect_directory_state(self, mode: str) -> dict:
        current_file = None
        if self.current_index is not None and self.entries:
            current_file = str(self.entries[self.current_index].relative_path)
        state = {
            "current_file": current_file,
            "current_index": self.current_index,
            "scroll_position": self._file_list.verticalScrollBar().value(),
            "file_states": {str(e.relative_path): e.status for e in self.entries if e.status != "pending"},
        }
        if mode == "video":
            state["playback_position"] = self._video_widget.current_position()
        return state

    def _prepare_restore_for_folder(self, mode: str, folder: Path):
        store = self._mode_state_store(mode)
        directory_state = store.get("directories", {}).get(self._directory_key(folder), {})
        self.persisted_statuses = dict(directory_state.get("file_states", {}) or {})
        self.pending_restore_photo = directory_state.get("current_file") if mode == "photo" else None
        self.pending_restore_video = directory_state.get("current_file") if mode == "video" else None
        self.pending_restore_index = directory_state.get("current_index")
        self.pending_restore_scroll = directory_state.get("scroll_position")
        self.pending_restore_video_position = int(directory_state.get("playback_position", 0) or 0)

    def _clear_pending_restore(self):
        self.pending_restore_photo = None
        self.pending_restore_video = None
        self.pending_restore_index = None
        self.pending_restore_scroll = None
        self.pending_restore_video_position = 0

    def _resolve_restore_index(self) -> int | None:
        if not self.entries:
            return None
        target = self.pending_restore_photo or self.pending_restore_video
        if target:
            for i, entry in enumerate(self.entries):
                if str(entry.relative_path) == target:
                    return i
        anchor = self.pending_restore_index if isinstance(self.pending_restore_index, int) else 0
        anchor = max(0, min(anchor, len(self.entries) - 1))
        for i in range(anchor, len(self.entries)):
            if self.entries[i].status == "pending":
                return i
        for i in range(anchor - 1, -1, -1):
            if self.entries[i].status == "pending":
                return i
        return anchor if self.entries else None

    def _restore_selection_after_scan(self):
        if not self.entries:
            self.current_index = None
            self._clear_pending_restore()
            return
        restore_target = self.pending_restore_photo or self.pending_restore_video
        target_index = self._resolve_restore_index()
        if target_index is None:
            target_index = 0
        if self._mode == "video":
            matched_target = (
                restore_target is not None
                and 0 <= target_index < len(self.entries)
                and str(self.entries[target_index].relative_path) == restore_target
            )
            if not matched_target:
                self.pending_restore_video_position = 0
        self._set_selection(target_index)
        if self.pending_restore_scroll is not None:
            self._file_list.verticalScrollBar().setValue(max(0, int(self.pending_restore_scroll)))
        self._clear_pending_restore()

    # ── UI 构建 ──────────────────────────────────────

    def _build_ui(self):
        central = QWidget(); self.setCentralWidget(central)
        root_layout = QVBoxLayout(central); root_layout.setContentsMargins(12, 12, 12, 12)

        # 顶部：模式切换 + 打开文件夹
        toolbar_w = QWidget(); toolbar = QHBoxLayout(toolbar_w)
        toolbar.setContentsMargins(0, 0, 0, 4)
        self._btn_photo_mode = QPushButton("📷 照片")
        self._btn_photo_mode.setCheckable(True); self._btn_photo_mode.setChecked(True)
        self._btn_photo_mode.clicked.connect(lambda: self._switch_mode("photo"))
        toolbar.addWidget(self._btn_photo_mode)
        self._btn_video_mode = QPushButton("🎬 视频")
        self._btn_video_mode.setCheckable(True)
        self._btn_video_mode.clicked.connect(lambda: self._switch_mode("video"))
        toolbar.addWidget(self._btn_video_mode)
        self._btn_open = QPushButton("打开文件夹")
        self._btn_open.clicked.connect(self.choose_folder)
        toolbar.addWidget(self._btn_open)
        self._lbl_folder = QLabel("尚未选择文件夹")
        toolbar.addWidget(self._lbl_folder, 1)
        root_layout.addWidget(toolbar_w)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧：文件列表
        left_panel = QWidget(); left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 0, 4, 0)
        self._lbl_list_title = QLabel("照片列表")
        left_layout.addWidget(self._lbl_list_title)
        self._file_list = QListWidget()
        self._file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._file_list.currentRowChanged.connect(self._on_list_selection)
        self._file_list.verticalScrollBar().valueChanged.connect(lambda _v: self._schedule_state_save())
        left_layout.addWidget(self._file_list, 1)
        self._lbl_summary = QLabel("共 0 项")
        left_layout.addWidget(self._lbl_summary)
        self._splitter.addWidget(left_panel)

        # 右侧：预览 + 信息 + 按钮
        right_panel = QWidget(); right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 0, 0, 0)

        self._lbl_title = QLabel("尚未选择")
        self._lbl_title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        right_layout.addWidget(self._lbl_title)

        quality_row = QWidget(); quality_layout = QHBoxLayout(quality_row); quality_layout.setContentsMargins(0, 0, 0, 4)
        quality_layout.addWidget(QLabel("播放质量"))
        self._cmb_video_quality = QComboBox()
        self._cmb_video_quality.addItems([VIDEO_QUALITY_LABELS[VIDEO_QUALITY_AUTO], VIDEO_QUALITY_LABELS["original"], VIDEO_QUALITY_LABELS["proxy"]])
        self._cmb_video_quality.currentTextChanged.connect(self._on_video_quality_text_changed)
        quality_layout.addWidget(self._cmb_video_quality)
        quality_layout.addStretch(1)
        right_layout.addWidget(quality_row)

        # 预览区（照片/视频共用此 frame）
        preview_frame = QFrame(); preview_frame.setFrameStyle(QFrame.Shape.StyledPanel)
        pf_layout = QVBoxLayout(preview_frame); pf_layout.setContentsMargins(8, 8, 8, 8)

        self._image_view = ImageGraphicsView()
        pf_layout.addWidget(self._image_view)
        self._video_widget = QtVideoPlayerWidget()
        self._apply_video_preferences()
        self._video_widget.on_state_changed = lambda high_frequency=False: self._schedule_state_save(1500 if high_frequency else 500)
        self._video_widget.setVisible(False)
        pf_layout.addWidget(self._video_widget)
        right_layout.addWidget(preview_frame, 1)

        self._lbl_info = QLabel("请先打开文件夹。")
        self._lbl_info.setWordWrap(True)
        right_layout.addWidget(self._lbl_info)

        # 操作按钮行 1
        aw1 = QWidget(); a1 = QHBoxLayout(aw1); a1.setContentsMargins(0, 8, 0, 0)
        self._btn_keep = QPushButton("保留（Enter）"); self._btn_keep.clicked.connect(self.keep_current); a1.addWidget(self._btn_keep)
        self._btn_delete = QPushButton("移到回收站（Del）"); self._btn_delete.clicked.connect(self.delete_current); a1.addWidget(self._btn_delete)
        self._btn_skip = QPushButton("跳过（S）"); self._btn_skip.clicked.connect(self.skip_current); a1.addWidget(self._btn_skip)
        self._btn_restore = QPushButton("恢复（Z）"); self._btn_restore.clicked.connect(self.restore_current); a1.addWidget(self._btn_restore)
        self._btn_commit = QPushButton("删除已标记"); self._btn_commit.clicked.connect(self.commit_marked_deletions); a1.addWidget(self._btn_commit)
        self._btn_cancel_delete = QPushButton("取消删除"); self._btn_cancel_delete.clicked.connect(self.cancel_video_delete); self._btn_cancel_delete.setEnabled(False); a1.addWidget(self._btn_cancel_delete)
        right_layout.addWidget(aw1)

        # 批量行
        aw2 = QWidget(); a2 = QHBoxLayout(aw2); a2.setContentsMargins(0, 4, 0, 0)
        self._lbl_batch = QLabel(""); a2.addWidget(self._lbl_batch, 1)
        self._btn_batch_keep = QPushButton("批量保留"); self._btn_batch_keep.clicked.connect(self.batch_keep); a2.addWidget(self._btn_batch_keep)
        self._btn_batch_delete = QPushButton("批量标记删除"); self._btn_batch_delete.clicked.connect(self.batch_delete); a2.addWidget(self._btn_batch_delete)
        self._btn_batch_skip = QPushButton("批量跳过"); self._btn_batch_skip.clicked.connect(self.batch_skip); a2.addWidget(self._btn_batch_skip)
        self._btn_batch_restore = QPushButton("批量恢复"); self._btn_batch_restore.clicked.connect(self.batch_restore); a2.addWidget(self._btn_batch_restore)
        right_layout.addWidget(aw2)

        self._lbl_hint = QLabel("Delete 只做删除标记。Shift/Ctrl 可多选。")
        right_layout.addWidget(self._lbl_hint)

        self._splitter.addWidget(right_panel)
        self._splitter.setStretchFactor(0, 1); self._splitter.setStretchFactor(1, 3)
        root_layout.addWidget(self._splitter, 1)

    def _build_menus(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("文件")
        file_menu.addAction("打开文件夹...", self.choose_folder)
        self._recent_menu = file_menu.addMenu("最近打开的目录")
        file_menu.addAction("预加载设置...", self.open_preload_settings)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close)
        self._refresh_recent_menu()

    def _setup_shortcuts(self):
        QAction(self).setShortcut(QKeySequence(Qt.Key.Key_Delete))
        self._shortcut_map = {
            Qt.Key.Key_Delete: self.delete_current,
            Qt.Key.Key_Return: self.keep_current,
            Qt.Key.Key_Enter: self.keep_current,
            Qt.Key.Key_S: self.skip_current,
            Qt.Key.Key_Z: self.restore_current,
        }

    def keyPressEvent(self, event):
        if self._delete_in_progress:
            event.ignore()
            return
        key = event.key(); mod = event.modifiers()
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            super().keyPressEvent(event); return
        if mod & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_Z:
            self._undo_last_action(); return
        if mod & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_A:
            self._file_list.selectAll(); self._update_batch_label(); return
        if key in self._shortcut_map:
            self._shortcut_map[key](); return
        if key == Qt.Key.Key_Up: self._move_selection(-1); return
        if key == Qt.Key.Key_Down: self._move_selection(1); return
        super().keyPressEvent(event)

    # ── 模式切换 ──────────────────────────────────────

    def _switch_mode(self, mode: str):
        if self._delete_in_progress:
            return
        if mode == self._mode and self.entries:
            return
        self._save_state()
        if self._mode == "video" and hasattr(self, '_video_widget'):
            self._cancel_video_probe()
            self._cancel_video_proxy()
            self._video_widget.stop()
        if self._mode == "photo":
            self._photo_folder = self.current_folder
            self._photo_entries = self.entries
            self._photo_index = self.current_index
            self._photo_undo_stack = self._undo_stack
        else:
            self._video_folder = self.current_folder
            self._video_entries = self.entries
            self._video_index = self.current_index
            self._video_undo_stack = self._undo_stack
        self._mode = mode
        if mode == "photo":
            self.current_folder = self._photo_folder
            self.entries = self._photo_entries
            self.current_index = self._photo_index
            self._undo_stack = self._photo_undo_stack
            self._btn_photo_mode.setChecked(True)
            self._btn_video_mode.setChecked(False)
            self._lbl_list_title.setText("照片列表")
            self._image_view.setVisible(True)
            self._video_widget.setVisible(False)
        else:
            self.current_folder = self._video_folder
            self.entries = self._video_entries
            self.current_index = self._video_index
            self._undo_stack = self._video_undo_stack
            self._btn_photo_mode.setChecked(False)
            self._btn_video_mode.setChecked(True)
            self._lbl_list_title.setText("视频列表")
            self._image_view.setVisible(False)
            self._video_widget.setVisible(True)
            self._apply_video_preferences()
            if not self._video_entries:
                self._restore_video_session()
                return
        self._lbl_folder.setText(str(self.current_folder) if self.current_folder else "尚未选择文件夹")
        self._file_list.blockSignals(True)
        self._file_list.clear()
        for entry in self.entries:
            self._file_list.addItem(entry.display_name)
        self._file_list.blockSignals(False)
        self._update_list_styles()
        if self.current_index is not None and self.entries:
            self._set_selection(self.current_index)
        else:
            self._show_current()
        self._update_summary()
        self._update_controls()
        save_settings(self.preview_cache_size, self.preview_lookahead, self._mode)

    # ── 文件夹操作 ─────────────────────────────────────

    def choose_folder(self):
        if self._delete_in_progress:
            return
        base_folder = self.current_folder or (self._video_folder if self._mode == "video" else self._photo_folder)
        start_dir = str(base_folder) if base_folder else str(Path.home())
        if not Path(start_dir).exists(): start_dir = str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹", start_dir)
        if not folder: return
        folder_path = Path(folder)
        self.current_folder = folder_path
        if self._mode == "video":
            self._video_folder = folder_path
        else:
            self._photo_folder = folder_path
        self._lbl_folder.setText(str(folder_path))
        self.entries.clear(); self.current_index = None
        self._file_list.clear()
        self._cancel_video_probe()
        self._cancel_video_proxy()
        self._image_view.clear_image(); self._video_widget.stop()
        self._lbl_summary.setText("正在扫描...")
        self._prepare_restore_for_folder(self._mode, folder_path)
        self._scan_folder(folder_path)
        self._add_recent_folder(str(folder_path))
        self._save_state()

    def _scan_folder(self, folder: Path):
        self.scan_request_id += 1; self.is_scanning = True
        self.scan_requests.put((self.scan_request_id, folder, self._mode))

    def _scan_folder_batches(self, folder: Path, mode: str):
        if mode == "video":
            files = sorted([f for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS])
        else:
            files = sorted([f for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in JPG_EXTENSIONS])
        batch_size = 50
        for i in range(0, len(files), batch_size):
            batch = []
            for f in files[i:i + batch_size]:
                if mode == "video":
                    batch.append(VideoEntry(video_path=f, relative_path=f.relative_to(folder)))
                else:
                    raw_paths = []
                    for raw_ext in RAW_EXTENSIONS:
                        for candidate in (f.with_suffix(raw_ext), f.with_suffix(raw_ext.upper())):
                            if candidate.exists() and candidate not in raw_paths:
                                raw_paths.append(candidate)
                    batch.append(PhotoEntry(jpg_path=f, relative_path=f.relative_to(folder), raw_paths=raw_paths))
            yield batch

    # ── 列表操作 ──────────────────────────────────────

    def _on_list_selection(self, index: int):
        if index < 0 or index >= len(self.entries): return
        self.current_index = index; self._show_current()
        self._update_batch_label(); self._update_controls(); self._save_state()

    def _move_selection(self, delta: int):
        if not self.entries: return
        target = max(0, min(len(self.entries) - 1, (self.current_index or 0) + delta))
        self._set_selection(target)

    def _set_selection(self, index: int):
        self._file_list.blockSignals(True)
        self._file_list.clearSelection()
        self._file_list.setCurrentRow(index)
        item = self._file_list.item(index)
        if item: self._file_list.scrollToItem(item)
        self._file_list.blockSignals(False)
        self.current_index = index; self._show_current()
        self._update_batch_label(); self._update_controls(); self._save_state()

    def _refresh_list(self):
        self._file_list.blockSignals(True)
        self._file_list.clear()
        for entry in self.entries: self._file_list.addItem(entry.display_name)
        self._update_list_styles()
        self._file_list.blockSignals(False)

    def _update_list_styles(self):
        for i, entry in enumerate(self.entries):
            item = self._file_list.item(i)
            if item is None: continue
            clr = {"deleted": Qt.GlobalColor.gray, "kept": Qt.GlobalColor.darkGreen,
                   "skipped": Qt.GlobalColor.darkYellow}.get(entry.status, Qt.GlobalColor.black)
            item.setForeground(clr)

    def _update_list_row(self, index: int):
        if 0 <= index < len(self.entries):
            item = self._file_list.item(index)
            if item: item.setText(self.entries[index].display_name)
        self._update_list_styles()

    def _get_selected_indices(self) -> list[int]:
        return [i.row() for i in self._file_list.selectedIndexes()]

    # ── 预览 ──────────────────────────────────────────

    def _show_current(self):
        if self.current_index is None or not self.entries:
            self._set_status("尚未选择")
            return
        entry = self.entries[self.current_index]
        self._lbl_title.setText(str(entry.relative_path))

        if self._mode == "video":
            self._show_video_entry(entry)
            self.pending_restore_video_position = 0
            return

        lines = [f"路径：{entry.jpg_path}", f"状态：{entry.status_text()}",
                 f"匹配到的原始文件：{len(entry.raw_paths)} 个"]
        if entry.status == "deleted":
            lines.append("提示：这张照片已标记删除，点击「删除已标记」后会移到回收站。")
        if entry.raw_paths: lines.extend(f"  - {r.name}" for r in entry.raw_paths)
        self._lbl_info.setText("\n".join(lines))

        cached = self._get_cached_preview(entry.jpg_path)
        if cached is not None:
            self._image_view.set_image(cached, str(entry.jpg_path))
            self._queue_preview_prefetch(); return
        self._image_view.clear_image(); self._lbl_info.setText("正在加载预览...")
        self.preview_request_id += 1
        self._enqueue_preview_request(entry.jpg_path, priority=0, request_id=self.preview_request_id, force=True)
        self._queue_preview_prefetch()

    def _set_status(self, message: str):
        self._lbl_title.setText("尚未选择"); self._lbl_info.setText(message)
        self._image_view.clear_image()

    # ── 标记操作 ──────────────────────────────────────

    def keep_current(self):
        if self._delete_in_progress:
            return
        if self.current_index is None: return
        selected = self._get_selected_indices()
        if len(selected) > 1:
            self._mark_entries(selected, "kept"); self._advance_after_batch(selected); return
        self._push_undo(self.current_index, self.entries[self.current_index].status)
        self.entries[self.current_index].status = "kept"
        self._update_list_row(self.current_index); self._update_summary()
        self._set_selection(self.current_index); self._advance_to_next(); self._save_state()

    def delete_current(self):
        if self._delete_in_progress:
            return
        if self.current_index is None: return
        selected = self._get_selected_indices()
        if len(selected) > 1:
            applicable = [i for i in selected if self.entries[i].status != "deleted"]
            if applicable: self._mark_entries(applicable, "deleted"); self._advance_after_batch(applicable)
            return
        entry = self.entries[self.current_index]
        if entry.status == "deleted": self._advance_to_next(); return
        self._push_undo(self.current_index, "pending")
        entry.status = "deleted"
        idx = self.current_index
        self._update_list_row(idx); self._update_summary()
        self._set_selection(idx); self._advance_to_next(); self._save_state()

    def skip_current(self):
        if self._delete_in_progress:
            return
        if self.current_index is None: return
        selected = self._get_selected_indices()
        if len(selected) > 1:
            self._mark_entries(selected, "skipped"); self._advance_after_batch(selected); return
        self._push_undo(self.current_index, self.entries[self.current_index].status)
        self.entries[self.current_index].status = "skipped"
        self._update_list_row(self.current_index); self._update_summary()
        self._set_selection(self.current_index); self._advance_to_next(); self._save_state()

    def restore_current(self):
        if self._delete_in_progress:
            return
        if self.current_index is None: return
        selected = self._get_selected_indices()
        if len(selected) > 1: self.batch_restore(); return
        entry = self.entries[self.current_index]
        if entry.status != "deleted": return
        entry.status = "pending"
        self._update_list_row(self.current_index); self._set_selection(self.current_index)
        self._update_controls(); self._update_summary(); self._save_state()

    def _advance_to_next(self):
        if not self.entries or self.current_index is None: return
        for i in range(self.current_index + 1, len(self.entries)):
            if self.entries[i].status != "deleted": self._set_selection(i); return
        for i in range(self.current_index):
            if self.entries[i].status != "deleted": self._set_selection(i); return
        self._set_selection(self.current_index)

    # ── 批量操作 ──────────────────────────────────────

    def _mark_entries(self, indices: list[int], status: str):
        for i in indices:
            if 0 <= i < len(self.entries):
                self.entries[i].status = status; self._update_list_row(i)
        self._update_summary(); self._update_controls(); self._save_state()

    def _advance_after_batch(self, changed: list[int]):
        if not self.entries: self.current_index = None; self._set_status("列表已空。"); self._update_controls(); return
        max_changed = max(changed) if changed else (self.current_index or 0)
        for i in range(max_changed + 1, len(self.entries)):
            if self.entries[i].status != "deleted": self._set_selection(i); return
        for i in range(len(self.entries)):
            if self.entries[i].status != "deleted": self._set_selection(i); return
        self._set_selection(min(max(self.current_index or 0, 0), len(self.entries) - 1))

    def batch_keep(self):
        if self._delete_in_progress:
            return
        indices = self._get_selected_indices()
        if indices: self._mark_entries(indices, "kept"); self._advance_after_batch(indices)

    def batch_delete(self):
        if self._delete_in_progress:
            return
        indices = self._get_selected_indices()
        applicable = [i for i in indices if 0 <= i < len(self.entries) and self.entries[i].status != "deleted"]
        if applicable: self._mark_entries(applicable, "deleted"); self._advance_after_batch(applicable)

    def batch_skip(self):
        if self._delete_in_progress:
            return
        indices = self._get_selected_indices()
        if indices: self._mark_entries(indices, "skipped"); self._advance_after_batch(indices)

    def batch_restore(self):
        if self._delete_in_progress:
            return
        indices = self._get_selected_indices()
        applicable = [i for i in indices if 0 <= i < len(self.entries) and self.entries[i].status == "deleted"]
        if applicable: self._mark_entries(applicable, "pending")
        if applicable: self._set_selection(max(applicable)); self._update_controls()

    def commit_marked_deletions(self):
        if self._delete_in_progress:
            return
        deleted = [e for e in self.entries if e.status == "deleted"]
        if not deleted:
            QMessageBox.information(self, "提示", "当前没有已标记删除的项目。"); return
        cnt = len(deleted)
        msg = f"确定要删除已经标记的 {cnt} 个项目吗？\n\n这些文件将被移动到 Windows 系统回收站。"
        if QMessageBox.question(self, "确认删除", msg) != QMessageBox.StandardButton.Yes:
            return
        if self._mode == "video":
            self._start_video_delete(deleted)
            return
        selected_path = None; fallback_path = None
        if self.current_index is not None and self.entries:
            selected_path = self.entries[self.current_index].relative_path if hasattr(self.entries[self.current_index], 'relative_path') else None
            for i in range(self.current_index + 1, len(self.entries)):
                if self.entries[i].status != "deleted": fallback_path = i; break
            if fallback_path is None:
                for i in range(self.current_index - 1, -1, -1):
                    if self.entries[i].status != "deleted": fallback_path = i; break
        try:
            targets = []
            for e in deleted:
                if hasattr(e, 'jpg_path'): targets.extend([e.jpg_path, *e.raw_paths])
                elif hasattr(e, 'video_path'): targets.append(e.video_path)
            move_to_recycle_bin(targets)
        except Exception as ex:
            QMessageBox.critical(self, "删除失败", str(ex)); return

        self.entries = [e for e in self.entries if e.status != "deleted"]
        if not self.entries:
            self.current_index = None; self._refresh_list()
            self._set_status("列表已清空。")
        else:
            self._refresh_list()
            target = 0
            if fallback_path is not None:
                target = min(fallback_path, len(self.entries) - 1)
            self._set_selection(target)
        self._update_summary(); self._update_controls(); self._save_state()

    def _start_video_delete(self, deleted: list):
        targets = [e.video_path for e in deleted if hasattr(e, "video_path")]
        self._record_delete_restore_context()
        self._delete_result = None
        self._delete_cancel_requested = False
        self._log_delete(
            logging.INFO,
            "Video delete requested: total=%s current_folder=%s gui_thread=%s current_video=%s player=%s api=%s",
            len(targets),
            self.current_folder,
            self._gui_thread_ident,
            self._video_widget.current_media_path(),
            self._video_widget.state_snapshot(),
            RECYCLE_BIN_API,
        )
        self._set_delete_ui_busy(True)
        self._log_delete(logging.INFO, "Delete confirm clicked: gui_thread=%s total=%s", self._gui_thread_ident, len(targets))
        self._release_video_for_delete()
        QTimer.singleShot(200, lambda: self._launch_video_delete_process(targets))

    def _launch_video_delete_process(self, targets: list[Path]):
        if self._delete_process is not None:
            return
        self._delete_job_file = self._write_delete_job_file(targets)
        program, args = self._delete_process_program_and_args(self._delete_job_file)
        self._delete_process = QProcess(self)
        self._delete_process.setProgram(program)
        self._delete_process.setArguments(args)
        self._delete_process.setWorkingDirectory(str(Path.cwd()))
        self._delete_process.readyReadStandardOutput.connect(self._on_delete_process_stdout)
        self._delete_process.readyReadStandardError.connect(self._on_delete_process_stderr)
        self._delete_process.finished.connect(self._on_delete_process_finished)
        self._delete_process.errorOccurred.connect(self._on_delete_process_error)
        self._log_delete(
            logging.INFO,
            "Starting delete process: total=%s main_thread=%s program=%s args=%s job=%s",
            len(targets),
            self._gui_thread_ident,
            program,
            args,
            self._delete_job_file,
        )
        self._delete_process.start()

    def _consume_delete_process_event(self, payload: dict):
        event_type = payload.get("event")
        if event_type == "progress":
            index = int(payload.get("index", 0) or 0)
            total = int(payload.get("total", 0) or 0)
            path = payload.get("path")
            phase = payload.get("phase")
            result_label = payload.get("result")
            self._lbl_summary.setText(f"删除视频：{index} / {total}")
            self._log_delete(
                logging.INFO,
                "Delete process progress: phase=%s processed=%s total=%s result=%s path=%s",
                phase,
                index,
                total,
                result_label,
                path,
            )
        elif event_type == "started":
            self._log_delete(logging.INFO, "Delete process started event=%s", payload)
        elif event_type == "finished":
            self._delete_result = payload
        elif event_type == "fatal":
            self._delete_result = {
                "successful_paths": [],
                "missing_paths": [],
                "failed_items": [{
                    "path": payload.get("job_path", ""),
                    "exception_type": payload.get("exception_type", "RuntimeError"),
                    "error_message": payload.get("error_message", "删除进程初始化失败"),
                }],
                "processed_count": 0,
                "total_count": 0,
                "cancelled": False,
                "elapsed_time_ms": 0,
                "last_path": None,
            }

    def _on_delete_process_stdout(self):
        if self._delete_process is None:
            return
        chunk = bytes(self._delete_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._delete_output_buffer += chunk
        while "\n" in self._delete_output_buffer:
            line, self._delete_output_buffer = self._delete_output_buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                self._log_delete(logging.WARNING, "Invalid delete process stdout line=%s", line)
                continue
            self._consume_delete_process_event(payload)

    def _on_delete_process_stderr(self):
        if self._delete_process is None:
            return
        stderr_text = bytes(self._delete_process.readAllStandardError()).decode("utf-8", errors="replace")
        if stderr_text.strip():
            self._log_delete(logging.ERROR, "Delete process stderr=%s", stderr_text.strip())

    def _finalize_video_delete_result(self, result: dict, exit_status: str):
        success_set = set(result.get("successful_paths", []))
        missing_set = set(result.get("missing_paths", []))
        failed_map = {item["path"]: item for item in result.get("failed_items", [])}
        self._log_delete(
            logging.INFO,
            "Delete process returned: exit_status=%s result=%s",
            exit_status,
            result,
        )
        for path_text in success_set | missing_set:
            try:
                remove_proxy_artifacts(_settings_dir(), Path(path_text))
            except OSError:
                LOGGER.exception("Failed to clean proxy artifacts for path=%s", path_text)
        if success_set or missing_set:
            self.entries = [
                e for e in self.entries
                if str(e.video_path) not in success_set and str(e.video_path) not in missing_set
            ]
        for entry in self.entries:
            if str(entry.video_path) in failed_map:
                entry.status = "deleted"
        self.persisted_statuses = {
            str(e.relative_path): e.status
            for e in self.entries
            if e.status != "pending"
        }
        self._log_delete(logging.INFO, "Refreshing list after delete")
        self._refresh_list()
        self._restore_after_video_delete()
        self._update_summary()
        self._update_controls()
        self._log_delete(logging.INFO, "Saving state after delete")
        self._save_state()
        self._set_delete_ui_busy(False)
        self._cleanup_delete_process()
        if failed_map or missing_set:
            QMessageBox.information(
                self,
                "删除完成",
                "删除完成：\n\n"
                f"成功移入回收站：{len(success_set)}\n"
                f"删除失败：{len(failed_map)}\n"
                f"文件不存在：{len(missing_set)}\n\n"
                "失败详情已写入日志。"
            )
        else:
            QMessageBox.information(
                self,
                "删除完成",
                f"成功将 {len(success_set)} 个视频移动到 Windows 系统回收站。"
            )
        if self._close_after_delete:
            self._close_after_delete = False
            QTimer.singleShot(0, self.close)

    def _on_delete_process_finished(self, exit_code: int, exit_status):
        self._on_delete_process_stdout()
        result = self._delete_result or {
            "successful_paths": [],
            "missing_paths": [],
            "failed_items": [{
                "path": "",
                "exception_type": "ProcessExitError",
                "error_message": f"删除进程未返回结果，exit_code={exit_code}, exit_status={exit_status}",
            }],
            "processed_count": 0,
            "total_count": 0,
            "cancelled": self._delete_cancel_requested,
            "elapsed_time_ms": 0,
            "last_path": None,
        }
        self._finalize_video_delete_result(result, f"exit_code={exit_code}, exit_status={exit_status}")

    def _on_delete_process_error(self, process_error):
        self._log_delete(logging.ERROR, "Delete process errorOccurred=%s", process_error)

    def cancel_video_delete(self):
        if not self._delete_in_progress:
            return
        self._delete_cancel_requested = True
        self._lbl_hint.setText("正在取消删除任务，请稍候…")
        self._log_delete(logging.INFO, "User requested delete cancellation")
        if self._delete_process is not None:
            self._delete_process.terminate()
            QTimer.singleShot(1500, self._kill_delete_process_if_needed)

    def _kill_delete_process_if_needed(self):
        if self._delete_process is not None and self._delete_process.state() != QProcess.ProcessState.NotRunning:
            self._log_delete(logging.WARNING, "Delete process did not terminate in time; killing process")
            self._delete_process.kill()

    # ── UI 更新 ──────────────────────────────────────

    def _update_controls(self):
        if self._delete_in_progress:
            self._btn_keep.setEnabled(False); self._btn_delete.setEnabled(False); self._btn_skip.setEnabled(False)
            self._btn_restore.setEnabled(False); self._btn_commit.setEnabled(False)
            self._btn_batch_keep.setEnabled(False); self._btn_batch_delete.setEnabled(False)
            self._btn_batch_skip.setEnabled(False); self._btn_batch_restore.setEnabled(False)
            self._btn_cancel_delete.setEnabled(True)
            return
        has_current = self.current_index is not None and len(self.entries) > 0
        enabled = bool(has_current)
        self._btn_keep.setEnabled(enabled); self._btn_delete.setEnabled(enabled); self._btn_skip.setEnabled(enabled)
        is_deleted = has_current and self.entries[self.current_index].status == "deleted"
        self._btn_restore.setEnabled(is_deleted)
        has_any_deleted = any(e.status == "deleted" for e in self.entries)
        self._btn_commit.setEnabled(has_any_deleted)
        selected = self._get_selected_indices(); is_multi = len(selected) > 1
        self._btn_batch_keep.setEnabled(is_multi); self._btn_batch_delete.setEnabled(is_multi)
        self._btn_batch_skip.setEnabled(is_multi)
        if is_multi:
            has_del = any(self.entries[i].status == "deleted" for i in selected if 0 <= i < len(self.entries))
            self._btn_batch_restore.setEnabled(has_del)
        else:
            self._btn_batch_restore.setEnabled(False)
        self._btn_cancel_delete.setEnabled(False)
        self._update_batch_label()

    def _update_batch_label(self):
        selected = self._get_selected_indices()
        unit = "个视频" if self._mode == "video" else "张照片"
        self._lbl_batch.setText(f"已选中 {len(selected)} {unit}" if len(selected) > 1 else "")

    def _update_summary(self):
        total = len(self.entries)
        kept = sum(1 for e in self.entries if e.status == "kept")
        deleted = sum(1 for e in self.entries if e.status == "deleted")
        skipped = sum(1 for e in self.entries if e.status == "skipped")
        play_failed = sum(1 for e in self.entries if e.status == "play_failed")
        pending = total - kept - deleted - skipped - play_failed
        unit = "个视频" if self._mode == "video" else "项"
        extra = f"  |  播放失败 {play_failed}" if self._mode == "video" and play_failed else ""
        self._lbl_summary.setText(f"共 {total} {unit}  |  待处理 {pending}  |  保留 {kept}  |  标记删除 {deleted}  |  跳过 {skipped}{extra}")

    def _update_image_info(self): pass

    # ── 撤回 ──────────────────────────────────────────

    def _push_undo(self, index: int, previous_status: str):
        self._undo_stack.append({"index": index, "previous_status": previous_status})
        if len(self._undo_stack) > 50: self._undo_stack.pop(0)

    def _undo_last_action(self):
        if not self._undo_stack: return
        action = self._undo_stack.pop()
        idx = action["index"]
        if 0 <= idx < len(self.entries):
            self.entries[idx].status = action["previous_status"]
            self._update_list_row(idx); self._update_summary()
            self._set_selection(idx); self._update_controls(); self._save_state()

    def closeEvent(self, event):
        if self._delete_in_progress:
            answer = QMessageBox.question(
                self,
                "删除仍在进行",
                "批量删除任务仍在执行。\n\n是否请求取消，并在当前文件处理完成后关闭窗口？"
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._close_after_delete = True
                self.cancel_video_delete()
            event.ignore()
            return
        self._cancel_video_probe()
        self._cancel_video_proxy()
        self._video_widget.dispose() if hasattr(self, '_video_widget') else None
        save_settings(self.preview_cache_size, self.preview_lookahead, self._mode)
        self._save_state(); super().closeEvent(event)

    # ── 预加载设置 ────────────────────────────────────

    def open_preload_settings(self):
        dlg = QDialog(self); dlg.setWindowTitle("预加载设置")
        layout = QFormLayout(dlg)
        spin_cache = QSpinBox(); spin_cache.setRange(24, 2000); spin_cache.setValue(self.preview_cache_size)
        layout.addRow("预缓存数量", spin_cache)
        spin_look = QSpinBox(); spin_look.setRange(3, 200); spin_look.setValue(self.preview_lookahead)
        layout.addRow("向前预读数量", spin_look)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept); btns.rejected.connect(dlg.reject)
        layout.addRow(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.preview_cache_size = spin_cache.value(); self.preview_lookahead = spin_look.value()
            save_settings(self.preview_cache_size, self.preview_lookahead, self._mode)
            self._lbl_hint.setText(f"Delete 只做删除标记。预缓存 {self.preview_cache_size} 张，向前预读 {self.preview_lookahead} 张。")

    # ── 最近目录 ──────────────────────────────────────

    def _add_recent_folder(self, path_str: str):
        sessions = [s for s in self.recent_sessions if s.get("folder") != path_str]
        sessions.insert(0, {"folder": path_str})
        self.recent_sessions = sessions[:MAX_RECENT_SESSIONS]; self._refresh_recent_menu()

    def _refresh_recent_menu(self):
        self._recent_menu.clear()
        if not self.recent_sessions: self._recent_menu.addAction("(无)").setEnabled(False); return
        for s in self.recent_sessions:
            folder = s.get("folder")
            if folder: self._recent_menu.addAction(folder, lambda f=folder: self._open_recent_folder(f))

    def _open_recent_folder(self, path_str: str):
        if self._delete_in_progress:
            return
        p = Path(path_str)
        if p.is_dir():
            self.current_folder = p; self._lbl_folder.setText(path_str)
            if self._mode == "video":
                self._video_folder = p
            else:
                self._photo_folder = p
            self.entries.clear(); self.current_index = None; self._file_list.clear()
            self._cancel_video_probe()
            self._cancel_video_proxy()
            self._image_view.clear_image(); self._video_widget.stop()
            self._prepare_restore_for_folder(self._mode, p)
            self._lbl_summary.setText("正在扫描..."); self._scan_folder(p)

    # ── 状态持久化 ────────────────────────────────────

    def _save_state(self):
        if not self.current_folder or self.is_scanning:
            return
        mode = self._mode
        store = self._mode_state_store(mode)
        folder_key = self._directory_key(self.current_folder)
        if folder_key is None:
            return
        store["last_directory"] = folder_key
        store["directories"][folder_key] = self._collect_directory_state(mode)
        if mode == "photo":
            store["recent_sessions"] = self.recent_sessions
        else:
            store["preferences"] = self._get_video_preferences()
        save_ok = safe_write_json(self._state_file_for_mode(mode), store)
        if mode == "video":
            self._log_delete(
                logging.INFO,
                "State save after video operation: success=%s path=%s current_file=%s current_index=%s",
                save_ok,
                self._state_file_for_mode(mode),
                store["directories"][folder_key].get("current_file"),
                store["directories"][folder_key].get("current_index"),
            )

    def _save_video_state(self):
        if self._mode == "video":
            self._save_state()

    def _restore_last_session(self):
        self._photo_state_store = self._load_mode_state("photo")
        self._video_state_store = self._load_mode_state("video")
        self.recent_sessions = self._photo_state_store.get("recent_sessions", [])
        self._refresh_recent_menu()
        # 如果当前模式已有文件夹在扫描，不要覆盖
        if self.is_scanning or self.current_folder is not None:
            return
        folder = self._photo_state_store.get("last_directory")
        if folder and Path(folder).is_dir():
            self.current_folder = Path(folder)
            self._photo_folder = self.current_folder
            self._lbl_folder.setText(folder)
            self.entries.clear(); self._file_list.clear()
            self._lbl_summary.setText("正在扫描...")
            self._prepare_restore_for_folder("photo", self.current_folder)
            if self._mode != "photo":
                self._switch_mode("photo")
            self._scan_folder(self.current_folder)
            self._add_recent_folder(folder)
        last = load_json_file(SETTINGS_FILE, {}).get("last_mode", "photo")
        if last == "video" and not self.is_scanning:
            QTimer.singleShot(300, lambda: self._switch_mode("video"))

    def _restore_video_session(self):
        self._video_state_store = self._load_mode_state("video")
        self._apply_video_preferences()
        folder = self._video_state_store.get("last_directory")
        if not folder or not Path(folder).is_dir():
            return
        self.current_folder = Path(folder); self._video_folder = self.current_folder
        self._lbl_folder.setText(str(folder))
        self.entries.clear(); self._file_list.clear()
        self._lbl_summary.setText("正在扫描...")
        self._prepare_restore_for_folder("video", self.current_folder)
        self._scan_folder(self.current_folder)
        self._add_recent_folder(folder)

    # ── 后台线程 ──────────────────────────────────────

    def _scan_worker_loop(self):
        while True:
            req_id, folder, mode = self.scan_requests.get()
            for batch in self._scan_folder_batches(folder, mode):
                self.scan_results.put((req_id, folder, batch, False, mode))
            self.scan_results.put((req_id, folder, [], True, mode))

    def _preview_worker_loop(self):
        while True:
            _pri, _tid, req_id, path = self.preview_requests.get()
            with self.preview_queue_lock: self.preview_queued_paths.discard(path)
            cached = self._get_cached_preview(path)
            if cached is not None: self.preview_results.put((req_id, path, cached, None)); continue
            image = None; error = None
            try:
                with Image.open(path) as opened:
                    processed = ImageOps.exif_transpose(opened)
                    if max(processed.size) > MAX_PREVIEW_SOURCE_EDGE:
                        processed.thumbnail((MAX_PREVIEW_SOURCE_EDGE, MAX_PREVIEW_SOURCE_EDGE), Image.LANCZOS)
                    image = processed.copy()
            except Exception as e: error = str(e)
            self.preview_results.put((req_id, path, image, error))

    def _process_preview_results(self):
        while True:
            try: req_id, path, image, error = self.preview_results.get_nowait()
            except queue.Empty: break
            if image is not None: self._store_cached_preview(path, image)
            if self._mode != "photo": continue
            current_entry = (self.entries[self.current_index]
                             if self.current_index is not None and self.entries else None)
            if current_entry is None or current_entry.jpg_path != path: continue
            if image is not None:
                self._image_view.set_image(image, str(path))
                self._lbl_info.setText(f"路径：{path}\n状态：{current_entry.status_text()}\n匹配到的原始文件：{len(current_entry.raw_paths)} 个")
            else: self._image_view.clear_image(); self._lbl_info.setText(f"预览失败：\n{error}")

    def _process_scan_results(self):
        while True:
            try: req_id, folder, batch, done, mode = self.scan_results.get_nowait()
            except queue.Empty: break
            if req_id != self.scan_request_id or self.current_folder != folder: continue
            if mode != self._mode: continue
            if batch:
                start = len(self.entries); self.entries.extend(batch)
                for entry in batch: self._file_list.addItem(entry.display_name)
                self._update_list_styles()
                for i in range(start, len(self.entries)):
                    rel = str(self.entries[i].relative_path)
                    if rel in self.persisted_statuses:
                        self.entries[i].status = self.persisted_statuses[rel]; self._update_list_row(i)
                self._update_summary(); self._update_controls()
            if done:
                self.is_scanning = False
                if not self.entries: self._set_status("当前文件夹中没有找到可处理的项目。")
                else: self._restore_selection_after_scan()
                self._update_summary(); self._update_controls(); self._save_state()

    # ── 预加载 ────────────────────────────────────────

    def _queue_preview_prefetch(self):
        if self.current_index is None or self._mode != "photo": return
        offsets = list(range(1, self.preview_lookahead + 1)); offsets.extend((-1, -2, -3))
        for off in offsets:
            idx = self.current_index + off
            if 0 <= idx < len(self.entries):
                self._enqueue_preview_request(self.entries[idx].jpg_path, priority=10 + abs(off), request_id=0)

    def _enqueue_preview_request(self, path: Path, priority: int, request_id: int, force: bool = False):
        if self._get_cached_preview(path) is not None: return
        with self.preview_queue_lock:
            if path in self.preview_queued_paths and not force: return
            self.preview_task_id += 1; self.preview_queued_paths.add(path)
            self.preview_requests.put((priority, self.preview_task_id, request_id, path))

    def _get_cached_preview(self, path: Path) -> Image.Image | None:
        with self.preview_cache_lock:
            cached = self.preview_cache.get(path)
            if cached is None: return None
            self.preview_cache.move_to_end(path); return cached

    def _store_cached_preview(self, path: Path, image: Image.Image):
        with self.preview_cache_lock:
            self.preview_cache[path] = image; self.preview_cache.move_to_end(path)
            while len(self.preview_cache) > self.preview_cache_size: self.preview_cache.popitem(last=False)

# ═══════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════

def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--delete-worker":
        sys.exit(run_delete_worker_job(sys.argv[2]))
    if len(sys.argv) >= 3 and sys.argv[1] == "--video-probe-worker":
        sys.exit(run_video_probe_worker_job(sys.argv[2]))
    if len(sys.argv) >= 3 and sys.argv[1] == "--video-proxy-worker":
        sys.exit(run_video_proxy_worker_job(sys.argv[2]))
    app = QApplication(sys.argv); app.setStyle("Fusion")
    window = PhotoCullerWindow(); window.show(); sys.exit(app.exec())

if __name__ == "__main__":
    main()
