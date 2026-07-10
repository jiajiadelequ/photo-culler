"""真实 GUI 视频切换压力测试 — 启动 PhotoCullerQt 主窗口，模拟列表选择。

用法: python gui_video_stress_test.py --rounds 30
"""

import argparse, json, os, sys, tempfile, time, subprocess
from pathlib import Path

def _make_test_videos():
    tmp = Path(tempfile.gettempdir()) / "pc_gui_stress"
    tmp.mkdir(exist_ok=True)
    for lb in ["A","B","C","D"]:
        p = tmp / f"video_{lb}.mp4"
        if not p.exists():
            subprocess.run(["ffmpeg","-y","-f","lavfi","-i","color=c=black:s=320x240:d=2",
                "-c:v","libx264","-pix_fmt","yuv420p","-an",str(p)],
                capture_output=True, timeout=20)
    return tmp

GUI_SCRIPT = r"""
import json, os, sys, time, logging
from pathlib import Path
os.environ.pop('QT_QPA_PLATFORM', None)
from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtMultimedia import QMediaPlayer

# 设置日志
LOG_FILE = Path(os.environ.get('LOCALAPPDATA','')) / 'PhotoCuller' / 'logs' / 'gui_stress_test.log'
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('gui_stress')

# 导入主窗口
sys.path.insert(0, r'E:\picture_tool')
from photo_culler_qt import PhotoCullerWindow, QApplication as QA

TEST_DIR = sys.argv[1]
ROUNDS = int(sys.argv[2])

app = QA(sys.argv)
window = PhotoCullerWindow()
window.resize(1024, 600)
window.show()
app.processEvents()
time.sleep(0.5)

# 切换到视频模式
window._switch_mode("video")
app.processEvents()
time.sleep(0.5)

# 打开测试目录
folder = Path(TEST_DIR)
window.current_folder = folder
window._lbl_folder.setText(str(folder))
window.pending_restore_photo = None
window.pending_restore_video = None
window.entries.clear()
window.current_index = None
window._file_list.clear()
window._image_view.clear_image()
window._video_widget.stop()
window._lbl_summary.setText("loading...")
window._scan_folder(folder)
logger.info("Opened folder: %s", folder)

# 等待扫描完成
for i in range(80):
    app.processEvents()
    time.sleep(0.15)
    logger.info("Wait %d: scanning=%s entries=%d", i, window.is_scanning, len(window.entries))
    if not window.is_scanning and window.entries:
        break

if not window.entries:
    print(json.dumps({"p":False,"e":"no entries after scan","switches":0}))
    window.close()
    sys.exit(1)

logger.info("Scanned %d entries", len(window.entries))

# 记录所有 setSource 调用
orig_load = window._video_widget.load
set_source_log = []
load_count = [0]

def tracing_load(src, orig_path=None):
    load_count[0] += 1
    set_source_log.append({"src": str(src), "orig": str(orig_path) if orig_path else "", "time": time.monotonic()})
    return orig_load(src, orig_path)

window._video_widget.load = tracing_load

# 记录播放器状态变化
errors_log = []
orig_error = window._video_widget._on_error
def tracing_error(*args):
    errors_log.append({"args": [str(a) for a in args], "source": window._video_widget._source_path, "time": time.monotonic()})
    return orig_error(*args)
window._video_widget._on_error = tracing_error

heartbeat_active = [True]
last_heartbeat = [time.monotonic()]
def heartbeat_tick():
    last_heartbeat[0] = time.monotonic()

heartbeat_timer = QTimer()
heartbeat_timer.setInterval(500)
heartbeat_timer.timeout.connect(lambda: (
    heartbeat_tick(),
    setattr(heartbeat_active, '__bool__', lambda: (time.monotonic() - last_heartbeat[0] < 3.0))
))
heartbeat_timer.start()

# 执行切换序列
seq_a_b_a = [("A","B","A")] * ROUNDS
seq_a_b_c_d = [("A","B","C","D")] * ROUNDS

vid_map = {}
for i, entry in enumerate(window.entries):
    name = entry.relative_path.name
    for lb in ["A","B","C","D"]:
        if f"video_{lb}" in name:
            vid_map[lb] = i
            break

if len(vid_map) < 4:
    print(json.dumps({"p":False,"e":f"only {len(vid_map)} videos found, need 4","switches":0}))
    window.close()
    sys.exit(1)

switch_log = []
switch_count = 0

def do_switch(lb, seq_num):
    global switch_count
    idx = vid_map[lb]
    switch_count += 1
    # 通过 setCurrentRow 模拟真实列表点击
    window._file_list.setCurrentRow(idx)
    switch_log.append({"seq": seq_num, "label": lb, "idx": idx, "time": time.monotonic()})

def run_sequence(name, sequence):
    seq_num = 0
    for batch in sequence:
        for lb in batch:
            seq_num += 1
            do_switch(lb, seq_num)
            time.sleep(0.08)
            app.processEvents()
            if time.monotonic() - last_heartbeat[0] > 3.0:
                return False

start = time.monotonic()
ok1 = run_sequence("ABA", seq_a_b_a)
if not ok1:
    print(json.dumps({"p":False,"e":"heartbeat dead during ABA","switches":switch_count}))
    window.close()
    sys.exit(1)

ok2 = run_sequence("ABCD", seq_a_b_c_d)
elapsed = time.monotonic() - start

# 最终检查
time.sleep(0.5)
app.processEvents()
final_source = window._video_widget._player.source().toString()
last_label = switch_log[-1]["label"] if switch_log else "?"

# 验证最终 source 匹配最后选择的视频
last_idx = vid_map[last_label]
last_entry = window.entries[last_idx]
expected_name = last_entry.relative_path.name

# 检查 heartbeat
hb_ok = (time.monotonic() - last_heartbeat[0]) < 3.0

result = {
    "p": ok1 and ok2 and hb_ok,
    "switches": switch_count,
    "set_sources": load_count[0],
    "errors": len(errors_log),
    "error_details": [{k:v for k,v in e.items() if k != 'time'} for e in errors_log],
    "elapsed_s": round(elapsed, 1),
    "final_source": final_source[-60:],
    "final_label": last_label,
    "heartbeat_ok": hb_ok,
    "entries_count": len(window.entries),
    "last_5_sources": [s["src"][-60:] for s in set_source_log[-5:]],
}

logger.info("Test complete: %s", result)
window.close()
app.quit()
print(json.dumps(result, ensure_ascii=False))
sys.exit(0 if result["p"] else 1)
"""

