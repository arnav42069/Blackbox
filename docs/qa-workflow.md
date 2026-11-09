# Persistent QA workflow

The Python controller `scripts/qa_loop.py` reuses pytest, Node Playwright, system Chromium, and existing deterministic Ollama/HTTP/SFTP fixtures. No dependencies or models are downloaded. It never opens production BlackBox data, pairing tokens or SSH profiles. The isolated browser app pairs through the normal `/api/pair` endpoint.

```bash
cd ~/Projects/blackbox
./scripts/qa-loop.sh --report-only
./scripts/qa-loop.sh --demo --once --budget-seconds 600
./scripts/qa-loop.sh --once
./scripts/qa-loop.sh --max-cycles 5
./scripts/qa-loop.sh --stop
```

`--report-only` executes all tests without Codex or Git mutations. `--once` allows one repair followed by focused and full regression checks. At most five repair cycles are allowed. Ctrl+C, SIGTERM and `--stop` stop owned child processes and preserve partial reports.

The default `--test-port auto` prefers port 8765 when free, otherwise selects an available loopback port. An explicit port, such as `--test-port 8876`, never falls back silently. An occupied explicit port is an infrastructure failure; no existing server is killed, paired or reused. The selected URL appears in `report.json` as `server_url`.

Exit codes distinguish outcomes:

| Code | Meaning |
|---|---|
| 0 | All executed checks passed |
| 1 | Test/infrastructure failures or an exhausted resource/repair bound |
| 2 | Intervention required or a harness error |
| 130 | Intentional Ctrl+C, SIGTERM or `--stop` shutdown |

Every final JSON report contains `exit_code`, `status`, `stop_reason`, `stop_request`, per-command exit codes, and failure categories. A negative command code such as -15 means the child received SIGTERM; it does not mean the application failed its tests.

## Source isolation and ownership

Report-only mode freezes reviewed source into `.runtime/qa-runs/<run-id>/`, records hashes/modes, links installed dependencies, and builds/tests there. Private runtime/config/session data, `.env`, caches and existing build output are excluded. Source hashes are checked before and after copying and before claiming a passing current-source result. Concurrent development cannot overwrite this run's fixture/build; source drift produces intervention rather than a misleading pass. Report-only mode therefore does not need to treat an idle interactive Codex process as an active source writer.

The demo also starts from a frozen source copy with independent Git metadata. It checkpoints a `qa/repair-<run-id>` branch/worktree, injects the metrics regression there, and never applies changes to the development tree. The real worker is checked for competing processes in its own isolated worktree.

Ordinary repair mode can apply verified fixes to the development checkout. It holds `.runtime/qa-writer.lock`, refuses other interactive Codex sessions attached to the checkout, and checks again while the worker runs and before application. Close other editing sessions before running ordinary repairs. Use the shared lease for future interactive sessions:

```bash
./scripts/qa-edit.sh codex
./scripts/qa-edit.sh your-editor
```

Idle app-server transports and the invoking coordinator are excluded. The coordinator must wait while the worker edits. All writers must cooperate with the advisory lease; an arbitrary editor cannot be physically prevented from writing. Original source/index fingerprints are checked before applying any fix, and unrelated changes cause intervention and are preserved.

The checkout originally had no `.git`. Integration created an initial source commit before applying the QA files. Ordinary repair mode requires an existing commit. A temporary Git index checkpoints tracked and reviewed untracked source without changing the user's index or branch. No reset, stash, checkout or history rewriting is used. Worktrees and evidence remain reviewable under `.runtime/` and `qa-reports/`.

## Checks and evidence

Every cycle runs Python unit/API/integration/QA tests, frontend typecheck/build and the Playwright suite. Backend and browser fixture processes are recreated each cycle. Failed Python nodeids are rerun first after a repair, followed by the full Python/browser suite. Browser failures are rerun in the browser suite. Repeated identical failures, no improvement, new regressions and exhausted budgets stop the loop.

