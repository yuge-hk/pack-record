# FFmpeg 工具

Windows：把同套 FFmpeg 7+ 发行包的 `ffmpeg.exe`、`ffprobe.exe` 放在本目录。
选用包含 `h264_qsv` / `hevc_qsv`、`dshow` 和 AAC 的构建；保留发行包附带的说明、
许可证和依赖文件。打包脚本会复制整个 tools 目录到发布包。

Linux：建议安装发行版的 FFmpeg 7+（包含 V4L2、PulseAudio/ALSA 和所需编码器），
或在设置里指定 ffmpeg、ffprobe 路径。

程序不会下载或自动更换编码器。二进制文件不提交到 Git。
