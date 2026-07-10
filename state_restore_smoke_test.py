import os
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path


class Dummy:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return Dummy()

    def __getattr__(self, name):
        return Dummy()

    def __or__(self, other):
        return 0

    def connect(self, *args, **kwargs):
        return None

    def emit(self, *args, **kwargs):
        return None

    def start(self, *args, **kwargs):
        return None

    def stop(self, *args, **kwargs):
        return None

    def setSingleShot(self, *args, **kwargs):
        return None

    @staticmethod
    def singleShot(*args, **kwargs):
        return None


class DummyQt:
    Key = types.SimpleNamespace(
        Key_Delete=0,
        Key_Return=0,
        Key_Enter=0,
        Key_S=0,
        Key_Z=0,
        Key_Up=0,
        Key_Down=0,
        Key_Control=0,
        Key_Shift=0,
        Key_Alt=0,
        Key_Meta=0,
        Key_A=0,
    )
    KeyboardModifier = types.SimpleNamespace(ControlModifier=0)
    Orientation = types.SimpleNamespace(Horizontal=0)
    AspectRatioMode = types.SimpleNamespace(KeepAspectRatio=0)
    WidgetAttribute = types.SimpleNamespace(WA_NativeWindow=0)
    ScrollBarPolicy = types.SimpleNamespace(ScrollBarAlwaysOff=0)
    GlobalColor = types.SimpleNamespace(gray=0, darkGreen=0, darkYellow=0, black=0)


qtcore = types.ModuleType("PySide6.QtCore")
qtcore.Qt = DummyQt
qtcore.QTimer = Dummy
qtcore.QRectF = Dummy
qtcore.QPointF = Dummy
qtcore.QObject = Dummy
qtcore.QThread = Dummy
qtcore.QUrl = Dummy
qtcore.qInstallMessageHandler = lambda *args, **kwargs: None
qtcore.QtMsgType = types.SimpleNamespace(QtFatalMsg=0, QtCriticalMsg=1, QtWarningMsg=2)
qtcore.Signal = lambda *args, **kwargs: Dummy()
qtcore.QProcess = Dummy

qtgui = types.ModuleType("PySide6.QtGui")
for name in ["QAction", "QKeySequence", "QPixmap", "QImage", "QWheelEvent", "QMouseEvent", "QFont"]:
    setattr(qtgui, name, Dummy)

qtwidgets = types.ModuleType("PySide6.QtWidgets")
for name in [
    "QApplication", "QMainWindow", "QWidget", "QVBoxLayout", "QHBoxLayout",
    "QSplitter", "QListWidget", "QListWidgetItem", "QGraphicsView", "QGraphicsScene",
    "QGraphicsPixmapItem", "QPushButton", "QLabel", "QMenuBar", "QMenu",
    "QToolBar", "QStatusBar", "QFileDialog", "QMessageBox", "QFrame", "QSizePolicy",
    "QSlider", "QComboBox", "QDialog", "QFormLayout", "QSpinBox", "QDialogButtonBox",
]:
    setattr(qtwidgets, name, Dummy)
qtwidgets.QMessageBox.StandardButton = types.SimpleNamespace(Yes=1)
qtwidgets.QDialog.DialogCode = types.SimpleNamespace(Accepted=1)

qtmultimedia = types.ModuleType("PySide6.QtMultimedia")
for name in ["QAudioOutput", "QMediaMetaData", "QMediaPlayer", "QPlaybackOptions"]:
    setattr(qtmultimedia, name, Dummy)

qtmultimediawidgets = types.ModuleType("PySide6.QtMultimediaWidgets")
qtmultimediawidgets.QVideoWidget = Dummy

pyside6 = types.ModuleType("PySide6")
sys.modules["PySide6"] = pyside6
sys.modules["PySide6.QtCore"] = qtcore
sys.modules["PySide6.QtGui"] = qtgui
sys.modules["PySide6.QtWidgets"] = qtwidgets
sys.modules["PySide6.QtMultimedia"] = qtmultimedia
sys.modules["PySide6.QtMultimediaWidgets"] = qtmultimediawidgets

vlc_stub = types.ModuleType("vlc")


class _StubMedia:
    def release(self):
        return None


