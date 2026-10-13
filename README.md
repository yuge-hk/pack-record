# PackRecord · 打包录像（原 ecom-love-obs）

Linux / Windows 电商发货扫码录像工具。直接使用 FFmpeg 采集 USB 摄像头和麦克风，
不需要安装、启动或连接 OBS。当前版本 **2.0.0**。

默认：**2560×1440、30 FPS、H.265 / HEVC Intel QSV、AAC 音频、MKV 文件**。
文件名包含快递单号，不在画面上叠加文字。质量值 24、预设 fast 是初始设置，
不保证与 OBS 同数值参数的画质等价，应在目标机器上用包裹标签和小字核对。
为减少切单时的编码缓冲，禁用 B 帧；软件编码使用低延迟模式。有声音时以较短的
音视频流收尾，避免文件尾部出现没有对应声音的画面。这些设置可能影响压缩效率。

## 环境与安装

- Windows 10/11 或桌面 Linux，Python 3.10+。
- FFmpeg **7+** 和同套发行包中的 FFprobe。FFmpeg 需要支持摄像头、音频设备、AAC
  和你选择的视频编码器。工具查找顺序：设置中的路径 → 程序目录下 `tools/` → 程序目录 → PATH。
- Windows 使用 DirectShow。Linux 摄像头使用 V4L2；声音支持 PulseAudio（包括提供
  PulseAudio 兼容服务的 PipeWire）或 ALSA。
- Intel QSV 需要合适的 Intel 显卡驱动和支持 QSV 的 FFmpeg 构建。Linux 通常还需要
  Intel media driver / oneVPL 及 `/dev/dri/renderD*` 的访问权限。

```bash
python -m venv .venv
# Windows：.venv\Scripts\activate
# Linux：source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

Windows 可从 [FFmpeg 官方下载页](https://ffmpeg.org/download.html) 指向的 Windows
构建下载 FFmpeg，将 `ffmpeg.exe`、`ffprobe.exe` 放入 `tools/`，或在设置中指定路径。
Linux 可安装发行版提供的 FFmpeg 7+；例如适用的 Debian / Ubuntu 版本：

```bash
sudo apt install ffmpeg v4l-utils pulseaudio-utils
```

Linux 的 PySide6 还依赖系统图形库。缺少库时按导入错误安装对应包；例如
`libxkbcommon0`。Qt 的 X11 插件可能需要 `libxcb-cursor0` 等桌面库。
程序目录需要可写，不建议放入 `Program Files`。

## 首次配置

1. 打开“设置”，点击“刷新摄像头与麦克风”，选择具体设备并保存。
2. 默认摄像头输入为 **MJPEG**。如果打不开 2560×1440 / 30 FPS，点击“查看摄像头
   支持的格式”，选择设备实际支持的分辨率、帧率及输入格式。不会偷偷降低录像分辨率。
3. Windows N100 可选择 **H.265 QSV** 或 **H.264 QSV**。CPU 软件编码是手动选项，
   QSV 失败时程序不会自动切换到软件编码。
4. 点击“试拍预览”确认取景与设备能打开。试拍会同时打开麦克风，但不播放声音、
   不保存文件；实际收音和同步请用一段保存的录像核查。
5. 扫描一单进行实际录制，输入 `STOPSTOP` 后检查文件能播放、标签清楚、声音正确。

“检查 FFmpeg 包含的编码器”仅检查编译支持，不保证本机硬件编码初始化成功。
必须通过实际录像验证 QSV。H.265 文件的播放/提交兼容性由播放端决定；需要更广
兼容性时可选择 H.264。

## 扫码流程

- 扫描 A → 启动 FFmpeg。实际收到视频和要求的音频编码数据包后才显示“录制中”。
- 扫描 B → 正常停止 A、验证并归档，再开始 B。
- 保存期间又扫描 C、D → 只保留 D，不建立队列；B、C 不会录制。
- 同一单号在去重窗口内，以及与当前录制单号相同的扫描，会被忽略。
- `STOPSTOP`（不区分大小写）或“停止并保存”会清除待处理单号，并正常停止当前录像。
- 在程序主窗口内，输入焦点离开扫码框时也能接收键盘式扫码；不提供系统全局键盘钩子。
- Windows 扫码框禁用 Qt 输入法，并尝试激活英文布局。快速扫码被输入法吞掉回车时，
  180 ms 静默自动提交兜底；慢速手输需要回车。扫码枪/输入法行为仍需 Windows 实机检查。
- 两个平台均使用单实例锁。关闭时先停止并等待文件归档，过程中不接收新单号。

**切单会停止并重新打开采集设备，存在录制间隙。当前版本不承诺连续无间隙录像。**
预览与录制共享一个 FFmpeg 采集进程，不会重复打开摄像头。录制保持完整分辨率；
预览为 640×360、默认 8 FPS，内存中只保留最新一帧。空闲时可点击试拍预览，
扫码会先结束试拍，再开始正式录像。

## 故障与保存恢复

- 摄像头/麦克风/编码器启动失败，或任一音视频输出停滞，会锁定扫码，保留原单号，
  丢弃尚未开始的后续单号。修复设备或设置后点击“修复后重试原单号”。
- FFmpeg 停止使用标准输入 `q` 正常收尾；超过配置的超时才强制结束。强制结束
  不能保证文件完整，原始文件和恢复任务会保留。
- 故障后有可验证的片段时会归档，但 JSONL 标为 `capture.interrupted=true`，
  并继续保留故障单号等待重新录制。它不表示整个打包过程已完整录下。
- 每次录制在启动 FFmpeg **之前**写入恢复任务。归档使用预写日志、唯一文件名
  保留和 `recording_id` 幂等记录，覆盖文件移动、写记录和跨盘复制中断后的重试。
- “恢复保存任务”验证文件中的实际音视频包、编码、分辨率和结束信息后再归档。
  空文件、缺音轨或没有完整结束信息的文件不会被自动当作成功录像；任务保留供人工检查。
- 同盘移动，跨盘采用唯一临时文件复制、刷盘、原子改名，再删除源文件。
- 启动新录像及录像过程中检查归档盘和临时录制盘；默认低于 40 GB 警告、
  低于 10 GB 禁止新录像并停止正在录制的内容。跨盘复制默认额外预留 1 GB。

## 数据与代码

| 路径 | 用途 |
|---|---|
| `recordings/YYYY-MM-DD/单号.mkv` | 默认归档视频，重名加 `(1)`、`(2)` 等序号 |
| `recordings-in-progress/录制ID.mkv` | 正在录制或尚未归档的原始文件 |
| `records/YYYY-MM-DD.jsonl` | 单号、时间、时长、分辨率、路径和录制状态 |
| `records/recorder-failed-jobs.json` | FFmpeg 持久恢复任务 |
| `recorder-settings.json` | 本机设备、编码和存储设置 |
| `logs/recorder.log` | 5 MB 轮转日志，最多 5 份历史文件 |
| `main.py` / `settings_dialog.py` | PySide6 主界面与设置 |
| `ffmpeg_backend.py` | 设备发现、命令生成、进程、音视频输出监测和媒体验证 |
| `recording_controller.py` | 串行扫码、录制、归档及恢复流程 |
| `app_core.py` | 复用的文件移动、JSONL 与任务存储基础逻辑 |

旧 OBS 设置 `settings.json` 和旧恢复任务 `records/failed_jobs.json` 不会被改写。
历史 OBS 入口保留在 `archive/main_obs.py`，旧 Linux 入口在 `archive/main_linux.py`。
旧版只作参考/回退，不参与新版运行；运行 OBS 回退版本需要另装 `obsws-python`。

## 开发验证

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m py_compile main.py app_core.py recorder_settings.py ffmpeg_backend.py recording_controller.py settings_dialog.py
```

