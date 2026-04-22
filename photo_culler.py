import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
from collections import OrderedDict
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:
    Image = None
    ImageOps = None
    ImageTk = None


RAW_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".cr2",
    ".cr3",
    ".dcr",
    ".dng",
    ".erf",
    ".kdc",
    ".mos",
    ".mrw",
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".raf",
    ".raw",
    ".rw2",
    ".sr2",
    ".srf",
    ".x3f",
}
JPG_EXTENSIONS = {".jpg", ".jpeg"}

FO_DELETE = 3
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400
FOF_SILENT = 0x0004
DEFAULT_PREVIEW_CACHE_SIZE = 192
DEFAULT_PREVIEW_LOOKAHEAD = 20
MAX_RECENT_SESSIONS = 12


def get_settings_file() -> Path:
    if sys.platform == "win32":
        base_dir = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base_dir = Path.home() / ".local" / "state"
    settings_dir = base_dir / "PhotoCuller"
    settings_dir.mkdir(parents=True, exist_ok=True)
    return settings_dir / "settings.json"


def get_state_file() -> Path:
    if sys.platform == "win32":
        base_dir = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base_dir = Path.home() / ".local" / "state"
    state_dir = base_dir / "PhotoCuller"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "photo_culler_state.json"


STATE_FILE = get_state_file()
SETTINGS_FILE = get_settings_file()


def load_settings() -> dict[str, int]:
    settings = {
        "preview_cache_size": DEFAULT_PREVIEW_CACHE_SIZE,
        "preview_lookahead": DEFAULT_PREVIEW_LOOKAHEAD,
    }
    try:
        payload = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings

    cache_size = payload.get("preview_cache_size")
    lookahead = payload.get("preview_lookahead")
    if isinstance(cache_size, int):
        settings["preview_cache_size"] = max(24, min(cache_size, 2000))
    if isinstance(lookahead, int):
        settings["preview_lookahead"] = max(3, min(lookahead, 200))
    return settings


def save_settings(preview_cache_size: int, preview_lookahead: int) -> None:
    payload = {
        "preview_cache_size": max(24, min(preview_cache_size, 2000)),
        "preview_lookahead": max(3, min(preview_lookahead, 200)),
    }
    try:
        SETTINGS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def move_to_recycle_bin(paths: list[Path]) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if not existing:
        return

    # SHFileOperation 需要以双空字符结尾的路径列表。
    joined = "\0".join(existing) + "\0\0"
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = FO_DELETE
    operation.pFrom = joined
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0:
        raise OSError(f"系统回收站操作失败，错误代码：{result}")
    if operation.fAnyOperationsAborted:
        raise OSError("删除操作已中止")


@dataclass
class PhotoEntry:
    jpg_path: Path
    relative_path: Path
    raw_paths: list[Path] = field(default_factory=list)
    status: str = "pending"

    @property
    def stem(self) -> str:
        return self.jpg_path.stem

    @property
    def display_name(self) -> str:
        raw_suffix = f" | 原始文件 {len(self.raw_paths)} 个" if self.raw_paths else ""
        return f"{self.status_label()} {self.relative_path}{raw_suffix}"

    def status_label(self) -> str:
        labels = {
            "pending": "[待处理]",
            "kept": "[保留]",
            "deleted": "[-]",
            "skipped": "[跳过]",
        }
        return labels[self.status]

    def status_text(self) -> str:
        labels = {
            "pending": "待处理",
            "kept": "已保留",
            "deleted": "已标记删除",
            "skipped": "已跳过",
        }
        return labels[self.status]


class PhotoCullerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("照片筛选")
        self.root.geometry("1280x760")
        self.root.minsize(980, 620)

        self.current_folder: Path | None = None
        self.entries: list[PhotoEntry] = []
        self.current_index: int | None = None
        self.deleted_count = 0
        self.preview_image = None
        settings = load_settings()
        self.preview_cache_size = settings["preview_cache_size"]
        self.preview_lookahead = settings["preview_lookahead"]
        self.last_session: dict[str, object] = {"folder": None, "current_photo": None, "photo_statuses": {}}
        self.recent_sessions: list[dict[str, object]] = []
        self.preview_cache: OrderedDict[Path, Image.Image] = OrderedDict()
        self.preview_requests: queue.PriorityQueue[tuple[int, int, int, Path]] = queue.PriorityQueue()
        self.preview_results: queue.Queue[tuple[int, Path, Image.Image | None, str | None]] = queue.Queue()
        self.preview_request_id = 0
        self.preview_task_id = 0
        self.preview_queued_paths: set[Path] = set()
        self.preview_cache_lock = threading.Lock()
        self.preview_queue_lock = threading.Lock()
        self.preview_workers: list[threading.Thread] = []
        self.scan_requests: queue.Queue[tuple[int, Path]] = queue.Queue()
        self.scan_results: queue.Queue[tuple[int, Path, list[PhotoEntry], bool]] = queue.Queue()
        self.scan_request_id = 0
        self.is_scanning = False
        self.pending_restore_photo: str | None = None
        self.persisted_statuses: dict[str, str] = {}
        for _ in range(3):
            worker = threading.Thread(target=self._preview_worker_loop, daemon=True)
            worker.start()
            self.preview_workers.append(worker)
        self.scan_worker = threading.Thread(target=self._scan_worker_loop, daemon=True)
        self.scan_worker.start()

        self._build_ui()
        self._bind_shortcuts()
        self.root.after(0, self._restore_last_session)
        self.root.after(50, self._process_preview_results)
        self.root.after(50, self._process_scan_results)
        self._update_controls()

    def _build_ui(self) -> None:
        self.menu_bar = tk.Menu(self.root)
        self.file_menu = tk.Menu(self.menu_bar, tearoff=False)
        self.recent_menu = tk.Menu(self.file_menu, tearoff=False)
        self.file_menu.add_command(label="打开文件夹...", command=self.choose_folder)
        self.file_menu.add_cascade(label="最近打开的目录", menu=self.recent_menu)
        self.file_menu.add_command(label="预加载设置...", command=self.open_preload_settings)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="退出", command=self.root.destroy)
        self.menu_bar.add_cascade(label="文件", menu=self.file_menu)
        self.root.configure(menu=self.menu_bar)
        self._refresh_recent_menu()

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self.root, padding=(12, 12, 12, 8))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(1, weight=1)

        ttk.Button(toolbar, text="打开文件夹", command=self.choose_folder).grid(row=0, column=0, sticky="w")
        self.folder_var = tk.StringVar(value="尚未选择文件夹")
        ttk.Label(toolbar, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew", padx=(12, 0))

        main = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        main.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        left = ttk.Frame(main, padding=8)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        main.add(left, weight=1)

        ttk.Label(left, text="照片列表").grid(row=0, column=0, sticky="w", pady=(0, 8))

        list_frame = ttk.Frame(left)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.file_list = tk.Listbox(list_frame, activestyle="none")
        self.file_list.grid(row=0, column=0, sticky="nsew")
        self.file_list.bind("<<ListboxSelect>>", self.on_select)
        self.file_list.bind("<Up>", lambda _event: self._handle_arrow_key(-1))
        self.file_list.bind("<Down>", lambda _event: self._handle_arrow_key(1))
        self.file_list.bind("<Button-3>", self._show_file_list_context_menu)

        self.file_list_menu = tk.Menu(self.root, tearoff=False)
        self.file_list_menu.add_command(label="打开所在文件夹", command=self.open_current_folder)

        list_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_list.yview)
        list_scroll.grid(row=0, column=1, sticky="ns")
        self.file_list.configure(yscrollcommand=list_scroll.set)

        self.summary_var = tk.StringVar(value="共 0 项")
        ttk.Label(left, textvariable=self.summary_var).grid(row=2, column=0, sticky="w", pady=(8, 0))

        right = ttk.Frame(main, padding=8)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        main.add(right, weight=3)

        self.title_var = tk.StringVar(value="尚未选择图片")
        ttk.Label(right, textvariable=self.title_var, font=("Segoe UI", 12, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )

        preview_card = ttk.Frame(right, relief="solid", borderwidth=1, padding=8)
        preview_card.grid(row=1, column=0, sticky="nsew")
        preview_card.columnconfigure(0, weight=1)
        preview_card.rowconfigure(0, weight=1)

        self.preview_label = ttk.Label(preview_card, anchor="center", text="请选择一张照片进行预览")
        self.preview_label.grid(row=0, column=0, sticky="nsew")

        info_frame = ttk.Frame(right, padding=(0, 10, 0, 0))
        info_frame.grid(row=2, column=0, sticky="ew")
        info_frame.columnconfigure(0, weight=1)

        self.info_var = tk.StringVar(value="当前环境缺少图片预览依赖。")
        ttk.Label(info_frame, textvariable=self.info_var, justify="left").grid(row=0, column=0, sticky="w")

        actions = ttk.Frame(right, padding=(0, 12, 0, 0))
        actions.grid(row=3, column=0, sticky="ew")

        self.keep_button = ttk.Button(actions, text="保留（Enter）", command=self.keep_current)
        self.keep_button.grid(row=0, column=0, padx=(0, 8))
        self.delete_button = ttk.Button(actions, text="移到回收站（Del）", command=self.delete_current)
        self.delete_button.grid(row=0, column=1, padx=(0, 8))
        self.skip_button = ttk.Button(actions, text="跳过（S）", command=self.skip_current)
        self.skip_button.grid(row=0, column=2)
        self.restore_button = ttk.Button(actions, text="恢复（Z）", command=self.restore_current)
        self.restore_button.grid(row=0, column=3, padx=(8, 0))
        self.commit_delete_button = ttk.Button(actions, text="删除已标记", command=self.commit_marked_deletions)
        self.commit_delete_button.grid(row=0, column=4, padx=(8, 0))

        hint = ttk.Label(
            actions,
            text=(
                f"Delete 只做删除标记。当前预缓存 {self.preview_cache_size} 张，"
                f"向前预读 {self.preview_lookahead} 张。"
            ),
        )
        hint.grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))
        self.hint_label = hint

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Delete>", lambda _event: self.delete_current())
        self.root.bind("<Return>", lambda _event: self.keep_current())
        self.root.bind("<KP_Enter>", lambda _event: self.keep_current())
        self.root.bind("<Key-s>", lambda _event: self.skip_current())
        self.root.bind("<Key-S>", lambda _event: self.skip_current())
        self.root.bind("<Key-z>", lambda _event: self.restore_current())
        self.root.bind("<Key-Z>", lambda _event: self.restore_current())

    def choose_folder(self) -> None:
        initial_dir = str(self.current_folder) if self.current_folder else str(Path.home())
        folder = filedialog.askdirectory(initialdir=initial_dir, title="选择图片文件夹")
        if not folder:
            return
        self.pending_restore_photo = None
        self.load_folder(Path(folder))

    def load_folder(self, folder: Path) -> None:
        self.current_folder = folder
        self.folder_var.set(str(folder))
        self.entries = []
        self.current_index = None
        self.deleted_count = 0
        self.is_scanning = True
        with self.preview_cache_lock:
            self.preview_cache.clear()
        with self.preview_queue_lock:
            self.preview_queued_paths.clear()
        self.preview_request_id += 1
        self._refresh_list()
        self._clear_preview("正在扫描文件夹，请稍候...")
        self._update_summary()
        self._update_controls()
        self._save_state()
        self.scan_request_id += 1
        self.scan_requests.put((self.scan_request_id, folder))

    def _state_payload(self) -> dict[str, object]:
        current_photo = None
        if self.current_index is not None and 0 <= self.current_index < len(self.entries):
            current_photo = str(self.entries[self.current_index].relative_path)
        statuses = {
            str(entry.relative_path): entry.status
            for entry in self.entries
            if entry.status != "pending"
        }
        return {
            "folder": str(self.current_folder) if self.current_folder else None,
            "current_photo": current_photo,
            "photo_statuses": statuses,
        }

    def _save_state(self) -> None:
        payload = self._state_payload()
        self.last_session = payload
        self._upsert_recent_session(payload)
        self._refresh_recent_menu()
        try:
            STATE_FILE.write_text(
                json.dumps(
                    {
                        "last_session": self.last_session,
                        "recent_sessions": self.recent_sessions,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _restore_last_session(self) -> None:
        try:
            payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.last_session = {"folder": None, "current_photo": None, "photo_statuses": {}}
            self.recent_sessions = []
            self.persisted_statuses = {}
            self._refresh_recent_menu()
            return

        if isinstance(payload.get("recent_sessions"), list):
            self.recent_sessions = [item for item in payload["recent_sessions"] if isinstance(item, dict)]
        else:
            self.recent_sessions = []

        session_payload = payload.get("last_session") if isinstance(payload.get("last_session"), dict) else payload
        self.last_session = {
            "folder": session_payload.get("folder"),
            "current_photo": session_payload.get("current_photo"),
            "photo_statuses": session_payload.get("photo_statuses", {}),
        }
        self._upsert_recent_session(self.last_session)
        photo_statuses = self.last_session.get("photo_statuses")
        if isinstance(photo_statuses, dict):
            self.persisted_statuses = {
                str(path): str(status)
                for path, status in photo_statuses.items()
                if status in {"kept", "deleted", "skipped"}
            }
        else:
            self.persisted_statuses = {}
        self._refresh_recent_menu()

        folder_text = self.last_session.get("folder")
        if not folder_text:
            return

        folder = Path(folder_text)
        if not folder.is_dir():
            return

        self._open_recent_session_from_payload(self.last_session)

    def _upsert_recent_session(self, session: dict[str, object]) -> None:
        folder_text = session.get("folder")
        if not folder_text:
            return
        normalized: dict[str, object] = {
            "folder": str(folder_text),
            "current_photo": session.get("current_photo"),
            "photo_statuses": session.get("photo_statuses", {}),
        }
        self.recent_sessions = [
            item for item in self.recent_sessions if str(item.get("folder")) != normalized["folder"]
        ]
        self.recent_sessions.insert(0, normalized)
        self.recent_sessions = self.recent_sessions[:MAX_RECENT_SESSIONS]

    def _open_recent_folder(self, session: dict[str, object]) -> None:
        folder_text = session.get("folder")
        if not folder_text:
            return

        folder = Path(str(folder_text))
        if not folder.is_dir():
            messagebox.showerror("目录不存在", f"最近打开的目录不存在：\n\n{folder}")
            return

        self._open_recent_session_from_payload(session)

    def _open_recent_session_from_payload(self, session: dict[str, object]) -> None:
        photo_statuses = session.get("photo_statuses")
        if isinstance(photo_statuses, dict):
            self.persisted_statuses = {
                str(path): str(status)
                for path, status in photo_statuses.items()
                if status in {"kept", "deleted", "skipped"}
            }
        else:
            self.persisted_statuses = {}
        self._open_recent_session(Path(str(session["folder"])), session.get("current_photo"))

    def _open_recent_session(self, folder: Path, current_photo: str | None) -> None:
        self.pending_restore_photo = current_photo
        self.load_folder(folder)

    def _refresh_recent_menu(self) -> None:
        self.recent_menu.delete(0, tk.END)

        if not self.recent_sessions:
            self.recent_menu.add_command(label="暂无记录", state="disabled")
            return

        for session in self.recent_sessions:
            folder_text = session.get("folder")
            if not folder_text:
                continue
            current_photo = session.get("current_photo")
            folder = Path(str(folder_text))
            photo_label = current_photo if current_photo else "未定位到具体照片"
            label = f"{folder} | 上次到：{photo_label}"
            self.recent_menu.add_command(label=label, command=lambda s=session: self._open_recent_folder(s))

    def open_preload_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("预加载设置")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="缓存张数").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=(0, 8))
        cache_var = tk.StringVar(value=str(self.preview_cache_size))
        ttk.Entry(frame, textvariable=cache_var, width=12).grid(row=0, column=1, sticky="ew", pady=(0, 8))

        ttk.Label(frame, text="向前预读张数").grid(row=1, column=0, sticky="w", padx=(0, 12))
        lookahead_var = tk.StringVar(value=str(self.preview_lookahead))
        ttk.Entry(frame, textvariable=lookahead_var, width=12).grid(row=1, column=1, sticky="ew")

        ttk.Label(
            frame,
            text="建议：缓存 128-256，预读 12-30。更大更顺，但会更占内存。",
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

        button_bar = ttk.Frame(frame)
        button_bar.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))

        def apply_settings() -> None:
            try:
                preview_cache_size = int(cache_var.get())
                preview_lookahead = int(lookahead_var.get())
            except ValueError:
                messagebox.showerror("输入无效", "请输入整数。", parent=dialog)
                return

            self.preview_cache_size = max(24, min(preview_cache_size, 2000))
            self.preview_lookahead = max(3, min(preview_lookahead, 200))
            save_settings(self.preview_cache_size, self.preview_lookahead)
            self._update_hint_text()
            with self.preview_cache_lock:
                while len(self.preview_cache) > self.preview_cache_size:
                    self.preview_cache.popitem(last=False)
            self._queue_preview_prefetch()
            dialog.destroy()

        ttk.Button(button_bar, text="取消", command=dialog.destroy).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(button_bar, text="保存", command=apply_settings).grid(row=0, column=1)

    def _update_hint_text(self) -> None:
        self.hint_label.configure(
            text=(
                f"Delete 只做删除标记。当前预缓存 {self.preview_cache_size} 张，"
                f"向前预读 {self.preview_lookahead} 张。"
            )
        )

    def _scan_folder_batches(self, folder: Path) -> list[list[PhotoEntry]]:
        batches: list[list[PhotoEntry]] = []
        for root_text, dir_names, file_names in os.walk(folder):
            dir_names.sort(key=str.lower)
            file_names.sort(key=str.lower)

            root = Path(root_text)
            files = [root / file_name for file_name in file_names]

            raw_by_stem: dict[str, list[Path]] = {}
            for path in files:
                suffix = path.suffix.lower()
                if suffix in RAW_EXTENSIONS:
                    raw_by_stem.setdefault(path.stem.lower(), []).append(path)

            batch: list[PhotoEntry] = []
            for path in files:
                if path.suffix.lower() not in JPG_EXTENSIONS:
                    continue
                raw_paths = sorted(raw_by_stem.get(path.stem.lower(), []), key=lambda item: item.name.lower())
                batch.append(
                    PhotoEntry(
                        jpg_path=path,
                        relative_path=path.relative_to(folder),
                        raw_paths=raw_paths,
                        status=self.persisted_statuses.get(str(path.relative_to(folder)), "pending"),
                    )
                )

            if batch:
                batches.append(batch)
        return batches

    def _refresh_list(self) -> None:
        self.file_list.delete(0, tk.END)
        for index, entry in enumerate(self.entries):
            self.file_list.insert(tk.END, entry.display_name)
            self._style_list_row(index)

    def _update_list_row(self, index: int) -> None:
        self.file_list.delete(index)
        self.file_list.insert(index, self.entries[index].display_name)
        self._style_list_row(index)

    def _style_list_row(self, index: int) -> None:
        entry = self.entries[index]
        foreground = "#b91c1c" if entry.status == "deleted" else ""
        try:
            self.file_list.itemconfig(index, foreground=foreground)
        except tk.TclError:
            pass

    def _update_summary(self) -> None:
        total = len(self.entries)
        kept = sum(entry.status == "kept" for entry in self.entries)
        skipped = sum(entry.status == "skipped" for entry in self.entries)
        deleted = sum(entry.status == "deleted" for entry in self.entries)
        pending = total - kept - skipped - deleted
        if self.is_scanning:
            self.summary_var.set(f"正在扫描图片... 已加载 {total} 张")
            return
        self.summary_var.set(f"当前 {total} 张 | 待处理 {pending} 张 | 已保留 {kept} 张 | 已标记删除 {deleted} 张 | 已跳过 {skipped} 张")

    def on_select(self, _event=None) -> None:
        selection = self.file_list.curselection()
        if not selection:
            return
        self.current_index = selection[0]
        self._show_current()
        self._update_controls()
        self._save_state()

    def _show_current(self) -> None:
        if self.current_index is None or not self.entries:
            self._clear_preview("尚未选择照片")
            return

        entry = self.entries[self.current_index]
        self.title_var.set(str(entry.relative_path))

        info_lines = [
            f"路径：{entry.jpg_path}",
            f"状态：{entry.status_text()}",
            f"匹配到的原始文件：{len(entry.raw_paths)} 个",
        ]
        if entry.status == "deleted":
            info_lines.append("提示：这张照片已标记删除，点击“删除已标记”后会移到回收站。")
        if entry.raw_paths:
            info_lines.extend(f"  - {raw_path.name}" for raw_path in entry.raw_paths)
        self.info_var.set("\n".join(info_lines))

        if Image is None:
            self.preview_label.configure(text="当前环境缺少图片预览依赖，暂时无法显示预览。", image="")
            self.preview_image = None
            return

        cached = self._get_cached_preview(entry.jpg_path)
        if cached is not None:
            self.preview_image = ImageTk.PhotoImage(cached)
            self.preview_label.configure(image=self.preview_image, text="")
            self._queue_preview_prefetch()
            return

        self.preview_image = None
        self.preview_label.configure(text="正在加载预览...", image="")
        self.preview_request_id += 1
        self._enqueue_preview_request(entry.jpg_path, priority=0, request_id=self.preview_request_id, force=True)
        self._queue_preview_prefetch()

    def _clear_preview(self, message: str) -> None:
        self.title_var.set("尚未选择照片")
        if Image is None:
            self.info_var.set("当前环境缺少图片预览依赖。")
        else:
            self.info_var.set("请先打开文件夹，再选择一张照片。")
        self.preview_image = None
        self.preview_label.configure(text=message, image="")

    def _set_selection(self, index: int) -> None:
        self.file_list.selection_clear(0, tk.END)
        self.file_list.selection_set(index)
        self.file_list.activate(index)
        self.file_list.see(index)
        self.current_index = index
        self._show_current()
        self._save_state()

    def _move_selection(self, delta: int) -> None:
        if not self.entries:
            return
        if self.current_index is None:
            target = 0
        else:
            target = max(0, min(len(self.entries) - 1, self.current_index + delta))
        self._set_selection(target)
        self._update_controls()

    def _handle_arrow_key(self, delta: int) -> str:
        self._move_selection(delta)
        return "break"

    def _show_file_list_context_menu(self, event: tk.Event) -> str:
        if not self.entries:
            return "break"

        index = self.file_list.nearest(event.y)
        if index < 0 or index >= len(self.entries):
            return "break"

        self._set_selection(index)
        self._update_controls()
        self.file_list_menu.tk_popup(event.x_root, event.y_root)
        self.file_list_menu.grab_release()
        return "break"

    def open_current_folder(self) -> None:
        if self.current_index is None or not (0 <= self.current_index < len(self.entries)):
            return

        photo_path = self.entries[self.current_index].jpg_path
        folder = photo_path.parent
        if not folder.is_dir():
            messagebox.showerror("目录不存在", f"无法打开所在文件夹：\n\n{folder}")
            return

        try:
            subprocess.Popen(["explorer.exe", f"/select,{photo_path}"])
        except OSError as exc:
            messagebox.showerror("打开失败", f"无法在资源管理器中定位文件：\n\n{photo_path}\n\n{exc}")

    def _advance_to_next(self) -> None:
        if not self.entries:
            self.current_index = None
            self._clear_preview("当前文件夹中没有找到可处理的照片。")
            self._update_controls()
            return

        if self.current_index is None:
            start_index = -1
        else:
            start_index = self.current_index

        for index in range(start_index + 1, len(self.entries)):
            if self.entries[index].status != "deleted":
                self._set_selection(index)
                self._update_controls()
                return

        for index in range(0, len(self.entries)):
            if self.entries[index].status != "deleted":
                self._set_selection(index)
                self._update_controls()
                return

        self._set_selection(min(max(start_index, 0), len(self.entries) - 1))
        self._update_controls()

    def _mark_current(self, status: str) -> None:
        if self.current_index is None:
            return
        self.entries[self.current_index].status = status
        self._update_list_row(self.current_index)
        self._set_selection(self.current_index)
        self._update_summary()
        self._save_state()

    def keep_current(self) -> None:
        if self.current_index is None:
            return
        self._mark_current("kept")
        self._advance_to_next()

    def skip_current(self) -> None:
        if self.current_index is None:
            return
        self._mark_current("skipped")
        self._advance_to_next()

    def delete_current(self) -> None:
        if self.current_index is None:
            return

        entry = self.entries[self.current_index]
        if entry.status == "deleted":
            self._advance_to_next()
            return

        entry.status = "deleted"
        current_index = self.current_index
        self._update_list_row(current_index)
        self._update_summary()
        self._set_selection(current_index)
        self._advance_to_next()
        self._save_state()

    def restore_current(self) -> None:
        if self.current_index is None:
            return

        entry = self.entries[self.current_index]
        if entry.status != "deleted":
            return

        entry.status = "pending"
        self._update_list_row(self.current_index)
        self._set_selection(self.current_index)
        self._update_controls()
        self._update_summary()
        self._save_state()

    def _update_controls(self) -> None:
        state = "normal" if self.current_index is not None else "disabled"
        self.keep_button.configure(state=state)
        self.delete_button.configure(state=state)
        self.skip_button.configure(state=state)
        restore_state = (
            "normal"
            if self.current_index is not None and self.entries[self.current_index].status == "deleted"
            else "disabled"
        )
        self.restore_button.configure(state=restore_state)
        has_deleted = any(entry.status == "deleted" for entry in self.entries)
        self.commit_delete_button.configure(state="normal" if has_deleted else "disabled")

    def commit_marked_deletions(self) -> None:
        deleted_entries = [entry for entry in self.entries if entry.status == "deleted"]
        if not deleted_entries:
            return

        selected_path = None
        fallback_path = None
        if self.current_index is not None and 0 <= self.current_index < len(self.entries):
            selected_path = self.entries[self.current_index].jpg_path
            for index in range(self.current_index + 1, len(self.entries)):
                if self.entries[index].status != "deleted":
                    fallback_path = self.entries[index].jpg_path
                    break
            if fallback_path is None:
                for index in range(self.current_index - 1, -1, -1):
                    if self.entries[index].status != "deleted":
                        fallback_path = self.entries[index].jpg_path
                        break

        try:
            targets = [path for entry in deleted_entries for path in [entry.jpg_path, *entry.raw_paths]]
            move_to_recycle_bin(targets)
        except Exception as exc:
            messagebox.showerror("删除失败", f"无法将已标记文件移到回收站。\n\n{exc}")
            return

        self.entries = [entry for entry in self.entries if entry.status != "deleted"]
        self.persisted_statuses = {
            str(entry.relative_path): entry.status
            for entry in self.entries
            if entry.status != "pending"
        }
        if not self.entries:
            self.current_index = None
            self._refresh_list()
            self._clear_preview("列表中的照片已全部处理或删除。")
        else:
            self._refresh_list()
            target_path = selected_path if any(entry.jpg_path == selected_path for entry in self.entries) else fallback_path
            if target_path is not None:
                for index, entry in enumerate(self.entries):
                    if entry.jpg_path == target_path:
                        self._set_selection(index)
                        break
                else:
                    self._set_selection(0)
            else:
                self._set_selection(0)
        self._update_summary()
        self._update_controls()
        self._save_state()

    def _preview_worker_loop(self) -> None:
        while True:
            _priority, _task_id, request_id, path = self.preview_requests.get()
            with self.preview_queue_lock:
                self.preview_queued_paths.discard(path)

            cached = self._get_cached_preview(path)
            if cached is not None:
                self.preview_results.put((request_id, path, cached, None))
                continue

            image = None
            error = None
            try:
                with Image.open(path) as opened:
                    processed = ImageOps.exif_transpose(opened)
                    processed.thumbnail((780, 580))
                    image = processed.copy()
            except Exception as exc:
                error = str(exc)
            self.preview_results.put((request_id, path, image, error))

    def _scan_worker_loop(self) -> None:
        while True:
            request_id, folder = self.scan_requests.get()
            for batch in self._scan_folder_batches(folder):
                self.scan_results.put((request_id, folder, batch, False))
            self.scan_results.put((request_id, folder, [], True))

    def _process_preview_results(self) -> None:
        while True:
            try:
                request_id, path, image, error = self.preview_results.get_nowait()
            except queue.Empty:
                break

            if image is not None:
                self._store_cached_preview(path, image)

            current_entry = self.entries[self.current_index] if self.current_index is not None and self.entries else None
            if current_entry is None or current_entry.jpg_path != path:
                continue

            if image is not None:
                self.preview_image = ImageTk.PhotoImage(image)
                self.preview_label.configure(image=self.preview_image, text="")
            else:
                self.preview_image = None
                self.preview_label.configure(text=f"预览失败：\n{error}", image="")

        self.root.after(50, self._process_preview_results)

    def _process_scan_results(self) -> None:
        while True:
            try:
                request_id, folder, batch, done = self.scan_results.get_nowait()
            except queue.Empty:
                break

            if request_id != self.scan_request_id or self.current_folder != folder:
                continue

            if batch:
                start_index = len(self.entries)
                self.entries.extend(batch)
                for index, entry in enumerate(batch, start=start_index):
                    self.file_list.insert(tk.END, entry.display_name)
                    self._style_list_row(index)

                if self.pending_restore_photo:
                    for index in range(start_index, len(self.entries)):
                        if str(self.entries[index].relative_path) == self.pending_restore_photo:
                            self._set_selection(index)
                            self.pending_restore_photo = None
                            break

                if self.current_index is None and self.entries and self.pending_restore_photo is None:
                    self._set_selection(0)

                self._update_summary()
                self._update_controls()

            if done:
                self.is_scanning = False
                if not self.entries:
                    self._clear_preview("当前文件夹中没有找到可处理的照片。")
                elif self.current_index is None:
                    self._set_selection(0)
                self.pending_restore_photo = None
                self._update_summary()
                self._update_controls()
                self._save_state()

        self.root.after(50, self._process_scan_results)

    def _queue_preview_prefetch(self) -> None:
        if self.current_index is None or Image is None:
            return

        offsets = list(range(1, self.preview_lookahead + 1))
        offsets.extend((-1, -2, -3))
        for offset in offsets:
            index = self.current_index + offset
            if 0 <= index < len(self.entries):
                path = self.entries[index].jpg_path
                self._enqueue_preview_request(path, priority=10 + abs(offset), request_id=0)

    def _enqueue_preview_request(self, path: Path, priority: int, request_id: int, force: bool = False) -> None:
        if self._get_cached_preview(path) is not None:
            return

        with self.preview_queue_lock:
            if path in self.preview_queued_paths and not force:
                return
            self.preview_task_id += 1
            self.preview_queued_paths.add(path)
            self.preview_requests.put((priority, self.preview_task_id, request_id, path))

    def _get_cached_preview(self, path: Path) -> Image.Image | None:
        with self.preview_cache_lock:
            cached = self.preview_cache.get(path)
            if cached is None:
                return None
            self.preview_cache.move_to_end(path)
            return cached

    def _store_cached_preview(self, path: Path, image: Image.Image) -> None:
        with self.preview_cache_lock:
            self.preview_cache[path] = image
            self.preview_cache.move_to_end(path)
            while len(self.preview_cache) > self.preview_cache_size:
                self.preview_cache.popitem(last=False)


def main() -> None:
    root = tk.Tk()
    app = PhotoCullerApp(root)
    if Image is None:
        messagebox.showwarning(
            "缺少图片预览依赖",
            "当前环境缺少图片预览依赖，暂时无法显示照片预览。",
        )
    app._update_controls()
    root.mainloop()


if __name__ == "__main__":
    main()
