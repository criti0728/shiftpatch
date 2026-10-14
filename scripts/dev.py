#!/usr/bin/env python3
"""Run both local development servers; Ctrl+C stops both process groups."""
from pathlib import Path
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.settings import GeminiSettings, agent_timeout_seconds, load_env


def web_dependencies_ready():
    web = ROOT / "apps/web"
    packages = json.loads((web / "package.json").read_text())
    required = list(packages["dependencies"]) + ["vite", "typescript"]
    return all((web / "node_modules" / name / "package.json").is_file() for name in required) and all(
        (web / "node_modules" / path).is_file()
        for path in ["vite/bin/vite.js", "typescript/bin/tsc"]
    )


def servers_ready():
    try:
        with urlopen("http://127.0.0.1:8000/api/health", timeout=0.5) as response:
            if json.load(response).get("status") != "ok":
                return False
        with urlopen("http://127.0.0.1:5173/", timeout=0.5) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def main():
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    npm = shutil.which("npm")
    if not python.exists() or not npm:
        sys.exit("먼저 README의 설치 명령을 실행해 주세요. (.venv, npm, node_modules 필요)")
    if not web_dependencies_ready():
        sys.exit("프런트엔드 패키지 설치가 완료되지 않았습니다. npm ci --prefix apps/web 가 성공한 뒤 다시 실행해 주세요.")
    try:
        load_env()
        settings = GeminiSettings.from_env()
        if settings.mode == "gemini":
            settings.require_credentials()
            agent_timeout_seconds()
    except ValueError as error:
        sys.exit(str(error))
    os.environ.setdefault("SHIFTPATCH_AI_MODE", settings.mode)
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
        deadline = time.monotonic() + 20
        while all(process.poll() is None for process in processes):
            if servers_ready():
                break
            if time.monotonic() >= deadline:
                print("서버 시작을 확인하지 못했습니다. 위 오류와 포트 8000·5173 상태를 확인해 주세요.", flush=True)
                return 1
            time.sleep(0.2)
        else:
            return next(process.returncode for process in processes if process.poll() is not None) or 1
        print("\nShiftPatch 실행 준비 완료: http://127.0.0.1:5173  |  API: http://127.0.0.1:8000/docs", flush=True)
        print(f"AI: {settings.description()}  |  Ctrl+C to stop\n", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.3)
        failed = next(process for process in processes if process.poll() is not None)
        return failed.returncode or 1
    except KeyboardInterrupt:
        return 0
    finally:
        for process in processes:
            if process.poll() is not None:
                continue
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
