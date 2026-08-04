# ecom-love-obs

面向电商发货场景的 Windows 扫码录像工具。程序通过 OBS WebSocket 控制录像，按快递单号整理视频，并保存可审计的 JSONL 记录。

## 主要能力

- 扫码开始录像，再扫新单号时停止当前录像并切换。
- 快速连续扫码采用“最新值覆盖”策略：不建立队列，只保留最后一次扫描的单号。
- 等待 OBS 确认真实进入录制状态后才更新内部状态，避免快速扫码造成状态错乱。
- OBS 启动、停止和状态查询请求串行执行；请求超时后查询真实状态，不直接假定成功或失败。
- 同时监听录像事件并定期轮询；录像被外部停止时也能自动对账并归档。
- 条码枪在程序内焦点不位于输入框时仍可录入；1 秒内同一单号的重复扫描会被忽略。
- 扫码框聚焦时自动锁定英文键盘布局，并对被输入法吞掉的回车提供快速输入自动提交兜底。
- 自动启动和重连 OBS；OBS 异常退出时自动确认“以正常模式运行”，无需重启应用。
- Windows 下自动左右排列控制窗口与 OBS，并聚焦扫码输入框。
- OBS 信息窗口默认隐藏，可按需显示给 OBS 捕获。
- 程序启动时若 OBS 已在录像，可选择接管、停止并保存，或保持录像并锁定扫码。
- 关闭程序时若仍在录像，会先请求停止并等待文件保存完成。
- Windows 下禁止启动多个程序实例。
- 文件名会过滤 Windows 非法字符，原始单号仍完整写入记录。
- 同盘使用原子移动；跨磁盘使用“复制到唯一临时文件 → 刷盘 → 原子改名 → 删除源文件”。
- 归档前确认文件非空且大小稳定，OBS 崩溃产生的空文件不会被记录为成功录像。
- 保存采用预写恢复任务；在移动文件或写 JSONL 的任意阶段退出后均可幂等重试。
- 损坏的 `failed_jobs.json` 会另存为 `.corrupt-*`，不会被静默覆盖。
- 同时监控归档盘和不同卷上的 OBS 输出盘；任一磁盘低于 40 GB 时警告，低于 10 GB 时禁止开始新录像；跨盘复制额外保留 1 GB。
- 运行日志按大小轮转，最多保留 5 份历史日志。
- 内置“常规 / OBS / 扫码与存储 / 高级”设置面板，配置保存后即时生效。

## 运行要求

- Windows 10/11
- Python 3.9+
- OBS Studio，启用内置 WebSocket 服务（端口 `4455`）

Linux 旧版本仅保存在 `archive/main_linux.py`，当前不继续开发。

## 独立 EXE

从 Gitea Release 下载 `ecom-love-obs-v1.0.0-windows-x64.zip`，解压到具有写入权限的
目录后直接运行 `ecom-love-obs.exe`，无需安装 Python。首次启动会在 EXE 同级目录
创建 `recordings`、`records`、`logs` 等运行目录；请勿直接放入需要管理员权限写入的
`Program Files`。

## 安装与运行

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python main.py
```

程序会在 OBS 标准安装目录中查找 `obs64.exe`。便携版或自定义安装位置需要手动启动 OBS；程序仍会自动重连。

建议在 OBS 中使用 MKV 或 Hybrid MP4/MOV。普通 MP4 在 OBS 被强制结束时可能只留下无法恢复的空文件；程序会将这类文件标记为恢复任务，但无法重建 OBS 未写出的媒体数据。

## 操作逻辑

1. 扫描单号 A，程序请求 OBS 开始录像。
2. OBS 确认录像后，界面显示“录制中”。
3. 扫描单号 B，程序停止并保存 A，然后开始 B。
4. 如果保存 A 期间又快速扫描 C、D，只保留 D；B、C 不会进入等待队列。
5. 输入 `STOPSTOP` 并回车可停止当前录像且不启动下一单。

## 数据目录

- 视频：`recordings/YYYY-MM-DD/单号.<OBS 输出格式>`
- 记录：`records/YYYY-MM-DD.jsonl`，每行一个独立 JSON 对象；schema v2 包含幂等的 `recording_id`
- 保存失败任务：`records/failed_jobs.json`
- 日志：`logs/app.log`，单文件 5 MB，保留 5 个备份

录像文件、运行记录和日志均已加入 `.gitignore`。

## 设置面板

主界面右侧的“设置”按钮可调整，也可按 `Ctrl+,` 快速打开：

- 录像归档目录、最长录像时长、OBS 信息窗口默认状态。
- OBS 自动启动、窗口自动排列、扫码框英文输入法锁定。
- OBS 主程序路径和 WebSocket 地址、端口、密码。
- 磁盘警告/禁止阈值、跨盘复制预留空间、同单号防重复窗口。
- 开始录像确认超时和停止录像重试次数。

设置保存在项目目录的 `settings.json`，该文件已加入 `.gitignore`。首次运行或
设置文件缺失时使用代码内默认值；损坏或不合法的设置会备份为
`settings.json.corrupt-*` 后恢复默认值。按内部开发环境要求，OBS WebSocket
密码仍以明文保存在本机设置文件中。

修改录像目录或 OBS 连接参数需要处于空闲状态。恢复事务、录像文件完整性检查、
“只保留最新扫码”等安全规则不会出现在设置面板中，避免误操作破坏一致性。

## 开发与测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m py_compile main.py app_core.py
```

`app_core.py` 保存不依赖 Qt/OBS 的文件移动、JSONL 和失败任务逻辑，便于自动化测试。`main.py` 是当前唯一应用入口。

Windows 实机/虚拟机联调辅助工具：

- `tests/windows_obs_control.py`：查询、开始或外部停止 OBS 录像。
- `tests/windows_gui_scenario.ps1`：执行快速扫码、重复扫码、STOPSTOP、中文输入法和压力场景。

Windows 独立 EXE 构建：

```powershell
.\build-windows.ps1
```

构建产物位于 `dist\ecom-love-obs.exe`，文件版本与产品版本均为 `1.0.0`。