class _StubPlayer:
    def set_media(self, media):
        self.media = media

    def set_hwnd(self, hwnd):
        return None

    def audio_set_volume(self, value):
        return None

    def audio_set_mute(self, value):
        return None

    def set_rate(self, value):
        return None

    def play(self):
        return None

    def pause(self):
        return None

    def stop(self):
        return None

    def release(self):
        return None

    def get_time(self):
        return 0

    def get_length(self):
        return 0

    def set_time(self, value):
        return None


class _StubInstance:
    def __init__(self, *args, **kwargs):
        pass

    def media_player_new(self):
        return _StubPlayer()

    def media_new(self, path):
        return _StubMedia()

    def release(self):
        return None


vlc_stub.Instance = _StubInstance
vlc_stub.MediaPlayer = _StubPlayer
vlc_stub.Media = _StubMedia
sys.modules["vlc"] = vlc_stub

_test_appdata = Path(tempfile.mkdtemp(prefix="pc_appdata_"))
os.environ["LOCALAPPDATA"] = str(_test_appdata)

import photo_culler_qt as pc


class FakeScrollBar:
    def __init__(self, value=0):
        self.value_store = value

    def value(self):
        return self.value_store

    def setValue(self, value):
        self.value_store = value


class FakeList:
    def __init__(self):
        self.scrollbar = FakeScrollBar()
        self.rows = []

    def verticalScrollBar(self):
        return self.scrollbar

    def blockSignals(self, _value):
        return None

    def clearSelection(self):
        return None

    def setCurrentRow(self, _index):
        return None

    def item(self, _index):
        return Dummy()

    def scrollToItem(self, _item):
        return None

    def clear(self):
        self.rows = []

    def addItem(self, item):
        self.rows.append(item)

    def selectedIndexes(self):
        return []


class FakeLabel:
    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = text


class FakeVideoWidget:
    def __init__(self):
        self.position_ms = 0
        self.prepared_position = 0
        self.loaded_path = None
        self.prefs = {
            "playback_rate": 2.0,
            "volume": 70,
            "muted": False,
            "autoplay": True,
            "auto_advance": False,
        }

    def current_position(self):
        return self.position_ms

    def get_preferences(self):
        return dict(self.prefs)

    def apply_preferences(self, prefs):
        self.prefs.update(prefs or {})

    def prepare_restore(self, position_ms):
        self.prepared_position = position_ms

    def load(self, path, original_path=None):
        self.loaded_path = path

    def current_source_path(self):
        return self.loaded_path or ""

    def detach_for_delete(self):
        self.loaded_path = None

    def stop(self):
        self.loaded_path = None

    def dispose(self):
        return None


def make_window(state_root: Path):
    win = pc.PhotoCullerWindow.__new__(pc.PhotoCullerWindow)
    win.current_folder = None
    win._photo_folder = None
    win._video_folder = None
    win._photo_entries = []
    win._video_entries = []
    win._photo_index = None
    win._video_index = None
    win._mode = "photo"
    win.entries = []
    win.current_index = None
    win.is_scanning = False
    win.pending_restore_photo = None
    win.pending_restore_video = None
    win.pending_restore_index = None
    win.pending_restore_scroll = None
    win.pending_restore_video_position = 0
    win.recent_sessions = []
    win.persisted_statuses = {}
    win._undo_stack = []
    win._photo_undo_stack = []
    win._video_undo_stack = []
    win._delete_in_progress = False
    win._delete_process = None
    win._delete_output_buffer = ""
    win._delete_job_file = None
    win._delete_result = None
    win._close_after_delete = False
    win._delete_cancel_requested = False
    win._delete_restore_context = {}
    win._gui_thread_ident = 1
    win._video_quality_mode = pc.VIDEO_QUALITY_AUTO
    win._video_tools = {"ffmpeg": None, "ffprobe": None}
    win._video_probe_process = None
    win._video_probe_job_file = None
    win._video_probe_output_buffer = ""
    win._video_probe_target = None
    win._video_probe_cache = {}
    win._video_proxy_process = None
    win._video_proxy_job_file = None
    win._video_proxy_output_buffer = ""
    win._video_proxy_target = None
    win._video_proxy_output_path = None
    win._video_proxy_progress_text = ""
    win._file_list = FakeList()
    win._lbl_folder = FakeLabel()
    win._lbl_summary = FakeLabel()
    win._lbl_title = FakeLabel()
    win._lbl_info = FakeLabel()
    win._video_widget = FakeVideoWidget()
    win._image_view = types.SimpleNamespace(clear_image=lambda: None)
    win._update_summary = lambda: None
    win._update_controls = lambda: None
    win._refresh_list = lambda: None
    win._set_status = lambda message: setattr(win._lbl_info, "text", message)
    win._show_current = types.MethodType(pc.PhotoCullerWindow._show_current, win)
    win._state_save_timer = types.SimpleNamespace(start=lambda *_args, **_kwargs: None)
    win._photo_state_store = win._empty_mode_state("photo")
    win._video_state_store = win._empty_mode_state("video")
    win._cleanup_delete_process = types.MethodType(pc.PhotoCullerWindow._cleanup_delete_process, win)

    def state_file_for_mode(mode: str):
        return state_root / ("video.json" if mode == "video" else "photo.json")

    win._state_file_for_mode = state_file_for_mode
    win._set_selection = types.MethodType(fake_set_selection, win)
    return win


