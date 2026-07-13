"""
Server lifecycle state tests.

Run:
    python tests/test_server_runtime.py
"""
from __future__ import annotations

import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click.testing import CliRunner

from src import cli as cli_module
from src import server_runtime


def test_write_and_read_server_state_round_trips_pid_and_url() -> None:
    with TemporaryDirectory() as tmp:
        state = server_runtime.write_server_state(
            pid=1234,
            host="127.0.0.1",
            port=8000,
            runtime_dir=Path(tmp),
        )

        loaded = server_runtime.read_server_state(runtime_dir=Path(tmp))

        assert state.pid == 1234
        assert loaded is not None
        assert loaded.pid == 1234
        assert loaded.host == "127.0.0.1"
        assert loaded.port == 8000
        assert loaded.url == "http://127.0.0.1:8000"
        assert (Path(tmp) / "server.pid").read_text(encoding="utf-8").strip() == "1234"


def test_get_server_status_distinguishes_running_from_stale_pid() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(pid=2222, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        running = server_runtime.get_server_status(
            runtime_dir=runtime_dir,
            process_checker=lambda pid: pid == 2222,
        )
        stale = server_runtime.get_server_status(
            runtime_dir=runtime_dir,
            process_checker=lambda pid: False,
        )

        assert running["state"] == "running"
        assert running["url"] == "http://127.0.0.1:8000"
        assert stale["state"] == "stale"
        assert stale["pid"] == 2222


def test_should_start_server_blocks_duplicate_running_process() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(pid=3333, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        decision = server_runtime.should_start_server(
            runtime_dir=runtime_dir,
            process_checker=lambda pid: pid == 3333,
            port_owner_checker=lambda port: 3333,
        )

        assert decision["ok"] is False
        assert "已在运行" in decision["message"]
        assert decision["status"]["url"] == "http://127.0.0.1:8000"


def test_should_start_server_clears_running_pid_when_port_has_no_listener() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(pid=3334, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        decision = server_runtime.should_start_server(
            runtime_dir=runtime_dir,
            process_checker=lambda pid: pid == 3334,
            port_owner_checker=lambda port: None,
        )

        assert decision["ok"] is True
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) is None
        assert "没有发现端口监听" in decision["message"]


def test_should_start_server_clears_running_pid_when_port_owner_mismatches() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(pid=3335, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        decision = server_runtime.should_start_server(
            runtime_dir=runtime_dir,
            process_checker=lambda pid: pid == 3335,
            port_owner_checker=lambda port: 9999,
        )

        assert decision["ok"] is True
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) is None
        assert "不匹配" in decision["message"]


def test_stop_recorded_server_calls_terminator_and_clears_state() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        stopped: list[int] = []
        process_states = [True, False]
        owners = [4444, None]
        server_runtime.write_server_state(pid=4444, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        result = server_runtime.stop_recorded_server(
            runtime_dir=runtime_dir,
            process_checker=lambda pid: process_states.pop(0),
            port_owner_checker=lambda port: owners.pop(0),
            terminator=lambda pid: stopped.append(pid),
            wait_attempts=1,
            wait_interval_seconds=0,
        )

        assert result["stopped"] is True
        assert stopped == [4444]
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) is None
        assert not (runtime_dir / "server.pid").exists()


def test_stop_recorded_server_refuses_when_recorded_pid_does_not_own_port() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        stopped: list[int] = []
        server_runtime.write_server_state(pid=5555, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        result = server_runtime.stop_recorded_server(
            runtime_dir=runtime_dir,
            process_checker=lambda pid: pid == 5555,
            port_owner_checker=lambda port: 9999,
            terminator=lambda pid: stopped.append(pid),
        )

        assert result["stopped"] is False
        assert stopped == []
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) is None
        assert "不匹配" in result["message"]


def test_stop_recorded_server_refuses_when_port_has_no_listener() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        stopped: list[int] = []
        server_runtime.write_server_state(pid=6666, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        result = server_runtime.stop_recorded_server(
            runtime_dir=runtime_dir,
            process_checker=lambda pid: pid == 6666,
            port_owner_checker=lambda port: None,
            terminator=lambda pid: stopped.append(pid),
        )

        assert result["stopped"] is False
        assert stopped == []
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) is None
        assert "没有发现端口监听" in result["message"]


def test_stop_recorded_server_keeps_state_when_terminator_fails() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(pid=7777, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        result = server_runtime.stop_recorded_server(
            runtime_dir=runtime_dir,
            process_checker=lambda pid: pid == 7777,
            port_owner_checker=lambda port: 7777,
            terminator=lambda pid: (_ for _ in ()).throw(RuntimeError("taskkill failed")),
        )

        assert result["stopped"] is False
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) is not None
        assert "停止失败" in result["message"]


def test_stop_recorded_server_waits_for_port_to_release_before_clearing_state() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        sleeps: list[float] = []
        owners = [7778, 7778, None]
        process_states = [True, False, False]
        server_runtime.write_server_state(pid=7778, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        result = server_runtime.stop_recorded_server(
            runtime_dir=runtime_dir,
            process_checker=lambda pid: process_states.pop(0),
            port_owner_checker=lambda port: owners.pop(0),
            terminator=lambda pid: None,
            wait_attempts=3,
            wait_interval_seconds=0.01,
            sleeper=lambda seconds: sleeps.append(seconds),
        )

        assert result["stopped"] is True
        assert sleeps == [0.01]
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) is None


def test_stop_recorded_server_keeps_state_when_port_remains_after_terminator() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        process_states = [True, False]
        server_runtime.write_server_state(pid=7779, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        result = server_runtime.stop_recorded_server(
            runtime_dir=runtime_dir,
            process_checker=lambda pid: process_states.pop(0),
            port_owner_checker=lambda port: 7779,
            terminator=lambda pid: None,
            wait_attempts=1,
            wait_interval_seconds=0,
            sleeper=lambda seconds: None,
        )

        assert result["stopped"] is False
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) is not None
        assert "未确认停止" in result["message"]


def test_service_audit_identifies_recorded_service_owning_port() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(pid=1111, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        audit = server_runtime.get_service_audit(
            runtime_dir=runtime_dir,
            configured_port=8000,
            process_checker=lambda pid: pid == 1111,
            port_owner_checker=lambda port: 1111,
        )

        assert audit["relation"] == "own_service_running"
        assert audit["action"] == "stop"
        assert audit["recorded_pid"] == 1111
        assert audit["port_owner_pid"] == 1111
        assert "uv run python -m src.cli stop" in audit["next_step"]


def test_service_audit_identifies_external_listener_without_state() -> None:
    with TemporaryDirectory() as tmp:
        audit = server_runtime.get_service_audit(
            runtime_dir=Path(tmp),
            configured_port=8000,
            process_checker=lambda pid: False,
            port_owner_checker=lambda port: 2222,
        )

        assert audit["relation"] == "external_listener"
        assert audit["action"] == "inspect_external"
        assert audit["recorded_pid"] is None
        assert audit["port_owner_pid"] == 2222
        assert "不要用 python -m src.cli stop" in audit["next_step"]


def test_service_audit_identifies_stale_record() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(pid=3333, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        audit = server_runtime.get_service_audit(
            runtime_dir=runtime_dir,
            configured_port=8000,
            process_checker=lambda pid: False,
            port_owner_checker=lambda port: None,
        )

        assert audit["relation"] == "stale_record"
        assert audit["action"] == "repair"
        assert audit["recorded_pid"] == 3333
        assert audit["port_owner_pid"] is None
        assert "Douyin Recall Repair State" in audit["next_step"]


def test_service_audit_identifies_record_without_listener() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(pid=4445, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        audit = server_runtime.get_service_audit(
            runtime_dir=runtime_dir,
            configured_port=8000,
            process_checker=lambda pid: pid == 4445,
            port_owner_checker=lambda port: None,
        )

        assert audit["relation"] == "record_without_listener"
        assert audit["action"] == "repair"
        assert audit["recorded_pid"] == 4445
        assert audit["port_owner_pid"] is None
        assert "重新检查" in audit["next_step"]


def test_service_audit_identifies_record_port_mismatch() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(pid=5556, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        audit = server_runtime.get_service_audit(
            runtime_dir=runtime_dir,
            configured_port=8000,
            process_checker=lambda pid: pid == 5556,
            port_owner_checker=lambda port: 9999,
        )

        assert audit["relation"] == "record_port_mismatch"
        assert audit["action"] == "repair"
        assert audit["recorded_pid"] == 5556
        assert audit["port_owner_pid"] == 9999
        assert "不要结束 pid=9999" in audit["next_step"]


def test_service_audit_identifies_clear_port_without_state() -> None:
    with TemporaryDirectory() as tmp:
        audit = server_runtime.get_service_audit(
            runtime_dir=Path(tmp),
            configured_port=8000,
            process_checker=lambda pid: False,
            port_owner_checker=lambda port: None,
        )

        assert audit["relation"] == "clear"
        assert audit["action"] == "start"
        assert audit["recorded_pid"] is None
        assert audit["port_owner_pid"] is None
        assert "没有后台 Web 服务占用" in audit["message"]


def test_status_command_prints_service_audit_guidance() -> None:
    status = {
        "state": "running",
        "running": True,
        "pid": 1111,
        "url": "http://127.0.0.1:8000",
        "message": "本地 Web 服务正在运行：http://127.0.0.1:8000 (pid=1111)",
    }
    audit = {
        "relation": "own_service_running",
        "action": "stop",
        "port": 8000,
        "recorded_pid": 1111,
        "port_owner_pid": 1111,
        "message": "Recorded Douyin Recall service owns port 8000.",
        "next_step": "uv run python -m src.cli stop",
        "status": status,
    }

    with patch.object(cli_module.server_runtime, "get_server_status", return_value=status):
        with patch.object(cli_module.server_runtime, "get_service_audit", return_value=audit):
            result = CliRunner().invoke(cli_module.cli, ["status"])

    assert result.exit_code == 0
    assert "Service audit: own_service_running" in result.output
    assert "Port owner PID: 1111" in result.output
    assert "Next step: uv run python -m src.cli stop" in result.output


def test_windows_terminator_uses_force_and_raises_on_taskkill_failure() -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 1
        stdout = ""
        stderr = "This process can only be terminated forcefully."

    def fake_run(args, **kwargs):
        calls.append(args)
        return Result()

    with patch.object(server_runtime.sys, "platform", "win32"):
        with patch.object(server_runtime.subprocess, "run", fake_run):
            try:
                server_runtime.terminate_process(8888)
            except RuntimeError as e:
                assert "taskkill failed" in str(e)
            else:
                raise AssertionError("terminate_process should raise when taskkill fails")

    assert calls == [["taskkill", "/PID", "8888", "/T", "/F"]]


def test_windows_process_checker_uses_win32_api_without_tasklist() -> None:
    with patch.object(server_runtime.sys, "platform", "win32"):
        with patch.object(server_runtime, "_is_process_running_windows", return_value=True) as checker:
            assert server_runtime.is_process_running(1234) is True

    checker.assert_called_once_with(1234)
    source = inspect.getsource(server_runtime.is_process_running)
    assert "tasklist" not in source
    assert "_is_process_running_windows" in source


def test_windows_process_checker_reports_false_when_win32_api_cannot_open_pid() -> None:
    with patch.object(server_runtime.sys, "platform", "win32"):
        with patch.object(server_runtime, "_is_process_running_windows", return_value=False):
            assert server_runtime.is_process_running(1234) is False


if __name__ == "__main__":
    tests = [
        test_write_and_read_server_state_round_trips_pid_and_url,
        test_get_server_status_distinguishes_running_from_stale_pid,
        test_should_start_server_blocks_duplicate_running_process,
        test_should_start_server_clears_running_pid_when_port_has_no_listener,
        test_should_start_server_clears_running_pid_when_port_owner_mismatches,
        test_stop_recorded_server_calls_terminator_and_clears_state,
        test_stop_recorded_server_refuses_when_recorded_pid_does_not_own_port,
        test_stop_recorded_server_refuses_when_port_has_no_listener,
        test_stop_recorded_server_keeps_state_when_terminator_fails,
        test_stop_recorded_server_waits_for_port_to_release_before_clearing_state,
        test_stop_recorded_server_keeps_state_when_port_remains_after_terminator,
        test_service_audit_identifies_recorded_service_owning_port,
        test_service_audit_identifies_external_listener_without_state,
        test_service_audit_identifies_stale_record,
        test_service_audit_identifies_record_without_listener,
        test_service_audit_identifies_record_port_mismatch,
        test_service_audit_identifies_clear_port_without_state,
        test_status_command_prints_service_audit_guidance,
        test_windows_terminator_uses_force_and_raises_on_taskkill_failure,
        test_windows_process_checker_tolerates_non_utf8_tasklist_output,
        test_windows_process_checker_handles_missing_stdout_without_crashing,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as e:
            print(f"FAIL  {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {test.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(failed)
