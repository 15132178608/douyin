"""
Server lifecycle state tests.

Run:
    python tests/test_server_runtime.py
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import inspect
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
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


def test_database_runtime_lock_is_exclusive_and_released() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        with server_runtime.database_runtime_lock(runtime_dir=runtime_dir) as lock_path:
            assert lock_path.name == server_runtime.DATABASE_RUNTIME_LOCK_FILENAME
            try:
                with server_runtime.database_runtime_lock(runtime_dir=runtime_dir):
                    raise AssertionError("second runtime lock must not be acquired")
            except server_runtime.DatabaseRuntimeLockUnavailable:
                pass
            else:
                raise AssertionError("second runtime lock acquisition must fail")

        assert lock_path.read_bytes()[:1] == b"\0"
        try:
            with server_runtime.database_runtime_lock(runtime_dir=runtime_dir):
                raise OSError("body failure must not be mistaken for lock contention")
        except OSError as exc:
            assert "body failure" in str(exc)
        else:
            raise AssertionError("context body failure must escape")

        # Both normal and exceptional exits release the Windows byte lock / POSIX flock.
        with server_runtime.database_runtime_lock(runtime_dir=runtime_dir):
            pass


def test_database_runtime_lock_closes_handle_when_lock_file_initialization_fails() -> None:
    class FakeParent:
        def mkdir(self, **_kwargs) -> None:
            return None

    class FakeHandle:
        closed = False

        def seek(self, *_args) -> None:
            return None

        def tell(self) -> int:
            return 0

        def write(self, _value: bytes) -> None:
            return None

        def flush(self) -> None:
            raise OSError("simulated flush failure")

        def close(self) -> None:
            self.closed = True

    class FakePath:
        parent = FakeParent()

        def open(self, _mode: str) -> FakeHandle:
            return fake_handle

        def __str__(self) -> str:
            return "fake-runtime-lock"

    fake_handle = FakeHandle()
    with patch.object(
        server_runtime,
        "_database_runtime_lock_path",
        return_value=FakePath(),
    ):
        try:
            with server_runtime.database_runtime_lock():
                raise AssertionError("initialization failure must prevent acquisition")
        except server_runtime.DatabaseRuntimeLockUnavailable as exc:
            assert "simulated flush failure" in str(exc)
        else:
            raise AssertionError("lock initialization failure must escape")

    assert fake_handle.closed is True


def test_database_runtime_lock_is_exclusive_across_processes() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        child_code = """
import sys
from pathlib import Path
from src import server_runtime

