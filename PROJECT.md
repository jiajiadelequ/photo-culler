# 照片筛选 (PhotoCuller)

基于 **PySide6 + QGraphicsView** 的图片浏览筛选工具。

## 技术栈

- Python 3.12
- PySide6（Qt GUI）
- Pillow（图片读取）
- PyInstaller（打包）

## 文件结构

| 文件 | 说明 |
|---|---|
| `photo_culler_qt.py` | **主程序** |
| `PhotoCullerQt.spec` | PyInstaller 打包配置 |
| `一键打包_Qt.bat` | 一键打包脚本 |
| `requirements.txt` | 依赖清单 |
| `LICENSE` | MIT |
| `PROJECT.md` | 本文件 |

## 环境配置

虚拟环境路径（所有依赖在 E 盘，不占 C 盘）：

```
E:\python-envs\photo-culler-qt
```

依赖安装：

```bash
E:\python-envs\photo-culler-qt\Scripts\python.exe -m pip install PySide6 Pillow PyInstaller
```

## 启动

```bash
E:\python-envs\photo-culler-qt\Scripts\python.exe E:\picture_tool\photo_culler_qt.py
```

## 打包

双击 `一键打包_Qt.bat`，输出：`dist_release\照片筛选_Qt.exe`

或命令行：

```bash
E:\python-envs\photo-culler-qt\Scripts\python.exe -m PyInstaller PhotoCullerQt.spec --distpath dist_release
```

## 架构

- **`ImageGraphicsView(QGraphicsView)`**：图片预览组件
  - Ctrl + 滚轮缩放 → `setTransform().scale()`，零成本，不调用 Pillow resize
  - 拖拽平移 → `setDragMode(ScrollHandDrag)`
  - 双击 → `fit_to_view()` 自适应窗口
- **`PhotoCullerWindow(QMainWindow)`**：主窗口，QSplitter 左列表右预览
- **`QListWidget`**：文件列表，支持 Shift/Ctrl 多选
- 业务逻辑复用：`PhotoEntry` 数据类、Windows 回收站、JSON 持久化、后台线程预加载

## 快捷键

| 键 | 功能 |
|---|---|
| `Delete` | 标记删除 |
| `Enter` | 保留 |
| `S` | 跳过 |
| `Z` | 恢复 |
| `↑` `↓` | 上一张/下一张 |
| `Ctrl + A` | 全选 |
| `Ctrl + 滚轮` | 缩放 |
| 双击图片 | 适应窗口 |

## 数据持久化

设置和会话状态保存在：

```
%LOCALAPPDATA%\PhotoCuller\
  ├── settings.json
  └── photo_culler_state.json
```