def fake_set_selection(self, index: int):
    self.current_index = index
    self._show_current()


def save_settings_to(state_root: Path, last_mode: str):
    pc.safe_write_json(state_root / "settings.json", {
        "preview_cache_size": pc.DEFAULT_PREVIEW_CACHE_SIZE,
        "preview_lookahead": pc.DEFAULT_PREVIEW_LOOKAHEAD,
        "last_mode": last_mode,
    })


class CollectorSignal:
    def __init__(self):
        self.items = []

    def emit(self, *args):
        self.items.append(args)


def run_video_delete_worker_test(root: Path):
    batch_dir = root / "worker_batch"
    batch_dir.mkdir()
    paths = []
    for i in range(1, 151):
        path = batch_dir / f"clip{i:03d}.mp4"
        path.write_bytes(b"x")
        paths.append(path)
    missing_targets = {str(path) for path in paths[140:145]}
    error_targets = {str(path) for path in paths[145:150]}
    for path in paths[140:145]:
        path.unlink()

    original_move = pc.move_path_to_recycle_bin
    original_event = pc.delete_worker_event

    def fake_move(path: Path):
        if str(path) in error_targets:
            raise OSError("mock recycle error")
        if path.exists():
            path.unlink()

    events = []
    pc.delete_worker_event = lambda payload: events.append(payload)
    pc.move_path_to_recycle_bin = fake_move
    try:
        job_file = root / "delete_job.json"
        pc.safe_write_json(job_file, {"paths": [str(path) for path in paths]})
        exit_code = pc.run_delete_worker_job(str(job_file))
        assert exit_code == 0
        progress_events = [event for event in events if event.get("event") == "progress" and event.get("phase") == "after_delete"]
        result = next(event for event in events if event.get("event") == "finished")
        assert len(result["successful_paths"]) == 140
        assert len(result["missing_paths"]) == 5
        assert len(result["failed_items"]) == 5
        assert result["processed_count"] == 150
        assert progress_events[0]["index"] == 1
        assert progress_events[-1]["index"] == 150
    finally:
        pc.move_path_to_recycle_bin = original_move
        pc.delete_worker_event = original_event


