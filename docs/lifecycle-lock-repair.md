# Lifecycle lock repair

## Root cause

A disposable backend launched through `.venv/bin/python3` reproduced the reported
`server.lock` error when controlled through `.venv/bin/python`. Discovery compared
the interpreter spelling rather than recognizing aliases in the same environment.
It rejected the genuine lock-owning backend, then attempted stale cleanup by
acquiring that backend's occupied lifetime lock. No shutdown signal was reached.
The original regression failed before the fix (1 failed in 1.14 seconds).

The normal data directory had no live backend or lock holder when inspected during
this repair; the earlier process's exact launch command therefore cannot be proven.
Global `/proc/locks` ownership also supplied an unnecessarily fragile identity
check for inherited descriptors and PID namespaces.

## Changes

- Recognize equivalent interpreter aliases within the installed environment.
- Prove the live process holds the exact private `server.lock` inode using its
  `/proc/<pid>/fdinfo` flock entry. Opening a file alone is insufficient.
- Serialize lifecycle operations with `lifecycle.lock`, separate from the backend's
  lifetime `server.lock`. Wait at most five seconds for another operation, then
  report the holder PID and lock role. Never unlink lock files.
- Stop the independently verified backend through a pidfd, rechecking identity
  before SIGTERM and escalating after three seconds if necessary. Acquire the
  instance lock for cleanup only after the backend exits.
- Register startup identity and release the startup operation mutex before slow
  application initialization, so a starting backend remains stoppable.
- Handle legacy `control.lock`: ignore it only when held solely by the verified
  backend being stopped; wait boundedly for a separate legacy controller.
- Recover a provably expired registration only from a separately verified live
  backend/lock owner. Never use the stale PID as authority to send a signal.

No QA orchestrator, tool policy, pairing mechanism, scope checks, or dependency
installation logic was changed. Restart continues generating a fresh backend
session and pairing credentials.

## Validation

All lifecycle integration tests use explicit temporary data directories and
disposable processes. No developer backend was terminated.

```bash
.venv/bin/python -m pytest -q tests/unit/test_processes.py tests/integration/test_cli_lifecycle.py --junitxml=qa-reports/lifecycle-lock-focused.xml
.venv/bin/python -m pytest -q --junitxml=qa-reports/lifecycle-lock-backend.xml
cd frontend
npm test
npm run build
npm run test:e2e
npm run test:e2e -- --grep 'authorized lab inspection' --repeat-each=3
```

Focused lifecycle suite: **61 passed** (32.51 seconds). Coverage includes occupied
locks, interpreter aliases, inherited flock descriptors, legacy backend/controller
locks, stale registration, PID reuse, permission failures, unrelated processes,
SIGKILL escalation, multiple data directories, blocked startup, restart after
shutdown, and concurrent restart/kill without duplicate or orphan backends.

Frontend unit tests: **12 passed**. Type checking and production build passed.
The first browser run had **16 passed, 1 failed**: the authorized inspection test
entered text before the asynchronous authorization dialog had closed and timed
out on disabled RUN. Both scope-setting tests now wait for the dialog to close;
the affected inspection test passed **3/3 repeated runs**.

Installed-launcher acceptance also passed from `/`, using
`/home/arnav/.local/bin/blackbox` and a temporary data/config directory:
restart replaced PID 984449 with PID 984514, preserved model and port 36265,
generated a different pairing token, and retained the same lock inode. Kill used
SIGTERM, released the port, and left no backend; a second kill correctly reported
no running instance. Pairing tokens were redacted from captured output.

Final full-suite reruns: **314 backend tests passed** (68.90 seconds), including
all 61 lifecycle cases; **17 browser tests passed** (17.1 seconds). No tests failed
or were skipped in either final run. `git diff --check` also passed.

## Files and installation

Repair files: `backend/blackbox/processes.py`, `backend/blackbox/cli.py`,
`tests/unit/test_processes.py`, `tests/integration/test_cli_lifecycle.py`,
`tests/e2e/chat-tool-loop.spec.ts` (authorization synchronization only), `README.md`,
`docs/cli-process-management.md`, and this report.

The installed launcher resolves to this checkout's editable environment, so no
reinstall or rebuild is required for these Python CLI changes. A currently running
backend retains its old loaded code until restarted; the updated controller can
identify and stop compatible pre-upgrade backends.

Linux `/proc` visibility and pidfd support remain required. If process identity
cannot be verified, shutdown fails closed with an owner diagnostic. A separate
lifecycle operation exceeding the five-second contention window requires a retry.
No claim is made about stopping the user's previously reported process because it
was no longer running when this investigation began.