def run_gui_test(rounds=30, timeout=120):
    test_dir = _make_test_videos()
    sp = Path(tempfile.gettempdir()) / "_pc_gui_stress.py"
    sp.write_text(GUI_SCRIPT, encoding="utf-8")
    
    py = r"E:\python-envs\photo-culler-qt\Scripts\python.exe"
    p = subprocess.Popen([py, str(sp), str(test_dir), str(rounds)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(timeout=timeout)
        r = json.loads(out.strip()) if out.strip() else {}
        return p.returncode == 0 and r.get("p", False), r, err
    except subprocess.TimeoutExpired:
        p.kill(); p.wait()
        return False, {"p": False, "e": f"timeout {timeout}s"}, ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args()
    print(f"GUI stress test: {a.rounds} rounds, {a.timeout}s timeout")
    ok, r, err = run_gui_test(a.rounds, a.timeout)
    print(f"  Passed: {ok}")
    print(f"  Switches: {r.get('switches','?')}, setSources: {r.get('set_sources','?')}, Errors: {r.get('errors','?')}")
    if r.get('error_details'):
        for d in r['error_details'][:3]:
            print(f"    Error: {d}")
    print(f"  Heartbeat OK: {r.get('heartbeat_ok','?')}")
    print(f"  Final label: {r.get('final_label','?')}")
    if not ok:
        print(f"  FAIL: {r.get('e','?')}")
    if err.strip():
        print(f"  STDERR (first 300): {err[:300]}")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
