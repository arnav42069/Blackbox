# CLI process management

```bash
blackbox status
blackbox status --json
blackbox kill
blackbox restart
blackbox restart --model qwen3.5:4b --port 8877 --no-browser
blackbox status --data-dir /absolute/path/to/private-data
blackbox kill --data-dir /absolute/path/to/private-data
```

`kill` stops the independently verified backend with SIGTERM, waits up to three
seconds, then uses SIGKILL if its pinned process identity still matches. It checks
exit, port release and lock availability before reporting completion. An already
stopped instance returns success. Identity, permission and port errors are
reported without using sudo or terminating unrelated processes.

`restart` preserves startup model, port, data/config directories, Ollama URL and
browser choice, with optional CLI overrides. It starts a fresh detached interpreter
when replacing a running CLI instance. It verifies both Uvicorn readiness and an
HTTP health response carrying the new backend's instance ID. Startup errors and
timeouts clean up the new child. With no old instance, restart retains normal
foreground startup behavior. Restart does not install dependencies or build UI
assets. New pairing credentials are printed; old credentials and scopes expire.
Session history, execution checkpoints and data files use the existing database
recovery and retention policies.

`status` reports process PID/start/executable/command/instance ID; server and
operation locks; port availability/listening state; and the health request result.
`--json` produces the same information as structured JSON. `running` and `stopped`
return zero; `unhealthy` and `unverified` return one. It creates no data directory
and changes no persistent state. An HTTP 200 alone cannot prove process ownership.
Older backends without a health ID expose `instance_matches: null`.

## Ownership and races

The installed `blackbox` launcher or exact `python -m blackbox[.cli]` command must
match this environment. Discovery requires same-user process identity and kernel
ownership of the exact private `server.lock` file. New records also bind the
command, boot ID and random instance ID. A pidfd pins each signalled process, with
identity checks after opening it and before escalation. Parent PID changes caused
by orphaning are allowed. PID reuse, unrelated Python/Uvicorn processes, protected
CLI/ancestor processes and incidental command substrings cannot authorize signals.

Descriptor stat and exclusive flock evidence are checked together. Btrfs can
report different device numbers through stat and fdinfo; its lock superblock
device is not compared with its subvolume stat device. An open file without an
actual flock remains insufficient proof.

`server.lock` remains locked throughout backend lifetime and is never unlinked.
`lifecycle.lock` serializes start/kill/restart operations with bounded contention.
A service-managed or privately launched replacement acquires the lifetime lock
while its controller retains the operation lock. Startup publishes identity before
slow app initialization, so starting backends remain stoppable. Stale registrations
are removed only after acquiring the lifetime lock. An invisible owner or malformed
record never authorizes shutdown or lock removal. Genuine inaccessible `/proc`
identity requires running as the backend owner with process visibility.

Legacy `control.lock` is ignored only when held by the verified backend being
stopped; another legacy controller gets a bounded wait. Interpreter aliases within
the same environment are recognized. Record-less old CLI processes can be adopted
only with independently verified command, executable and flock ownership.

Known backend tool children are pinned before shutdown. New tools also carry exact
instance/data-directory markers so detached descendants and tools orphaned by a
crash can be cleaned up from a stale registration. Unrelated untagged tools and
browser/application children are excluded.

Without a selector, discovery selects one verified instance. Multiple instances
require `--data-dir`; `BLACKBOX_DATA_DIR` also selects an instance. No command means
“kill all”. Commands work from any working directory.

## User services and development

A systemd **user** service should execute the installed `blackbox` CLI directly
(or use an exec wrapper), with the backend as MainPID. Its cgroup and manager's
MainPID must both match the verified process before service operations. Explicit
manager stop prevents `Restart=always` from undoing a kill. Restart preserves the
same service, including transient units, and confirms a fresh healthy registration.
A live D-Bus reference retains a transient unit across stop/start. These operations
use systemctl and systemd's existing libsystemd; no Python dependency is added.
Pairing URLs for service replacements are in `journalctl --user -u UNIT`.

Change a service's ExecStart configuration before requesting different startup
options. Conflicting CLI overrides are rejected before stopping the running unit.
System services and services with an unrelated MainPID are not stopped as units.

`./scripts/dev.sh` uses the same managed CLI backend and watches frontend output.
It forwards CLI flags, including `--data-dir`, `--port` and `--no-browser`. Apply
Python changes with `blackbox restart`; a fresh pairing link is required. Arbitrary
unregistered Uvicorn/reload launchers retain their own lifecycle controls and are
never targeted by a command substring.

## Installation and checks

For this workspace, `~/.local/bin/blackbox` executes this checkout's editable
`.venv/bin/blackbox`. Python CLI changes take effect immediately in new commands;
a running backend loads new runtime code after restart. Linux pidfds and readable
process identity/lock evidence are required.

See [the verified investigation and test results](lifecycle-lock-repair.md).