真实媒体测试使用合成画面和正弦波声音，显式选择 CPU 编码，不替代 N100 的 QSV 验收：

```bash
# Linux
ECOM_TEST_FFMPEG=/usr/bin/ffmpeg ECOM_TEST_FFPROBE=/usr/bin/ffprobe QT_QPA_PLATFORM=offscreen python -m pytest -q
```

```powershell
# Windows
$env:ECOM_TEST_FFMPEG = (Resolve-Path .\tools\ffmpeg.exe).Path
$env:ECOM_TEST_FFPROBE = (Resolve-Path .\tools\ffprobe.exe).Path
$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest -q
```

`tests/windows_*` 中的 OBS 联调脚本属于旧版，不能用于新版。
N100 实机验收应比较原 OBS 与新版的相同摄像头输入格式、分辨率、帧率、编码质量和
音频设置，记录 CPU/GPU 占用、丢帧、长时间音画同步与切单间隙；不能只比较编码器名称。

## 打包

Windows：先准备 `tools/ffmpeg.exe` 和 `tools/ffprobe.exe`，再执行：

```powershell
.\build-windows.ps1
```

构建 EXE 与包含 FFmpeg 工具的 ZIP，输出在 `dist/`。Python 打包本身不能跨平台：
Windows EXE 在 Windows 构建，Linux 可执行文件在 Linux 构建。

Linux：安装系统 FFmpeg 7+ 后执行 `bash build-linux.sh`，输出
`dist/pack-record`。Linux 构建仍使用本机安装的 FFmpeg 和图形库。

设备接口与编码参数依据 [FFmpeg 设备文档](https://ffmpeg.org/ffmpeg-devices.html)、
[编码器文档](https://ffmpeg.org/ffmpeg-codecs.html) 和
[进度/数据包统计文档](https://ffmpeg.org/ffmpeg.html) 实现。