def run_video_delete_finalize_test(root: Path):
    folder = root / "finalize_case"
    folder.mkdir()
    files = []
    for i in range(1, 7):
        path = folder / f"video{i:03d}.mp4"
        path.write_bytes(b"x")
        files.append(path)

    win = make_window(root / "state_finalize")
    (root / "state_finalize").mkdir(exist_ok=True)
    win._mode = "video"
    win.current_folder = folder
    win._video_folder = folder
    win._delete_in_progress = True
    win.entries = [pc.VideoEntry(video_path=p, relative_path=p.relative_to(folder)) for p in files]
    for entry in win.entries[:4]:
        entry.status = "deleted"
    win.current_index = 3
    win._delete_restore_context = {
        "current_path": str(win.entries[3].relative_path),
        "current_index": 3,
        "scroll_position": 12,
    }
    win._refresh_list = lambda: None
    win._update_summary = lambda: None
    win._update_controls = lambda: None
    win._saved_once = 0
    win._save_state = lambda: setattr(win, "_saved_once", getattr(win, "_saved_once", 0) + 1)
    win._set_delete_ui_busy = lambda busy: setattr(win, "_delete_in_progress", busy)
    win._delete_job_file = None
    qtwidgets.QMessageBox.information = lambda *args, **kwargs: None

    result = {
        "successful_paths": [str(files[0]), str(files[1])],
        "missing_paths": [str(files[2])],
        "failed_items": [{
            "path": str(files[3]),
            "exception_type": "PermissionError",
            "error_message": "still in use",
        }],
        "processed_count": 4,
        "total_count": 4,
        "cancelled": False,
        "elapsed_time_ms": 10,
        "last_path": str(files[3]),
    }
    win._finalize_video_delete_result(result, "test")
    remaining = [str(e.relative_path) for e in win.entries]
    assert "video001.mp4" not in remaining
    assert "video002.mp4" not in remaining
    assert "video003.mp4" not in remaining
    assert "video004.mp4" in remaining
    failed_entry = next(e for e in win.entries if str(e.relative_path) == "video004.mp4")
    assert failed_entry.status == "deleted"
    assert win.current_index is not None
    assert getattr(win, "_saved_once", 0) == 1


