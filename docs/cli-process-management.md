# CLI process management

```bash
blackbox kill
blackbox restart
blackbox restart --model huihui_ai/qwen3.5-abliterated:4b
blackbox restart --port 8877 --no-browser
blackbox kill --data-dir /absolute/path/to/private-data
blackbox restart --data-dir /absolute/path/to/private-data
blackbox doctor
blackbox --help
```

`kill` stops a verified backend with SIGTERM, waits up to three seconds, then uses
SIGKILL if necessary. Output identifies the PID and the signal needed. Already
stopped instances return success with “No running BlackBox instance found.”
Permission/identity/port errors return a nonzero status with an actionable message.
No sudo, pgrep, shell kill command, or process-name substring matching is used.

`restart` captures the effective startup model, port, data/config directories,
Ollama URL and browser option. Optional model/port/config-directory/no-browser flags
override preserved values. Per-session model selections and transcript history
remain in SQLite. The new backend uses a fresh interpreter from the current
installation, with the current package checkout first on its import path. It never
pulls Git changes, installs packages, rebuilds assets or resumes the old interpreter.

The command waits for the old backend to exit, checks port release (allowing normal
TCP TIME_WAIT), starts one managed detached backend, and waits for Uvicorn to bind
its socket. Only then does it print the new PID, model, listening address and fresh
one-time pairing URL. A private pipe carries readiness/authentication data; tokens
are not written to the instance record or the detached backend log. Runtime output
is in the private `server.log` under the data directory. Failed/timed-out startup
cleans up the new child; there is no automatic retry/restart loop. An explicitly
requested conflicting port is checked before stopping the old backend.

If no instance exists, `restart` behaves like normal **foreground** startup,
including browser behavior. A restart with a running instance returns after the
new managed backend becomes ready. `--no-browser` is preserved unless explicitly
set; the CLI does not have a flag to force browser opening when the saved option
was true. New pairing credentials are mandatory. The normal database startup path
revokes every previous session scope and interrupts active runs/goals.

## Identity and locking

Discovery scans same-user `/proc` entries, considering only exact installed
BlackBox entrypoint/module invocations. Before any signal, it verifies:

- UID, trusted executable/entrypoint, PID start time and protected process exclusions.
- Ownership and privacy of the data directory and regular lock/record files.
- The live PID's kernel `flock` ownership of that exact `server.lock` inode,
  verified through its `/proc/<pid>/fdinfo` locked descriptor.
- New-format record fields matching the process, data directory and package source.

A Linux pidfd pins the process before a second identity check and signal delivery,
preventing a reused PID from receiving the signal. The invoking CLI, its ancestors,
PID 1, unrelated Uvicorn servers and applications with incidental “blackbox”
arguments are excluded. Known fixed-tool children of a verified backend are also
pinned and cleaned up if needed; unrelated browser/application children are excluded.

`lifecycle.lock` serializes start/kill/restart operations, with a five-second bounded
wait and owner diagnostics. `server.lock` prevents duplicate backends and stays held
for the backend's lifetime. Shutdown signals the verified backend through a pidfd
without acquiring its `server.lock`; only after shutdown is that lock acquired for
cleanup. Startup releases its operation lock as soon as the process record exists,
before application initialization, so even a starting backend remains stoppable.

Legacy `control.lock` is retained: a verified backend holding it cannot block its
own shutdown; a separate old lifecycle controller must finish first. Lock files are
**never unlinked**. Stale records can be removed only after
obtaining the actual server lock; a still-live recorded backend with a missing or
unverifiable lock blocks stale cleanup and duplicate startup. Symlinks, malformed
records and unsafe permissions are rejected rather than guessed around.

Interpreter aliases such as `.venv/bin/python` and `.venv/bin/python3` are accepted
only within the same environment and when they resolve to the same executable.
A provably expired registration can be recovered from the independently verified
live lock owner; the stale recorded PID is never used as authority to signal.

A running pre-record **CLI** backend remains discoverable by its exact installed
entrypoint, executable and kernel-owned data lock. Supported flags/environment and
preferences supply its startup configuration. Record-less arbitrary Uvicorn/reload
servers do not have this proof and are deliberately not terminated. Use the installed
`blackbox` launcher for managed lifecycle commands. The existing development reload
script and QA orchestrator retain their own lifecycle controls.

