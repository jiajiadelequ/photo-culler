import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtMultimedia import QAudioOutput, QMediaMetaData, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

try:
    from PySide6.QtMultimedia import QPlaybackOptions
except Exception:  # pragma: no cover - depends on Qt version
    QPlaybackOptions = None

LOGGER = logging.getLogger("photo_culler")

VIDEO_QUALITY_AUTO = "auto"
VIDEO_QUALITY_ORIGINAL = "original"
VIDEO_QUALITY_PROXY = "proxy"
VIDEO_QUALITY_LABELS = {
    VIDEO_QUALITY_AUTO: "自动",
    VIDEO_QUALITY_ORIGINAL: "原片",
    VIDEO_QUALITY_PROXY: "流畅预览",
}
VIDEO_PROXY_PROTOCOL_VERSION = 1
DEFAULT_PROXY_HEIGHT = 1080
DEFAULT_PROXY_VIDEO_CODEC = "libx264"
DEFAULT_PROXY_PIXEL_FORMAT = "yuv420p"
PROXY_CONTAINER_SUFFIX = ".mp4"


def install_qt_logging_bridge(qtcore_module):
    if getattr(install_qt_logging_bridge, "_installed", False):
        return
    if not hasattr(qtcore_module, "qInstallMessageHandler"):
        return

    QtMsgType = getattr(qtcore_module, "QtMsgType", None)

    def _handler(msg_type, context, message):
        category = getattr(context, "category", "qt")
        if QtMsgType is not None and msg_type == QtMsgType.QtFatalMsg:
            level = logging.CRITICAL
        elif QtMsgType is not None and msg_type == QtMsgType.QtCriticalMsg:
            level = logging.ERROR
        elif QtMsgType is not None and msg_type == QtMsgType.QtWarningMsg:
            level = logging.WARNING
        else:
            level = logging.INFO
        LOGGER.log(level, "[Qt][%s] %s", category, message)

    qtcore_module.qInstallMessageHandler(_handler)
    install_qt_logging_bridge._installed = True


def discover_ffmpeg_tools() -> dict:
    ffmpeg = os.environ.get("PHOTOCULLER_FFMPEG")
    ffprobe = os.environ.get("PHOTOCULLER_FFPROBE")
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass))
    candidates.extend([Path.cwd(), Path(__file__).resolve().parent])
    if ffmpeg:
        ffmpeg_path = Path(ffmpeg)
    else:
        ffmpeg_path = _which_or_none("ffmpeg", candidates)
    if ffprobe:
        ffprobe_path = Path(ffprobe)
    else:
        ffprobe_path = _which_or_none("ffprobe", candidates)
    return {
        "ffmpeg": str(ffmpeg_path) if ffmpeg_path else None,
        "ffprobe": str(ffprobe_path) if ffprobe_path else None,
    }


def _which_or_none(name: str, search_dirs: list[Path]) -> Path | None:
    hit = shutil.which(name)
    if hit:
        return Path(hit)
    for directory in search_dirs:
        for candidate in [directory / name, directory / f"{name}.exe"]:
            if candidate.exists():
                return candidate
    return None


@dataclass
class VideoProbeInfo:
    path: str
    codec_name: str = ""
    codec_long_name: str = ""
    profile: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pix_fmt: str = ""
    bit_depth: int = 8
    color_transfer: str = ""
    color_primaries: str = ""
    color_space: str = ""
    duration_ms: int = 0
    has_audio: bool = False
    backend: str = "qt_ffmpeg"
    hwaccel_requested: str = "d3d11va,d3d12va"
    hwaccel_active: bool | None = None
    probe_ok: bool = False
    error_message: str = ""

    def is_heavy_for_auto(self) -> bool:
        return (
            self.width >= 3840
            or self.height >= 2160
            or self.fps > 60.0
            or self.bit_depth > 8
            or (self.codec_name.lower() in {"hevc", "h265"} and "422" in self.pix_fmt.lower())
        )

    def is_heavy_for_proxy_mode(self) -> bool:
        return (
            self.is_heavy_for_auto()
            or self.width >= 1920
            or self.height >= 1080
            or self.fps > 30.0
            or self.codec_name.lower() in {"hevc", "h265", "av1", "prores"}
        )

    def proxy_fps(self) -> int:
        return 60 if self.fps > 45.0 else 30

    def summary_text(self) -> str:
        fps = f"{self.fps:.2f}".rstrip("0").rstrip(".")
        return (
            f"{self.codec_name or 'unknown'} | {self.width}x{self.height} | "
            f"{fps or '0'}fps | {self.bit_depth}-bit | {self.pix_fmt or 'unknown'}"
        )