def run():
    root = Path(tempfile.mkdtemp(prefix="pc_state_test_"))
    try:
        photo_dir = root / "photos"
        video_a = root / "videos_a"
        video_b = root / "videos_b"
        photo_dir.mkdir()
        video_a.mkdir()
        video_b.mkdir()

        for i in range(1, 4):
            (photo_dir / f"photo{i:03d}.jpg").write_bytes(b"x")
        for i in range(1, 9):
            (video_a / f"video{i:03d}.mp4").write_bytes(b"x")
        for i in range(1, 5):
            (video_b / f"video{i:03d}.mp4").write_bytes(b"x")

        state_root = root / "state"
        state_root.mkdir()

        photo_state = state_root / "photo.json"
        video_state = state_root / "video.json"
        settings_state = state_root / "settings.json"

        original_state = pc.STATE_FILE
        original_video_state = pc.VIDEO_STATE_FILE
        original_settings = pc.SETTINGS_FILE
        pc.STATE_FILE = photo_state
        pc.VIDEO_STATE_FILE = video_state
        pc.SETTINGS_FILE = settings_state

        # 场景一：视频目录 A 的状态保存与恢复
        win1 = make_window(state_root)
        win1._mode = "video"
        win1.current_folder = video_a
        win1._video_folder = video_a
        win1.entries = [
            pc.VideoEntry(video_path=p, relative_path=p.relative_to(video_a))
            for p in sorted(video_a.glob("*.mp4"))
        ]
        for idx in range(3):
            win1.entries[idx].status = "kept"
        win1.entries[3].status = "deleted"
        win1.entries[4].status = "deleted"
        win1.current_index = 5
        win1._file_list.verticalScrollBar().setValue(840)
        win1._video_widget.position_ms = 42000
        win1._save_state()
        save_settings_to(state_root, "video")

        win2 = make_window(state_root)
        win2._photo_state_store = win2._load_mode_state("photo")
        win2._video_state_store = win2._load_mode_state("video")
        win2._prepare_restore_for_folder("video", video_a)
        win2._mode = "video"
        win2.current_folder = video_a
        win2.entries = [
            pc.VideoEntry(video_path=p, relative_path=p.relative_to(video_a))
            for p in sorted(video_a.glob("*.mp4"))
        ]
        for entry in win2.entries:
            rel = str(entry.relative_path)
            if rel in win2.persisted_statuses:
                entry.status = win2.persisted_statuses[rel]
        win2._restore_selection_after_scan()
        assert win2.current_index == 5
        assert win2.entries[0].status == "kept"
        assert win2.entries[3].status == "deleted"
        assert win2._file_list.verticalScrollBar().value() == 840

        # 场景二：目录 A / B 相互独立
        win2._mode = "video"
        win2._video_state_store = win2._load_mode_state("video")
        win2.current_folder = video_b
        win2.entries = [
            pc.VideoEntry(video_path=p, relative_path=p.relative_to(video_b))
            for p in sorted(video_b.glob("*.mp4"))
        ]
        win2.current_index = 1
        win2.entries[0].status = "skipped"
        win2.entries[1].status = "kept"
        win2._save_state()
        saved_after_b = pc.load_json_file(video_state, {})
        video_b_key = next(key for key in saved_after_b["directories"] if key.endswith("videos_b"))

        win3 = make_window(state_root)
        win3._video_state_store = win3._load_mode_state("video")
        win3._prepare_restore_for_folder("video", video_a)
        assert win3.persisted_statuses["video001.mp4"] == "kept"
        win3._prepare_restore_for_folder("video", video_b)
        assert win3.persisted_statuses["video001.mp4"] == "skipped"
        assert win3.persisted_statuses["video002.mp4"] == "kept"

        # 场景三：已标记删除在确认前不会自动物理删除；确认后状态消失
        assert (video_a / "video004.mp4").exists()
        deleted_paths = [video_a / "video004.mp4", video_a / "video005.mp4"]
        pc.move_to_recycle_bin(deleted_paths)
        for path in deleted_paths:
            assert not path.exists()
        win4 = make_window(state_root)
        win4._mode = "video"
        win4._video_state_store = win4._load_mode_state("video")
        win4.current_folder = video_a
        win4.entries = [
            pc.VideoEntry(video_path=p, relative_path=p.relative_to(video_a))
            for p in sorted(video_a.glob("*.mp4"))
        ]
        win4.current_index = 3
        win4._save_state()
        saved_after_delete = pc.load_json_file(video_state, {})
        dir_state = saved_after_delete["directories"][str(video_a.resolve())]
        assert "video004.mp4" not in dir_state["file_states"]

        # 场景四：上次视频已不存在时，回退到最近待处理项
        win5 = make_window(state_root)
        win5._video_state_store = win5._load_mode_state("video")
        saved = win5._video_state_store
        saved_dir = saved["directories"][video_b_key]
        saved_dir["current_file"] = "video003.mp4"
        saved_dir["current_index"] = 2
        saved["directories"][video_b_key] = saved_dir
        pc.safe_write_json(video_state, saved)
        (video_b / "video003.mp4").unlink()

        win6 = make_window(state_root)
        win6._video_state_store = win6._load_mode_state("video")
        win6._prepare_restore_for_folder("video", video_b)
        win6._mode = "video"
        win6.current_folder = video_b
        win6.entries = [
            pc.VideoEntry(video_path=p, relative_path=p.relative_to(video_b))
            for p in sorted(video_b.glob("*.mp4"))
        ]
        for entry in win6.entries:
            rel = str(entry.relative_path)
            if rel in win6.persisted_statuses:
                entry.status = win6.persisted_statuses[rel]
        win6._restore_selection_after_scan()
        assert str(win6.entries[win6.current_index].relative_path) != "video003.mp4"

        # 场景五：照片与视频完全隔离
        win7 = make_window(state_root)
        win7._mode = "photo"
        win7.current_folder = photo_dir
        win7._photo_folder = photo_dir
        win7.entries = [
            pc.PhotoEntry(jpg_path=p, relative_path=p.relative_to(photo_dir))
            for p in sorted(photo_dir.glob("*.jpg"))
        ]
        win7.current_index = 1
        win7.entries[0].status = "kept"
        win7._save_state()

        photo_saved = pc.load_json_file(photo_state, {})
        video_saved = pc.load_json_file(video_state, {})
        assert str(photo_dir.resolve()) in photo_saved["directories"]
        assert str(video_a.resolve()) in video_saved["directories"]
        assert str(photo_dir.resolve()) not in video_saved["directories"]

        run_video_delete_worker_test(root)
        run_video_delete_finalize_test(root)

        pc.STATE_FILE = original_state
        pc.VIDEO_STATE_FILE = original_video_state
        pc.SETTINGS_FILE = original_settings
        print("state restore smoke test passed")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(_test_appdata, ignore_errors=True)


if __name__ == "__main__":
    run()
