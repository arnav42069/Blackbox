# Lifecycle ownership repair — 2026-11-10

## Confirmed cause

The actual installed launcher is `~/.local/bin/blackbox`, a shell wrapper that
executes this checkout's editable `.venv/bin/blackbox`. Its Python entrypoint is
`blackbox.cli:main`; the CLI acquires `server.lock`, writes `instance.json`, then
runs Uvicorn in that same process. No additional Uvicorn worker owns this instance.

Read-only inspection found the reported failure on live backend PID 1601688.
Its start time, executable, exact CLI command, private instance record and open
lock descriptor all matched. `lslocks` showed its active flock. However:

- `stat(server.lock)` and `stat(/proc/1601688/fd/4)` both reported device `00:36`,
  inode 970863.
- `/proc/1601688/fdinfo/4` reported `FLOCK ADVISORY WRITE 1601688 00:1d:970863`.

Btrfs reports a subvolume device through stat and its superblock device through
kernel lock information. Comparing those device numbers rejected the real backend.
Discovery silently discarded it, then stale cleanup attempted to acquire its
occupied lock and printed the misleading invisible-owner error.

The regression test reproduces that device mismatch without requiring Btrfs. It
failed before the fix. Descriptor verification now compares the actual open
file's device and inode against the private lock path, and checks the descriptor's
own exclusive flock and inode without comparing the Btrfs superblock device.
A descriptor merely opening the file still provides no ownership proof.

## Resulting behavior

`kill` signals the verified backend through a pidfd, waits three seconds after
SIGTERM, rechecks process identity and escalates only if necessary. It never waits
for an owned server lock to become free before sending SIGTERM. The lock's inode
is never unlinked. Lifetime ownership and lifecycle-operation locking remain
separate. Process identity includes PID, kernel start time, executable, exact
command and, for new registrations, boot and instance identifiers. Reparenting
does not invalidate a process identity.

`restart` waits for exit, port release and lock availability, then confirms the
fresh instance's HTTP health identity. A stale or unrelated health response cannot
confirm a replacement. Existing data, sessions and checkpoints remain subject to
the application's existing recovery and retention behavior.

`status [--json]` independently reports verified process identity, the actual
flock probe and visible owners, lifecycle lock, listening/bind state and HTTP
health. A hidden owner still reports a held lock. Inaccessible identity fails
closed with an actionable diagnostic; it never authorizes a guessed signal.
Older backends can report healthy HTTP responses without an instance identifier;
status exposes that distinction. Fresh restart confirmation requires the new ID.

Fixed tools receive exact instance/data-directory markers. Detached or orphaned
tools can be pinned and cleaned up after a crash; untagged unrelated processes are
excluded. User services are recognized only when the verified backend is their
MainPID and its cgroup matches the manager. Explicit manager stop suppresses
`Restart=always`; a live D-Bus unit reference also retains transient units between
stop and start. Replacement health is confirmed within the same user service.
CLI option changes for services require updating the unit's ExecStart first.

The supported development launcher now uses the registered CLI backend and keeps
Vite's watched frontend build. Use `blackbox restart` for Python changes. Arbitrary
unregistered Uvicorn/reload processes remain outside managed discovery.

## Validation

All shutdown/restart acceptance checks used disposable data and processes. The
original user backend was inspected without receiving any signal.

- Final full Python unit/integration/QA suite: **367 passed, 2 skipped**, 94.95
  seconds. Both skips are opt-in live Ollama inference tests requiring
  `BLACKBOX_LIVE_OLLAMA_MODEL`.
- All **83 lifecycle tests** passed in that final run, covering active/stale locks,
  Btrfs device mismatch, hidden owners, permission failures, PID/command/boot/instance
  changes, graceful and forced shutdown, detached orphans, restart races, status,
  user-service ownership and reference lifetime, health identity, and preservation
  of committed attachments, files, sessions and execution checkpoints.
- Installed launcher on this checkout's Btrfs filesystem: start/status/restart/kill
  passed; PID 1645213 became 1645340, health ID changed, and lock inode 1114520 stayed
  unchanged.
- Actual transient systemd user service with `Restart=always`: restart changed PID
  1645562 to 1645644, retained service ownership, confirmed new health and stopped
  into `inactive` without respawning.
- `bash -n scripts/dev.sh` and `git diff --check` for all task-owned files passed.
  The separately edited `codex-prompts.md` was preserved; the global whitespace
  check reports trailing whitespace in that unrelated file.

JUnit evidence: `qa-reports/lifecycle-v2-focused.xml` and
`qa-reports/lifecycle-v2-full.xml`; installed/service acceptance evidence is in
`qa-reports/lifecycle-v2-acceptance.json`. Pairing credentials are not included in this
report. No dependencies were installed and no user service files were modified.
