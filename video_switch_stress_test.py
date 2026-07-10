"""视频切换压力测试 — 真实 QMediaPlayer，子进程运行，超时卡死检测。

用法:
    python video_switch_stress_test.py          # 默认 100 轮
    python video_switch_stress_test.py --rounds 200

父进程设置 90s 超时，子进程超时未退出则判定卡死。
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STRESS_SCRIPT = r"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_MEDIA_BACKEND"] = "ffmpeg"

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtMultimedia import QMediaPlayer

# 生成短测试视频（3s 黑屏）
def _make_test_video(path: Path, label: str):
    if path.exists():
        return
    import subprocess
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", "color=c=black:s=320x240:d=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-an", str(path),
    ]
    subprocess.run(cmd, capture_output=True, timeout=30)


class HeartbeatMonitor:
    def __init__(self, max_gap=2.0):
        self._last = time.monotonic()
        self._max_gap = max_gap
        self._dead = False

    def tick(self):
        self._last = time.monotonic()

    def check(self):
        return (time.monotonic() - self._last) < self._max_gap


class VideoSwitchStressTester:
    def __init__(self, rounds):
        self._rounds = rounds
        self._switch_id = 0
        self._loaded_count = 0
        self._stale_count = 0
        self._errors = []
        self._heartbeat = HeartbeatMonitor()
        self._player = QMediaPlayer()
        self._expected_source = None
        self._last_target = None
        self._load_state = "idle"
        self._poll_timer = QTimer()
        self._poll_timer.setInterval(80)
        self._poll_timer.timeout.connect(self._poll)
        self._pending_entry = None
        self._pending_source = None
        self._stop_started_at = 0.0

        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_error)

    def _on_media_status(self, status):
        self._heartbeat.tick()
        if status == QMediaPlayer.MediaStatus.NoMedia:
            if self._load_state == "stopping":
                self._do_load()

    def _on_error(self, error, error_string):
        self._heartbeat.tick()
        self._errors.append(f"error={error} msg={error_string}")

    def switch_to(self, path: str, switch_id: int):
        self._switch_id = switch_id
        self._last_target = path
        if self._load_state == "stopping":
            self._pending_source = path
            return
        self._load_state = "stopping"
        self._pending_source = path
        self._player.stop()
        self._stop_started_at = time.monotonic()
        self._poll_timer.start()

    def _poll(self):
        if self._load_state != "stopping":
            self._poll_timer.stop()
            return
        status = self._player.mediaStatus()
        if status in (QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.LoadedMedia):
            self._poll_timer.stop()
            self._do_load()
        elif time.monotonic() - self._stop_started_at > 3.0:
            self._poll_timer.stop()
            self._do_load()

    def _do_load(self):
        source = self._pending_source
        if source is None:
            self._load_state = "idle"
            return
        self._pending_source = None
        self._load_state = "loading"
        self._player.setSource(source)
        self._expected_source = source
        self._loaded_count += 1
        self._load_state = "playing"

    def run(self):
        tmp = Path(tempfile.gettempdir()) / "photo_culler_stress_test"
        tmp.mkdir(exist_ok=True)
        videos = {}
        for label in ["A", "B", "C", "D"]:
            p = tmp / f"test_{label}.mp4"
            _make_test_video(p, label)
            videos[label] = str(p.resolve())

        seq_a_b_a = [("A", "B", "A")] * self._rounds
        seq_seq = [("A", "B", "C", "D")] * self._rounds

        def run_sequence(name, sequence):
            sid = 0
            for batch in sequence:
                for label in batch:
                    sid += 1
                    path = videos[label]
                    self.switch_to(path, sid)
                    if not self._heartbeat.check():
                        return False, f"{name}: heartbeat dead after {sid} switches"
                    QApplication.processEvents()
            return True, None

        start = time.monotonic()
        ok, err = run_sequence("A->B->A", seq_a_b_a)
        if not ok:
            return {"passed": False, "error": str(err), "switches_a_b_a": self._loaded_count}

        ok2, err2 = run_sequence("A->B->C->D", seq_seq)
        elapsed = time.monotonic() - start
        return {
            "passed": ok and ok2,
            "error": str(err or err2 or ""),
            "switches_total": self._loaded_count,
            "rounds": self._rounds,
            "elapsed_s": round(elapsed, 1),
            "errors_emitted": len(self._errors),
            "stale_ignored": self._stale_count,
        }


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    app = QApplication(sys.argv)
    tester = VideoSwitchStressTester(rounds)
    result = tester.run()
    app.quit()
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
"""


def run_stress_test(rounds=100, timeout=90):
    """在子进程中运行压力测试，返回 (passed, result_dict)。"""
    script_path = Path(tempfile.gettempdir()) / "_photo_culler_stress_script.py"
    script_path.write_text(STRESS_SCRIPT, encoding="utf-8")
    
    python = r"E:\python-envs\photo-culler-qt\Scripts\python.exe"
    proc = subprocess.Popen(
        [python, str(script_path), str(rounds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        result = json.loads(stdout.strip()) if stdout.strip() else {}
        return proc.returncode == 0 and result.get("passed", False), result, stderr
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return False, {"passed": False, "error": f"Timeout after {timeout}s — likely deadlocked"}, ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()

    print(f"Video switch stress test: {args.rounds} rounds, {args.timeout}s timeout")
    t0 = time.monotonic()
    passed, result, stderr = run_stress_test(args.rounds, args.timeout)
    elapsed = time.monotonic() - t0
    
    print(f"  Passed: {passed}")
    print(f"  Switches: {result.get('switches_total', 'N/A')}")
    print(f"  Errors: {result.get('errors_emitted', 'N/A')}")
    print(f"  Elapsed: {result.get('elapsed_s', round(elapsed, 1))}s")
    if not passed:
        print(f"  FAIL REASON: {result.get('error', 'unknown')}")
        if stderr.strip():
            print(f"  STDERR: {stderr[:500]}")
    print()
    
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