Without a selector, one verified instance is selected automatically. Multiple
instances require `--data-dir`; `BLACKBOX_DATA_DIR` also acts as a selector. This
never means “kill all”. Commands work from any working directory.

## Dependencies and installation

Requires Linux `/proc`, visible kernel flock ownership, and pidfd APIs (modern Arch/
Omarchy kernels and Python support these). If identity cannot be established, the
command refuses to signal. Missing native tools retain existing capability warnings
and fallbacks; missing Python runtime dependencies/configuration errors are reported
without automatic installation.

Restart warns if the frontend build is absent or older than frontend source files.
Run the printed `npm --prefix ... run build` command when necessary. CLI-only changes
do not require a frontend rebuild. A noneditable Python installation requires its
normal reinstall/refresh to expose new source changes.

For this workspace, `blackbox-local` is installed editable from
`/home/arnav/Projects/blackbox`, and `~/.local/bin/blackbox` resolves to this checkout's
`.venv/bin/blackbox`. The actual installed `blackbox --help` displays `kill` and
`restart`; **no reinstall or launcher refresh is needed here**. A running old backend
needs restart to load the updated runtime. The developer's actual backend was not
terminated or restarted during implementation/testing.

## Implementation validation

All signalling tests used mocked operations or disposable processes with explicit
pytest-owned temporary data directories. No test issued a global, unscoped kill or
restart command. The installed launcher was checked read-only with `--help` from
`/tmp`.

| Command | Actual result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/unit/test_processes.py tests/integration/test_cli_lifecycle.py --junitxml=qa-reports/cli-lifecycle.xml` | **48 passed**, 0 failed, 18.37 s |
| `.venv/bin/python -m pytest -q --junitxml=qa-reports/cli-full-regression.xml` | **282 passed**, 0 failed, 0 skipped, 53.40 s |
| `npm --prefix frontend run typecheck` | Passed |
| `npm --prefix frontend run build` | Passed |
| `npm --prefix frontend run test:e2e` (final rerun) | **14 passed**, 0 failed, 12.3 s |
| `bash -n scripts/install_omarchy.sh` | Passed |
| `git diff --check` | Passed |
| `/home/arnav/.local/bin/blackbox --help` from `/tmp` | Passed; both commands available |

The first disposable integration run found one lifecycle failure: a no-instance foreground
restart was not recognized as a backend because its command line retained the word
`restart`. Fixed the exact-command parser and reran the affected tests. Added a
separate guard so a transient restart controller without a backend record cannot
be mistaken for a server during cleanup. All final tests above pass.

Coverage includes graceful SIGTERM, real three-second SIGKILL escalation, no process,
stale/malformed/foreign records, live/missing/unsafe locks, PID reuse, permission
failures, multiple data directories, unrelated Uvicorn/incidental arguments, owned
tool cleanup, preserved/overridden startup configuration, foreground restart with no
instance, startup failures, conflicting ports, fresh pairing credentials, revoked
scope, concurrent restarts, duplicate/orphan prevention, help and offline doctor.

Files changed for this task:

- `backend/blackbox/cli.py`: command integration, registration, lifecycle locking,
  fresh-process startup/readiness and error reporting.
- `backend/blackbox/processes.py` (new): Linux identity/lock inspection, pidfd signals,
  stale-record handling, port checks and managed child launch/cleanup.
- `tests/unit/test_processes.py` (new): deterministic process-operation tests.
- `tests/integration/test_cli_lifecycle.py` (new): disposable real CLI/backend tests.
- `tests/e2e/chat-tool-loop.spec.ts`: scoped Python syntax/copy assertions to the Python example rather than counting the separate evidence JSON block.
- `README.md`: usage and configuration/credential behavior.
- `docs/cli-process-management.md` (new): implementation, limits and results.

Other existing workspace changes and QA orchestration/token optimizations were
preserved. No dependencies were added and no system configuration was edited.

The latest browser suite initially reported **13 passed, 1 failed**: a streaming
Markdown assertion counted highlighted strings/code-copy controls across both the
Python example and appended tool-evidence JSON. Scoped those checks to the Python
block without changing application behavior. The affected test rerun
(`npm --prefix frontend run test:e2e -- --grep 'authorized lab inspection'`) passed
**1/1** (3.3 s).

The complete browser suite rerun passed **14/14** after the scoped assertion fix.
