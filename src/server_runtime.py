"""Local Web server lifecycle state helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ctypes
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


def _is_process_running_windows(pid: int) -> bool:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        return _is_process_running_windows(int(pid))
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


def get_service_audit(
    *,
    runtime_dir: Path | None = None,
    configured_port: int = 8000,
    process_checker: Callable[[int], bool] | None = None,
    port_owner_checker: Callable[[int], int | None] | None = None,
) -> dict:
    """Return read-only guidance about the recorded service and port listener."""
    checker = process_checker or is_process_running
    owner_checker = port_owner_checker or get_port_owner_pid
    status = get_server_status(runtime_dir=runtime_dir, process_checker=checker)
    port = int(status.get("port") or configured_port or 8000)
    recorded_pid = status.get("pid")
    port_owner_pid = owner_checker(port)

    def audit(relation: str, action: str, message: str, next_step: str) -> dict:
        return {
            "relation": relation,
            "action": action,
            "port": port,
            "recorded_pid": recorded_pid,
            "port_owner_pid": port_owner_pid,
            "message": message,
            "next_step": next_step,
            "status": status,
        }

    if status["state"] == "stopped":
        if port_owner_pid is None:
            return audit(
                "clear",
                "start",
                f"端口 {port} 没有后台 Web 服务占用。",
                "需要使用时运行 uv run python -m src.cli serve，或点击 Douyin Recall。",
            )
        return audit(
            "external_listener",
            "inspect_external",
            f"端口 {port} 正被 pid={port_owner_pid} 占用，但没有本项目服务记录。",
            f"不要用 python -m src.cli stop 结束未知进程；请先确认 pid={port_owner_pid}，或修改 .env 的 WEB_PORT 后重试。",
        )

    if status["state"] == "stale":
        if port_owner_pid is None:
            return audit(
                "stale_record",
                "repair",
                f"发现陈旧服务记录 pid={recorded_pid}，端口 {port} 没有监听。",
                "运行 uv run python -m src.cli stop，或点击 Douyin Recall Repair State 清理陈旧状态。",
            )
        return audit(
            "stale_record_with_listener",
            "repair",
            f"服务记录 pid={recorded_pid} 已陈旧，但端口 {port} 正被 pid={port_owner_pid} 占用。",
            f"先运行 uv run python -m src.cli stop 或 Douyin Recall Repair State 清理项目状态；不要结束 pid={port_owner_pid}，除非你确认它是可关闭的进程。",
        )

    if port_owner_pid is None:
        return audit(
            "record_without_listener",
            "repair",
            f"服务记录 pid={recorded_pid} 仍存在，但端口 {port} 没有监听。",
            "运行 uv run python -m src.cli stop，或点击 Douyin Recall Repair State 清理状态，然后重新检查。",
        )
    if recorded_pid is not None and int(port_owner_pid) == int(recorded_pid):
        return audit(
            "own_service_running",
            "stop",
            f"Douyin Recall 记录的服务 pid={recorded_pid} 正在占用端口 {port}。",
            "运行 uv run python -m src.cli stop，或点击 Douyin Recall Stop Service。",
        )
    return audit(
        "record_port_mismatch",
        "repair",
        f"服务记录 pid={recorded_pid} 与端口 {port} 的 owner pid={port_owner_pid} 不一致。",
        f"运行 uv run python -m src.cli stop 或 Douyin Recall Repair State 清理项目状态；不要结束 pid={port_owner_pid}，除非你确认它是可关闭的进程。",
    )


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
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr or b""
            stdout = result.stdout or b""
            if isinstance(stderr, bytes):
                stderr_text = stderr.decode("utf-8", errors="ignore")
            else:
                stderr_text = str(stderr)
            if isinstance(stdout, bytes):
                stdout_text = stdout.decode("utf-8", errors="ignore")
            else:
                stdout_text = str(stdout)
            detail = (stderr_text or stdout_text or "").strip()
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
