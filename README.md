# PackRecord

Debian 打包工位扫码录像工具，当前版本 **2.1.0**。PySide6 桌面默认 1280×720（16:9），左侧取景、右侧显示单号、状态和提醒，FFmpeg 负责采集；管理员通过内置 Vue 3 + TypeScript WebUI 配置设备、查询和备份录像。无需 OBS。

当前以 Debian 13 amd64 为交付目标。Windows 工位计划使用 PackingProof，本项目暂不继续 Windows 适配；旧构建脚本保留，Windows CI 仅可手动触发。后续独立 Go 管理中心接入两套客户端，目前没有实现 Go 中心或 PackingProof-Mobile 协议。

## 安装和首次配置

```bash
sudo apt install ./pack-record_2.1.0-1_amd64.deb
pack-record
```

直接启动桌面时，同时启动本机 HTTP 服务。访问 `http://127.0.0.1:18765`，输入数据目录中的 `service.token` 管理员密钥。已安装版本数据默认在 `${XDG_DATA_HOME:-~/.local/share}/pack-record`，源码运行默认为项目目录；可通过 `--data-dir` 指定。

```bash
cat ~/.local/share/pack-record/service.token
```

在 Web 的“采集设备与首次配置”刷新设备，选择摄像头、麦克风和实际支持的分辨率、格式及帧率；保存设置后开始 3 秒试录，检查回放、音画同步和语音，再确认完成配置。初始默认 2560×1440 / 30 FPS / H.265 QSV / AAC / MKV；无可用 Intel 核显时请手动选 CPU H.264。QSV 初始化失败会报错，程序不会静默换编码器或降分辨率。

摄像头使用 V4L2；麦克风支持 PulseAudio / PipeWire 或 ALSA。推荐 `/dev/v4l/by-id/` 稳定摄像头路径，账号需有 video/render 设备访问权限。FFmpeg 编译包含 QSV 不代表硬件编码可用。

中文播报使用桌面会话的 Qt TextToSpeech / Speech Dispatcher：

```bash
sudo apt install speech-dispatcher speech-dispatcher-espeak-ng pulseaudio-utils
```

Web“在打包桌面测试语音”会向运行中的 Qt 桌面发送测试事件。浏览器和无桌面后台不直接发声；安装后仍需实际确认中文语音和扬声器输出。浏览器对 MKV/H.265 的支持有差异，正式录像可生成 H.264 MP4 兼容文件；试录还可在本机备用向导回放。

## 打包桌面

- 扫 A 开始录像；再扫 B 时先保存 A，再开始 B。保存期间收到多个单号时，只处理最新待开始单号，不形成无限队列。
- `STOPSTOP` 或“停止并保存”停止；重复扫描当前单号会忽略。
- 输入框支持键盘式扫码枪与回车，快速扫码缺少回车时有静默提交兜底。没有系统级键盘钩子。
- 原有停止、试拍、恢复任务和故障重试保留。设置按钮打开 Web。新增管理页和向导隐藏，不自动弹出；备用快捷键：`Ctrl+,` 本机设置、`Ctrl+Shift+M` 录像管理、`Ctrl+Shift+W` 配置向导。
- 预览与录像共享同一个 FFmpeg 采集进程。启动、声音/画面停滞及编码故障会锁定扫码并保留原单号。关闭程序会等待停止和保存。

## 录像、剪辑和恢复

Web 按单号、日期、工位查询，支持分页、回放、下载原片与异步剪辑。剪辑输出独立的 H.264/AAC MP4，不改原片。导出最多排队 20 个任务；导出文件目前需管理员自行管理容量，不自动过期。

录像索引使用 SQLite，历史按日 JSONL 会在启动时增量导入。录像 ID 唯一，同一单号可有多条录像。故障片段持续保留 `capture.interrupted=true`，不能作为完整打包过程的保证。

启动 FFmpeg 前持久写入恢复任务。归档使用唯一目标名、预写任务和幂等录像 ID；跨盘先复制、刷盘、校验、改名，再删临时源文件。强杀后重新启动，使用“恢复保存任务”；不能通过媒体验证的文件继续保留供人工检查。旧 OBS 设置和旧恢复任务不会被覆盖。

## 存储和离线补传

Web 可配置多个录像目录、保留天数和水位。工位在两段录像之间选择可用目录；不会在一段录像中途切换磁盘。主目录首次可自动创建，附加目录必须预先挂载并存在。使用过的目录持久保存磁盘标识，标识缺失时拒绝写入，防止拔盘后把空挂载点当作原磁盘。

默认低于 40 GB 预警，低于 10 GB 禁止新录像并停止当前录像；测试机可调整。自动清理默认关闭，支持：

| 策略 | 条件 |
| --- | --- |
| 关闭 | 不自动删除 |
| 已验证备份 | 超期或低于清理水位，且已收到校验确认 |
| 纯本地循环 | 未配置上传时，可清理超期或低水位录像；配置上传后仍必须验证成功 |

清理只处理索引内、受管理目录下的原片；正在回放、导出或待上传/失败上传的录像受保护。修改过的文件拒绝自动清理。清理记录保留审计和删除标记，上传成功本身不立即删除原片。