with server_runtime.database_runtime_lock(runtime_dir=Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.readline()
"""
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(runtime_dir)],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ready = Event()
        ready_lines: list[str] = []

        def read_child_ready() -> None:
            assert child.stdout is not None
            ready_lines.append(child.stdout.readline().strip())
            ready.set()

        reader = Thread(target=read_child_ready, daemon=True)
        reader.start()
        try:
            assert ready.wait(timeout=10), "runtime-lock child did not report readiness"
            assert ready_lines == ["locked"]
            try:
                with server_runtime.database_runtime_lock(runtime_dir=runtime_dir):
                    raise AssertionError("parent must not acquire the child's runtime lock")
            except server_runtime.DatabaseRuntimeLockUnavailable:
                pass
            else:
                raise AssertionError("cross-process lock acquisition must fail")
        finally:
            if child.stdin is not None:
                try:
                    child.stdin.write("release\n")
                    child.stdin.flush()
                    child.stdin.close()
                except OSError:
                    pass
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
                raise AssertionError("runtime-lock child did not exit after release")
            stderr = child.stderr.read() if child.stderr is not None else ""
            assert child.returncode == 0, f"stderr={stderr!r}"
            reader.join(timeout=1)

        with server_runtime.database_runtime_lock(runtime_dir=runtime_dir):
            pass


def test_web_listener_probe_is_cross_platform() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    try:
        assert server_runtime.is_web_listener_active(
            host="0.0.0.0",
            port=port,
        ) is True
    finally:
        listener.close()

    assert server_runtime.is_web_listener_active(
        host="127.0.0.1",
        port=port,
        timeout_seconds=0.05,
    ) is False


def test_web_lifespan_holds_runtime_lock_before_database_initialization() -> None:
    from src.web import app as web_app

    events: list[str] = []
    state = server_runtime.ServerState(
        pid=1234,
        host="127.0.0.1",
        port=8000,
        started_at="test",
    )

    @contextmanager
    def fake_runtime_lock():
        events.append("lock")
        try:
            yield Path("database-runtime.lock")
        finally:
            events.append("unlock")

    async def exercise_lifespan() -> None:
        async with web_app.lifespan(web_app.app):
            events.append("serve")
            assert events == ["lock", "validate", "init", "start", "state", "serve"]

    with patch.object(web_app.server_runtime, "database_runtime_lock", fake_runtime_lock):
        with patch.object(
            web_app,
            "validate_web_security_config",
            side_effect=lambda: events.append("validate"),
        ):
            with patch.object(web_app, "init_schema", side_effect=lambda: events.append("init")):
                with patch.object(
                    web_app.runtime,
                    "start_background_workers",
                    side_effect=lambda **_kwargs: events.append("start"),
                ):
                    with patch.object(
                        web_app.runtime,
                        "shutdown_workers",
                        side_effect=lambda: events.append("stop"),
                    ):
                        with patch.object(
                            web_app.server_runtime,
                            "write_server_state",
                            side_effect=lambda **_kwargs: (events.append("state"), state)[1],
                        ):
                            with patch.object(
                                web_app.server_runtime,
                                "read_server_state",
                                side_effect=lambda: (events.append("read"), state)[1],
                            ):
                                with patch.object(
                                    web_app.server_runtime,
                                    "clear_server_state",
                                    side_effect=lambda: events.append("clear"),
                                ):
                                    asyncio.run(exercise_lifespan())

    assert events == [
        "lock",
        "validate",
        "init",
        "start",
        "state",
        "serve",
        "stop",
        "read",
        "clear",
        "unlock",
    ]


def test_web_lifespan_waits_for_cancelled_init_before_releasing_runtime_lock() -> None:
    from src.web import app as web_app

    lock_entered = Event()
    lock_released = Event()
    init_started = Event()
    allow_init = Event()
    init_finished = Event()

    @contextmanager
    def fake_runtime_lock():
        lock_entered.set()
        try:
            yield Path("database-runtime.lock")
        finally:
            assert init_finished.is_set()
            lock_released.set()

    def blocking_init() -> None:
        init_started.set()
        if not allow_init.wait(timeout=5):
            raise AssertionError("test did not release synchronous schema initialization")
        init_finished.set()

    async def attempt_startup() -> None:
        async with web_app.lifespan(web_app.app):
            raise AssertionError("cancelled startup must not enter serving state")

    async def exercise_cancellation() -> None:
        task = asyncio.create_task(attempt_startup())
        assert await asyncio.to_thread(lock_entered.wait, 5)
        assert await asyncio.to_thread(init_started.wait, 5)

        task.cancel()
        task.cancel()
        assert lock_released.is_set() is False
        assert task.done() is False

        allow_init.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("startup cancellation must remain observable")

        assert init_finished.is_set()
        assert lock_released.is_set()

    with patch.object(web_app.server_runtime, "database_runtime_lock", fake_runtime_lock):
        with patch.object(web_app, "validate_web_security_config"):
            with patch.object(web_app, "init_schema", blocking_init):
                with patch.object(web_app.runtime, "start_background_workers") as start:
                    with patch.object(web_app.server_runtime, "write_server_state") as write_state:
                        asyncio.run(exercise_cancellation())

    start.assert_not_called()
    write_state.assert_not_called()


def test_web_lifespan_waits_for_cancelled_shutdown_before_releasing_runtime_lock() -> None:
    from src.web import app as web_app

    lock_entered = Event()
    lock_released = Event()
    serving = Event()
    shutdown_started = Event()
    allow_shutdown = Event()
    shutdown_finished = Event()
    state_cleared = Event()
    state = server_runtime.ServerState(
        pid=1234,
        host="127.0.0.1",
        port=8000,
        started_at="test",
    )

    @contextmanager
    def fake_runtime_lock():
        lock_entered.set()
        try:
            yield Path("database-runtime.lock")
        finally:
            assert shutdown_finished.is_set()
            assert state_cleared.is_set()
            lock_released.set()

    def blocking_shutdown() -> None:
        shutdown_started.set()
        if not allow_shutdown.wait(timeout=5):
            raise AssertionError("test did not release synchronous worker shutdown")
        shutdown_finished.set()

    async def serve_until_cancelled() -> None:
        async with web_app.lifespan(web_app.app):
            serving.set()
            await asyncio.Event().wait()

    async def exercise_cancellation() -> None:
        task = asyncio.create_task(serve_until_cancelled())
        assert await asyncio.to_thread(lock_entered.wait, 5)
        assert await asyncio.to_thread(serving.wait, 5)

        task.cancel()
        assert await asyncio.to_thread(shutdown_started.wait, 5)
        task.cancel()
        assert lock_released.is_set() is False
        assert task.done() is False

        allow_shutdown.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("lifespan cancellation must remain observable")

        assert shutdown_finished.is_set()
        assert lock_released.is_set()

    with patch.object(web_app.server_runtime, "database_runtime_lock", fake_runtime_lock):
        with patch.object(web_app, "validate_web_security_config"):
            with patch.object(web_app, "init_schema"):
                with patch.object(web_app.runtime, "start_background_workers"):
                    with patch.object(web_app.runtime, "shutdown_workers", blocking_shutdown):
                        with patch.object(
                            web_app.server_runtime,
                            "write_server_state",
                            return_value=state,
                        ):
                            with patch.object(
                                web_app.server_runtime,
                                "read_server_state",
                                return_value=state,
                            ):
                                with patch.object(
                                    web_app.server_runtime,
                                    "clear_server_state",
                                    side_effect=state_cleared.set,
                                ):
                                    asyncio.run(exercise_cancellation())


def test_web_lifespan_does_not_clear_state_owned_by_another_process() -> None:
    from src.web import app as web_app

    own_state = server_runtime.ServerState(
        pid=1234,
        host="127.0.0.1",
        port=8000,
        started_at="own",
    )
    replacement_state = server_runtime.ServerState(
        pid=5678,
        host="127.0.0.1",
        port=8000,
        started_at="replacement",
    )

    @contextmanager
    def fake_runtime_lock():
        yield Path("database-runtime.lock")

    async def exercise_lifespan() -> None:
        async with web_app.lifespan(web_app.app):
            pass

    with patch.object(web_app.server_runtime, "database_runtime_lock", fake_runtime_lock):
        with patch.object(web_app, "validate_web_security_config"):
            with patch.object(web_app, "init_schema"):
                with patch.object(web_app.runtime, "start_background_workers"):
                    with patch.object(web_app.runtime, "shutdown_workers"):
                        with patch.object(
                            web_app.server_runtime,
                            "write_server_state",
                            return_value=own_state,
                        ):
                            with patch.object(
                                web_app.server_runtime,
                                "read_server_state",
                                return_value=replacement_state,
                            ):
                                with patch.object(
                                    web_app.server_runtime,
                                    "clear_server_state",
                                ) as clear_state:
                                    asyncio.run(exercise_lifespan())

    clear_state.assert_not_called()


def test_second_web_lifespan_cannot_overwrite_or_clear_winner_state() -> None:
    from src.web import app as web_app

    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)

        async def exercise_two_lifespans() -> None:
            async with web_app.lifespan(web_app.app):
                winner = server_runtime.read_server_state(runtime_dir=runtime_dir)
                assert winner is not None
                assert winner.host == "127.0.0.1"
                assert winner.port == 8765

                try:
                    async with web_app.lifespan(web_app.app):
                        raise AssertionError("second lifespan must not acquire the runtime lock")
                except server_runtime.DatabaseRuntimeLockUnavailable:
                    pass
                else:
                    raise AssertionError("second lifespan must fail before initialization")

                preserved = server_runtime.read_server_state(runtime_dir=runtime_dir)
                assert preserved == winner

            assert server_runtime.read_server_state(runtime_dir=runtime_dir) is None

        with patch.object(server_runtime, "DEFAULT_RUNTIME_DIR", runtime_dir):
            with patch.object(web_app.settings, "web_host", "127.0.0.1"):
                with patch.object(web_app.settings, "web_port", 8765):
                    with patch.object(web_app, "validate_web_security_config"):
                        with patch.object(web_app, "init_schema") as init_schema:
                            with patch.object(web_app.runtime, "start_background_workers") as start:
                                with patch.object(web_app.runtime, "shutdown_workers") as shutdown:
                                    asyncio.run(exercise_two_lifespans())

        init_schema.assert_called_once_with()
        start.assert_called_once_with(reset_shutdown=True)
        shutdown.assert_called_once_with()


def test_web_lifespan_lock_failure_has_no_database_or_worker_side_effects() -> None:
    from src.web import app as web_app

    events: list[str] = []

    @contextmanager
    def unavailable_runtime_lock():
        raise server_runtime.DatabaseRuntimeLockUnavailable("held by rollback")
        yield  # pragma: no cover - keeps this a contextmanager generator

    async def exercise_lifespan() -> None:
        async with web_app.lifespan(web_app.app):
            raise AssertionError("lifespan must not start while the runtime lock is held")

    with patch.object(
        web_app.server_runtime,
        "database_runtime_lock",
        unavailable_runtime_lock,
    ):
        with patch.object(
            web_app,
            "validate_web_security_config",
            side_effect=lambda: events.append("validate"),
        ):
            with patch.object(web_app, "init_schema", side_effect=lambda: events.append("init")):
                with patch.object(
                    web_app.runtime,
                    "start_background_workers",
                    side_effect=lambda: events.append("start"),
                ):
                    try:
                        asyncio.run(exercise_lifespan())
                    except server_runtime.DatabaseRuntimeLockUnavailable as exc:
                        assert "held by rollback" in str(exc)
                    else:
                        raise AssertionError("runtime lock failure must abort lifespan")

    assert events == []


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


def test_should_start_server_preserves_starting_state_while_runtime_lock_is_held() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(
            pid=3336,
            host="127.0.0.1",
            port=8000,
            runtime_dir=runtime_dir,
        )

        with server_runtime.database_runtime_lock(runtime_dir=runtime_dir):
            decision = server_runtime.should_start_server(
                runtime_dir=runtime_dir,
                process_checker=lambda _pid: True,
                port_owner_checker=lambda _port: None,
            )

        assert decision["ok"] is False
        assert "数据库运行锁" in decision["message"]
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) is not None


def test_should_start_server_blocks_restore_lock_before_pid_state_exists() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        with server_runtime.database_runtime_lock(runtime_dir=runtime_dir):
            decision = server_runtime.should_start_server(runtime_dir=runtime_dir)

        assert decision["ok"] is False
        assert "数据库恢复" in decision["message"]

        retry = server_runtime.should_start_server(runtime_dir=runtime_dir)
        assert retry["ok"] is True


def test_should_start_server_holds_runtime_lock_while_clearing_stale_state() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(
            pid=3337,
            host="127.0.0.1",
            port=8000,
            runtime_dir=runtime_dir,
        )
        original_clear = server_runtime.clear_server_state
        clear_observed_lock = False

        def checked_clear(path: Path | None = None) -> None:
            nonlocal clear_observed_lock
            try:
                with server_runtime.database_runtime_lock(runtime_dir=runtime_dir):
                    raise AssertionError("state cleanup must remain inside the runtime lock")
            except server_runtime.DatabaseRuntimeLockUnavailable:
                clear_observed_lock = True
            else:
                raise AssertionError("nested runtime lock acquisition must fail")
            original_clear(path)

        with patch.object(server_runtime, "clear_server_state", checked_clear):
            decision = server_runtime.should_start_server(
                runtime_dir=runtime_dir,
                process_checker=lambda _pid: False,
            )

        assert decision["ok"] is True
        assert clear_observed_lock is True
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) is None


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


def test_repair_stale_server_state_clears_unchanged_stale_record() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(
            pid=4331,
            host="127.0.0.1",
            port=8000,
            runtime_dir=runtime_dir,
            started_at="stale",
        )

        result = server_runtime.repair_stale_server_state(
            runtime_dir=runtime_dir,
            process_checker=lambda _pid: False,
        )

        assert result["repaired"] is True
        assert "已清理陈旧服务状态" in result["message"]
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) is None


def test_repair_stale_server_state_preserves_record_while_runtime_lock_is_held() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        expected = server_runtime.write_server_state(
            pid=4332,
            host="127.0.0.1",
            port=8000,
            runtime_dir=runtime_dir,
            started_at="starting",
        )

        with server_runtime.database_runtime_lock(runtime_dir=runtime_dir):
            result = server_runtime.repair_stale_server_state(
                runtime_dir=runtime_dir,
                process_checker=lambda _pid: False,
            )

        assert result["repaired"] is False
        assert "数据库运行锁" in result["message"]
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) == expected


def test_repair_stale_server_state_preserves_concurrent_replacement() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(
            pid=4333,
            host="127.0.0.1",
            port=8000,
            runtime_dir=runtime_dir,
            started_at="old",
        )
        replacement: list[server_runtime.ServerState] = []

        def replace_record(_pid: int) -> bool:
            replacement.append(
                server_runtime.write_server_state(
                    pid=9333,
                    host="127.0.0.1",
                    port=8000,
                    runtime_dir=runtime_dir,
                    started_at="replacement",
                )
            )
            return False

        result = server_runtime.repair_stale_server_state(
            runtime_dir=runtime_dir,
            process_checker=replace_record,
        )

        assert result["repaired"] is False
        assert "状态已发生变化" in result["message"]
        assert server_runtime.read_server_state(runtime_dir=runtime_dir) == replacement[0]


def test_repair_stale_server_state_clears_malformed_stale_artifacts() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        runtime_dir.joinpath("server.json").write_text("{not-json", encoding="utf-8")
        runtime_dir.joinpath("server.pid").write_text("4334\n", encoding="utf-8")

        result = server_runtime.repair_stale_server_state(
            runtime_dir=runtime_dir,
            process_checker=lambda _pid: False,
        )

        assert result["repaired"] is True
        assert "无法解析" in result["message"]
        assert not runtime_dir.joinpath("server.json").exists()
        assert not runtime_dir.joinpath("server.pid").exists()


def test_repair_stale_server_state_preserves_malformed_artifacts_for_live_pid() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        runtime_dir.joinpath("server.json").write_text("{not-json", encoding="utf-8")
        runtime_dir.joinpath("server.pid").write_text("4335\n", encoding="utf-8")

        result = server_runtime.repair_stale_server_state(
            runtime_dir=runtime_dir,
            process_checker=lambda pid: pid == 4335,
        )

        assert result["repaired"] is False
        assert "仍在运行" in result["message"]
        assert runtime_dir.joinpath("server.json").exists()
        assert runtime_dir.joinpath("server.pid").exists()


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


def test_stop_recorded_server_preserves_starting_state_while_runtime_lock_is_held() -> None:
    for owner_pid in (None, 9999):
        with TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp)
            stopped: list[int] = []
            expected = server_runtime.write_server_state(
                pid=4445,
                host="127.0.0.1",
                port=8000,
                runtime_dir=runtime_dir,
                started_at="starting",
            )

            with server_runtime.database_runtime_lock(runtime_dir=runtime_dir):
                result = server_runtime.stop_recorded_server(
                    runtime_dir=runtime_dir,
                    process_checker=lambda _pid: True,
                    port_owner_checker=lambda _port: owner_pid,
                    terminator=lambda pid: stopped.append(pid),
                )

            assert result["stopped"] is False
            assert stopped == []
            assert server_runtime.read_server_state(runtime_dir=runtime_dir) == expected
            assert "数据库运行锁" in result["message"]
            assert "已保留 PID 记录" in result["message"]


def test_stop_recorded_server_preserves_state_when_new_instance_takes_over() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        old_pid = 4446
        replacement_pid = 8888
        process_states = [True, False]
        owners = [old_pid, replacement_pid]
        replacement_ready = Event()
        release_replacement = Event()
        replacement_state: list[server_runtime.ServerState] = []

        server_runtime.write_server_state(
            pid=old_pid,
            host="127.0.0.1",
            port=8000,
            runtime_dir=runtime_dir,
            started_at="old",
        )

        def run_replacement() -> None:
            with server_runtime.database_runtime_lock(runtime_dir=runtime_dir):
                replacement_state.append(
                    server_runtime.write_server_state(
                        pid=replacement_pid,
                        host="127.0.0.1",
                        port=8000,
                        runtime_dir=runtime_dir,
                        started_at="replacement",
                    )
                )
                replacement_ready.set()
                if not release_replacement.wait(timeout=5):
                    raise AssertionError("test did not release replacement runtime lock")

        replacement_thread = Thread(
            target=run_replacement,
            name="test-replacement-server",
        )

        def stop_old_and_start_replacement(pid: int) -> None:
            assert pid == old_pid
            replacement_thread.start()
            assert replacement_ready.wait(timeout=5)

        try:
            result = server_runtime.stop_recorded_server(
                runtime_dir=runtime_dir,
                process_checker=lambda _pid: process_states.pop(0),
                port_owner_checker=lambda _port: owners.pop(0),
                terminator=stop_old_and_start_replacement,
                wait_attempts=1,
                wait_interval_seconds=0,
            )

            assert result["stopped"] is True
            assert len(replacement_state) == 1
            assert server_runtime.read_server_state(runtime_dir=runtime_dir) == replacement_state[0]
            assert "另一个实例正在启动或运行" in result["message"]
            assert "已保留当前服务状态" in result["message"]
        finally:
            release_replacement.set()
            if replacement_thread.ident is not None:
                replacement_thread.join(timeout=5)
            assert replacement_thread.is_alive() is False


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


def test_service_audit_identifies_stale_record_with_listener() -> None:
    with TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        server_runtime.write_server_state(pid=3334, host="127.0.0.1", port=8000, runtime_dir=runtime_dir)

        audit = server_runtime.get_service_audit(
            runtime_dir=runtime_dir,
            configured_port=8000,
            process_checker=lambda pid: False,
            port_owner_checker=lambda port: 7777,
        )

        assert audit["relation"] == "stale_record_with_listener"
        assert audit["action"] == "repair"
        assert audit["recorded_pid"] == 3334
        assert audit["port_owner_pid"] == 7777
        assert "Douyin Recall Repair State" in audit["next_step"]
        assert "不要结束 pid=7777" in audit["next_step"]


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
        assert audit["action"] == "stop"
        assert audit["recorded_pid"] == 4445
        assert audit["port_owner_pid"] is None
        assert "Douyin Recall Stop Service" in audit["next_step"]
        assert "Repair State" not in audit["next_step"]


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
        assert audit["action"] == "stop"
        assert audit["recorded_pid"] == 5556
        assert audit["port_owner_pid"] == 9999
        assert "Douyin Recall Stop Service" in audit["next_step"]
        assert "Repair State" not in audit["next_step"]
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


def test_repair_state_command_uses_lock_aware_runtime_helper() -> None:
    payload = {
        "repaired": False,
        "status": {"state": "running", "pid": 1111},
        "message": "已保留服务状态。",
    }

    with patch.object(
        cli_module.server_runtime,
        "repair_stale_server_state",
        return_value=payload,
    ) as repair:
        result = CliRunner().invoke(cli_module.cli, ["repair-state"])

    assert result.exit_code == 0
    assert payload["message"] in result.output
    repair.assert_called_once_with()


def test_serve_host_override_rejects_insecure_public_bind_before_side_effects() -> None:
    with patch.object(cli_module.settings, "web_host", "127.0.0.1"):
        with patch.object(cli_module.settings, "web_auth_required", True):
            with patch.object(cli_module.settings, "session_cookie_secure", False):
                with patch.object(cli_module.db_module, "init_schema") as init_schema:
                    with patch.object(
                        cli_module.server_runtime,
                        "should_start_server",
                        return_value={"ok": True},
                    ) as should_start_server:
                        with patch.object(cli_module.server_runtime, "write_server_state") as write_state:
                            with patch.object(
                                cli_module.server_runtime,
                                "read_server_state",
                                return_value=None,
                            ):
                                with patch.object(cli_module.server_runtime, "clear_server_state") as clear_state:
                                    with patch("uvicorn.run") as uvicorn_run:
                                        result = CliRunner().invoke(
                                            cli_module.cli,
                                            ["serve", "--host", "0.0.0.0"],
                                        )

    error_text = f"{result.output}\n{result.exception or ''}"
    assert result.exit_code != 0
    assert "SESSION_COOKIE_SECURE=true" in error_text
    init_schema.assert_not_called()
    should_start_server.assert_not_called()
    write_state.assert_not_called()
    clear_state.assert_not_called()
    uvicorn_run.assert_not_called()


def test_serve_loopback_override_is_shared_with_app_lifespan_validation() -> None:
    observed: dict[str, object] = {}

    def validate_inside_uvicorn(*_args, host: str, port: int, **_kwargs) -> None:
        from src.web.security import validate_web_security_config

        observed["host"] = host
        observed["port"] = port
        observed["settings_host"] = cli_module.settings.web_host
        observed["settings_port"] = cli_module.settings.web_port
        validate_web_security_config()

    with patch.object(cli_module.settings, "web_host", "0.0.0.0"):
        with patch.object(cli_module.settings, "web_port", 8000):
            with patch.object(cli_module.settings, "web_auth_required", True):
                with patch.object(cli_module.settings, "session_cookie_secure", False):
                    with patch.object(cli_module.db_module, "init_schema") as init_schema:
                        with patch.object(
                            cli_module.server_runtime,
                            "should_start_server",
                            return_value={"ok": True},
                        ):
                            with patch("uvicorn.run", side_effect=validate_inside_uvicorn):
                                result = CliRunner().invoke(
                                    cli_module.cli,
                                    ["serve", "--host", "127.0.0.1", "--port", "8765"],
                                )

    assert result.exit_code == 0, result.output
    # The FastAPI lifespan now owns schema initialization after acquiring the
    # cross-process database runtime lock.
    init_schema.assert_not_called()
    assert observed == {
        "host": "127.0.0.1",
        "port": 8765,
        "settings_host": "127.0.0.1",
        "settings_port": 8765,
    }


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
        test_database_runtime_lock_is_exclusive_and_released,
        test_database_runtime_lock_closes_handle_when_lock_file_initialization_fails,
        test_database_runtime_lock_is_exclusive_across_processes,
        test_web_listener_probe_is_cross_platform,
        test_web_lifespan_holds_runtime_lock_before_database_initialization,
        test_web_lifespan_waits_for_cancelled_init_before_releasing_runtime_lock,
        test_web_lifespan_waits_for_cancelled_shutdown_before_releasing_runtime_lock,
        test_web_lifespan_does_not_clear_state_owned_by_another_process,
        test_second_web_lifespan_cannot_overwrite_or_clear_winner_state,
        test_web_lifespan_lock_failure_has_no_database_or_worker_side_effects,
        test_should_start_server_blocks_duplicate_running_process,
        test_should_start_server_preserves_starting_state_while_runtime_lock_is_held,
        test_should_start_server_blocks_restore_lock_before_pid_state_exists,
        test_should_start_server_holds_runtime_lock_while_clearing_stale_state,
        test_should_start_server_clears_running_pid_when_port_has_no_listener,
        test_should_start_server_clears_running_pid_when_port_owner_mismatches,
        test_repair_stale_server_state_clears_unchanged_stale_record,
        test_repair_stale_server_state_preserves_record_while_runtime_lock_is_held,
        test_repair_stale_server_state_preserves_concurrent_replacement,
        test_repair_stale_server_state_clears_malformed_stale_artifacts,
        test_repair_stale_server_state_preserves_malformed_artifacts_for_live_pid,
        test_stop_recorded_server_calls_terminator_and_clears_state,
        test_stop_recorded_server_preserves_starting_state_while_runtime_lock_is_held,
        test_stop_recorded_server_preserves_state_when_new_instance_takes_over,
        test_stop_recorded_server_refuses_when_recorded_pid_does_not_own_port,
        test_stop_recorded_server_refuses_when_port_has_no_listener,
        test_stop_recorded_server_keeps_state_when_terminator_fails,
        test_stop_recorded_server_waits_for_port_to_release_before_clearing_state,
        test_stop_recorded_server_keeps_state_when_port_remains_after_terminator,
        test_service_audit_identifies_recorded_service_owning_port,
        test_service_audit_identifies_external_listener_without_state,
        test_service_audit_identifies_stale_record,
        test_service_audit_identifies_stale_record_with_listener,
        test_service_audit_identifies_record_without_listener,
        test_service_audit_identifies_record_port_mismatch,
        test_service_audit_identifies_clear_port_without_state,
        test_status_command_prints_service_audit_guidance,
        test_repair_state_command_uses_lock_aware_runtime_helper,
        test_serve_host_override_rejects_insecure_public_bind_before_side_effects,
        test_serve_loopback_override_is_shared_with_app_lifespan_validation,
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
