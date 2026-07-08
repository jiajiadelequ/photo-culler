# Photo Culler

一个面向 Windows 本地使用的照片筛选工具，适合在拍摄后快速浏览 JPG 预览图，并把对应的废片和同名 RAW 一起移到回收站。

项目当前是一个基于 `tkinter` 的桌面应用，主程序位于 [`photo_culler.py`](./photo_culler.py)，支持直接用 Python 运行，也支持通过 PyInstaller 打包成独立可执行文件。

## 功能特点

- 扫描指定目录及其子目录中的 `JPG/JPEG` 文件
- 自动匹配同名 RAW 文件，并在界面中显示关联数量
- 左侧列表快速切图，右侧显示照片预览和文件信息
- 支持将照片标记为“保留 / 删除 / 跳过”
- 删除时不会立刻物理删除，而是先做标记
- 点击“删除已标记”后，将 JPG 和匹配到的 RAW 一并移到 Windows 回收站
- 自动保存最近打开目录、当前定位照片和筛选状态
- 支持预览缓存和预读设置，减少切图卡顿

## 支持的文件类型

### 预览图

- `.jpg`
- `.jpeg`

### 自动关联的 RAW

- `.3fr`
- `.arw`
- `.cr2`
- `.cr3`
- `.dcr`
- `.dng`
- `.erf`
- `.kdc`
- `.mos`
- `.mrw`
- `.nef`
- `.nrw`
- `.orf`
- `.pef`
- `.raf`
- `.raw`
- `.rw2`
- `.sr2`
- `.srf`
- `.x3f`

RAW 的匹配规则是“同目录下同文件名 stem”。例如：

- `IMG_0001.jpg`
- `IMG_0001.cr3`
- `IMG_0001.dng`

它们会被视为同一组，删除 JPG 时会一起进入回收站。

## 运行环境

- Windows
- Python 3.10+，推荐 Python 3.11 或更新版本
- 依赖库：
  - `Pillow`

说明：

- GUI 使用的是 Python 标准库自带的 `tkinter`
- 如果没有安装 `Pillow`，程序仍可启动，但无法显示图片预览
- 回收站删除逻辑使用的是 Windows Shell API，因此该项目实际是按 Windows 桌面环境设计的

## 安装依赖

```powershell
py -3 -m pip install pillow
```

如果需要打包：

```powershell
py -3 -m pip install pyinstaller pillow
```

## 直接运行

在项目根目录执行：

```powershell
py -3 photo_culler.py
```

仓库内已经提供了启动脚本：

```powershell
.\启动照片筛选.bat
```

这个脚本会优先尝试 `py -3`，找不到时再尝试 `python`。

## 打包为 EXE

项目已包含 PyInstaller 配置文件 [`PhotoCuller.spec`](./PhotoCuller.spec)。

### 方式一：使用现成脚本

```powershell
.\一键打包.bat
```

对应执行的是：

```powershell
py -3 -m PyInstaller PhotoCuller.spec --distpath dist_release
```

### 方式二：手动执行

```powershell
py -3 -m PyInstaller PhotoCuller.spec --distpath dist_release
```

打包结果默认输出到：

```text
dist_release\照片筛选.exe
```

当前配置为：

- 窗口程序，不弹控制台
- 启用 `upx=True`
- 可执行文件名称为 `照片筛选`

## 使用说明

1. 启动程序后，点击“打开文件夹”。
2. 选择包含照片的目录。
3. 程序会递归扫描子目录中的 JPG/JPEG 文件。
4. 选择左侧照片后，右侧会显示预览、当前状态、完整路径和已匹配的 RAW 文件。
5. 根据需要将照片标记为保留、跳过或删除。
6. 所有删除操作先只是“标记删除”。
7. 确认无误后，点击“删除已标记”，程序会把这些 JPG 及其匹配的 RAW 一起移到回收站。

## 快捷键

- `Enter`：保留当前照片
- `Delete`：标记当前照片为删除
- `S`：跳过当前照片
- `Z`：恢复当前已标记删除的照片
- `Up / Down`：在照片列表中上下移动

## 会话保存与恢复

程序会自动保存以下信息：

- 上次打开的目录
- 最近打开的目录列表
- 当前定位到的照片
- 每张照片的筛选状态
- 预览缓存和预读设置

在 Windows 下，状态文件默认保存在：

```text
%LOCALAPPDATA%\PhotoCuller\
```

其中主要包括：

- `photo_culler_state.json`
- `settings.json`

## 预加载设置

程序支持两个与流畅度相关的参数：

- `preview_cache_size`：预览缓存张数
- `preview_lookahead`：向前预读张数

默认值：

- 缓存张数：`192`
- 向前预读：`20`

界面菜单中可以通过“文件 -> 预加载设置...”调整。一般来说：

- 更大的缓存和预读会让连续浏览更顺畅
- 但也会占用更多内存

源码中给出的建议区间是：

- 缓存：`128-256`
- 预读：`12-30`

## 项目结构

```text
photo-culler/
├─ photo_culler.py        # 主程序
├─ PhotoCuller.spec       # PyInstaller 打包配置
├─ 启动照片筛选.bat       # 本地启动脚本
├─ 一键打包.bat           # 一键打包脚本
├─ dist_release/          # 打包输出目录
└─ build/                 # PyInstaller 构建中间文件
```

## 当前实现说明

- 界面基于 `tkinter + ttk`
- 图片预览由 `Pillow` 提供，并会自动处理 EXIF 方向
- 目录扫描和预览加载使用后台线程，避免界面完全阻塞
- 删除不是直接 `unlink`，而是调用 Windows Shell API 移到回收站

## 可能的后续改进

- 增加对更多预览格式的支持
- 支持“仅看未处理照片”过滤
- 增加批量快捷操作和更强的键盘流
- 增加缩略图网格视图
- 增加打包发布说明和版本号管理

## 许可证

当前仓库中还没有明确的开源许可证文件。如果你准备公开发布，建议补充 `LICENSE`。
