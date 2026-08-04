"""Windows/OBS 集成测试辅助命令；不由生产程序调用。"""

import argparse
import json
import sys
from pathlib import Path

import obsws_python as obs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import OBS_WS_HOST, OBS_WS_PASSWORD, OBS_WS_PORT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("status", "start", "stop"))
    args = parser.parse_args()

    client = obs.ReqClient(
        host=OBS_WS_HOST,
        port=OBS_WS_PORT,
        password=OBS_WS_PASSWORD,
        timeout=3,
    )
    if args.action == "start":
        client.start_record()
    elif args.action == "stop":
        response = client.stop_record()
        print(json.dumps({"output_path": response.output_path}, ensure_ascii=False))
        return

    status = client.get_record_status()
    directory = client.get_record_directory()
    print(
        json.dumps(
            {
                "output_active": bool(status.output_active),
                "output_path": getattr(status, "output_path", None),
                "record_directory": directory.record_directory,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
