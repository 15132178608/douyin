"""Local Web server lifecycle state helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Callable

from src.config import PROJECT_ROOT


DEFAULT_RUNTIME_DIR = PROJECT_ROOT / "data" / "runtime"
PID_FILENAME = "server.pid"
STATE_FILENAME = "server.json"


@dataclass(frozen=True)
class ServerState:
    pid: int
    host: str
    port: int
    started_at: str

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _runtime_dir(runtime_dir: Path | None = None) -> Path:
    return Path(runtime_dir) if runtime_dir is not None else DEFAULT_RUNTIME_DIR


def _pid_path(runtime_dir: Path | None = None) -> Path:
    return _runtime_dir(runtime_dir) / PID_FILENAME


def _state_path(runtime_dir: Path | None = None) -> Path:
    return _runtime_dir(runtime_dir) / STATE_FILENAME


def write_server_state(
    *,
    pid: int,
    host: str,
    port: int,
    runtime_dir: Path | None = None,
    started_at: str | None = None,
) -> ServerState:
    root = _runtime_dir(runtime_dir)
    root.mkdir(parents=True, exist_ok=True)
    state = ServerState(
        pid=int(pid),
        host=str(host),
        port=int(port),
        started_at=started_at or datetime.now(timezone.utc).isoformat(),
    )
    _pid_path(root).write_text(f"{state.pid}\n", encoding="utf-8")
    _state_path(root).write_text(
        json.dumps(
            {
                "pid": state.pid,
                "host": state.host,
                "port": state.port,
                "started_at": state.started_at,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return state


def read_server_state(runtime_dir: Path | None = None) -> ServerState | None:
    state_path = _state_path(runtime_dir)
    pid_path = _pid_path(runtime_dir)
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            return ServerState(
                pid=int(data["pid"]),
                host=str(data.get("host") or "127.0.0.1"),
                port=int(data.get("port") or 8000),
                started_at=str(data.get("started_at") or ""),
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return ServerState(pid=pid, host="127.0.0.1", port=8000, started_at="")


def clear_server_state(runtime_dir: Path | None = None) -> None:
    for path in (_state_path(runtime_dir), _pid_path(runtime_dir)):
        if path.exists() and path.is_file():
            path.unlink()


def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_server_status(
    *,
    runtime_dir: Path | None = None,
    process_checker: Callable[[int], bool] | None = None,
) -> dict:
    checker = process_checker or is_process_running
    state = read_server_state(runtime_dir)
    if state is None:
        return {
            "state": "stopped",
            "running": False,
            "pid": None,
            "url": None,
            "message": "本地 Web 服务未运行。",
        }
    running = checker(state.pid)
    status = {
        "state": "running" if running else "stale",
        "running": running,
        "pid": state.pid,
        "host": state.host,
        "port": state.port,
        "url": state.url,
        "started_at": state.started_at,
    }
    if running:
        status["message"] = f"本地 Web 服务正在运行：{state.url} (pid={state.pid})"
    else:
        status["message"] = f"发现陈旧 PID 记录：pid={state.pid}，进程已不存在。"
    return status


def should_start_server(
    *,
    runtime_dir: Path | None = None,
    process_checker: Callable[[int], bool] | None = None,
    port_owner_checker: Callable[[int], int | None] | None = None,
) -> dict:
    status = get_server_status(runtime_dir=runtime_dir, process_checker=process_checker)
    if status["state"] == "running":
        owner_checker = port_owner_checker or (get_port_owner_pid if sys.platform.startswith("win") else None)
        if owner_checker is not None:
            owner_pid = owner_checker(int(status["port"]))
            if owner_pid is None:
                clear_server_state(runtime_dir)
                return {
                    "ok": True,
                    "status": status,
                    "message": f"没有发现端口监听，已清理 PID 记录：pid={status['pid']} port={status['port']}",
                }
            if owner_pid != int(status["pid"]):
                clear_server_state(runtime_dir)
                return {
                    "ok": True,
                    "status": status,
                    "message": f"PID 记录和端口监听进程不匹配，已清理记录：pid={status['pid']} port_owner={owner_pid}",
                }
        return {
            "ok": False,
            "status": status,
            "message": f"本地 Web 服务已在运行：{status['url']}",
        }
    if status["state"] == "stale":
        clear_server_state(runtime_dir)
    return {"ok": True, "status": status, "message": "可以启动本地 Web 服务。"}


def terminate_process(pid: int) -> None:
    if sys.platform.startswith("win"):
        result = subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"taskkill failed for pid={int(pid)}: {detail}")
        return
    os.kill(pid, signal.SIGTERM)


def get_port_owner_pid(port: int) -> int | None:
    if not sys.platform.startswith("win"):
        return None
    command = (
        "$c = Get-NetTCPConnection -LocalPort "
        f"{int(port)} -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; "
        "if ($c) { $c.OwningProcess }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        return int(text.splitlines()[0].strip())
    except ValueError:
        return None


def wait_until_server_stopped(
    *,
    pid: int,
    port: int,
    process_checker: Callable[[int], bool],
    port_owner_checker: Callable[[int], int | None],
    attempts: int = 20,
    interval_seconds: float = 0.25,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    total_attempts = max(1, int(attempts or 1))
    for attempt in range(total_attempts):
        running = process_checker(pid)
        owner_pid = port_owner_checker(port)
        if not running and owner_pid != pid:
            return True
        if attempt < total_attempts - 1:
            sleeper(max(0.0, float(interval_seconds)))
    return False


def stop_recorded_server(
    *,
    runtime_dir: Path | None = None,
    process_checker: Callable[[int], bool] | None = None,
    port_owner_checker: Callable[[int], int | None] | None = None,
    terminator: Callable[[int], None] | None = None,
    wait_attempts: int = 20,
    wait_interval_seconds: float = 0.25,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict:
    checker = process_checker or is_process_running
    owner_checker = port_owner_checker or get_port_owner_pid
    stop_one = terminator or terminate_process
    status = get_server_status(runtime_dir=runtime_dir, process_checker=checker)
    if status["state"] == "stopped":
        return {"stopped": False, "status": status, "message": "本地 Web 服务未运行。"}
    if status["state"] == "stale":
        clear_server_state(runtime_dir)
        return {"stopped": False, "status": status, "message": "已清理陈旧 PID 记录。"}
    owner_pid = owner_checker(int(status["port"]))
    if owner_pid is None:
        clear_server_state(runtime_dir)
        return {
            "stopped": False,
            "status": status,
            "message": f"没有发现端口监听，已清理 PID 记录：pid={status['pid']} port={status['port']}",
        }
    if owner_pid is not None and owner_pid != int(status["pid"]):
        clear_server_state(runtime_dir)
        return {
            "stopped": False,
            "status": status,
            "message": f"PID 记录和端口监听进程不匹配，已清理记录：pid={status['pid']} port_owner={owner_pid}",
        }
    try:
        stop_one(int(status["pid"]))
    except Exception as e:
        return {
            "stopped": False,
            "status": status,
            "message": f"停止失败，已保留 PID 记录：pid={status['pid']} ({e})",
        }
    confirmed = wait_until_server_stopped(
        pid=int(status["pid"]),
        port=int(status["port"]),
        process_checker=checker,
        port_owner_checker=owner_checker,
        attempts=wait_attempts,
        interval_seconds=wait_interval_seconds,
        sleeper=sleeper,
    )
    if not confirmed:
        return {
            "stopped": False,
            "status": status,
            "message": f"未确认停止，已保留 PID 记录：pid={status['pid']} port={status['port']}",
        }
    clear_server_state(runtime_dir)
    return {"stopped": True, "status": status, "message": f"已请求停止本地 Web 服务：pid={status['pid']}"}
