import asyncio
import json
import os
import signal
import sys
import time

from src.main import TradingSystem

PID_PATH = "daemon.pid"
CURRENT_PROMPT_PATH = "logs/current_prompt.json"
STATUS_PATH = "logs/status.json"
ANALYSIS_STATE_PATH = "logs/analysis_state.json"


def _write_json(path, payload):
    with open(path, "w") as f:
        json.dump(payload, f)


def _ensure_singleton():
    if os.path.exists(PID_PATH):
        try:
            with open(PID_PATH) as f:
                existing_pid = int(f.read().strip() or "0")
            if existing_pid and existing_pid != os.getpid():
                os.kill(existing_pid, 0)
                print(f"daemon already running with pid {existing_pid}", flush=True)
                sys.exit(0)
        except (OSError, ValueError):
            pass

    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))


def _cleanup_pid(*_args):
    if os.path.exists(PID_PATH):
        try:
            os.remove(PID_PATH)
        except OSError:
            pass
    raise SystemExit(0)


async def main():
    os.makedirs("logs", exist_ok=True)
    _ensure_singleton()
    signal.signal(signal.SIGTERM, _cleanup_pid)
    signal.signal(signal.SIGINT, _cleanup_pid)

    system = TradingSystem()
    await system.start()
    print("SYSTEM_READY", flush=True)

    while True:
        try:
            prompt = system.get_prompt()
            _write_json(CURRENT_PROMPT_PATH, prompt)
            _write_json(ANALYSIS_STATE_PATH, system.export_analysis_state())
            _write_json(STATUS_PATH, system.get_status())
            print(f"[{time.strftime('%H:%M:%S')}] collector cycle done", flush=True)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] collector error: {e}", flush=True)

        await asyncio.sleep(30)


asyncio.run(main())
