# Service Lifecycle Audit Design

## Context

Douyin Recall already records the local Web service PID in `data\runtime\server.json` and `server.pid`. `recall serve` avoids duplicate starts, `recall stop` stops only the recorded service when its PID owns the configured port, and the Windows installer provides Start Menu entries for status, stop, health, and stale-state repair.

The remaining long-term-use gap is clarity. When a background service keeps running after development or normal use, the user needs to know whether the port is occupied by this project, whether the state file is stale, and which action is safe. A vague port-owner PID is not enough; the tool should say when to use Stop Service, when to use Repair State, and when not to touch an unrelated process.

## Goals

- Add a read-only service audit that combines service state, recorded PID, configured port, and current port listener.
- Make `recall status` print the audit result and the safest next action.
- Make the Windows control summary and health check show the same service-lifecycle decision.
- Keep cleanup conservative: stop only the recorded Douyin Recall service, and repair only explicit runtime state files.
- Document the improved background-process workflow for installed Windows users.

## Non-Goals

- Do not add any bulk process killer.
- Do not terminate unknown processes that happen to own the configured port.
- Do not delete logs, database files, browser profiles, login state, backups, or runtime caches.
- Do not expose the local Web UI publicly.
- Do not replace Task Manager or Windows service management.

## Design

Add `server_runtime.get_service_audit()`. The helper accepts the runtime directory, configured port, a process checker, and a port-owner checker. It returns a dictionary with:

```python
{
    "relation": "own_service_running" | "stale_record" | "external_listener" | ...,
    "action": "stop" | "repair" | "inspect_external" | "start",
    "port": 8000,
    "recorded_pid": 1234,
    "port_owner_pid": 1234,
    "message": "...",
    "next_step": "...",
    "status": {...},
}
```

The relation drives user-facing guidance:

- `own_service_running`: the recorded process owns the port; use `uv run recall stop` or `Douyin Recall Stop Service` when finished.
- `stale_record`: the recorded process is gone; use `uv run recall stop` or `Douyin Recall Repair State` to clean the runtime state files.
- `external_listener`: the port is occupied but no valid Douyin Recall service record exists; do not use project cleanup to kill it. Change `WEB_PORT` or inspect that process manually.
- `record_without_listener` or `record_port_mismatch`: the state record and port listener disagree; use stop/repair to clear project state, then re-check.
- `clear`: no listener and no state record; nothing is occupying the local Web port.

`recall status` keeps its current concise status line, then prints the audit relation, configured port, recorded PID, port owner PID, and next step. This makes the command useful after a forgotten background run without requiring the user to inspect JSON state files.

The Windows control script adds equivalent PowerShell helpers instead of shelling out to Python for the menu summary. The summary and health check print:

- `Service audit: ...`
- `Recorded PID: ...`
- `Port owner PID: ...`
- `Next step: ...`

The Windows `stop` action continues to call `recall stop`, so process termination remains centralized in the existing Python safety checks.

## Testing

- `tests/test_server_runtime.py` covers service-audit relations for own service, stale state, external listener, missing port listener, and mismatched port owner.
- CLI status tests cover the new audit lines without starting a real server.
- Windows packaging tests assert the control script contains service-audit helpers, user-facing guidance, no recursive delete, and the docs/release notes mention Stop Service and Repair State for background cleanup.
- Release verification builds the installer, installs it, verifies shortcuts and scripts, runs health checks without starting the Web service, and confirms ports are free afterward.
