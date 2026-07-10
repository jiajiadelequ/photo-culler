"""真实 GUI 视频切换压力测试 — app.exec() + QTimer。"""

import argparse, json, os, subprocess, sys, time
from pathlib import Path

GUI_SCRIPT = r"""
import json, os, sys, time, logging
from pathlib import Path
os.environ.pop('QT_QPA_PLATFORM', None)
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
LOG_FILE = Path(os.environ.get('LOCALAPPDATA','')) / 'PhotoCuller' / 'logs' / 'real_gui.log'
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s')
L = logging.getLogger('gui')
sys.path.insert(0, r'E:\picture_tool')
from photo_culler_qt import PhotoCullerWindow

TEST_DIR = Path(sys.argv[1]); ROUNDS = int(sys.argv[2])

app = QApplication(sys.argv)
window = PhotoCullerWindow()
window._switch_mode("video")
window.current_folder = TEST_DIR
window._lbl_folder.setText(str(TEST_DIR))
window.entries.clear(); window.current_index = None
window._file_list.clear()
window._scan_folder(TEST_DIR)
L.info("Scan:%s", TEST_DIR)

stats = {"sel":0, "db":0, "src":0, "play":0, "err":0, "errs":[]}
orig_load = window._video_widget.load
def tl(src, op=None): stats["src"]+=1; return orig_load(src, op)
window._video_widget.load = tl
orig_ds = window._video_do_switch
def tds(sid): stats["db"]+=1; return orig_ds(sid)
window._video_do_switch = tds
orig_er = window._video_widget._on_error
def ter(*a): stats["err"]+=1; stats["errs"].append([str(x) for x in a]); return orig_er(*a)
window._video_widget._on_error = ter

last_hb = time.monotonic()
hb = QTimer(); hb.setInterval(500); hb.timeout.connect(lambda: globals().update(last_hb=time.monotonic())); hb.start()

def check_scan():
    if len(window.entries) >= 4:
        L.info("Scan OK: %d entries", len(window.entries))
        QTimer.singleShot(200, run_test)
        return
    if time.monotonic() - t0 > 15:
        print(json.dumps({"p":False,"e":"scan timeout","n":len(window.entries)}))
        app.quit(); return
    QTimer.singleShot(300, check_scan)

def run_test():
    seqs = []
    for _ in range(ROUNDS):
        for i in [0,1,0]: seqs.append(i)  # A→B→A
    for _ in range(ROUNDS):
        for i in [0,1,2,3]: seqs.append(i)  # A→B→C→D
    
    total = len(seqs)
    L.info("Start: %d steps", total)
    step = [0]
    stuck = [window._video_load_state, time.monotonic()]
    
    def do_step():
        if step[0] >= total:
            # Done
            fs = window._video_widget._player.source().toString()
            result = {"p":True,"sel":stats["sel"],"db":stats["db"],"src":stats["src"],
                      "play":stats["play"],"err":stats["err"],"errs":stats["errs"],
                      "final_src":Path(fs).name if fs else "none",
                      "hb_ok":(time.monotonic()-last_hb)<3.0}
            print(json.dumps(result, ensure_ascii=False))
            L.info("DONE: %s", result)
            app.quit(); return
        
        # Check stuck
        s = window._video_load_state
        if s != stuck[0]:
            stuck[0] = s; stuck[1] = time.monotonic()
        elif s in ("stopping","loading") and time.monotonic()-stuck[1] > 5:
            print(json.dumps({"p":False,"e":f"stuck {s}","step":step[0]}))
            app.quit(); return
        
        idx = seqs[step[0]]
        step[0] += 1
        stats["sel"] += 1
        window._file_list.setCurrentRow(idx)
        QTimer.singleShot(80, do_step)
    
    do_step()

t0 = time.monotonic()
QTimer.singleShot(100, check_scan)
app.exec()
"""

def run(rounds=30, timeout=120):
    d = r"E:\CameraTextures\2025-11-29\videos"
    sp = Path(os.environ.get("TEMP","/tmp")) / "_real_gui_test.py"
    sp.write_text(GUI_SCRIPT, encoding="utf-8")
    py = r"E:\python-envs\photo-culler-qt\Scripts\python.exe"
    p = subprocess.Popen([py, str(sp), d, str(rounds)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
    ap.add_argument("--timeout",type=int,default=120)
    a = ap.parse_args()
    print(f"REAL GUI test: {a.rounds} rounds, {a.timeout}s timeout")
    ok, r, err = run(a.rounds, a.timeout)
    print(f"  Passed: {ok}")
    print(f"  Sel:{r.get('sel','?')} DB:{r.get('db','?')} Src:{r.get('src','?')} Play:{r.get('play','?')} Err:{r.get('err','?')}")
    if r.get('errs'):
        for e in r['errs'][:3]: print(f"    E: {e}")
    print(f"  Final: {r.get('final_src','?')}")
    if not ok: print(f"  FAIL: {r.get('e','?')}")
    sys.exit(0 if ok else 1)

if __name__=="__main__": main()
