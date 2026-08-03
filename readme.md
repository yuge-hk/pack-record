# 🎬 ecom-love-obs - 发货视频录制系统 💝

`ecom-love-obs` 是一个基于PySide6和OBS WebSockets的发货视频录制系统，专为电商发货流程设计，支持扫码自动录制、实时状态显示和数据持久化存储 🚀✨

## ✨ 功能特性

- **🔍 扫码录制**: 通过条码枪扫描快递单号自动开始录制 📱
- **⏰ 实时显示**: 在OBS中显示当前单号、时间等信息 ⏱️
- **📁 自动管理**: 自动按日期组织录制文件 📅
- **💾 数据持久化**: JSON 格式存储每日录制记录 🗄️
- **⏱️ 超时保护**: 防止长时间录制占用存储空间 ⏳
- **📊 磁盘监控**: 实时监控可用磁盘空间 💾

## 🛠️ 系统要求

- Python 3.9+ 👨‍💻
- OBS Studio (启用WebSockets插件) 🎥
- Windows/macOS/Linux 🖥️

## 🔧 安装依赖

```bash
python -m pip install -r requirements.txt
```

## ⚙️ 配置说明

修改代码开头的配置参数：

```python
OBS_WS_HOST = "localhost"          # OBS WebSockets主机地址 🌐
OBS_WS_PORT = 4455                 # OBS WebSockets端口 🚪
OBS_WS_PASSWORD = "83vMnnkOgq0nHu6l" # OBS WebSockets密码 🔑
RECORD_PATH = str(BASE_DIR / "recordings")  # 录制文件存储路径 📁
MAX_RECORD_DURATION_MS = 20 * 60 * 1000      # 最大录制时长(毫秒) ⏰
DISK_WARN_GB = 40                  # 磁盘警告阈值(GB) ⚠️
JSON_DIR = BASE_DIR / "records"   # JSON记录存储目录 📊
```

## 🎯 使用方法

1. 🚀 启动OBS Studio并确保WebSockets插件已启用
2. 🐍 运行Python脚本启动录制系统
3. 📦 扫描快递单号开始录制
4. 📺 系统会在OBS中显示当前单号和录制信息
5. 💾 录制完成后文件自动保存并记录到JSON数据库

## 🎪 特殊命令

- 输入 `STOPSTOP` 并回车可手动停止录制 🛑

## 📂 数据存储

- 📹 录制文件: `recordings/YYYY-MM-DD/` 目录
- 📋 记录数据: `records/YYYY-MM-DD.json` 文件

## 🧭 开发入口

- `main.py`：当前唯一开发入口，使用每日 JSON 文件保存记录。
- `archive/main_linux.py`：早期 Linux 实验版本，仅归档参考，不再继续开发。

录制文件优先直接重命名；当 OBS 输出目录和 `recordings` 位于不同磁盘时，
程序会自动改用复制后删除源文件的方式完成跨磁盘移动。

## 🖥️ 界面说明

- 🎨 主窗口: 控制录制流程和状态显示
- 📺 OBS信息窗口: 显示单号、时间和录制时长

## ⚠️ 注意事项

- 🔐 确保OBS WebSockets密码与配置一致
- 🟢 在OBS中使用色度键滤镜去除绿色背景
- 💾 定期清理存储空间防止磁盘满载

## 📜 开源协议

MIT License ©️

---

## 🎉 项目亮点

📦 专为电商发货优化  
🚀 高效自动化流程  
💡 智能磁盘管理  
🎯 一键式操作体验  

该项目适用于电商发货视频录制场景，可有效提高发货效率和视频管理自动化水平 💪✨
