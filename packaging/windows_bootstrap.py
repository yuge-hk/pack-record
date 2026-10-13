"""Entry point for the embedded Windows runtime (no system Python required)."""
import os
import sys
import traceback
from pathlib import Path

sys._pack_record_bundle = True
try:
    from main import main
    sys.exit(main())
except Exception:
    error = traceback.format_exc()
    directory = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'PackRecord'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'startup-error.log').write_text(error, encoding='utf-8')
    if '--self-test' in sys.argv:
        print(error, file=sys.stderr)
    else:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f'程序无法启动。错误日志：{directory / "startup-error.log"}\n\n{error[-1500:]}', 'PackRecord', 0x10)
    sys.exit(1)
