"""视频切换逻辑单元测试 — 使用 FakeVideoPlayer 验证状态机。

用法: pytest test_video_switch_logic.py -v
"""

import sys
import time
import logging
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

# ── Fake QTimer ─────────────────────────────────────

class FakeTimer:
    """不依赖 Qt 的假定时器，按需触发。"""
    def __init__(self, parent=None):
        self._interval = 80
        self._active = False
        self._cb = None

    def setInterval(self, ms):
        self._interval = ms

    def timeout(self):
        class Signal:
            def connect(s, cb):
                self._cb = cb
            def disconnect(s):
                self._cb = None
        return Signal()

    def start(self):
        self._active = True

    def stop(self):
        self._active = False

    def fire(self):
        """手动触发回调。"""
        if self._cb:
            self._cb()


class FakeMediaPlayer:
    def __init__(self):
        self._status = "NoMedia"
        self._source = ""
        self._set_source_calls = []

    def stop(self):
        self._status = "NoMedia"

    def setSource(self, url):
        self._source = str(url)
        self._set_source_calls.append(str(url))
        self._status = "LoadedMedia"

    def mediaStatus(self):
        return self._status

    def set_media_status(self, s):
        self._status = s


class FakePlayerWidget:
    def __init__(self):
        self.player = FakeMediaPlayer()
        self._loaded_paths = []
        self._pos = 0

    def stop(self):
        self.player.stop()

    def load(self, path, original_path=None):
        self.player.setSource(path)
        self._loaded_paths.append(path)

    def current_media_status(self):
        return self.player.mediaStatus()

    def current_media_path(self):
        return self.player._source

    def prepare_restore(self, pos):
        self._pos = pos


# ── 提取的可测试状态机 ──────────────────────────────

class VideoSwitchState:
    """从 PhotoCullerWindow 提取的视频切换状态机核心逻辑。"""
    def __init__(self, player_widget):
        self._video_switch_id = 0
        self._video_load_state = "idle"
        self._video_load_timer = FakeTimer()
        self._video_pending_source = None
        self._video_pending_entry = None
        self._video_stop_started_at = 0.0
        self._video_widget = player_widget
        self._lbl_info = None
        self.pending_restore_video_position = 0

    def _load_video_source(self, entry, source_path, using_proxy=False):
        if self._video_load_state == "stopping":
            self._video_pending_source = source_path
            self._video_pending_entry = entry
            return
        self._video_load_state = "stopping"
        self._video_pending_source = source_path
        self._video_pending_entry = entry
        self._video_widget.stop()
        self._video_stop_started_at = time.monotonic()
        self._start_load_poll()

    def _start_load_poll(self):
        timer = self._video_load_timer
        timer.timeout().disconnect()
        current_switch = self._video_switch_id
        timer.timeout().connect(lambda sid=current_switch: self._poll_for_load_ready(sid))
        timer.start()

    def _poll_for_load_ready(self, switch_id):
        if self._video_load_state != "stopping":
            self._video_load_timer.stop()
            return
        if switch_id != self._video_switch_id:
            self._video_load_timer.stop()
            return  # stale: never modify state
        status = self._video_widget.current_media_status()
        elapsed = time.monotonic() - self._video_stop_started_at
        if status in ("NoMedia", "LoadedMedia"):
            self._video_load_timer.stop()
            self._do_load_pending()
        elif elapsed > 3.0:
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
        self._video_widget.load(str(source_path), str(entry.video_path))
        self._video_load_state = "playing"


class TestVideoSwitchStateMachine:
    @pytest.fixture
    def state(self):
        pw = FakePlayerWidget()
        return VideoSwitchState(pw)

    def test_single_switch(self, state):
        entry = type("E", (), {"video_path": Path("/v/A.mp4")})()
        state._video_switch_id = 1
        state._load_video_source(entry, Path("/v/A.mp4"))
        assert state._video_load_state == "stopping"
        state._video_widget.player.set_media_status("NoMedia")
        state._video_load_timer.fire()
        assert state._video_load_state in ("playing", "loading")
        assert len(state._video_widget._loaded_paths) == 1

    def test_stale_poll_no_modify(self, state):
        state._video_load_state = "stopping"
        state._video_switch_id = 5
        old = state._video_load_state
        state._poll_for_load_ready(3)
        assert state._video_load_state == old  # 未修改

    def test_stopping_queues(self, state):
        ea = type("E", (), {"video_path": Path("/v/A.mp4")})()
        eb = type("E", (), {"video_path": Path("/v/B.mp4")})()
        state._video_switch_id = 1
        state._load_video_source(ea, Path("/v/A.mp4"))
        assert state._video_load_state == "stopping"
        state._load_video_source(eb, Path("/v/B.mp4"))
        assert state._video_pending_source is not None
        assert state._video_load_state == "stopping"

    def test_no_concurrent_setsource(self, state):
        state._video_switch_id = 1
        state._load_video_source(type("E", (), {"video_path": Path("/v/A.mp4")})(), Path("/v/A.mp4"))
        state._video_widget.player.set_media_status("NoMedia")
        state._video_load_timer.fire()
        assert len(state._video_widget.player._set_source_calls) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
