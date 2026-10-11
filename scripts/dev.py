#!/usr/bin/env python3
"""Run both local development servers; Ctrl+C stops both process groups."""
from pathlib import Path
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def load_env():
    path = ROOT / ".env"
    if not path.exists():
        return
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, raw = line.partition("=")
        name = name.strip()
        if not separator or not name.replace("_", "").isalnum():
            raise ValueError(f".env:{number}: expected NAME=value")
        values = shlex.split(raw, comments=True)
        if len(values) > 1:
            raise ValueError(f".env:{number}: quote values containing spaces")
        os.environ.setdefault(name, values[0] if values else "")


def main():
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    npm = shutil.which("npm")
    if not python.exists() or not (ROOT / "apps/web/node_modules").exists() or not npm:
        sys.exit("먼저 README의 설치 명령을 실행해 주세요. (.venv, npm, node_modules 필요)")
    load_env()
    os.environ.setdefault("SHIFTPATCH_AI_MODE", "calculation_only")
    processes = []

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        for command in [
            [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"],
            [npm, "--prefix", "apps/web", "run", "dev", "--", "--port", "5173", "--strictPort"],
        ]:
            processes.append(subprocess.Popen(command, cwd=ROOT, start_new_session=os.name != "nt"))
        print("\nShiftPatch: http://127.0.0.1:5173  |  API: http://127.0.0.1:8000/docs", flush=True)
        print(f"AI mode: {os.environ['SHIFTPATCH_AI_MODE']}  |  Ctrl+C to stop\n", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.3)
        failed = next(process for process in processes if process.poll() is not None)
        return failed.returncode or 1
    except KeyboardInterrupt:
        return 0
    finally:
        for process in processes:
            try:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)


if __name__ == "__main__":
    sys.exit(main())