Fixed oracles check completion promises, missing tool calls, executor/journal disagreement, freshly planted workspace observations, SSH flag retrieval, scope/redirect denial, cancellation and descendant cleanup. Host tests compare production executor fields with actual OS commands. The model does not grade the QA result. Defined fixture contracts and measured fields establish ground truth; arbitrary model prose is not proof of execution.

Real local inference is opt-in:

```bash
./scripts/qa-loop.sh --report-only --model huihui_ai/qwen3.5-abliterated:4b --budget-seconds 300
```

This checks an independently colored image, application image/tool probes and a real `host.preflight` run checked against `os.uname()`. Capability and agent-completion results are reported separately: successful tool calls do not excuse invented extra work or a denied terminal run. No target scope is granted to the real-model session. Unsupported behavior and offline Ollama fail explicitly. Without `--model`, real inference is listed as untested; deterministic protocol fixtures remain fully exercised.

`qa-reports/<UTC-run-id>/` contains JSON/Markdown reports, JUnit and Playwright results, desktop/tablet/phone screenshots, console/page/network errors, API responses, tool journals, seed/fix patches, and worker event metadata/exit codes. `qa-reports/latest.json` points to the latest run. The directory is private and Git-ignored. Pairing responses, headers, image bytes and browser traces/HAR are excluded. Text evidence is redacted. Worker reasoning and arbitrary command stdout are not retained; event types, command/file-change statuses, usage, error summaries and exit codes are retained. All app evidence is from synthetic sessions and bounded host checks.

## Repair boundaries and budgets

The real worker uses normal saved Codex authentication and:

```text
codex exec --ignore-user-config --sandbox workspace-write
  --config approval_policy="never"
  --config sandbox_workspace_write.network_access=false
  --config features.multi_agent=false --json --ephemeral -C WORKTREE -
```

There are no approval/sandbox bypass flags, root commands or unrestricted shell tools. Approval-requiring actions fail. Ignoring user config prevents it from widening permissions or injecting hooks/MCP servers. Workspace-write is a write boundary, not complete read isolation for the local OS user; arbitrary worker stdout is therefore not retained.

Tests, dependencies, prompts, QA code, auth/scope/policy/budget/tool executors and security-sensitive UI/API files are protected. Deletions, mode changes, symlinks, unknown files and oversized patches require intervention. Checks run before retests and application. Infrastructure, model and scope failures require intervention. BlackBox's safety restrictions are never weakened. Tests use localhost fixtures only; external labs require separate explicit authorization and reviewed scope configuration.

After focused/full checks pass, a non-demo patch uses `git apply --check` then `git apply`, only if original source/index fingerprints are unchanged. The user's index is preserved. Failed/interrupted repairs stay isolated. Demo patches are retained as evidence and never applied to development source.

Default budgets: 900 seconds total, 180 seconds per worker/probe, 1 MiB captured output per command, 64 MiB report storage, 100,000 worker tokens. See `--help` for overrides. Disk/output limits are polled, allowing a small bounded overshoot. Token usage is available at turn completion and stops subsequent work; this is not a provider-side hard token or monetary cap.

## Repeatable repair and shutdown checks

```bash
./scripts/qa-loop.sh --demo --once --budget-seconds 600
PYTHONPATH=. .venv/bin/python scripts/qa_validate_shutdown.py
```

The demo seeds one real bug: skipped results are counted as failures. The unchanged `test_failure_metrics_exclude_skipped_results` must fail. A real `codex exec` worker may edit only `scripts/qa_metrics.py`, then the focused test and full suite run again. Seed/fix patches and worker exit/usage evidence are retained. The development tree is never seeded or patched.

The shutdown verifier requires free port 8876. It interrupts the real browser fixture with Ctrl+C and `--stop`, checks exit 130, retained reports and closure of the owned fixture port. These intentional interruptions must not be interpreted as failing application tests.

Physical Wayland clipboard, external labs, prolonged GPU/VRAM soak and minimum-version Python remain outside this workflow. Missing optional host binaries are explicit skips, never counted as passes.
