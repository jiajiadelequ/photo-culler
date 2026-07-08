# Photo Culler

Windows 本地照片筛选工具，适合拍摄后快速浏览 JPG 预览图，把废片和同名 RAW 一起移到回收站。

基于 **PySide6 + QGraphicsView**，缩放流畅，零成本 GPU 加速。

## 功能特点

- 扫描指定目录中的 JPG/JPEG 文件，自动匹配同名 RAW
- 左侧列表快速切图，右侧大图预览
- **鼠标滚轮缩放**（QGraphicsView transform，不调用 Pillow resize）
- 鼠标拖拽平移
- 双击自适应窗口
- 标记：保留 / 删除 / 跳过 / 恢复
- 删除标记后统一移到 Windows 回收站（JPG + RAW 一起）
- Ctrl+Z 撤回上一步标记操作
- 会话持久化：自动记忆上次目录、当前图片、标记状态
- 后台线程预加载，切图流畅

## 支持的文件类型

**预览图**：`.jpg` `.jpeg`

**RAW**：`.3fr` `.arw` `.cr2` `.cr3` `.dcr` `.dng` `.erf` `.kdc` `.mos` `.mrw` `.nef` `.nrw` `.orf` `.pef` `.raf` `.raw` `.rw2` `.sr2` `.srf` `.x3f`

## 环境

- Windows
- Python 3.12+
- 虚拟环境：`E:\python-envs\photo-culler-qt`（E 盘，不占 C 盘）

## 安装

```powershell
py -3 -m venv E:\python-envs\photo-culler-qt
E:\python-envs\photo-culler-qt\Scripts\activate
pip install PySide6 Pillow PyInstaller
```

## 运行

```powershell
# 源码运行
双击: 运行_Qt_源码版.bat

# 或命令行
E:\python-envs\photo-culler-qt\Scripts\python.exe photo_culler_qt.py
```

## 打包

```powershell
# 开发版 (onedir, 快速增量)
双击: 一键打包_Qt_Dev.bat
输出: dist_dev\照片筛选_Qt\

# 发布版 (onefile, 单 exe)
双击: 一键打包_Qt_Release.bat
输出: dist_release\照片筛选_Qt.exe
```

## 快捷键

| 键 | 功能 |
|---|---|
| `Delete` | 标记删除当前图片 |
| `Enter` | 保留当前图片 |
| `S` | 跳过当前图片 |
| `Z` | 恢复当前已标记删除的图片 |
| `Ctrl+Z` | 撤回上一步标记操作 |
| `Ctrl+A` | 全选列表 |
| `↑` `↓` | 上一张 / 下一张 |
| `鼠标滚轮` | 缩放图片 |
| `拖拽` | 平移图片 |
| `双击图片` | 自适应窗口 |

## 数据持久化

状态文件保存在：`%LOCALAPPDATA%\PhotoCuller\`

- `photo_culler_state.json` — 目录、当前图片、标记状态、最近目录
- `settings.json` — 预缓存数量、预读数量

## 项目结构

```text
photo-culler/
├── photo_culler_qt.py         # PySide6 主程序
├── PhotoCullerQt_dev.spec     # 开发版打包配置 (onedir)
├── PhotoCullerQt_release.spec # 发布版打包配置 (onefile)
├── 运行_Qt_源码版.bat         # 源码运行脚本
├── 一键打包_Qt_Dev.bat        # 开发版打包脚本
├── 一键打包_Qt_Release.bat    # 发布版打包脚本
├── requirements.txt           # PySide6 + Pillow
├── PROJECT.md                 # 工程说明
├── README.md                  # 本文件
└── LICENSE                    # MIT
```

## 架构

- `ImageGraphicsView(QGraphicsView)` — 图片预览，缩放通过 setTransform 零成本
- `PhotoCullerWindow(QMainWindow)` — 主窗口，QSplitter 左右布局
- `QListWidget` — 文件列表，支持 Shift/Ctrl 多选
- `PhotoEntry` — 数据模型
- Windows Shell API — 回收站操作

## 许可证

MIT