当前备份支持：

1. **挂载目录**：本地目录或已挂载 SMB/NFS；复制、刷盘及 SHA256 核验通过后标记已验证。挂载和账号由系统管理。
2. **其他 PackRecord 中心**：由中心签发绑定工位 ID 的上传密钥；分块续传、分块校验、整片 SHA256、媒体探测和持久回执。断线可继续本地录像，队列在重启后继续；失败退避重试，8 次后可手动重试。

目标设置随任务保存，修改设置不会悄悄改变旧任务目标。上传密钥轮换后，管理员需为受影响录像重新排队并处理旧失败任务；当前无 Web 删除队列入口。不要随意复制整套数据目录作为新工位，否则会复制节点身份。直连 S3、WebDAV、PackingProof 和 Mobile 上传尚未实现。

## 订单语音

Web 可录入单号、商品件数、买家留言、卖家备注、商品摘要及退款状态。匹配单号实际开始录制后，桌面播报多件商品、备注和退款警告；录制中收到更新也会提醒。默认超过 24 小时的订单信息不播报，可配置 1～168 小时。

退款提醒不执行电商平台拦单。XLSX、油猴和平台 API 适配尚未实现；预留通用 `/v1/orders` JSON 入口，订单未导入时不推测件数或退款状态。

## 角色和服务运行

默认 `integrated`，本机录像与保存中心一体。另有 `station` 录像工位、`center` 只做保存中心、`viewer` 查看角色。扫码切单始终由本地串行控制器负责；备份中心离线不能阻断本地录像。

直接启动桌面时，退出桌面也会退出服务。若需桌面退出后后台仍录像，可使用独立后台：

```bash
pack-record --service --data-dir /path/to/station-data
pack-record --connect --data-dir /path/to/station-data
```

包还带有默认不启用的 systemd 服务，运行账号 `pack-record`，数据目录 `/var/lib/pack-record`：

```bash
sudo systemctl enable --now pack-record.service
sudo usermod -aG pack-record "$USER"
# 注销重新登录后，启动“打包录像（后台服务）”或：
pack-record --connect --token-file /var/lib/pack-record/service.token
```

systemd 服务没有用户的 PulseAudio 会话；应在 Web 设置适合服务账号的 ALSA 输入，或自行配置音频访问。服务开启 `ProtectHome=true`，录像盘建议挂到 `/mnt` 或 `/srv` 并授权 pack-record 账号。

默认只监听 127.0.0.1。在 Web 允许局域网后需重启后台。HTTP 不提供传输加密，只在可信网络使用；跨网访问应放在 TLS 反向代理或安全隧道之后，不要把管理员令牌直接暴露到公网。

管理员密钥、查看密钥、上传设备密钥分权。查看端可以查询、回放、下载和创建剪辑，不能扫码、更改配置或清理；设备密钥只能访问节点信息和自身上传。设置和队列含目标凭据，数据目录及备份应限制读取权限。媒体链接为限时持有者凭证，不要公开分享。

## 数据与接入契约

| 路径 | 内容 |
| --- | --- |
| `.station-id` | 首次生成的节点身份 |
| `recorder-settings.json` | 设备、目录、角色、播报和目标凭据 |
| `service.token` / `viewer.token` | 管理 / 查看密钥 |
| `records/library.sqlite3` | 录像索引、队列、订单、审计、同步修订 |
| `records/YYYY-MM-DD.jsonl` | 追加录像日志 |
| `records/recorder-failed-jobs.json` | 待恢复任务 |
| `recordings-in-progress/` | 录制中的原始文件 |
| `recordings/YYYY-MM-DD/` | 默认正式归档 |
| `exports/` / `setup-tests/` | 独立剪辑 / 配置试录 |
| `logs/` | 轮转日志 |

未来 Go 中心接入约定见 [docs/pack-record-v1.md](docs/pack-record-v1.md)。协议独立于 Python 类及 SQLite 表；稳定节点身份、持久同步游标、删除标记、设备凭据和校验回执已预留并实现基础接口。Go 中心、主动出站接入及远程控制不在当前实现内。

## 开发与构建

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-build.txt -r requirements-dev.txt -c .github/build-constraints.txt
npm --prefix web ci
npm --prefix web run build
ECOM_TEST_FFMPEG=/usr/bin/ffmpeg ECOM_TEST_FFPROBE=/usr/bin/ffprobe python -m pytest -q
./build-linux.sh
```

构建需在 Debian 13 amd64 上进行，并安装 Node.js >=22.12。产物在 `dist/releases/`，前端先进行 vue-tsc 类型检查和 Vite 构建，静态资源随程序打包，安装后无需 Node.js 和 CDN。前端开发说明见 [web/README.md](web/README.md)。包使用系统 FFmpeg 7+；`pack-record --self-test` 只检查 GUI、运行库和编码器编译支持，不能代替摄像头、QSV、中文发声和磁盘实机验收。

重点实机验收：连续切单、目标摄像头及音频同步、麦克风/USB 断开、拔盘和写满、强杀后恢复、中心断线重试、中文播报、退出保存。仓库测试使用合成音视频覆盖软件流程，不代表上述硬件已经通过验收。
