"""视频切换压力测试 — 真实 QMediaPlayer，子进程，超时卡死检测。"""

import argparse, json, os, subprocess, sys, tempfile, time
from pathlib import Path

STRESS_SCRIPT = r"""
import json, os, sys, tempfile, time
from pathlib import Path
os.environ["QT_QPA_PLATFORM"]="offscreen"
os.environ["QT_MEDIA_BACKEND"]="ffmpeg"
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtMultimedia import QMediaPlayer

def _make_test_video(path, label):
    if path.exists(): return
    import subprocess
    subprocess.run(["ffmpeg","-y","-f","lavfi","-i","color=c=black:s=320x240:d=2",
        "-c:v","libx264","-pix_fmt","yuv420p","-an",str(path)],
        capture_output=True, timeout=20)

class StressTester:
    def __init__(self, rds):
        self.rounds = rds
        self._sid = 0
        self._loaded = 0
        self._errors = []
        self._last_beat = time.monotonic()
        self._player = QMediaPlayer()
        self._state = "idle"
        self._poll = QTimer()
        self._poll.setInterval(80)
        self._poll.timeout.connect(self._on_poll)
        self._pending = None
        self._stop_at = 0.0
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(lambda e,m: self._errors.append(f"e={e} m={m}"))

    def _on_status(self, s):
        self._last_beat = time.monotonic()
        if s == QMediaPlayer.MediaStatus.NoMedia and self._state == "stopping":
            self._do_load()

    def switch_to(self, path, sid):
        self._last_beat = time.monotonic()
        self._sid = sid
        if self._state == "stopping":
            self._pending = path; return
        self._state = "stopping"
        self._pending = path
        self._player.stop()
        self._stop_at = time.monotonic()
        self._poll.start()

    def _on_poll(self):
        self._last_beat = time.monotonic()  # tick every poll
        if self._state != "stopping": self._poll.stop(); return
        s = self._player.mediaStatus()
        if s in (QMediaPlayer.MediaStatus.NoMedia, QMediaPlayer.MediaStatus.LoadedMedia):
            self._poll.stop(); self._do_load()
        elif time.monotonic() - self._stop_at > 3.0:
            self._poll.stop(); self._do_load()

    def _do_load(self):
        src = self._pending
        if src is None: self._state = "idle"; return
        self._pending = None; self._state = "loading"
        self._player.setSource(src); self._loaded += 1; self._state = "playing"

    def heartbeat_ok(self):
        return (time.monotonic() - self._last_beat) < 2.0

    def run(self):
        tmp = Path(tempfile.gettempdir()) / "pc_stress"
        tmp.mkdir(exist_ok=True)
        vids = {}
        for lb in ["A","B","C","D"]:
            p = tmp / f"t_{lb}.mp4"
            _make_test_video(p, lb)
            vids[lb] = str(p.resolve())

        seq = [("A","B","A")] * self.rounds + [("A","B","C","D")] * self.rounds
        sid = 0
        for batch in seq:
            for lb in batch:
                sid += 1
                self.switch_to(vids[lb], sid)
                time.sleep(0.1)
                QApplication.processEvents()
                if not self.heartbeat_ok():
                    return {"p":False,"e":f"heartbeat: dead at switch {sid}","n":self._loaded}
        return {"p":True,"e":"","n":self._loaded,"r":self.rounds,"errs":len(self._errors)}

def main():
    rds = int(sys.argv[1]) if len(sys.argv)>1 else 30
    app = QApplication(sys.argv)
    t = StressTester(rds)
    result = t.run()
    app.quit()
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["p"] else 1)

if __name__=="__main__": main()
"""

def run_stress(rounds=30, timeout=90):
    sp = Path(tempfile.gettempdir()) / "_pc_stress.py"
    sp.write_text(STRESS_SCRIPT, encoding="utf-8")
    py = r"E:\python-envs\photo-culler-qt\Scripts\python.exe"
    p = subprocess.Popen([py, str(sp), str(rounds)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env={**os.environ, "QT_QPA_PLATFORM":"offscreen"})
    try:
        out, err = p.communicate(timeout=timeout)
        r = json.loads(out.strip()) if out.strip() else {}
        return p.returncode==0 and r.get("p",False), r, err
    except subprocess.TimeoutExpired:
        p.kill(); p.wait()
        return False, {"p":False,"e":f"timeout {timeout}s"},""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds",type=int,default=30)
    ap.add_argument("--timeout",type=int,default=90)
    a = ap.parse_args()
    print(f"Stress test: {a.rounds} rounds, {a.timeout}s timeout")
    ok, r, err = run_stress(a.rounds, a.timeout)
    print(f"  Passed: {ok}")
    print(f"  Switches: {r.get('n','?')}, Errors: {r.get('errs','?')}")
    if not ok: print(f"  FAIL: {r.get('e','?')}")
    sys.exit(0 if ok else 1)

if __name__=="__main__": main()