def video_proxy_root(settings_dir: Path) -> Path:
    path = settings_dir / "video_proxy"
    path.mkdir(parents=True, exist_ok=True)
    return path


def video_proxy_cache_key(source_path: Path) -> str:
    stat = source_path.stat()
    payload = f"{source_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()


def video_proxy_cache_path(settings_dir: Path, source_path: Path) -> Path:
    key = video_proxy_cache_key(source_path)
    root = video_proxy_root(settings_dir)
    bucket = root / key[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    return bucket / f"{key}{PROXY_CONTAINER_SUFFIX}"


def video_proxy_meta_path(settings_dir: Path, source_path: Path) -> Path:
    key = video_proxy_cache_key(source_path)
    root = video_proxy_root(settings_dir)
    bucket = root / key[:2]
    bucket.mkdir(parents=True, exist_ok=True)
    return bucket / f"{key}.json"


def load_probe_info(path: Path) -> VideoProbeInfo | None:
    try:
        return VideoProbeInfo(**json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def save_probe_info(path: Path, info: VideoProbeInfo) -> None:
    path.write_text(json.dumps(asdict(info), ensure_ascii=False, indent=2), encoding="utf-8")


def remove_proxy_artifacts(settings_dir: Path, source_path: Path) -> None:
    source_text = str(source_path)
    root = video_proxy_root(settings_dir)
    for meta_path in root.rglob("*.json"):
        info = load_probe_info(meta_path)
        if info is None or info.path != source_text:
            continue
        video_path = meta_path.with_suffix(PROXY_CONTAINER_SUFFIX)
        for target in [meta_path, video_path]:
            try:
                if target.exists():
                    target.unlink()
            except OSError:
                LOGGER.exception("Failed to remove proxy artifact path=%s", target)


def should_use_proxy(mode: str, info: VideoProbeInfo | None) -> bool:
    if mode == VIDEO_QUALITY_ORIGINAL:
        return False
    if info is None:
        return False
    if mode == VIDEO_QUALITY_PROXY:
        return info.is_heavy_for_proxy_mode()
    return info.is_heavy_for_auto()


def probe_video_file(video_path: Path, ffprobe_exe: str) -> VideoProbeInfo:
    cmd = [
        ffprobe_exe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(video_path),
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams", []) or []
    format_info = payload.get("format", {}) or {}
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = _parse_fraction(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/1")
    bit_depth = _parse_bit_depth(video_stream)
    duration_ms = int(float(format_info.get("duration") or 0.0) * 1000)
    return VideoProbeInfo(
        path=str(video_path),
        codec_name=video_stream.get("codec_name", "") or "",
        codec_long_name=video_stream.get("codec_long_name", "") or "",
        profile=video_stream.get("profile", "") or "",
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        fps=fps,
        pix_fmt=video_stream.get("pix_fmt", "") or "",
        bit_depth=bit_depth,
        color_transfer=video_stream.get("color_transfer", "") or "",
        color_primaries=video_stream.get("color_primaries", "") or "",
        color_space=video_stream.get("color_space", "") or "",
        duration_ms=duration_ms,
        has_audio=audio_stream is not None,
        probe_ok=True,
    )


def build_proxy_ffmpeg_command(ffmpeg_exe: str, source_path: Path, output_path: Path, info: VideoProbeInfo) -> list[str]:
    fps = info.proxy_fps()
    vf = f"scale=-2:{DEFAULT_PROXY_HEIGHT}:force_original_aspect_ratio=decrease,fps={fps}"
    return [
        ffmpeg_exe,
        "-y",
        "-hide_banner",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        vf,
        "-c:v",
        DEFAULT_PROXY_VIDEO_CODEC,
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        DEFAULT_PROXY_PIXEL_FORMAT,
        "-movflags",
        "+faststart",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_path),
    ]


def run_video_probe_worker_job(job_path: str) -> int:
    try:
        job = json.loads(Path(job_path).read_text(encoding="utf-8"))
        source_path = Path(job["source_path"])
        ffprobe_exe = job["ffprobe_exe"]
        settings_dir = Path(job["settings_dir"])
        info = probe_video_file(source_path, ffprobe_exe)
        meta_path = video_proxy_meta_path(settings_dir, source_path)
        save_probe_info(meta_path, info)
        _worker_emit({"event": "finished", "info": asdict(info)})
        return 0
    except Exception as exc:
        _worker_emit({
            "event": "fatal",
            "exception_type": type(exc).__name__,
            "error_message": str(exc),
            "job_path": job_path,
        })
        return 2


def run_video_proxy_worker_job(job_path: str) -> int:
    started_at = time.perf_counter()
    try:
        job = json.loads(Path(job_path).read_text(encoding="utf-8"))
        source_path = Path(job["source_path"])
        output_path = Path(job["output_path"])
        ffmpeg_exe = job["ffmpeg_exe"]
        duration_ms = int(job.get("duration_ms") or 0)
        probe_info = VideoProbeInfo(**job["probe_info"])
    except Exception as exc:
        _worker_emit({
            "event": "fatal",
            "exception_type": type(exc).__name__,
            "error_message": str(exc),
            "job_path": job_path,
        })
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_proxy_ffmpeg_command(ffmpeg_exe, source_path, output_path, probe_info)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    _worker_emit({
        "event": "started",
        "source_path": str(source_path),
        "output_path": str(output_path),
        "command": cmd,
    })
    progress_pattern = re.compile(r"time=(\d{2}:\d{2}:\d{2}(?:\.\d+)?)")
    while True:
        line = proc.stderr.readline() if proc.stderr is not None else ""
        if not line and proc.poll() is not None:
            break
        if not line:
            continue
        match = progress_pattern.search(line)
        if not match:
            continue
        position_ms = int(_parse_timestamp(match.group(1)) * 1000)
        percent = 0
        if duration_ms > 0:
            percent = min(100, int(position_ms * 100 / duration_ms))
        _worker_emit({
            "event": "progress",
            "source_path": str(source_path),
            "position_ms": position_ms,
            "duration_ms": duration_ms,
            "percent": percent,
        })

    exit_code = proc.wait()
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    if exit_code != 0 or not output_path.exists():
        stderr_tail = ""
        if proc.stderr is not None:
            try:
                stderr_tail = proc.stderr.read()[-4000:]
            except Exception:
                stderr_tail = ""
        _worker_emit({
            "event": "fatal",
            "exception_type": "ProxyEncodeError",
            "error_message": f"ffmpeg exit={exit_code}",
            "stderr_tail": stderr_tail,
            "source_path": str(source_path),
        })
        return 3

    _worker_emit({
        "event": "finished",
        "source_path": str(source_path),
        "output_path": str(output_path),
        "elapsed_ms": elapsed_ms,
    })
    return 0


def _worker_emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _parse_fraction(value: str) -> float:
    if not value or value == "0/0":
        return 0.0
    if "/" in value:
        left, right = value.split("/", 1)
        try:
            denom = float(right)
            return float(left) / denom if denom else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _parse_bit_depth(stream: dict) -> int:
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        value = stream.get(key)
        if value not in (None, "", "N/A"):
            try:
                return int(value)
            except ValueError:
                pass
    pix_fmt = (stream.get("pix_fmt") or "").lower()
    if "p10" in pix_fmt or "10le" in pix_fmt:
        return 10
    if "p12" in pix_fmt or "12le" in pix_fmt:
        return 12
    return 8


def _parse_timestamp(value: str) -> float:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


class VideoPlayerWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._current_path = ""
        self._source_path = ""
        self._is_playing = False
        self._duration_ms = 0
        self._volume = 100
        self._muted = False
        self._speed = 1.0
        self._seeking = False
        self._autoplay = True
        self._auto_advance = False
        self._pending_seek_ms = 0
        self._last_progress_second = -1
        self._last_metadata = {}
        self.on_state_changed = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._video_widget = QVideoWidget(self)
        self._video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self._player.setVideoOutput(self._video_widget)
        layout.addWidget(self._video_widget, 1)

        ctrl = QWidget(self)
        cl = QHBoxLayout(ctrl)
        cl.setContentsMargins(4, 2, 4, 2)

        self._btn_play = QPushButton("▶")
        self._btn_play.setFixedWidth(30)
        self._btn_play.clicked.connect(self._toggle_play)
        cl.addWidget(self._btn_play)

        self._lbl_time = QLabel("00:00 / 00:00")
        cl.addWidget(self._lbl_time)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 1000)
        self._slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self._slider.sliderReleased.connect(self._on_seek_end)
        cl.addWidget(self._slider, 1)

        self._btn_mute = QPushButton("🔊")
        self._btn_mute.setFixedWidth(30)
        self._btn_mute.clicked.connect(self._toggle_mute)
        cl.addWidget(self._btn_mute)

        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(100)
        self._vol_slider.setFixedWidth(80)
        self._vol_slider.valueChanged.connect(self._on_volume_change)
        cl.addWidget(self._vol_slider)

        self._cmb_speed = QComboBox()
        self._cmb_speed.addItems(["0.5x", "1.0x", "1.25x", "1.5x", "2.0x", "3.0x", "4.0x"])
        self._cmb_speed.setCurrentText("1.0x")
        self._cmb_speed.currentTextChanged.connect(self._on_speed_change)
        cl.addWidget(self._cmb_speed)

        layout.addWidget(ctrl)

        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._update_ui)
        self._update_timer.start(200)

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_playback_state_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._player.errorOccurred.connect(self._on_error)
        self._player.metaDataChanged.connect(self._on_metadata_changed)

        self._audio.setVolume(1.0)

    def load(self, path: str, original_path: str | None = None):
        self.stop()
        self._source_path = path
        self._current_path = original_path or path
        self._player.setSource(QUrl.fromLocalFile(path))
        self._audio.setVolume(self._volume / 100.0)
        self._audio.setMuted(self._muted)
        self._player.setPlaybackRate(self._speed)
        if self._autoplay:
            self._player.play()
        self._is_playing = self._autoplay
        self._btn_play.setText("⏸" if self._autoplay else "▶")
        if self._pending_seek_ms > 0 or not self._autoplay:
            QTimer.singleShot(150, self._apply_pending_start_state)
        self._emit_state_changed()

    def prepare_restore(self, position_ms: int = 0):
        self._pending_seek_ms = max(0, int(position_ms or 0))

    def current_position(self) -> int:
        return max(0, int(self._player.position()))

    def current_media_path(self) -> str:
        return self._current_path

    def current_source_path(self) -> str:
        return self._source_path

    def is_playing(self) -> bool:
        return self._is_playing

    def stop(self):
        self._player.stop()
        self._player.setSource(QUrl())
        self._reset_ui_state()

    def state_snapshot(self) -> str:
        return f"source={self._source_path} status={self._player.mediaStatus()} state={self._player.playbackState()}"

    def current_media_status(self):
        return self._player.mediaStatus()

    def detach_for_delete(self):
        self.stop()

    def dispose(self):
        self._update_timer.stop()
        self.stop()

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
        speed_text = f"{self._speed}x"
        valid_items = [self._cmb_speed.itemText(i) for i in range(self._cmb_speed.count())]
        self._cmb_speed.setCurrentText(speed_text if speed_text in valid_items else "1.0x")
        self._vol_slider.setValue(self._volume)
        self._btn_mute.setText("🔇" if self._muted else "🔊")
        self._audio.setVolume(self._volume / 100.0)
        self._audio.setMuted(self._muted)
        self._player.setPlaybackRate(self._speed)

    def state_snapshot(self) -> dict:
        return {
            "current_path": self._current_path,
            "source_path": self._source_path,
            "is_playing": self._is_playing,
            "duration_ms": self._duration_ms,
            "position_ms": self.current_position(),
            "volume": self._volume,
            "muted": self._muted,
            "speed": self._speed,
            "has_media": bool(self._source_path),
            "has_player": True,
            "metadata": dict(self._last_metadata),
        }

    def last_metadata(self) -> dict:
        return dict(self._last_metadata)

    def _reset_ui_state(self):
        self._is_playing = False
        self._current_path = ""
        self._source_path = ""
        self._duration_ms = 0
        self._pending_seek_ms = 0
        self._last_progress_second = -1
        self._btn_play.setText("▶")
        self._lbl_time.setText("00:00 / 00:00")
        self._slider.setValue(0)
        self._emit_state_changed()

    def _apply_pending_start_state(self):
        if self._pending_seek_ms > 0:
            self._player.setPosition(self._pending_seek_ms)
        if not self._autoplay:
            self._pause()
        self._pending_seek_ms = 0

    def _play(self):
        self._player.play()
        self._is_playing = True
        self._btn_play.setText("⏸")
        self._emit_state_changed()

    def _pause(self):
        self._player.pause()
        self._is_playing = False
        self._btn_play.setText("▶")
        self._emit_state_changed()

    def _toggle_play(self):
        self._pause() if self._is_playing else self._play()

    def _toggle_mute(self):
        self._muted = not self._muted
        self._audio.setMuted(self._muted)
        self._btn_mute.setText("🔇" if self._muted else "🔊")
        self._emit_state_changed()

    def _on_volume_change(self, value: int):
        self._volume = value
        self._audio.setVolume(value / 100.0)
        self._emit_state_changed(high_frequency=True)

    def _on_speed_change(self, text: str):
        self._speed = float(text.replace("x", ""))
        self._player.setPlaybackRate(self._speed)
        self._emit_state_changed()

    def _on_seek_end(self):
        self._seeking = False
        if self._duration_ms > 0:
            self._player.setPosition(int(self._slider.value() / 1000 * self._duration_ms))
            self._emit_state_changed(high_frequency=True)

    def _on_position_changed(self, pos: int):
        if self._duration_ms > 0 and not self._seeking:
            self._slider.setValue(int(pos / self._duration_ms * 1000))
        self._lbl_time.setText(f"{self._fmt(pos)} / {self._fmt(self._duration_ms)}")
        current_second = max(0, int(pos // 1000))
        if current_second != self._last_progress_second:
            self._last_progress_second = current_second
            self._emit_state_changed(high_frequency=True)

    def _on_duration_changed(self, duration: int):
        self._duration_ms = max(0, int(duration))
        self._lbl_time.setText(f"{self._fmt(self.current_position())} / {self._fmt(self._duration_ms)}")
        self._emit_state_changed()

    def _on_playback_state_changed(self, state):
        playing_state = getattr(QMediaPlayer, "PlaybackState", None)
        if playing_state is not None:
            self._is_playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._btn_play.setText("⏸" if self._is_playing else "▶")
        self._emit_state_changed()

    def _on_media_status_changed(self, status):
        LOGGER.info("QMediaPlayer mediaStatusChanged status=%s source=%s", status, self._source_path)

    def _on_error(self, *args):
        error = args[0] if args else None
        error_string = args[1] if len(args) > 1 else ""
        LOGGER.error("QMediaPlayer error=%s message=%s source=%s", error, error_string, self._source_path)

    def _on_metadata_changed(self):
        meta = self._player.metaData()
        data = {}
        try:
            for key in meta.keys():
                data[str(key)] = str(meta.value(key))
        except Exception:
            data = {}
        self._last_metadata = data
        LOGGER.info("QMediaPlayer metadataChanged source=%s metadata=%s", self._source_path, data)

    def _update_ui(self):
        if self._duration_ms <= 0:
            return
        pos = self.current_position()
        if not self._seeking:
            self._slider.setValue(int(pos / self._duration_ms * 1000))
        self._lbl_time.setText(f"{self._fmt(pos)} / {self._fmt(self._duration_ms)}")

    def _emit_state_changed(self, high_frequency: bool = False):
        if callable(self.on_state_changed):
            self.on_state_changed(high_frequency)

    @staticmethod
    def _fmt(ms: int) -> str:
        seconds = max(0, int(ms // 1000))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
