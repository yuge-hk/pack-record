# ecom-love-obs

面向电商发货场景的 Windows 扫码录像工具。程序通过 OBS WebSocket 控制录像，按快递单号整理视频，并保存可审计的 JSONL 记录。

## 主要能力

- 扫码开始录像，再扫新单号时停止当前录像并切换。
- 快速连续扫码采用“最新值覆盖”策略：不建立队列，只保留最后一次扫描的单号。
- 等待 OBS 确认真实进入录制状态后才更新内部状态，避免快速扫码造成状态错乱。
- 条码枪在程序内焦点不位于输入框时仍可录入；1 秒内同一单号的重复扫描会被忽略。
- 自动启动和重连 OBS；Windows 下自动左右排列控制窗口与 OBS，并聚焦扫码输入框。
- OBS 信息窗口默认隐藏，可按需显示给 OBS 捕获。
- 程序启动时若 OBS 已在录像，可选择接管、停止并保存，或保持录像并锁定扫码。
- 关闭程序时若仍在录像，会先请求停止并等待文件保存完成。
- Windows 下禁止启动多个程序实例。
- 文件名会过滤 Windows 非法字符，原始单号仍完整写入记录。
- 同盘使用原子移动；跨磁盘使用“复制到临时文件 → 原子改名 → 删除源文件”。
- 保存失败任务写入 `records/failed_jobs.json`，可在界面中重试恢复。
- 磁盘低于 40 GB 时警告，低于 10 GB 时禁止开始新录像；跨盘复制额外保留 1 GB。
- 运行日志按大小轮转，最多保留 5 份历史日志。

## 运行要求

- Windows 10/11
- Python 3.9+
- OBS Studio，启用内置 WebSocket 服务（端口 `4455`）

Linux 旧版本仅保存在 `archive/main_linux.py`，当前不继续开发。

## 安装与运行

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python main.py
```

程序会在 OBS 标准安装目录中查找 `obs64.exe`。便携版或自定义安装位置需要手动启动 OBS；程序仍会自动重连。

## 操作逻辑

1. 扫描单号 A，程序请求 OBS 开始录像。
2. OBS 确认录像后，界面显示“录制中”。
3. 扫描单号 B，程序停止并保存 A，然后开始 B。
4. 如果保存 A 期间又快速扫描 C、D，只保留 D；B、C 不会进入等待队列。
5. 输入 `STOPSTOP` 并回车可停止当前录像且不启动下一单。

## 数据目录

- 视频：`recordings/YYYY-MM-DD/单号.mkv`
- 记录：`records/YYYY-MM-DD.jsonl`，每行一个独立 JSON 对象
- 保存失败任务：`records/failed_jobs.json`
- 日志：`logs/app.log`，单文件 5 MB，保留 5 个备份

录像文件、运行记录和日志均已加入 `.gitignore`。

## 固定配置

项目按内部开发环境要求保留代码内配置，位于 `main.py` 顶部：

```python
OBS_WS_HOST = "localhost"
OBS_WS_PORT = 4455
OBS_WS_PASSWORD = "..."
RECORD_PATH = str(BASE_DIR / "recordings")
DISK_WARN_GB = 40
DISK_CRITICAL_GB = 10
```

## 开发与测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m py_compile main.py app_core.py
```

`app_core.py` 保存不依赖 Qt/OBS 的文件移动、JSONL 和失败任务逻辑，便于自动化测试。`main.py` 是当前唯一应用入口。
