#!/usr/bin/env python3
"""照片筛选 — PySide6 / QGraphicsView 版本。

实现了与 Tkinter 版本完全相同的功能：
- 打开图片文件夹，浏览 JPG 照片
- 自动匹配同目录 RAW 文件
- Ctrl+滚轮缩放（QGraphicsView transform，零成本）
- 鼠标拖拽平移
- 保留 / 删除 / 跳过 / 恢复 标记
- 快捷键、菜单、批量操作
- 会话持久化
"""

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import (
    QAction, QKeySequence, QPixmap, QImage,
    QWheelEvent, QMouseEvent, QFont,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QGraphicsView, QGraphicsScene,
    QGraphicsPixmapItem, QPushButton, QLabel, QMenuBar, QMenu,
    QToolBar, QStatusBar, QFileDialog, QMessageBox, QFrame, QSizePolicy,
)

# ─── 常量（与 Tkinter 版本一致）─────────────────────────────

RAW_EXTENSIONS = {
    ".3fr", ".arw", ".cr2", ".cr3", ".dcr", ".dng", ".erf", ".kdc",
    ".mos", ".mrw", ".nef", ".nrw", ".orf", ".pef", ".raf", ".raw",
    ".rw2", ".sr2", ".srf", ".x3f",
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
MAX_PREVIEW_SOURCE_EDGE = 4096
ZOOM_STEP = 1.15
ZOOM_MIN = 0.01
ZOOM_MAX = 10.0

# ─── 设置/状态文件路径（与 Tkinter 版本共用）───────────────

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


def load_settings() -> dict[str, int]:
    defaults = {"preview_cache_size": DEFAULT_PREVIEW_CACHE_SIZE,
                "preview_lookahead": DEFAULT_PREVIEW_LOOKAHEAD}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    for k in defaults:
        if k in data and isinstance(data[k], int):
            defaults[k] = max(1, min(data[k], 2000))
    return defaults


def save_settings(cache_size: int, lookahead: int) -> None:
    try:
        SETTINGS_FILE.write_text(
            json.dumps({"preview_cache_size": cache_size, "preview_lookahead": lookahead},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass

# ─── Windows 回收站（与 Tkinter 版本共用）─────────────────

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
    op.wFunc = FO_DELETE
    op.pFrom = joined
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0:
        raise OSError(f"回收站操作失败，错误代码：{result}")
    if op.fAnyOperationsAborted:
        raise OSError("删除操作已中止")

# ─── 数据模型（与 Tkinter 版本共用）───────────────────────

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
        raw_suffix = f" | RAW x{len(self.raw_paths)}" if self.raw_paths else ""
        return f"{self.status_label()} {self.relative_path}{raw_suffix}"

    def status_label(self) -> str:
        return {"pending": "[待处理]", "kept": "[保留]",
                "deleted": "[-]", "skipped": "[跳过]"}[self.status]

    def status_text(self) -> str:
        return {"pending": "待处理", "kept": "已保留",
                "deleted": "已标记删除", "skipped": "已跳过"}[self.status]


# ═══════════════════════════════════════════════════════════════
# 图片预览组件（QGraphicsView）
# ═══════════════════════════════════════════════════════════════

class ImageGraphicsView(QGraphicsView):
    """基于 QGraphicsView 的图片预览，支持缩放和平移。

    核心优势：缩放通过 setTransform 实现，不重新生成图片。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._current_pixmap: QPixmap | None = None
        self._current_path: str = ""
        self._zoom_level = 1.0
        self._fit_zoom = 1.0

        # 外观
        self.setStyleSheet("background-color: #111111; border: none;")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setRenderHints(self.renderHints())
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def set_image(self, image: Image.Image, path: str = "") -> None:
        """加载 PIL Image 并显示。"""
        self._current_path = path
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        data = image.tobytes("raw", image.mode)
        qimage = QImage(data, image.width, image.height, image.width * len(image.mode),
                         QImage.Format.Format_RGBA8888 if image.mode == "RGBA"
                         else QImage.Format.Format_RGB888)
        self._current_pixmap = QPixmap.fromImage(qimage)
        self._update_scene()
        self.fit_to_view()

    def _update_scene(self) -> None:
        if self._current_pixmap is None:
            return
        self._scene.clear()
        self._pixmap_item = QGraphicsPixmapItem(self._current_pixmap)
        self._scene.addItem(self._pixmap_item)
        self._scene.setSceneRect(QRectF(self._current_pixmap.rect()))

    def fit_to_view(self) -> None:
        """自适应窗口。"""
        if self._current_pixmap is None:
            return
        self.resetTransform()
        rect = self._current_pixmap.rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return
        vw = self.viewport().width()
        vh = self.viewport().height()
        self._fit_zoom = min(vw / rect.width(), vh / rect.height(), 1.0)
        self._zoom_level = self._fit_zoom
        self.setSceneRect(QRectF(rect))
        self.fitInView(QRectF(rect), Qt.AspectRatioMode.KeepAspectRatio)
        # 读取实际缩放
        self._zoom_level = self.transform().m11()

    def reset_view(self) -> None:
        self.resetTransform()
        self._zoom_level = 1.0
        self._fit_zoom = 1.0

    def clear_image(self) -> None:
        self._scene.clear()
        self._pixmap_item = None
        self._current_pixmap = None
        self._current_path = ""

    @property
    def zoom_level(self) -> float:
        return self._zoom_level

    # ── 滚轮事件 ──────────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Ctrl + 滚轮 → 缩放
            angle = event.angleDelta().y()
            if angle == 0:
                return
            factor = ZOOM_STEP if angle > 0 else 1.0 / ZOOM_STEP
            new_zoom = self._zoom_level * factor
            if ZOOM_MIN <= new_zoom <= ZOOM_MAX:
                self._zoom_level = new_zoom
                self.scale(factor, factor)
        else:
            super().wheelEvent(event)

    # ── 双击 → 适应窗口 ───────────────────────────────────

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.fit_to_view()
        event.accept()

    # ── 拖拽（使用 Qt 内置 ScrollHandDrag 模式）───────────

    # QGraphicsView.DragMode.ScrollHandDrag 已自动处理


# ═══════════════════════════════════════════════════════════════
# 主窗口
# ═══════════════════════════════════════════════════════════════

class PhotoCullerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("照片筛选")
        self.resize(1280, 760)
        self.setMinimumSize(980, 620)

        # ── 业务状态 ──────────────────────────────────────
        self.current_folder: Path | None = None
        self.entries: list[PhotoEntry] = []
        self.current_index: int | None = None
        self.preview_cache: OrderedDict[Path, Image.Image] = OrderedDict()
        self.preview_cache_lock = threading.Lock()

        # 后台线程通信
        self.preview_requests: queue.PriorityQueue = queue.PriorityQueue()
        self.preview_results: queue.Queue = queue.Queue()
        self.preview_request_id = 0
        self.preview_task_id = 0
        self.preview_queued_paths: set[Path] = set()
        self.preview_queue_lock = threading.Lock()
        self.scan_requests: queue.Queue = queue.Queue()
        self.scan_results: queue.Queue = queue.Queue()
        self.scan_request_id = 0
        self.is_scanning = False
        self.pending_restore_photo: str | None = None
        self._undo_stack: list[dict] = []

        # 持久化
        self.last_session: dict = {"folder": None, "current_photo": None, "photo_statuses": {}}
        self.recent_sessions: list[dict] = []
        self.persisted_statuses: dict[str, str] = {}

        # 设置
        settings = load_settings()
        self.preview_cache_size = settings["preview_cache_size"]
        self.preview_lookahead = settings["preview_lookahead"]

        # 后台线程
        for _ in range(3):
            t = threading.Thread(target=self._preview_worker_loop, daemon=True)
            t.start()
        threading.Thread(target=self._scan_worker_loop, daemon=True).start()

        # ── 构建 UI ───────────────────────────────────────
        self._build_ui()
        self._build_menus()
        self._setup_shortcuts()

        # 定时器（替代 Tkinter after）
        self._preview_timer = QTimer(self)
        self._preview_timer.timeout.connect(self._process_preview_results)
        self._preview_timer.start(50)

        self._scan_timer = QTimer(self)
        self._scan_timer.timeout.connect(self._process_scan_results)
        self._scan_timer.start(50)

        # 恢复上次会话
        QTimer.singleShot(100, self._restore_last_session)

        self._update_controls()
        self._update_image_info()

    # ── UI 构建 ──────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)

        # 顶部工具栏
        toolbar_w = QWidget()
        toolbar = QHBoxLayout(toolbar_w)
        toolbar.setContentsMargins(0, 0, 0, 4)
        self._btn_open = QPushButton("打开文件夹")
        self._btn_open.clicked.connect(self.choose_folder)
        toolbar.addWidget(self._btn_open)
        self._lbl_folder = QLabel("尚未选择文件夹")
        toolbar.addWidget(self._lbl_folder, 1)
        root_layout.addWidget(toolbar_w)

        # 中央分割区域
        self._splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── 左侧：文件列表 ──────────────────────────────
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 0, 4, 0)
        left_layout.addWidget(QLabel("照片列表"))
        self._file_list = QListWidget()
        self._file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._file_list.currentRowChanged.connect(self._on_list_selection)
        self._file_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._file_list.customContextMenuRequested.connect(self._show_list_context_menu)
        left_layout.addWidget(self._file_list, 1)
        self._lbl_summary = QLabel("共 0 项")
        left_layout.addWidget(self._lbl_summary)
        self._splitter.addWidget(left_panel)

        # ── 右侧：预览 + 信息 + 按钮 ────────────────────
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 0, 0, 0)

        # 标题
        self._lbl_title = QLabel("尚未选择图片")
        self._lbl_title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        right_layout.addWidget(self._lbl_title)

        # 预览区
        preview_frame = QFrame()
        preview_frame.setFrameStyle(QFrame.Shape.StyledPanel)
        preview_layout = QVBoxLayout(preview_frame)
        preview_layout.setContentsMargins(8, 8, 8, 8)
        self._image_view = ImageGraphicsView()
        preview_layout.addWidget(self._image_view)
        right_layout.addWidget(preview_frame, 1)

        # 信息行
        self._lbl_info = QLabel("请先打开文件夹，再选择一张照片。")
        self._lbl_info.setWordWrap(True)
        right_layout.addWidget(self._lbl_info)

        # 操作按钮行 1
        actions_w1 = QWidget()
        act1 = QHBoxLayout(actions_w1)
        act1.setContentsMargins(0, 8, 0, 0)
        self._btn_keep = QPushButton("保留（Enter）")
        self._btn_keep.clicked.connect(self.keep_current)
        act1.addWidget(self._btn_keep)
        self._btn_delete = QPushButton("移到回收站（Del）")
        self._btn_delete.clicked.connect(self.delete_current)
        act1.addWidget(self._btn_delete)
        self._btn_skip = QPushButton("跳过（S）")
        self._btn_skip.clicked.connect(self.skip_current)
        act1.addWidget(self._btn_skip)
        self._btn_restore = QPushButton("恢复（Z）")
        self._btn_restore.clicked.connect(self.restore_current)
        act1.addWidget(self._btn_restore)
        self._btn_commit = QPushButton("删除已标记")
        self._btn_commit.clicked.connect(self.commit_marked_deletions)
        act1.addWidget(self._btn_commit)
        right_layout.addWidget(actions_w1)

        # 批量操作行
        actions_w2 = QWidget()
        act2 = QHBoxLayout(actions_w2)
        act2.setContentsMargins(0, 4, 0, 0)
        self._lbl_batch = QLabel("")
        act2.addWidget(self._lbl_batch, 1)
        self._btn_batch_keep = QPushButton("批量保留")
        self._btn_batch_keep.clicked.connect(self.batch_keep)
        act2.addWidget(self._btn_batch_keep)
        self._btn_batch_delete = QPushButton("批量标记删除")
        self._btn_batch_delete.clicked.connect(self.batch_delete)
        act2.addWidget(self._btn_batch_delete)
        self._btn_batch_skip = QPushButton("批量跳过")
        self._btn_batch_skip.clicked.connect(self.batch_skip)
        act2.addWidget(self._btn_batch_skip)
        self._btn_batch_restore = QPushButton("批量恢复")
        self._btn_batch_restore.clicked.connect(self.batch_restore)
        act2.addWidget(self._btn_batch_restore)
        right_layout.addWidget(actions_w2)

        # 提示行
        self._lbl_hint = QLabel(
            f"Delete 只做删除标记。预缓存 {self.preview_cache_size} 张，"
            f"向前预读 {self.preview_lookahead} 张。Shift/Ctrl 可多选。"
        )
        right_layout.addWidget(self._lbl_hint)

        self._splitter.addWidget(right_panel)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 3)
        root_layout.addWidget(self._splitter, 1)

    def _build_menus(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("文件")
        file_menu.addAction("打开文件夹...", self.choose_folder)
        self._recent_menu = file_menu.addMenu("最近打开的目录")
        file_menu.addAction("预加载设置...", self.open_preload_settings)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close)
        self._refresh_recent_menu()

    def _setup_shortcuts(self) -> None:
        # 复用 Tkinter 版本的快捷键
        QAction(self).setShortcut(QKeySequence(Qt.Key.Key_Delete))
        self._shortcut_map = {
            Qt.Key.Key_Delete: self.delete_current,
            Qt.Key.Key_Return: self.keep_current,
            Qt.Key.Key_Enter: self.keep_current,
            Qt.Key.Key_S: self.skip_current,
            Qt.Key.Key_Z: self.restore_current,
        }

    def keyPressEvent(self, event) -> None:
        key = event.key()
        mod = event.modifiers()
        # 忽略纯修饰键
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            super().keyPressEvent(event)
            return
        # Ctrl+Z 撤回
        if mod & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_Z:
            self._undo_last_action()
            return
        # Ctrl+A 全选
        if mod & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_A:
            self._file_list.selectAll()
            self._update_batch_label()
            return
        # 自定义快捷键
        if key in self._shortcut_map:
            self._shortcut_map[key]()
            return
        # 上下方向键
        if key == Qt.Key.Key_Up:
            self._move_selection(-1)
            return
        if key == Qt.Key.Key_Down:
            self._move_selection(1)
            return
        super().keyPressEvent(event)

    # ── 文件夹操作 ───────────────────────────────────────

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择图片文件夹")
        if not folder:
            return
        self.pending_restore_photo = None
        folder_path = Path(folder)
        self.current_folder = folder_path
        self._lbl_folder.setText(str(folder_path))
        self.entries.clear()
        self.current_index = None
        self._file_list.clear()
        self._image_view.clear_image()
        self._lbl_summary.setText("正在扫描...")
        self._scan_folder(folder_path)
        self._add_recent_folder(str(folder_path))

    def _scan_folder(self, folder: Path) -> None:
        self.scan_request_id += 1
        self.is_scanning = True
        self.scan_requests.put((self.scan_request_id, folder))

    def _scan_folder_batches(self, folder: Path):
        """生成器：分批产出 PhotoEntry。"""
        jpg_files = sorted(
            [f for f in folder.iterdir()
             if f.is_file() and f.suffix.lower() in JPG_EXTENSIONS]
        )
        batch_size = 50
        for i in range(0, len(jpg_files), batch_size):
            batch = []
            for jpg_path in jpg_files[i:i + batch_size]:
                raw_paths = []
                for raw_ext in RAW_EXTENSIONS:
                    candidate = jpg_path.with_suffix(raw_ext)
                    if candidate.exists():
                        raw_paths.append(candidate)
                    candidate_upper = jpg_path.with_suffix(raw_ext.upper())
                    if candidate_upper.exists() and candidate_upper not in raw_paths:
                        raw_paths.append(candidate_upper)
                batch.append(PhotoEntry(
                    jpg_path=jpg_path,
                    relative_path=jpg_path.relative_to(folder),
                    raw_paths=raw_paths,
                ))
            yield batch

    # ── 图片列表操作 ─────────────────────────────────────

    def _on_list_selection(self, index: int) -> None:
        if index < 0 or index >= len(self.entries):
            return
        self.current_index = index
        self._show_current()
        self._update_batch_label()
        self._update_controls()
        self._save_state()

    def _move_selection(self, delta: int) -> None:
        if not self.entries:
            return
        if self.current_index is None:
            target = 0
        else:
            target = max(0, min(len(self.entries) - 1, self.current_index + delta))
        self._set_selection(target)

    def _set_selection(self, index: int) -> None:
        self._file_list.blockSignals(True)
        self._file_list.clearSelection()
        self._file_list.setCurrentRow(index)
        item = self._file_list.item(index)
        if item:
            self._file_list.scrollToItem(item)
        self._file_list.blockSignals(False)
        self.current_index = index
        self._show_current()
        self._update_batch_label()
        self._update_controls()
        self._save_state()

    def _refresh_list(self) -> None:
        self._file_list.blockSignals(True)
        self._file_list.clear()
        for entry in self.entries:
            self._file_list.addItem(entry.display_name)
        self._update_list_styles()
        self._file_list.blockSignals(False)

    def _update_list_styles(self) -> None:
        for i, entry in enumerate(self.entries):
            item = self._file_list.item(i)
            if item is None:
                continue
            if entry.status == "deleted":
                item.setForeground(Qt.GlobalColor.gray)
            elif entry.status == "kept":
                item.setForeground(Qt.GlobalColor.darkGreen)
            elif entry.status == "skipped":
                item.setForeground(Qt.GlobalColor.darkYellow)
            else:
                item.setForeground(Qt.GlobalColor.black)

    def _update_list_row(self, index: int) -> None:
        if 0 <= index < len(self.entries):
            item = self._file_list.item(index)
            if item:
                item.setText(self.entries[index].display_name)
        self._update_list_styles()

    def _get_selected_indices(self) -> list[int]:
        return [i.row() for i in self._file_list.selectedIndexes()]

    def _show_list_context_menu(self, pos) -> None:
        if not self.entries:
            return
        menu = QMenu(self)
        menu.addAction("打开所在文件夹", self.open_current_folder)
        menu.exec(self._file_list.mapToGlobal(pos))

    def open_current_folder(self) -> None:
        if self.current_index is None:
            return
        path = self.entries[self.current_index].jpg_path
        try:
            subprocess.Popen(["explorer.exe", f"/select,{path}"])
        except OSError as e:
            QMessageBox.critical(self, "打开失败", str(e))

    # ── 图片预览 ─────────────────────────────────────────

    def _show_current(self) -> None:
        if self.current_index is None or not self.entries:
            self._set_status("尚未选择照片")
            return
        entry = self.entries[self.current_index]
        self._lbl_title.setText(str(entry.relative_path))

        lines = [
            f"路径：{entry.jpg_path}",
            f"状态：{entry.status_text()}",
            f"匹配到的原始文件：{len(entry.raw_paths)} 个",
        ]
        if entry.status == "deleted":
            lines.append("提示：这张照片已标记删除，点击「删除已标记」后会移到回收站。")
        if entry.raw_paths:
            lines.extend(f"  - {r.name}" for r in entry.raw_paths)
        self._lbl_info.setText("\n".join(lines))

        cached = self._get_cached_preview(entry.jpg_path)
        if cached is not None:
            self._image_view.set_image(cached, str(entry.jpg_path))
            self._queue_preview_prefetch()
            return

        self._image_view.clear_image()
        self._lbl_info.setText("正在加载预览...")
        self.preview_request_id += 1
        self._enqueue_preview_request(entry.jpg_path, priority=0,
                                       request_id=self.preview_request_id, force=True)
        self._queue_preview_prefetch()

    def _set_status(self, message: str) -> None:
        self._lbl_title.setText("尚未选择照片")
        self._lbl_info.setText(message)
        self._image_view.clear_image()

    # ── 标记操作 ─────────────────────────────────────────

    def keep_current(self) -> None:
        if self.current_index is None:
            return
        selected = self._get_selected_indices()
        if len(selected) > 1:
            self._mark_entries(selected, "kept")
            self._advance_after_batch(selected)
            return
        self.entries[self.current_index].status = "kept"
        self._push_undo(self.current_index, "pending")
        self._update_list_row(self.current_index)
        self._update_summary()
        self._set_selection(self.current_index)
        self._advance_to_next()
        self._save_state()

    def delete_current(self) -> None:
        if self.current_index is None:
            return
        selected = self._get_selected_indices()
        if len(selected) > 1:
            applicable = [i for i in selected
                          if self.entries[i].status != "deleted"]
            if applicable:
                self._mark_entries(applicable, "deleted")
                self._advance_after_batch(applicable)
            return
        entry = self.entries[self.current_index]
        if entry.status == "deleted":
            self._advance_to_next()
            return
        entry.status = "deleted"
        idx = self.current_index
        self._push_undo(idx, entry.status)
        self._update_list_row(idx)
        self._update_summary()
        self._set_selection(idx)
        self._advance_to_next()
        self._save_state()

    def skip_current(self) -> None:
        if self.current_index is None:
            return
        selected = self._get_selected_indices()
        if len(selected) > 1:
            self._mark_entries(selected, "skipped")
            self._advance_after_batch(selected)
            return
        self.entries[self.current_index].status = "skipped"
        self._push_undo(self.current_index, "pending")
        self._update_list_row(self.current_index)
        self._update_summary()
        self._set_selection(self.current_index)
        self._advance_to_next()
        self._save_state()

    def restore_current(self) -> None:
        if self.current_index is None:
            return
        selected = self._get_selected_indices()
        if len(selected) > 1:
            self.batch_restore()
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

    def _advance_to_next(self) -> None:
        if not self.entries:
            return
        if self.current_index is None:
            return
        # 找下一个非 deleted
        for i in range(self.current_index + 1, len(self.entries)):
            if self.entries[i].status != "deleted":
                self._set_selection(i)
                return
        for i in range(self.current_index):
            if self.entries[i].status != "deleted":
                self._set_selection(i)
                return
        self._set_selection(self.current_index)

    # ── 批量操作 ─────────────────────────────────────────

    def _mark_entries(self, indices: list[int], status: str) -> None:
        for i in indices:
            if 0 <= i < len(self.entries):
                self.entries[i].status = status
                self._update_list_row(i)
        self._update_summary()
        self._update_controls()
        self._save_state()

    def _advance_after_batch(self, changed: list[int]) -> None:
        if not self.entries:
            self.current_index = None
            self._set_status("列表中的照片已全部处理或删除。")
            self._update_controls()
            return
        max_changed = max(changed) if changed else (self.current_index or 0)
        for i in range(max_changed + 1, len(self.entries)):
            if self.entries[i].status != "deleted":
                self._set_selection(i)
                return
        for i in range(len(self.entries)):
            if self.entries[i].status != "deleted":
                self._set_selection(i)
                return
        self._set_selection(min(max(self.current_index or 0, 0), len(self.entries) - 1))

    def batch_keep(self) -> None:
        indices = self._get_selected_indices()
        if indices:
            self._mark_entries(indices, "kept")
            self._advance_after_batch(indices)

    def batch_delete(self) -> None:
        indices = self._get_selected_indices()
        applicable = [i for i in indices
                      if 0 <= i < len(self.entries) and self.entries[i].status != "deleted"]
        if applicable:
            self._mark_entries(applicable, "deleted")
            self._advance_after_batch(applicable)

    def batch_skip(self) -> None:
        indices = self._get_selected_indices()
        if indices:
            self._mark_entries(indices, "skipped")
            self._advance_after_batch(indices)

    def batch_restore(self) -> None:
        indices = self._get_selected_indices()
        applicable = [i for i in indices
                      if 0 <= i < len(self.entries) and self.entries[i].status == "deleted"]
        if applicable:
            self._mark_entries(applicable, "pending")
            self._set_selection(max(applicable))
            self._update_controls()

    def commit_marked_deletions(self) -> None:
        deleted = [e for e in self.entries if e.status == "deleted"]
        if not deleted:
            return
        selected_path = None
        fallback_path = None
        if self.current_index is not None and 0 <= self.current_index < len(self.entries):
            selected_path = self.entries[self.current_index].jpg_path
            for i in range(self.current_index + 1, len(self.entries)):
                if self.entries[i].status != "deleted":
                    fallback_path = self.entries[i].jpg_path
                    break
            if fallback_path is None:
                for i in range(self.current_index - 1, -1, -1):
                    if self.entries[i].status != "deleted":
                        fallback_path = self.entries[i].jpg_path
                        break
        try:
            targets = [p for e in deleted for p in [e.jpg_path, *e.raw_paths]]
            move_to_recycle_bin(targets)
        except Exception as e:
            QMessageBox.critical(self, "删除失败", f"无法将已标记文件移到回收站。\n\n{e}")
            return

        self.entries = [e for e in self.entries if e.status != "deleted"]
        self.persisted_statuses = {
            str(e.relative_path): e.status
            for e in self.entries if e.status != "pending"
        }
        if not self.entries:
            self.current_index = None
            self._refresh_list()
            self._set_status("列表中的照片已全部处理或删除。")
        else:
            self._refresh_list()
            target = (selected_path
                      if any(e.jpg_path == selected_path for e in self.entries)
                      else fallback_path)
            if target:
                for i, e in enumerate(self.entries):
                    if e.jpg_path == target:
                        self._set_selection(i)
                        break
                else:
                    self._set_selection(0)
            else:
                self._set_selection(0)
        self._update_summary()
        self._update_controls()
        self._save_state()

    # ── UI 更新 ──────────────────────────────────────────

    def _update_controls(self) -> None:
        has_current = self.current_index is not None and self.entries
        enabled = has_current
        self._btn_keep.setEnabled(enabled)
        self._btn_delete.setEnabled(enabled)
        self._btn_skip.setEnabled(enabled)

        is_deleted = (has_current and
                      self.entries[self.current_index].status == "deleted")
        self._btn_restore.setEnabled(is_deleted)

        has_any_deleted = any(e.status == "deleted" for e in self.entries)
        self._btn_commit.setEnabled(has_any_deleted)

        selected = self._get_selected_indices()
        is_multi = len(selected) > 1
        self._btn_batch_keep.setEnabled(is_multi)
        self._btn_batch_delete.setEnabled(is_multi)
        self._btn_batch_skip.setEnabled(is_multi)
        if is_multi:
            has_del_sel = any(
                self.entries[i].status == "deleted"
                for i in selected if 0 <= i < len(self.entries))
            self._btn_batch_restore.setEnabled(has_del_sel)
        else:
            self._btn_batch_restore.setEnabled(False)

        self._update_batch_label()

    def _update_batch_label(self) -> None:
        selected = self._get_selected_indices()
        if len(selected) > 1:
            self._lbl_batch.setText(f"已选中 {len(selected)} 张照片")
        else:
            self._lbl_batch.setText("")

    def _push_undo(self, index: int, previous_status: str) -> None:
        """Record an action for Ctrl+Z undo."""
        self._undo_stack.append({"index": index, "previous_status": previous_status})
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)

    def _undo_last_action(self) -> None:
        """Ctrl+Z: undo the last mark operation."""
        if not self._undo_stack:
            return
        action = self._undo_stack.pop()
        idx = action["index"]
        if 0 <= idx < len(self.entries):
            self.entries[idx].status = action["previous_status"]
            self._update_list_row(idx)
            self._update_summary()
            self._set_selection(idx)
            self._update_controls()
            self._save_state()

    def closeEvent(self, event) -> None:
        """Save state on window close."""
        self._save_state()
        super().closeEvent(event)
        total = len(self.entries)
        kept = sum(1 for e in self.entries if e.status == "kept")
        deleted = sum(1 for e in self.entries if e.status == "deleted")
        skipped = sum(1 for e in self.entries if e.status == "skipped")
        pending = total - kept - deleted - skipped
        self._lbl_summary.setText(
            f"共 {total} 项  |  待处理 {pending}  |  保留 {kept}"
            f"  |  标记删除 {deleted}  |  跳过 {skipped}"
        )

    def _update_image_info(self) -> None:
        pass  # 缩放等状态由 ImageGraphicsView 内部管理

    # ── 预加载设置 ───────────────────────────────────────

    def open_preload_settings(self) -> None:
        # 简单对话框
        from PySide6.QtWidgets import QDialog, QFormLayout, QSpinBox, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("预加载设置")
        layout = QFormLayout(dlg)
        spin_cache = QSpinBox()
        spin_cache.setRange(24, 2000)
        spin_cache.setValue(self.preview_cache_size)
        layout.addRow("预缓存数量", spin_cache)
        spin_lookahead = QSpinBox()
        spin_lookahead.setRange(3, 200)
        spin_lookahead.setValue(self.preview_lookahead)
        layout.addRow("向前预读数量", spin_lookahead)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                 QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addRow(btns)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.preview_cache_size = spin_cache.value()
            self.preview_lookahead = spin_lookahead.value()
            save_settings(self.preview_cache_size, self.preview_lookahead)
            self._lbl_hint.setText(
                f"Delete 只做删除标记。预缓存 {self.preview_cache_size} 张，"
                f"向前预读 {self.preview_lookahead} 张。Shift/Ctrl 可多选。"
            )

    # ── 最近目录 ─────────────────────────────────────────

    def _add_recent_folder(self, path_str: str) -> None:
        sessions = list(self.recent_sessions)
        sessions = [s for s in sessions if s.get("folder") != path_str]
        sessions.insert(0, {"folder": path_str})
        self.recent_sessions = sessions[:MAX_RECENT_SESSIONS]
        self._refresh_recent_menu()

    def _refresh_recent_menu(self) -> None:
        self._recent_menu.clear()
        if not self.recent_sessions:
            self._recent_menu.addAction("(无)").setEnabled(False)
            return
        for session in self.recent_sessions:
            folder = session.get("folder")
            if folder:
                self._recent_menu.addAction(folder,
                    lambda f=folder: self._open_recent_folder(f))

    def _open_recent_folder(self, path_str: str) -> None:
        p = Path(path_str)
        if p.is_dir():
            self.current_folder = p
            self._lbl_folder.setText(path_str)
            self.pending_restore_photo = None
            self.entries.clear()
            self.current_index = None
            self._file_list.clear()
            self._image_view.clear_image()
            self._lbl_summary.setText("正在扫描...")
            self._scan_folder(p)

    # ── 会话持久化（与 Tkinter 版本共用 STATE_FILE）───

    def _save_state(self) -> None:
        if not self.current_folder or self.is_scanning:
            return
        data = {
            "folder": str(self.current_folder),
            "current_photo": (
                str(self.entries[self.current_index].relative_path)
                if self.current_index is not None and self.entries else None
            ),
            "photo_statuses": {
                str(e.relative_path): e.status
                for e in self.entries if e.status != "pending"
            },
            "recent_sessions": self.recent_sessions,
        }
        try:
            STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except OSError:
            pass

    def _restore_last_session(self) -> None:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        folder = data.get("folder")
        self.recent_sessions = data.get("recent_sessions", [])
        self._refresh_recent_menu()
        if not folder:
            return
        folder_path = Path(folder)
        if not folder_path.is_dir():
            return
        self.current_folder = folder_path
        self._lbl_folder.setText(str(folder_path))
        self.pending_restore_photo = data.get("current_photo")
        self.persisted_statuses = data.get("photo_statuses", {})
        print(f"[Restore] folder={folder_path}, photo={self.pending_restore_photo}, statuses={len(self.persisted_statuses)}")
        self.entries.clear()
        self._file_list.clear()
        self._lbl_summary.setText("正在扫描...")
        self._scan_folder(folder_path)
        self._add_recent_folder(str(folder_path))

    # ── 后台线程 ─────────────────────────────────────────

    def _scan_worker_loop(self) -> None:
        while True:
            req_id, folder = self.scan_requests.get()
            for batch in self._scan_folder_batches(folder):
                self.scan_results.put((req_id, folder, batch, False))
            self.scan_results.put((req_id, folder, [], True))

    def _preview_worker_loop(self) -> None:
        while True:
            _pri, _tid, req_id, path = self.preview_requests.get()
            with self.preview_queue_lock:
                self.preview_queued_paths.discard(path)
            cached = self._get_cached_preview(path)
            if cached is not None:
                self.preview_results.put((req_id, path, cached, None))
                continue
            image = None
            error = None
            try:
                with Image.open(path) as opened:
                    processed = ImageOps.exif_transpose(opened)
                    if max(processed.size) > MAX_PREVIEW_SOURCE_EDGE:
                        processed.thumbnail((MAX_PREVIEW_SOURCE_EDGE, MAX_PREVIEW_SOURCE_EDGE),
                                            Image.LANCZOS)
                    image = processed.copy()
            except Exception as e:
                error = str(e)
            self.preview_results.put((req_id, path, image, error))

    def _process_preview_results(self) -> None:
        while True:
            try:
                req_id, path, image, error = self.preview_results.get_nowait()
            except queue.Empty:
                break
            if image is not None:
                self._store_cached_preview(path, image)
            current_entry = (self.entries[self.current_index]
                             if self.current_index is not None and self.entries else None)
            if current_entry is None or current_entry.jpg_path != path:
                continue
            if image is not None:
                self._image_view.set_image(image, str(path))
                self._lbl_info.setText(
                    f"路径：{path}\n状态：{current_entry.status_text()}\n"
                    f"匹配到的原始文件：{len(current_entry.raw_paths)} 个"
                )
            else:
                self._image_view.clear_image()
                self._lbl_info.setText(f"预览失败：\n{error}")

    def _process_scan_results(self) -> None:
        while True:
            try:
                req_id, folder, batch, done = self.scan_results.get_nowait()
            except queue.Empty:
                break
            if req_id != self.scan_request_id or self.current_folder != folder:
                continue
            if batch:
                start = len(self.entries)
                self.entries.extend(batch)
                for entry in batch:
                    self._file_list.addItem(entry.display_name)
                self._update_list_styles()
                # 恢复状态
                for i in range(start, len(self.entries)):
                    rel = str(self.entries[i].relative_path)
                    if rel in self.persisted_statuses:
                        self.entries[i].status = self.persisted_statuses[rel]
                        self._update_list_row(i)
                if self.pending_restore_photo:
                    for i in range(start, len(self.entries)):
                        if str(self.entries[i].relative_path) == self.pending_restore_photo:
                            self._set_selection(i)
                            self.pending_restore_photo = None
                            break
                if self.current_index is None and self.entries and self.pending_restore_photo is None:
                    self._set_selection(0)
                self._update_summary()
                self._update_controls()
            if done:
                self.is_scanning = False
                if not self.entries:
                    self._set_status("当前文件夹中没有找到可处理的照片。")
                elif self.current_index is None:
                    self._set_selection(0)
                self.pending_restore_photo = None
                self._update_summary()
                self._update_controls()
                self._save_state()

    # ── 预加载 ───────────────────────────────────────────

    def _queue_preview_prefetch(self) -> None:
        if self.current_index is None:
            return
        offsets = list(range(1, self.preview_lookahead + 1))
        offsets.extend((-1, -2, -3))
        for off in offsets:
            idx = self.current_index + off
            if 0 <= idx < len(self.entries):
                self._enqueue_preview_request(
                    self.entries[idx].jpg_path,
                    priority=10 + abs(off), request_id=0)

    def _enqueue_preview_request(self, path: Path, priority: int,
                                  request_id: int, force: bool = False) -> None:
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


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = PhotoCullerWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
